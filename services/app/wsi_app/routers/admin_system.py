"""Admin server-info, storage and stats routes (read-only host and catalog facts)."""
from __future__ import annotations

import datetime
import fcntl
import ipaddress
import os
import platform
import re
import socket
import struct

import psutil
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import admin_helpers as _admin_helpers
from .. import disk_space as _disk_space
from ..auth import MANAGER_OR_ABOVE, ROLE_ADMIN, get_current_user, require_server_access
from ..config import settings
from ..db import get_db
from ..models import Slide, Tour, User
from ..update_check import get_update_status
from ..version import resolve_version

router = APIRouter()


# ---------------------------------------------------------------------------
# Server info helpers (stdlib-only, Linux)
# ---------------------------------------------------------------------------

def _interface_names() -> list[str]:
    """Every network interface, in kernel order, from ``/proc/net/dev``."""
    names: list[str] = []
    try:
        with open('/proc/net/dev') as f:
            for line in f.readlines()[2:]:  # two header lines
                name, sep, _ = line.partition(':')
                if sep and name.strip():
                    names.append(name.strip())
    except OSError:
        pass
    return names


def _ipv6_addresses() -> dict[str, list[str]]:
    """``{interface: ["addr/prefix", ...]}`` from ``/proc/net/if_inet6``."""
    out: dict[str, list[str]] = {}
    try:
        with open('/proc/net/if_inet6') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6:
                    continue
                addr_hex, prefix_len_hex, ifname = parts[0], parts[2], parts[5]
                try:
                    addr = str(ipaddress.IPv6Address(int(addr_hex, 16)))
                    prefix = int(prefix_len_hex, 16)
                except ValueError:
                    continue
                out.setdefault(ifname, []).append(f"{addr}/{prefix}")
    except OSError:
        pass
    return out


_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891b


def _ipv4_address(sock: socket.socket, name: str) -> tuple[str | None, str | None]:
    """(address, netmask) of ``name``'s primary IPv4 address, or Nones.

    One ``struct ifreq`` per call: the name, then a ``sockaddr_in`` whose
    address sits at bytes 20..24 on every architecture.
    """
    ifreq = struct.pack('16sH14s', name.encode()[:15], socket.AF_INET, b'\x00' * 14)
    try:
        addr = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, ifreq)[20:24])
    except OSError:
        return None, None  # no IPv4 address on this interface
    try:
        mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFNETMASK, ifreq)[20:24])
    except OSError:
        mask = None
    return addr, mask


def _server_network_interfaces():
    """Non-loopback interfaces carrying an IPv4 and/or IPv6 address.

    Not ``psutil.net_if_addrs()``: that is ``getifaddrs(3)``, which asks the
    kernel over an ``AF_NETLINK`` socket — a family the hardened wsi-app unit
    refuses (``RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6``), so it would
    raise in production. Names come from ``/proc/net/dev`` (what psutil's
    per-NIC counters read), IPv6 from ``/proc/net/if_inet6``, and IPv4 from a
    per-interface ``SIOCGIFADDR`` ioctl on an ``AF_INET`` socket.
    """
    # Loopback adds noise to the operator view (always present, fixed
    # 127.0.0.1) and never reflects a real connectivity issue worth
    # surfacing on the dashboard. Drop it at the source so per-IF stats
    # and the I/O table stay aligned.
    names = [n for n in _interface_names() if n != 'lo']
    ipv6 = _ipv6_addresses()
    result = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        sock = None
    try:
        for name in names:
            ipv4, netmask = _ipv4_address(sock, name) if sock is not None else (None, None)
            entry = {"name": name, "ipv4": ipv4, "netmask": netmask, "ipv6": ipv6.get(name, [])}
            if entry["ipv4"] or entry["ipv6"]:
                result.append(entry)
    finally:
        if sock is not None:
            sock.close()
    return result


def _server_load_avg():
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
            return {'m1': float(parts[0]), 'm5': float(parts[1]), 'm15': float(parts[2])}
    except Exception:
        return {'m1': 0.0, 'm5': 0.0, 'm15': 0.0}


def _server_uptime_seconds():
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


# The enabled site first: sites-available can hold an edited copy that is not
# the one nginx loads.
_NGINX_CONF_PATHS = [
    '/etc/nginx/sites-enabled/wsi',
    '/etc/nginx/conf.d/wsi.conf',
    '/etc/nginx/sites-available/wsi',
]
_NGINX_CACHE_DIR_DEFAULT = '/var/cache/nginx/wsi_dzi'
_NGINX_CACHE_STAT_FILE   = '/run/wsi-cache-stat/nginx_cache_stat.json'
# The tile cache's keys_zone (infra/nginx/wsi.conf); the file also declares a
# small edge-authz cache whose settings must not be read as the tile cache's.
_NGINX_TILE_ZONE = 'wsi_dzi_cache'
# wsi-cache-stat.timer rewrites the stat file every minute; one this old means
# the timer has stopped and the figures are no longer current.
_NGINX_STAT_STALE_SECONDS = 300
# Bound on the os.walk fallback (dev hosts without the stat timer): past this
# many files the figure is reported as a lower bound rather than walking a
# full production-size cache inside a request.
_NGINX_WALK_MAX_FILES = 200_000

import threading as _threading
import time as _time
_nginx_cache_info_lock = _threading.Lock()
# Held by the one request computing a fresh value; others serve the previous
# value meanwhile instead of all walking the cache at once.
_nginx_cache_compute_lock = _threading.Lock()
_nginx_cache_info_result: dict | None = None
_nginx_cache_info_ts: float = 0.0
_NGINX_CACHE_TTL = 60.0


