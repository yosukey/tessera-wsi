"""Host facts on the admin Server Info page (routers/admin_system.py).

The production unit runs with ProtectProc=invisible (PID 1 and every other
user's processes are hidden from /proc) and RestrictAddressFamilies=AF_UNIX
AF_INET AF_INET6 (no netlink), so these helpers must work from what that
sandbox leaves visible.
"""
from __future__ import annotations

import io
import os

import pytest

from wsi_app.routers import admin_system


@pytest.fixture
def fake_fs(monkeypatch):
    """Serve the named paths from a dict; everything else is unreadable."""
    files: dict[str, str] = {}

    def fake_open(path, *a, **kw):
        if path in files:
            return io.StringIO(files[path])
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(admin_system, "open", fake_open, raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: p in files)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    return files


def test_container_from_systemd_without_proc_1(fake_fs):
    # /proc/1/cgroup is hidden (PermissionError) under ProtectProc=invisible;
    # it must not be the only cgroup source consulted.
    fake_fs["/proc/self/cgroup"] = "0::/\n"
    fake_fs["/run/systemd/container"] = "lxc\n"
    assert admin_system._container_kind() == "lxc"
    assert admin_system._detect_virtualization() == "lxc"
    info = admin_system._cgroup_info()
    assert info["in_container"] is True and info["container_kind"] == "lxc"


@pytest.mark.parametrize("files, kind", [
    ({"/proc/self/cgroup": "0::/kubepods/burstable/pod1/abc\n", "/.dockerenv": ""}, "kubernetes"),
    ({"/proc/self/cgroup": "0::/\n", "/.dockerenv": ""}, "docker"),
    ({"/proc/self/cgroup": "0::/\n", "/run/.containerenv": ""}, "podman"),
    ({"/proc/self/cgroup": "12:memory:/docker/0123abcd\n"}, "docker"),
    ({"/proc/self/cgroup": "0::/lxc.payload.web/system.slice/wsi-app.service\n"}, "lxc"),
])
def test_container_kinds(fake_fs, files, kind):
    fake_fs.update(files)
    assert admin_system._container_kind() == kind


def test_plain_host_is_not_a_container(fake_fs):
    fake_fs["/proc/self/cgroup"] = "0::/system.slice/wsi-app.service\n"
    fake_fs["/sys/class/dmi/id/sys_vendor"] = "QEMU\n"
    assert admin_system._container_kind() is None
    assert admin_system._cgroup_info()["in_container"] is False
    assert admin_system._detect_virtualization() == "kvm"


def test_interfaces_include_ipv6_only_and_skip_loopback(fake_fs, monkeypatch):
    fake_fs["/proc/net/dev"] = (
        "Inter-|   Receive |  Transmit\n"
        " face |bytes    packets\n"
        "    lo: 1 2 3\n"
        "  eth0: 1 2 3\n"
        "   wg0: 1 2 3\n"
        "  eth1: 1 2 3\n"
    )
    fake_fs["/proc/net/if_inet6"] = (
        "fe800000000000000000000000000001 02 40 20 80 eth0\n"
        "20010db8000000000000000000000001 03 40 00 80 wg0\n"
        "00000000000000000000000000000001 01 80 10 80 lo\n"
    )
    monkeypatch.setattr(
        admin_system, "_ipv4_address",
        lambda sock, name: ("192.0.2.2", "255.255.255.0") if name == "eth0" else (None, None),
    )
    got = admin_system._server_network_interfaces()
    assert got == [
        {"name": "eth0", "ipv4": "192.0.2.2", "netmask": "255.255.255.0",
         "ipv6": ["fe80::1/64"]},
        # IPv6-only: a SIOCGIFCONF listing would only know IPv4 interfaces.
        {"name": "wg0", "ipv4": None, "netmask": None, "ipv6": ["2001:db8::1/64"]},
    ]