def _nginx_cache_info():
    global _nginx_cache_info_result, _nginx_cache_info_ts
    with _nginx_cache_info_lock:
        cached = _nginx_cache_info_result
        fresh = cached is not None and _time.monotonic() - _nginx_cache_info_ts < _NGINX_CACHE_TTL
    if fresh:
        return _with_stat_age(cached)
    # Someone else is already refreshing: hand out the previous value rather
    # than queueing behind (or duplicating) the walk. Only the very first
    # request of a worker, with nothing to hand out, waits.
    if not _nginx_cache_compute_lock.acquire(blocking=cached is None):
        return _with_stat_age(cached)
    try:
        with _nginx_cache_info_lock:
            if (_nginx_cache_info_result is not None
                    and _time.monotonic() - _nginx_cache_info_ts < _NGINX_CACHE_TTL):
                return _with_stat_age(_nginx_cache_info_result)
        result = _nginx_cache_info_compute()
        with _nginx_cache_info_lock:
            _nginx_cache_info_result = result
            _nginx_cache_info_ts = _time.monotonic()
        return _with_stat_age(result)
    finally:
        _nginx_cache_compute_lock.release()


def _with_stat_age(result: dict) -> dict:
    """``result`` plus how old the stat file's figures are right now (the
    result itself is cached for a minute)."""
    out = dict(result)
    mtime = out.pop('_stat_mtime', None)
    if mtime is None:
        out['stat_age_seconds'] = None
        out['stat_stale'] = False
    else:
        age = max(0, int(_time.time() - mtime))
        out['stat_age_seconds'] = age
        out['stat_stale'] = age > _NGINX_STAT_STALE_SECONDS
    return out


def _strip_nginx_comments(text: str) -> str:
    return re.sub(r'#[^\n]*', '', text)


def _tile_cache_directive(content: str) -> str | None:
    """The parameters of the tile cache's ``proxy_cache_path`` directive
    (up to its ``;``), ignoring commented-out lines; the first directive when
    none names the tile zone."""
    directives = re.findall(r'\bproxy_cache_path\s+([^;]*);', _strip_nginx_comments(content))
    for d in directives:
        if re.search(rf'\bkeys_zone={_NGINX_TILE_ZONE}:', d):
            return d
    return directives[0] if directives else None


def _nginx_cache_info_compute(walk: bool = True):
    """Cache settings from the nginx config plus disk use from the stat file.

    ``walk=False`` skips the os.walk fallback taken when the stat file is
    missing: on a full cache that walk visits millions of files, which a
    caller on a request path (the Performance page's condition snapshot)
    must not pay — it reports the use as unknown instead. With ``walk=True``
    the walk still stops after ``_NGINX_WALK_MAX_FILES`` files and reports
    what it counted as a lower bound (``partial``).
    """
    result: dict = {
        'path': _NGINX_CACHE_DIR_DEFAULT,
        'max_size': None,
        'inactive': None,
        'used_bytes': None,
        'file_count': None,
        # Where used_bytes/file_count came from: 'stat_file', 'walk' or None.
        'source': None,
        'partial': False,
        'stat_updated_at': None,
        '_stat_mtime': None,
    }
    # Parse nginx config for cache settings
    for conf_path in _NGINX_CONF_PATHS:
        try:
            with open(conf_path) as f:
                content = f.read()
        except OSError:
            continue
        directive = _tile_cache_directive(content)
        if directive is None:
            continue
        parts = directive.split()
        if parts:
            result['path'] = parts[0]
        for key in ('max_size', 'inactive'):
            m = re.search(rf'\b{key}=(\S+)', directive)
            if m:
                result[key] = m.group(1)
        break
    # Measure current cache disk usage.
    # nginx creates cache subdirectories with mode 0700 (owned by www-data), so
    # the app process (wsi user, NoNewPrivileges=true) cannot traverse them.
    # wsi-cache-stat.timer runs /usr/local/bin/wsi-cache-stat as www-data every
    # minute and writes results to _NGINX_CACHE_STAT_FILE; read that here.
    # Fall back to os.walk for root/dev environments where the dir is accessible.
    cache_dir = result['path']
    try:
        import json as _json
        with open(_NGINX_CACHE_STAT_FILE) as _sf:
            _stat = _json.load(_sf)
            try:
                mtime = os.fstat(_sf.fileno()).st_mtime
            except (OSError, AttributeError, ValueError):
                mtime = None
        result['used_bytes'] = _stat.get('used_bytes')
        result['file_count'] = _stat.get('file_count')
        result['source'] = 'stat_file'
        if mtime is not None:
            result['_stat_mtime'] = mtime
            result['stat_updated_at'] = (
                datetime.datetime.fromtimestamp(mtime).astimezone().isoformat(timespec='seconds')
            )
    except (OSError, ValueError, AttributeError):
        if not walk:
            return result
        errors: list = []
        total = 0
        count = 0
        partial = False
        for dirpath, _dirs, filenames in os.walk(cache_dir, onerror=errors.append):
            for fname in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                    count += 1
                except OSError:
                    pass
            if count >= _NGINX_WALK_MAX_FILES:
                partial = True
                break
        if not errors or total > 0 or count > 0:
            result['used_bytes'] = total
            result['file_count'] = count
            result['source'] = 'walk'
            result['partial'] = partial
        # errors and nothing found → leave used_bytes as None → UI shows N/A
    return result


def _read_os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open('/etc/os-release') as f:
            for line in f:
                line = line.strip()
                if not line or '=' not in line or line.startswith('#'):
                    continue
                k, v = line.split('=', 1)
                out[k] = v.strip('"').strip("'")
    except OSError:
        pass
    return out


def _container_kind() -> str | None:
    """The container runtime this process runs under, or None on a host/VM.

    Only signals the app can see from inside its own sandbox: the wsi-app unit
    runs with ``ProtectProc=invisible``, which hides PID 1 (and so
    ``/proc/1/cgroup``) from the service user. ``/run/systemd/container`` is
    systemd's own verdict (``docker``, ``lxc``, ``podman``, ``systemd-nspawn``
    ...) when systemd runs inside the container; ``/.dockerenv`` and
    ``/run/.containerenv`` are left by Docker and Podman; the process's own
    ``/proc/self/cgroup`` path names the runtime when the container shares
    the host's cgroup namespace.
    """
    try:
        with open('/proc/self/cgroup') as f:
            cg = f.read()
    except OSError:
        cg = ''
    if 'kubepods' in cg or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return 'kubernetes'
    try:
        with open('/run/systemd/container') as f:
            kind = f.read().strip()
        if kind:
            return kind
    except OSError:
        pass
    if os.path.exists('/.dockerenv'):
        return 'docker'
    if os.path.exists('/run/.containerenv'):
        return 'podman'
    if 'docker' in cg:
        return 'docker'
    if '/lxc/' in cg or 'lxc.payload' in cg:
        return 'lxc'
    return None


def _detect_virtualization() -> str:
    """Best-effort: a container kind (see ``_container_kind``) / kvm / vmware /
    hyper-v / xen / virtualbox / bare-metal."""
    container = _container_kind()
    if container:
        return container
    try:
        with open('/sys/class/dmi/id/sys_vendor') as f:
            vendor = f.read().strip().lower()
        if 'qemu' in vendor or 'kvm' in vendor:
            return 'kvm'
        if 'vmware' in vendor:
            return 'vmware'
        if 'microsoft' in vendor:
            return 'hyper-v'
        if 'xen' in vendor:
            return 'xen'
        if 'innotek' in vendor or 'virtualbox' in vendor:
            return 'virtualbox'
    except OSError:
        pass
    return 'bare-metal'


_DMI_PLACEHOLDERS = {
    '', 'to be filled by o.e.m.', 'system manufacturer', 'system product name',
    'system version', 'not specified', 'default string', 'unknown', 'none',
}


def _read_dmi() -> dict:
    out: dict = {'sys_vendor': None, 'product_name': None, 'product_version': None}
    for k in out:
        try:
            with open(f'/sys/class/dmi/id/{k}') as f:
                v = f.read().strip()
            if v.lower() not in _DMI_PLACEHOLDERS:
                out[k] = v
        except OSError:
            pass
    return out


def _parse_size_to_kb(s: str) -> int | None:
    s = s.strip().upper()
    try:
        if s.endswith('K'):
            return int(s[:-1])
        if s.endswith('M'):
            return int(s[:-1]) * 1024
        if s.endswith('G'):
            return int(s[:-1]) * 1024 * 1024
        return int(s) // 1024
    except ValueError:
        return None


_FLAGS_KEEP = ('avx', 'avx2', 'avx512f', 'sse4_1', 'sse4_2', 'aes', 'vmx', 'svm')


def _cpu_info() -> dict:
    info: dict = {
        'model': None,
        'vendor': None,
        'logical': psutil.cpu_count(logical=True) or 0,
        'physical': psutil.cpu_count(logical=False),
        'sockets': None,
        'freq_current_mhz': None,
        'freq_max_mhz': None,
        'usage_total_pct': None,
        'usage_per_core_pct': None,
        'temperature_c': None,
        'cache': {'l1d_kb': None, 'l1i_kb': None, 'l2_kb': None, 'l3_kb': None},
        'flags_subset': [],
    }
    try:
        with open('/proc/cpuinfo') as f:
            text_cpuinfo = f.read()
        sockets: set[str] = set()
        first_done = False
        for block in text_cpuinfo.split('\n\n'):
            d: dict[str, str] = {}
            for line in block.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    d[k.strip()] = v.strip()
            if 'physical id' in d:
                sockets.add(d['physical id'])
            if not first_done and d:
                if 'model name' in d:
                    info['model'] = d['model name']
                if 'vendor_id' in d:
                    info['vendor'] = d['vendor_id']
                if 'flags' in d:
                    flags = set(d['flags'].split())
                    info['flags_subset'] = [f for f in _FLAGS_KEEP if f in flags]
                first_done = True
        if sockets:
            info['sockets'] = len(sockets)
    except OSError:
        pass

    try:
        f = psutil.cpu_freq()
        if f:
            if f.current:
                info['freq_current_mhz'] = round(f.current, 0)
            if f.max:
                info['freq_max_mhz'] = round(f.max, 0)
    except Exception:
        pass

    # CPU usage sampling. interval > 0 blocks for that period — by design,
    # the user opted into "measure on refresh" so this latency is acceptable.
    try:
        per = psutil.cpu_percent(interval=0.3, percpu=True)
        if per:
            info['usage_per_core_pct'] = [round(x, 1) for x in per]
            info['usage_total_pct'] = round(sum(per) / len(per), 1)
    except Exception:
        pass

    try:
        temps = psutil.sensors_temperatures() or {}
        for key in ('coretemp', 'k10temp', 'cpu_thermal', 'zenpower'):
            entries = temps.get(key) or []
            vals = [t.current for t in entries if t.current]
            if vals:
                info['temperature_c'] = round(max(vals), 1)
                break
        if info['temperature_c'] is None:
            for entries in temps.values():
                vals = [t.current for t in entries if t.current]
                if vals:
                    info['temperature_c'] = round(max(vals), 1)
                    break
    except Exception:
        pass

    try:
        cache_dir = '/sys/devices/system/cpu/cpu0/cache'
        if os.path.isdir(cache_dir):
            for ix in os.listdir(cache_dir):
                ix_dir = os.path.join(cache_dir, ix)
                try:
                    with open(os.path.join(ix_dir, 'level')) as fh:
                        level = int(fh.read().strip())
                    with open(os.path.join(ix_dir, 'type')) as fh:
                        ctype = fh.read().strip()
                    with open(os.path.join(ix_dir, 'size')) as fh:
                        size = fh.read().strip()
                    kb = _parse_size_to_kb(size)
                    if kb is None:
                        continue
                    if level == 1 and ctype == 'Data':
                        info['cache']['l1d_kb'] = kb
                    elif level == 1 and ctype == 'Instruction':
                        info['cache']['l1i_kb'] = kb
                    elif level == 2:
                        info['cache']['l2_kb'] = kb
                    elif level == 3:
                        info['cache']['l3_kb'] = kb
                except (OSError, ValueError):
                    continue
    except OSError:
        pass

    return info