def test_ipv4_lookup_on_this_host():
    """The real ioctl path: whatever it finds must be well-formed, and an
    interface without IPv4 (or a bogus name) yields Nones, not an error."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        assert admin_system._ipv4_address(s, "no-such-if0") == (None, None)
        for name in admin_system._interface_names():
            addr, mask = admin_system._ipv4_address(s, name)
            if addr is not None:
                socket.inet_aton(addr)
                assert mask is None or socket.inet_aton(mask)


# ---------------------------------------------------------------------------
# Storage, memory, timezone
# ---------------------------------------------------------------------------

class _StatVfs:
    f_frsize = 4096
    f_blocks = 1000      # 4 096 000 bytes
    f_bfree = 100        # free including the root reserve
    f_bavail = 50        # free for the app (50 blocks reserved for root)
    f_files = 500
    f_ffree = 200
    f_favail = 150


def test_data_dir_usage_matches_df(monkeypatch):
    monkeypatch.setattr(admin_system.os, "statvfs", lambda p: _StatVfs)
    monkeypatch.setattr(admin_system, "_find_mount_for_path", lambda p: (None, None, None))
    d = admin_system._disk_data_dir_full("/data")
    # used = 900 blocks, available = 50: df shows 900 / 950.
    assert d["used_pct"] == round(900 / 950 * 100, 1)
    assert d["inodes_free"] == 200


def test_data_dir_unreadable_is_unknown_not_zero(monkeypatch):
    def boom(p):
        raise OSError("gone")
    monkeypatch.setattr(admin_system.os, "statvfs", boom)
    monkeypatch.setattr(admin_system, "_find_mount_for_path", lambda p: (None, None, None))
    d = admin_system._disk_data_dir_full("/data")
    assert d["total_gb"] is None and d["used_pct"] is None


def test_memory_unreadable_is_unknown_not_zero(fake_fs, monkeypatch):
    def boom():
        raise RuntimeError("no psutil")
    monkeypatch.setattr(admin_system.psutil, "virtual_memory", boom)
    monkeypatch.setattr(admin_system.psutil, "swap_memory", boom)
    m = admin_system._memory_full()
    assert m["total_mb"] is None and m["swap_total_mb"] is None and m["buffers_mb"] is None


@pytest.mark.parametrize("tz_env, expected", [
    ("Asia/Tokyo", "Asia/Tokyo"),
    (":Europe/Berlin", "Europe/Berlin"),
    ("JST-9", None),
    (":/etc/localtime", None),
    ("posix/Asia/Tokyo", "Asia/Tokyo"),
])
def test_tz_env_must_name_an_iana_zone(fake_fs, monkeypatch, tz_env, expected):
    monkeypatch.setenv("TZ", tz_env)
    monkeypatch.setattr(admin_system.os, "readlink", lambda p: (_ for _ in ()).throw(OSError()))
    assert admin_system._get_iana_timezone() == expected


def test_a_posix_rule_tz_is_unknown_not_the_localtime_zone(fake_fs, monkeypatch):
    """TZ overrides /etc/localtime for the process: TZ=JST-9 on a host whose
    localtime is UTC runs at +09:00, so naming the localtime zone would be wrong."""
    monkeypatch.setenv("TZ", "JST-9")
    monkeypatch.setattr(admin_system.os, "readlink", lambda p: "../usr/share/zoneinfo/Etc/UTC")
    assert admin_system._get_iana_timezone() is None


def test_tz_as_a_zoneinfo_path_names_that_zone(fake_fs, monkeypatch):
    monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/Asia/Tokyo")
    monkeypatch.setattr(admin_system.os, "readlink", lambda p: (_ for _ in ()).throw(OSError()))
    assert admin_system._get_iana_timezone() == "Asia/Tokyo"


def test_tz_naming_etc_localtime_reads_localtime(fake_fs, monkeypatch):
    monkeypatch.setenv("TZ", ":/etc/localtime")
    monkeypatch.setattr(admin_system.os, "readlink", lambda p: "/usr/share/zoneinfo/posix/Asia/Tokyo")
    assert admin_system._get_iana_timezone() == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# cgroup limits of the service unit
# ---------------------------------------------------------------------------

def test_systemd_unit_limits_are_read_on_a_plain_host(fake_fs):
    fake_fs["/proc/self/cgroup"] = "0::/system.slice/wsi-app.service\n"
    fake_fs["/sys/fs/cgroup/system.slice/wsi-app.service/memory.max"] = "2147483648\n"
    fake_fs["/sys/fs/cgroup/system.slice/wsi-app.service/cpu.max"] = "max 100000\n"
    # A tighter CPU limit on the parent slice is the one in force.
    fake_fs["/sys/fs/cgroup/system.slice/cpu.max"] = "150000 100000\n"
    fake_fs["/sys/fs/cgroup/system.slice/memory.max"] = "max\n"
    info = admin_system._cgroup_info()
    assert info["in_container"] is False
    assert info["cgroup_path"] == "/system.slice/wsi-app.service"
    assert info["memory_max_bytes"] == 2147483648
    assert (info["cpu_quota_us"], info["cpu_period_us"]) == (150000, 100000)


def test_unlimited_unit_reports_no_limits(fake_fs):
    fake_fs["/proc/self/cgroup"] = "0::/system.slice/wsi-app.service\n"
    fake_fs["/sys/fs/cgroup/system.slice/wsi-app.service/memory.max"] = "max\n"
    info = admin_system._cgroup_info()
    assert info["memory_max_bytes"] is None and info["cpu_quota_us"] is None


# ---------------------------------------------------------------------------
# nginx tile cache
# ---------------------------------------------------------------------------

_NGINX_CONF = """
# proxy_cache_path /old/cache levels=1:2 keys_zone=wsi_dzi_cache:10m max_size=1g;
proxy_cache_path /var/cache/nginx/wsi_dzi levels=1:2 keys_zone=wsi_dzi_cache:400m
                 max_size=50g inactive=30d use_temp_path=off;