def _memory_full() -> dict:
    meminfo: dict[str, int] = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':', 1)
                if len(parts) != 2:
                    continue
                k = parts[0].strip()
                vparts = parts[1].strip().split()
                meminfo[k] = int(vparts[0]) if vparts else 0
    except OSError:
        pass
    # An unreadable figure is None ("—" on the page), never 0: a 0 MB total
    # or 0 MB swap reads as a fact about the host.
    kb_to_mb = lambda k: meminfo[k] // 1024 if k in meminfo else None
    out: dict = {
        'total_mb': None, 'available_mb': None, 'used_mb': None,
        'buffers_mb': kb_to_mb('Buffers'),
        'cached_mb': kb_to_mb('Cached'),
        'swap_total_mb': None, 'swap_used_mb': None, 'swap_free_mb': None,
        'hugepages_total': meminfo.get('HugePages_Total'),
        'hugepages_size_kb': meminfo.get('Hugepagesize'),
    }
    mb = lambda b: int(b // (1024 * 1024))
    try:
        vm = psutil.virtual_memory()
        out['total_mb'] = mb(vm.total)
        out['available_mb'] = mb(vm.available)
        out['used_mb'] = mb(vm.used)
    except Exception:
        pass
    try:
        sm = psutil.swap_memory()
        out['swap_total_mb'] = mb(sm.total)
        out['swap_used_mb'] = mb(sm.used)
        out['swap_free_mb'] = mb(sm.free)
    except Exception:
        pass
    return out


def _find_mount_for_path(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (mountpoint, fstype, device) of the FS containing path. Picks
    the longest matching mountpoint prefix."""
    try:
        rp = os.path.realpath(path)
    except OSError:
        return (None, None, None)
    best: tuple[str | None, str | None, str | None] = (None, None, None)
    best_len = -1
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mp, fst = parts[0], parts[1], parts[2]
                if rp == mp or rp.startswith(mp.rstrip('/') + '/'):
                    if len(mp) > best_len:
                        best = (mp, fst, dev)
                        best_len = len(mp)
    except OSError:
        return (None, None, None)
    return best


def _device_rotational(device: str) -> bool | None:
    try:
        if not device.startswith('/dev/'):
            return None
        name = device[len('/dev/'):]
        candidates = [name]
        m = re.match(r'^(.+?)p?\d+$', name)
        if m:
            candidates.append(m.group(1))
        candidates.append(name.rstrip('0123456789'))
        for cand in candidates:
            p = f'/sys/block/{cand}/queue/rotational'
            if os.path.exists(p):
                with open(p) as f:
                    return f.read().strip() == '1'
    except OSError:
        pass
    return None


def _disk_data_dir_full(path: str) -> dict:
    out: dict = {
        'path': path,
        'total_gb': None, 'used_gb': None, 'free_gb': None, 'used_pct': None,
        'fstype': None, 'inodes_total': None, 'inodes_free': None, 'rotational': None,
    }
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        used  = total - st.f_bfree * st.f_frsize
        out['total_gb'] = round(total / 1024**3, 1)
        out['used_gb']  = round(used  / 1024**3, 1)
        out['free_gb']  = round(free  / 1024**3, 1)
        # df's (and psutil.disk_usage's, used for the other mounts) used /
        # (used + available): the blocks reserved for root are not available
        # to the app, so the volume is full for it at 100% here — dividing
        # by the raw total instead would read ~95% on a full ext4 volume.
        usable = used + free
        out['used_pct'] = round((used / usable) * 100, 1) if usable else 0.0
        out['inodes_total'] = st.f_files
        # f_ffree, as df -i counts: f_favail additionally hides the root
        # reserve and so would overstate the inodes in use.
        out['inodes_free']  = st.f_ffree
    except OSError:
        pass
    _, fst, dev = _find_mount_for_path(path)
    out['fstype'] = fst
    if dev:
        out['rotational'] = _device_rotational(dev)
    return out


_PSEUDO_FS = {
    'tmpfs', 'devtmpfs', 'overlay', 'squashfs', 'proc', 'sysfs', 'cgroup',
    'cgroup2', 'autofs', 'binfmt_misc', 'configfs', 'debugfs', 'devpts',
    'fusectl', 'hugetlbfs', 'mqueue', 'pstore', 'rpc_pipefs', 'securityfs',
    'tracefs', 'fuse.gvfsd-fuse', 'nsfs',
}

# Network filesystems are skipped because `statvfs` on a stale NFS/CIFS
# mount blocks the calling thread inside the kernel until the mount
# recovers or times out. The admin server-info endpoint runs all of this
# inline, so a single unresponsive remote mount would freeze the entire
# dashboard request.
_NETWORK_FS = {
    'nfs', 'nfs3', 'nfs4', 'cifs', 'smbfs', 'smb3', 'sshfs', 'fuse.sshfs',
    'glusterfs', 'ceph', 'afs', 'fuse', 'fuse.rclone',
}


def _other_mounts(skip_mountpoint: str | None) -> list[dict]:
    """Distinct real-disk mounts, excluding the data dir's filesystem.

    Bind mounts (e.g. ``/etc/avahi/services`` or ``/var/tmp`` mounted
    from the root FS by a hardened service unit) report the same
    ``st_dev`` as the underlying device. Without dedup they show up as
    extra rows displaying the host FS's total/used numbers, which is
    misleading. The list is collapsed by ``st_dev`` and any FS that is
    the same one already reported under "data directory" is dropped.
    """
    out: list[dict] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return out
    seen_devs: set[int] = set()
    if skip_mountpoint:
        try:
            seen_devs.add(os.stat(skip_mountpoint).st_dev)
        except OSError:
            pass
    seen_mps: set[str] = set()
    for part in partitions:
        mp = part.mountpoint
        if mp in seen_mps or mp == skip_mountpoint:
            continue
        if part.fstype in _PSEUDO_FS:
            continue
        if part.fstype in _NETWORK_FS:
            continue
        try:
            st_dev = os.stat(mp).st_dev
        except OSError:
            st_dev = None
        if st_dev is not None and st_dev in seen_devs:
            continue
        try:
            u = psutil.disk_usage(mp)
        except (OSError, PermissionError):
            continue
        out.append({
            'mountpoint': mp,
            'fstype':     part.fstype or None,
            'device':     part.device,
            'total_gb':   round(u.total / 1024**3, 1),
            'used_gb':    round(u.used  / 1024**3, 1),
            'free_gb':    round(u.free  / 1024**3, 1),
            'used_pct':   round(u.percent, 1),
        })
        seen_mps.add(mp)
        if st_dev is not None:
            seen_devs.add(st_dev)
    out.sort(key=lambda d: d['mountpoint'])
    return out


def _disk_io_total() -> dict | None:
    try:
        io = psutil.disk_io_counters()
    except Exception:
        return None
    if io is None:
        return None
    return {
        'read_bytes':  io.read_bytes,
        'write_bytes': io.write_bytes,
        'read_count':  io.read_count,
        'write_count': io.write_count,
    }


def _interfaces_full() -> list[dict]:
    base = _server_network_interfaces()
    try:
        stats = psutil.net_if_stats()
    except Exception:
        stats = {}
    try:
        io = psutil.net_io_counters(pernic=True)
    except Exception:
        io = {}
    for entry in base:
        s = stats.get(entry['name'])
        if s is not None:
            entry['link_state'] = 'up' if s.isup else 'down'
            entry['speed_mbps'] = s.speed if s.speed and s.speed > 0 else None
        else:
            entry['link_state'] = 'unknown'
            entry['speed_mbps'] = None
        d = io.get(entry['name'])
        if d is not None:
            entry['io'] = {
                'bytes_sent':   d.bytes_sent,
                'bytes_recv':   d.bytes_recv,
                'packets_sent': d.packets_sent,
                'packets_recv': d.packets_recv,
                'errin':        d.errin,
                'errout':       d.errout,
                'dropin':       d.dropin,
                'dropout':      d.dropout,
            }
        else:
            entry['io'] = None
    return base


_MB = 1024 * 1024
# Workers listed at most (a misread process tree must not list the host).
_MAX_LISTED_WORKERS = 64


def _is_spawned_worker(proc: "psutil.Process") -> bool:
    """A uvicorn worker: started by multiprocessing's spawn (``spawn_main``),
    which the supervisor's other child, the resource tracker, is not."""
    try:
        return any('spawn_main' in part for part in proc.cmdline())
    except psutil.Error:
        return False


def _app_workers() -> dict:
    """Every app worker process (this one marked ``current``), so the page
    is not one worker's snapshot that changes with whichever answered.

    Workers run as the service user, so ``ProtectProc=invisible`` leaves them
    (and their supervisor) visible to each other. A process not started as a
    worker (``--reload``, a single-process run) lists itself alone.
    """
    me = psutil.Process()
    procs = [me]
    try:
        if _is_spawned_worker(me):
            parent = me.parent()
            if parent is not None:
                siblings = [c for c in parent.children() if _is_spawned_worker(c)]
                if any(c.pid == me.pid for c in siblings):
                    procs = siblings
    except psutil.Error:
        pass
    items = []
    for p in sorted(procs, key=lambda p: p.pid)[:_MAX_LISTED_WORKERS]:
        try:
            with p.oneshot():
                items.append({
                    'pid': p.pid,
                    'rss_mb': p.memory_info().rss // _MB,
                    'threads': p.num_threads(),
                    'current': p.pid == me.pid,
                })
        except psutil.Error:
            continue
    return {
        'count': len(items),
        'rss_mb_total': sum(i['rss_mb'] for i in items),
        'items': items,
    }


def _app_process_info() -> dict:
    # Read the DB-recorded app_version so the dashboard can flag a mismatch
    # between the DB and the running binary at a glance.
    db_app_version: str | None = None
    try:
        from ..app_meta import sqlite_read_app_version
        from ..backup import _live_db_path
        db_app_version = sqlite_read_app_version(_live_db_path())
    except Exception:
        pass

    running = resolve_version()
    out: dict = {
        'pid': os.getpid(),
        'threads': None,
        'fds': None,
        'rss_mb': None,
        'vm_peak_mb': None,
        'vm_size_mb': None,
        'started_at': None,
        # Live read so a deploy (which rewrites VERSION) is reflected on the
        # admin tab without requiring a backend restart; the source says
        # whether it is a trusted stamp or the package.json fallback.
        'app_version': running.version,
        'app_version_source': running.source,
        'db_app_version': db_app_version,
        'python_version': platform.python_version(),
        'workers': None,
    }
    try:
        p = psutil.Process()
        out['threads'] = p.num_threads()
        try:
            out['fds'] = p.num_fds()
        except Exception:
            pass
        m = p.memory_info()
        out['rss_mb']     = m.rss // (1024 * 1024)
        out['vm_size_mb'] = m.vms // (1024 * 1024)
        out['started_at'] = (
            datetime.datetime.fromtimestamp(p.create_time())
            .astimezone()
            .isoformat(timespec='seconds')
        )
    except Exception:
        pass
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmPeak:'):
                    out['vm_peak_mb'] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    try:
        out['workers'] = _app_workers()
    except Exception:
        pass
    return out


def _system_processes_info() -> dict:
    # The processes this app can see, which is not the whole system: under the
    # unit's ProtectProc=invisible, /proc lists only the service user's own.
    try:
        return {'process_count': len(psutil.pids())}
    except Exception:
        return {'process_count': None}


def _db_info() -> dict:
    import sqlite3
    return {'kind': 'sqlite', 'version': sqlite3.sqlite_version}


_CGROUP_ROOT = '/sys/fs/cgroup'


def _own_cgroup_v2_path() -> str | None:
    """This process's cgroup v2 path ("/system.slice/wsi-app.service"), or
    None on a v1-only host."""
    try:
        with open('/proc/self/cgroup') as f:
            for line in f:
                if line.startswith('0::'):
                    return line[3:].strip() or '/'
    except OSError:
        pass
    return None


def _cgroup_v2_limits(path: str) -> tuple[int | None, int | None, int | None]:
    """(memory_max_bytes, cpu_quota_us, cpu_period_us): the tightest limit set
    on ``path`` or any ancestor up to the namespace root.

    A systemd ``MemoryMax=`` / ``CPUQuota=`` sits on the unit's own cgroup,
    not on the root this process sees: reading only the root's files would find
    nothing on a plain host, so the page would never show the unit's limits.
    """
    mem: int | None = None
    cpu: tuple[int, int] | None = None
    parts = [p for p in path.split('/') if p]
    for depth in range(len(parts), -1, -1):
        d = _CGROUP_ROOT + ('/' + '/'.join(parts[:depth]) if depth else '')
        try:
            with open(f'{d}/memory.max') as f:
                v = f.read().strip()
            if v != 'max':
                n = int(v)
                if mem is None or n < mem:
                    mem = n
        except (OSError, ValueError):
            pass
        try:
            with open(f'{d}/cpu.max') as f:
                fields = f.read().strip().split()
            if fields and fields[0] != 'max':
                q = int(fields[0])
                per = int(fields[1]) if len(fields) > 1 else 100000
                if per > 0 and (cpu is None or q / per < cpu[0] / cpu[1]):
                    cpu = (q, per)
        except (OSError, ValueError):
            pass
    return mem, (cpu[0] if cpu else None), (cpu[1] if cpu else None)


def _cgroup_info() -> dict:
    """Container runtime (if any) and the memory / CPU limits on this process.

    On a cgroup v2 host, limits are read for containers and for a plain host
    alike: the service unit's own ``MemoryMax=`` / ``CPUQuota=`` bound the app
    just as a container's do. On a v1-only host (or a hybrid one whose v2 tree
    carries no controllers) only the controller roots are read, which reflect
    a container's limits but not a unit's.
    """
    out: dict = {
        'in_container': False,
        'container_kind': None,
        'cgroup_path': None,
        'memory_max_bytes': None,
        'cpu_quota_us': None,
        'cpu_period_us': None,
    }
    kind = _container_kind()
    if kind is not None:
        out['in_container'] = True
        out['container_kind'] = kind
    path = _own_cgroup_v2_path()
    if path is not None:
        out['cgroup_path'] = path
        mem, quota, period = _cgroup_v2_limits(path)
        out['memory_max_bytes'] = mem
        out['cpu_quota_us'] = quota
        out['cpu_period_us'] = period
        if mem is not None or quota is not None:
            return out
    # cgroup v1 (no unified hierarchy, or a hybrid host whose v2 tree carries
    # no controllers): the controller roots.
    try:
        with open('/sys/fs/cgroup/memory/memory.limit_in_bytes') as f:
            v = int(f.read().strip())
        # cgroup v1 sets a giant value when unlimited; treat very-large as None
        if v < (1 << 62):
            out['memory_max_bytes'] = v
    except (OSError, ValueError):
        pass
    try:
        with open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us') as f:
            q = int(f.read().strip())
        with open('/sys/fs/cgroup/cpu/cpu.cfs_period_us') as f:
            p = int(f.read().strip())
        if q > 0:
            out['cpu_quota_us'] = q
            out['cpu_period_us'] = p
    except (OSError, ValueError):
        pass
    return out


def _iana_name_or_none(name: str) -> str | None:
    """``name`` if it is an IANA zone this host knows, else None.

    ``TZ`` may legally hold a POSIX rule ("JST-9") or a file path
    (":/etc/localtime"), and zoneinfo symlinks can point into the
    ``posix/`` / ``right/`` trees; none of those is a zone name the page (or
    a browser's Intl API) can use.
    """
    from zoneinfo import ZoneInfo

    name = name.strip()
    for prefix in ('posix/', 'right/'):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if not name or name.startswith(('/', '.')):
        return None
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


_ZONEINFO_DIRS = ('/usr/share/zoneinfo/', '/usr/lib/zoneinfo/', '/usr/share/lib/zoneinfo/')


def _zone_from_zoneinfo_path(path: str) -> str | None:
    """The IANA name of a zoneinfo file ``path`` is, or links to, if any."""
    candidates = [path]
    try:
        candidates.insert(0, os.readlink(path))
    except OSError:
        pass
    for target in candidates:
        for prefix in _ZONEINFO_DIRS:
            if prefix in target:
                name = _iana_name_or_none(target.split(prefix, 1)[1])
                if name:
                    return name
    return None


def _get_iana_timezone() -> str | None:
    """Return the IANA timezone name (e.g. 'Europe/Berlin') for the server.

    Priority: TZ env var > /etc/localtime symlink > /etc/timezone, each used
    only when it names a real IANA zone.
    /etc/timezone is checked last because container images often ship it
    as 'Etc/UTC' even when the actual runtime timezone differs.
    """
    # 1. TZ environment variable — set explicitly in most container runtimes.
    # When set, it — not /etc/localtime — is the zone the process runs in, so
    # a value that names no IANA zone is "unknown", never a fall-through to
    # /etc/localtime: TZ=JST-9 on a host whose localtime is UTC runs at +09:00.
    # A file path (":/usr/share/zoneinfo/Asia/Tokyo", "/etc/localtime") is
    # resolved like /etc/localtime below.
    tz_env = os.environ.get('TZ', '').strip().lstrip(':')
    if tz_env:
        name = _iana_name_or_none(tz_env)
        if name:
            return name
        if not tz_env.startswith('/'):
            return None
        name = _zone_from_zoneinfo_path(tz_env)
        if name or tz_env != '/etc/localtime':
            return name
        # TZ names /etc/localtime itself: the sources below describe it too.
    # 2. /etc/localtime symlink — reliable on systemd-managed hosts.
    name = _zone_from_zoneinfo_path('/etc/localtime')
    if name:
        return name
    # 3. /etc/timezone — may be stale in minimal container images.
    try:
        with open('/etc/timezone') as f:
            name = _iana_name_or_none(f.read())
            if name:
                return name
    except Exception:
        pass
    return None


def _os_extended() -> dict:
    osr = _read_os_release()
    glibc: str | None = None
    try:
        impl, ver = platform.libc_ver()
        if impl:
            glibc = f'{impl} {ver}'.strip() if ver else impl
    except Exception:
        pass
    server_time = None
    tz_label = None
    utc_offset: str | None = None
    try:
        now = datetime.datetime.now().astimezone()
        server_time = now.isoformat(timespec='seconds')
        tz_label = now.tzname() or '—'
        raw_offset = now.utcoffset()
        if raw_offset is not None:
            total = int(raw_offset.total_seconds())
            sign = '+' if total >= 0 else '-'
            h, rem = divmod(abs(total), 3600)
            utc_offset = f'{sign}{h:02d}:{rem // 60:02d}'
    except Exception:
        pass
    try:
        boot_time = (
            datetime.datetime.fromtimestamp(psutil.boot_time())
            .astimezone()
            .isoformat(timespec='seconds')
        )
    except Exception:
        boot_time = None
    return {
        'system':   platform.system(),
        'release':  platform.release(),
        'version':  platform.version(),
        'machine':  platform.machine(),
        'distro_pretty_name': osr.get('PRETTY_NAME'),
        'glibc_version':      glibc,
        'timezone':           tz_label,
        'tz_name':            _get_iana_timezone(),
        'utc_offset':         utc_offset,
        'server_time':        server_time,
        'boot_time':          boot_time,
        'virtualization':     _detect_virtualization(),
    }


# socket.getfqdn() is a reverse lookup; on a LAN with no (or an unreachable)
# DNS server it can block for the resolver's full timeout — several seconds
# on every refresh. It is resolved once per hostname, in the background; the
# page shows the hostname alone until the answer is in.
_FQDN_WAIT_SECONDS = 0.3
_fqdn_lock = _threading.Lock()
_fqdn_cache: dict[str, str] = {}
_fqdn_pending: set[str] = set()


def _resolve_fqdn(hostname: str) -> None:
    try:
        fqdn = socket.getfqdn(hostname)
    except Exception:
        fqdn = hostname
    with _fqdn_lock:
        _fqdn_cache[hostname] = fqdn
        _fqdn_pending.discard(hostname)


def _server_fqdn(hostname: str) -> str | None:
    """The FQDN for ``hostname``, or None while the lookup is still running."""
    with _fqdn_lock:
        if hostname in _fqdn_cache:
            return _fqdn_cache[hostname]
        start = hostname not in _fqdn_pending
        if start:
            _fqdn_pending.add(hostname)
    if start:
        t = _threading.Thread(target=_resolve_fqdn, args=(hostname,),
                              name="wsi-fqdn", daemon=True)
        t.start()
        t.join(_FQDN_WAIT_SECONDS)
    with _fqdn_lock:
        return _fqdn_cache.get(hostname)


@router.get("/api/admin/server/info")
def admin_server_info(
    response: Response,
    admin: User = Depends(require_server_access),
):
    from ..dzi import get_dz_cache_info
    disk = _disk_data_dir_full(settings.data_dir)
    data_mp, _, _ = _find_mount_for_path(settings.data_dir)
    # Live values (PID, uptime, version-after-deploy) must never be
    # served from a browser/proxy cache; otherwise a refresh after
    # restart silently shows the previous process's snapshot.
    response.headers["Cache-Control"] = "no-store"
    hostname = socket.gethostname()
    return {
        "hostname":         hostname,
        "fqdn":             _server_fqdn(hostname),
        "interfaces":       _interfaces_full(),
        "os":               _os_extended(),
        "dmi":              _read_dmi(),
        "cpu":              _cpu_info(),
        "memory":           _memory_full(),
        "load_avg":         _server_load_avg(),
        "uptime_seconds":   _server_uptime_seconds(),
        "app_process":      _app_process_info(),
        "system_processes": _system_processes_info(),
        "db":               _db_info(),
        "disk_data_dir":    disk,
        "other_mounts":     _other_mounts(data_mp),
        "disk_io_total":    _disk_io_total(),
        "cgroup":           _cgroup_info(),
        "dzi_cache":        get_dz_cache_info(),
        "nginx_cache":      _nginx_cache_info(),
        "app_update":       get_update_status(),
    }


def require_storage_access(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Gate the free-space figure behind the bulk-upload preflight.

    Uploading is a ``manager+`` capability, so whoever can upload must be able
    to see whether the files fit; gating this at server-access alone would
    leave the preflight silently disabled for every manager without the
    server-info flag. Anyone else still needs server access.
    """
    if user.role in MANAGER_OR_ABOVE:
        return user
    return require_server_access(user, db)


@router.get("/api/admin/storage")
def admin_storage(
    response: Response,
    admin: User = Depends(require_storage_access),
):
    """Free / total / used bytes on the data-dir filesystem.

    A deliberately lightweight counterpart to ``/server/info`` (no process,
    DMI, or cache scans) so the bulk-upload preflight can fetch it whenever the
    operator selects files. Byte-precise (``statvfs``) rather than the rounded
    ``free_gb`` that ``/server/info`` reports. Best-effort: concurrent writes
    and on-the-fly thumbnail/cache growth mean the figure is advisory, not a
    guarantee. Byte counts only: the data-dir path stays in ``/server/info``,
    since this endpoint is open to managers without server access.

    ``upload_space`` is what the bulk-upload screen needs to reproduce the
    server's own refusal (admin_helpers.upload_space_short, run by the
    pre-body check in main.py) for each file of a batch, instead of a
    client-side rule of its own: the free bytes of the two volumes that check
    measures — the one ``wsi/`` really lives on (``target``) and the
    multipart spool's — whether they are one filesystem, how many copies of
    the body land there, and the shared reserve policy (disk_space). The
    top-level figures measure ``data_dir``, which is not the volume the check
    measures once ``wsi/`` is symlinked elsewhere, and a fixed margin (say
    1 GiB) added to them on the screen would match no server rule at all: a
    batch the server would refuse file by file could pass it, and one the
    server takes could be blocked. The volumes come from
    admin_helpers.upload_space_volumes, the function the check itself uses, so
    the two cannot drift apart. A free figure is ``null`` when unknown
    (unreadable, or a filesystem reporting no capacity —
    disk_space.free_bytes), which the check treats as "fits", and so must the
    screen. The top-level fields serve callers that want only the data-dir
    figures.

    ``max_upload_bytes`` is the per-file slide upload / replace cap
    (``WSI_MAX_UPLOAD_BYTES``), so the dashboard checks and names the limit
    the server enforces instead of a number of its own. ``allowed_extensions``
    is ``WSI_ALLOWED_EXTENSIONS`` for the same reason: an operator who narrows
    or widens it would otherwise see the dashboard accept files the server
    refuses (a raw 400 after the whole body), or block ones it takes.
    """
    response.headers["Cache-Control"] = "no-store"
    out = {
        "total_bytes": 0, "free_bytes": 0, "used_bytes": 0,
        "max_upload_bytes": settings.wsi_max_upload_bytes,
        "allowed_extensions": list(settings.wsi_allowed_extensions),
    }
    try:
        st = os.statvfs(settings.data_dir)
        total = st.f_blocks * st.f_frsize
        out["total_bytes"] = total
        out["free_bytes"] = st.f_bavail * st.f_frsize
        out["used_bytes"] = total - st.f_bfree * st.f_frsize
    except OSError:
        pass
    target, spool, same_volume = _admin_helpers.upload_space_volumes()
    out["upload_space"] = {
        "target_free_bytes": _disk_space.free_bytes(target),
        "spool_free_bytes": _disk_space.free_bytes(spool),
        "same_volume": same_volume,
        "copies": _admin_helpers.SLIDE_UPLOAD_COPIES,
        "reserve_fraction": _disk_space.RESERVE_FRACTION,
        "reserve_min_bytes": _disk_space.RESERVE_MIN_BYTES,
    }
    return out


def _backups_summary() -> dict:
    """Aggregate count + total size across snapshots in settings.backup_dir.

    Reads sizes from the on-disk archives via ``list_backups`` so the
    figure tracks what an operator would see in the Backup & Restore tab. Failures
    (missing dir, permission errors) collapse to zeros so a broken backup
    store never breaks the stats endpoint.
    """
    try:
        from .. import backup as _backup
        records = _backup.list_backups()
    except Exception:
        return {"count": 0, "total_size_bytes": 0}
    return {
        "count":            len(records),
        "total_size_bytes": sum(r.size_bytes or 0 for r in records),
    }


@router.get("/api/admin/stats")
def admin_stats(admin: User = Depends(require_server_access), db: Session = Depends(get_db)):
    """Return summary statistics for slides and tours."""
    # Select ONLY the scalar columns the stats below actually read — never
    # `select(Slide)`, which materialises every ORM row including the
    # LargeBinary macro_data / label_data blobs (hundreds of MB for a large
    # catalog) on every call. Each Row here exposes the columns by name
    # (``s.file_size_bytes`` etc.), so the aggregation logic is unchanged.
    slides = db.execute(
        select(
            Slide.file_size_bytes,
            Slide.mpp_x,
            Slide.mpp_y,
            Slide.catalog_id,
            Slide.is_published,
            Slide.vendor,
            Slide.wsi_filename,
        )
    ).all()
    tours  = db.execute(select(Tour.is_published, Tour.slide_id)).all()

    total_slides      = len(slides)
    total_file_bytes  = sum(s.file_size_bytes or 0 for s in slides)
    avg_file_bytes    = int(total_file_bytes / total_slides) if total_slides else 0
    largest           = max(slides, key=lambda s: s.file_size_bytes or 0, default=None)
    with_mpp          = sum(1 for s in slides if s.mpp_x or s.mpp_y)
    with_catalog_id   = sum(1 for s in slides if s.catalog_id and s.catalog_id.strip())
    published_slides  = sum(1 for s in slides if s.is_published)

    vendor_counts: dict[str, int] = {}
    ext_counts:    dict[str, int] = {}
    for s in slides:
        v = s.vendor.strip() if s.vendor and s.vendor.strip() else "(unknown)"
        vendor_counts[v] = vendor_counts.get(v, 0) + 1
        m = re.search(r'\.([^.]+)$', s.wsi_filename or '')
        ext = m.group(1).upper() if m else "(unknown)"
        # An OME-TIFF carries a plain .tif/.tiff suffix; list it as its own
        # format rather than folding it into generic TIFF.
        if (s.vendor or "") == "ome-tiff" or (s.wsi_filename or "").lower().endswith((".ome.tif", ".ome.tiff")):
            ext = "OME-TIFF"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    by_vendor    = sorted([{"label": k, "count": v} for k, v in vendor_counts.items()], key=lambda x: -x["count"])
    by_extension = sorted([{"label": k, "count": v} for k, v in ext_counts.items()],    key=lambda x: -x["count"])

    total_tours  = len(tours)
    published    = sum(1 for t in tours if t.is_published)
    slides_with_tours = len({t.slide_id for t in tours})

    return {
        "slides": {
            "total":             total_slides,
            "total_file_bytes":  total_file_bytes,
            "avg_file_bytes":    avg_file_bytes,
            "largest_file_bytes": largest.file_size_bytes if largest else 0,
            "largest_file_name":  largest.wsi_filename    if largest else "",
            "with_mpp":          with_mpp,
            "without_mpp":       total_slides - with_mpp,
            "with_catalog_id":   with_catalog_id,
            "without_catalog_id": total_slides - with_catalog_id,
            "published":          published_slides,
            "draft":              total_slides - published_slides,
            "by_vendor":    by_vendor,
            "by_extension": by_extension,
        },
        "tours": {
            "total":                total_tours,
            "published":            published,
            "draft":                total_tours - published,
            "slides_with_tours":    slides_with_tours,
            "slides_without_tours": total_slides - slides_with_tours,
        },
        # Backups are an admin-only surface; a manager/instructor who holds
        # only the server-view flag gets null rather than the archive count
        # and size (the dashboard hides the card).
        "backups": _backups_summary() if admin.role == ROLE_ADMIN else None,
    }