proxy_cache_path /var/cache/nginx/wsi_authz levels=1:2 keys_zone=wsi_authz_cache:4m
                 inactive=2m use_temp_path=off;
"""


def test_nginx_conf_reads_the_tile_cache_directive(fake_fs, monkeypatch, tmp_path):
    fake_fs["/etc/nginx/sites-enabled/wsi"] = _NGINX_CONF
    fake_fs["/etc/nginx/sites-available/wsi"] = "proxy_cache_path /stale keys_zone=wsi_dzi_cache:1m max_size=1m;"
    monkeypatch.setattr(admin_system, "_NGINX_CACHE_STAT_FILE", "/nonexistent/stat.json")
    info = admin_system._nginx_cache_info_compute(walk=False)
    assert info["path"] == "/var/cache/nginx/wsi_dzi"
    assert info["max_size"] == "50g"
    assert info["inactive"] == "30d"


def test_nginx_stat_file_age_and_staleness(monkeypatch, tmp_path):
    stat = tmp_path / "stat.json"
    stat.write_text('{"used_bytes": 1234, "file_count": 5}')
    old = admin_system._time.time() - 3600
    os.utime(stat, (old, old))
    monkeypatch.setattr(admin_system, "_NGINX_CACHE_STAT_FILE", str(stat))
    monkeypatch.setattr(admin_system, "_NGINX_CONF_PATHS", [])
    monkeypatch.setattr(admin_system, "_nginx_cache_info_result", None)
    info = admin_system._nginx_cache_info()
    assert info["used_bytes"] == 1234 and info["source"] == "stat_file"
    assert info["stat_stale"] is True and info["stat_age_seconds"] >= 3500
    assert info["stat_updated_at"] and "_stat_mtime" not in info


def test_nginx_walk_is_bounded(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(5):
        (cache / f"f{i}").write_bytes(b"x" * 10)
    monkeypatch.setattr(admin_system, "_NGINX_CACHE_STAT_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(admin_system, "_NGINX_CONF_PATHS", [])
    monkeypatch.setattr(admin_system, "_NGINX_CACHE_DIR_DEFAULT", str(cache))
    monkeypatch.setattr(admin_system, "_NGINX_WALK_MAX_FILES", 3)
    info = admin_system._nginx_cache_info_compute()
    assert info["source"] == "walk" and info["partial"] is True and info["file_count"] >= 3

    monkeypatch.setattr(admin_system, "_NGINX_WALK_MAX_FILES", 100)
    info = admin_system._nginx_cache_info_compute()
    assert info["partial"] is False and info["file_count"] == 5 and info["used_bytes"] == 50


def test_nginx_cache_refresh_is_single_flight(monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_compute():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"path": "/c", "_stat_mtime": None}

    monkeypatch.setattr(admin_system, "_nginx_cache_info_compute", slow_compute)
    monkeypatch.setattr(admin_system, "_nginx_cache_info_result", {"path": "/old", "_stat_mtime": None})
    monkeypatch.setattr(admin_system, "_nginx_cache_info_ts", -1e9)
    t = threading.Thread(target=admin_system._nginx_cache_info)
    t.start()
    assert started.wait(5)
    # While one request refreshes, another is served the previous value.
    assert admin_system._nginx_cache_info()["path"] == "/old"
    release.set()
    t.join(5)
    assert len(calls) == 1
    assert admin_system._nginx_cache_info()["path"] == "/c"


# ---------------------------------------------------------------------------
# FQDN lookup and worker list
# ---------------------------------------------------------------------------

def test_fqdn_lookup_does_not_block_the_request(monkeypatch):
    import threading

    gate = threading.Event()

    def slow_getfqdn(name):
        gate.wait(5)
        return name + ".example.org"

    monkeypatch.setattr(admin_system.socket, "getfqdn", slow_getfqdn)
    monkeypatch.setattr(admin_system, "_FQDN_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(admin_system, "_fqdn_cache", {})
    monkeypatch.setattr(admin_system, "_fqdn_pending", set())
    assert admin_system._server_fqdn("hostq") is None
    assert admin_system._server_fqdn("hostq") is None  # still pending, not restarted
    gate.set()
    for _ in range(100):
        if admin_system._server_fqdn("hostq"):
            break
        admin_system._time.sleep(0.01)
    assert admin_system._server_fqdn("hostq") == "hostq.example.org"


def test_app_workers_lists_this_process_when_not_spawned():
    w = admin_system._app_workers()
    assert w["count"] >= 1
    assert [i for i in w["items"] if i["current"]][0]["pid"] == os.getpid()


def test_app_workers_lists_spawned_siblings(monkeypatch):
    class P:
        def __init__(self, pid, cmd, parent=None, kids=()):
            self.pid, self._cmd, self._parent, self._kids = pid, cmd, parent, list(kids)
        def cmdline(self): return self._cmd
        def parent(self): return self._parent
        def children(self): return self._kids
        def oneshot(self):
            import contextlib
            return contextlib.nullcontext()
        def memory_info(self):
            class M: rss = 100 * 1024 * 1024
            return M
        def num_threads(self): return 4

    spawn = ["python", "-c", "from multiprocessing.spawn import spawn_main; spawn_main()"]
    tracker = ["python", "-c", "from multiprocessing.resource_tracker import main"]
    parent = P(10, ["uvicorn"])
    me = P(12, spawn, parent)
    parent._kids = [P(11, spawn, parent), me, P(13, tracker, parent), P(14, spawn, parent)]
    monkeypatch.setattr(admin_system.psutil, "Process", lambda: me)
    w = admin_system._app_workers()
    assert [i["pid"] for i in w["items"]] == [11, 12, 14]
    assert [i["current"] for i in w["items"]] == [False, True, False]
    assert w["rss_mb_total"] == 300


def test_server_info_endpoint_shape(client, admin_headers):
    r = client.get("/api/admin/server/info", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert "workers" in body["app_process"]
    assert "cgroup_path" in body["cgroup"]
    assert {"stat_age_seconds", "stat_stale", "source", "partial"} <= set(body["nginx_cache"])
