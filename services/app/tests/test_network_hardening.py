"""network.py: address canonicalisation, the IP-filter evaluator, the trusted
hop for X-Forwarded-Proto and the mDNS service-file write."""
from __future__ import annotations

import os
import stat

import pytest

from wsi_app import app_auth as _app_auth
from wsi_app import network as _network


# --------------------------------------------------------------------------
# IPv4-mapped IPv6 clients are the IPv4 clients they carry
# --------------------------------------------------------------------------
# A dual-stack socket reports an IPv4 client as ::ffff:a.b.c.d, which
# ``ipaddress`` never places inside an IPv4 network. Every rule written for
# the IPv4 address would otherwise miss it.
def test_forwarded_mapped_address_is_reported_as_ipv4():
    headers = {"x-real-ip": "::ffff:203.0.113.5"}
    assert _network.get_client_ip(headers, "127.0.0.1") == "203.0.113.5"
    # The socket peer too, when it is the client itself.
    assert _network.get_client_ip({}, "::ffff:198.51.100.7") == "198.51.100.7"
    # Plain addresses and non-address peers are unchanged.
    assert _network.get_client_ip({"x-real-ip": "2001:db8::1"}, "127.0.0.1") == "2001:db8::1"
    assert _network.get_client_ip({}, "testclient") == "testclient"


def test_ip_filter_deny_rule_catches_a_mapped_client():
    rules = ({"ip": "203.0.113.0/24", "action": "deny"},)
    assert not _network.rules_allow_ip("203.0.113.5", True, "allow", rules)
    assert not _network.rules_allow_ip("::ffff:203.0.113.5", True, "allow", rules)
    single = ({"ip": "203.0.113.5", "action": "deny"},)
    assert not _network.rules_allow_ip("::ffff:203.0.113.5", True, "allow", single)


def test_ip_filter_allow_rule_admits_a_mapped_client_under_default_deny():
    rules = ({"ip": "192.168.1.0/24", "action": "allow"},)
    assert _network.rules_allow_ip("::ffff:192.168.1.20", True, "deny", rules)
    assert not _network.rules_allow_ip("::ffff:192.168.2.20", True, "deny", rules)


def test_a_rule_written_in_mapped_form_matches_the_ipv4_client():
    net_rule = ({"ip": "::ffff:10.0.0.0/104", "action": "deny"},)
    assert not _network.rules_allow_ip("10.1.2.3", True, "allow", net_rule)
    assert _network.rules_allow_ip("11.1.2.3", True, "allow", net_rule)
    host_rule = ({"ip": "::ffff:10.0.0.1", "action": "deny"},)
    assert not _network.rules_allow_ip("10.0.0.1", True, "allow", host_rule)


def test_mapped_loopback_is_still_loopback():
    rules = ({"ip": "0.0.0.0/0", "action": "deny"},)
    assert _network.rules_allow_ip("::ffff:127.0.0.1", True, "deny", rules)


def test_malformed_rules_are_skipped_not_raised():
    rules = (
        {"ip": ["not", "a", "string"], "action": "deny"},
        {"ip": 42, "action": "deny"},
        {"ip": "nonsense", "action": "deny"},
        {"action": "deny"},
        "not-a-dict",
        {"ip": "203.0.113.0/24", "action": "deny"},
    )
    assert not _network.rules_allow_ip("203.0.113.5", True, "allow", rules)
    assert _network.rules_allow_ip("198.51.100.5", True, "allow", rules)


def test_rules_past_the_cap_are_not_evaluated():
    filler = [{"ip": f"10.{i // 256}.{i % 256}.1", "action": "allow"}
              for i in range(_network.MAX_IP_RULES)]
    late_deny = {"ip": "203.0.113.5", "action": "deny"}
    assert _network.rules_allow_ip("203.0.113.5", True, "allow", filler + [late_deny])
    assert not _network.rules_allow_ip("203.0.113.5", True, "allow", filler[:-1] + [late_deny])


class _FakeDB:
    """Just enough of a Session for :func:`network.refresh`."""

    def __init__(self, row):
        self._row = row

    def get(self, _model, _pk):
        return self._row


def _settings_row(rules):
    import json
    from types import SimpleNamespace
    return SimpleNamespace(
        ip_rules=json.dumps(rules), ip_filter_enabled=True,
        ip_filter_default="allow", analytics_mode="off",
    )


def test_an_oversized_stored_rule_list_is_logged_once(monkeypatch, caplog):
    """A row past the cap (restored / hand-edited) is reported, once per list."""
    monkeypatch.setattr(_network, "_snapshot", _network._Snapshot())
    _network._warn_oversized_rules.cache_clear()
    oversized = [{"ip": f"10.{i // 256}.{i % 256}.1", "action": "allow"}
                 for i in range(_network.MAX_IP_RULES + 3)]
    with caplog.at_level("WARNING", logger="wsi_app.network"):
        _network.refresh(_FakeDB(_settings_row(oversized)))
        _network.refresh(_FakeDB(_settings_row(oversized)))
    hits = [r for r in caplog.records if "IGNORED" in r.getMessage()]
    assert len(hits) == 1
    assert str(_network.MAX_IP_RULES + 3) in hits[0].getMessage()
    # Evaluation is unchanged: the snapshot keeps the list, the evaluator
    # stops at the cap.
    assert len(_network._snapshot.rules) == _network.MAX_IP_RULES + 3

    # A different oversized list is reported again; a list within the cap never.
    caplog.clear()
    with caplog.at_level("WARNING", logger="wsi_app.network"):
        _network.refresh(_FakeDB(_settings_row(oversized[:-1])))
        _network.refresh(_FakeDB(_settings_row(oversized[:_network.MAX_IP_RULES])))
    assert len([r for r in caplog.records if "IGNORED" in r.getMessage()]) == 1


# --------------------------------------------------------------------------
# The trusted hop is compared in canonical form
# --------------------------------------------------------------------------
# Bound to a dual-stack socket, the app sees nginx / cloudflared on IPv4
# loopback as ::ffff:127.0.0.1. That is still the trusted proxy.
def test_mapped_loopback_peer_is_a_trusted_proxy():
    assert _network.is_trusted_proxy("127.0.0.1")
    assert _network.is_trusted_proxy("::ffff:127.0.0.1")
    assert _network.is_trusted_proxy("::1")
    assert _network.is_trusted_proxy("0:0:0:0:0:0:0:1")
    assert not _network.is_trusted_proxy("::ffff:203.0.113.9")
    assert not _network.is_trusted_proxy("203.0.113.9")
    assert not _network.is_trusted_proxy("")
    assert not _network.is_trusted_proxy(None)


def test_forwarded_headers_are_honoured_from_a_mapped_loopback_peer():
    headers = {"x-real-ip": "198.51.100.4", "x-forwarded-proto": "http"}
    assert _network.get_client_ip(headers, "::ffff:127.0.0.1") == "198.51.100.4"
    assert _network.forwarded_proto(headers, "https", client_host="::ffff:127.0.0.1") == "http"
    # Still ignored from a mapped outsider.
    assert _network.get_client_ip(headers, "::ffff:203.0.113.9") == "203.0.113.9"


def test_cf_access_trusts_a_mapped_loopback_hop():
    from wsi_app import cf_access as _cf
    assert _cf._is_trusted_hop("::ffff:127.0.0.1")
    assert not _cf._is_trusted_hop("::ffff:203.0.113.9")


def test_trusted_proxy_entries_are_canonicalised(tmp_path):
    """TRUSTED_PROXIES entries in mapped / long form name the same proxy."""
    import subprocess
    import sys
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "from wsi_app import network as n\n"
        "assert n._TRUSTED_PROXIES == frozenset({'10.0.0.2', '2001:db8::5'}), n._TRUSTED_PROXIES\n"
        "assert n.is_trusted_proxy('10.0.0.2')\n"
        "assert n.is_trusted_proxy('::ffff:10.0.0.2')\n"
        "assert n.is_trusted_proxy('2001:0db8:0:0:0:0:0:5')\n"
        "assert not n.is_trusted_proxy('127.0.0.1')\n"
    )
    env = {**os.environ, "TRUSTED_PROXIES": " ::ffff:10.0.0.2 , 2001:0DB8::0005 "}
    proc = subprocess.run([sys.executable, "-c", code], cwd=app_dir, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_lan_and_breakglass_ranges_see_mapped_clients(monkeypatch):
    assert _app_auth._is_lan_ip("::ffff:192.168.1.42")
    assert not _app_auth._is_lan_ip("::ffff:203.0.113.9")
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    headers = {"x-forwarded-proto": "http", "x-real-ip": "::ffff:192.168.1.42"}
    assert _app_auth.breakglass_exempt(headers, "127.0.0.1")
    headers = {"x-forwarded-proto": "http", "x-real-ip": "::ffff:203.0.113.9"}
    assert not _app_auth.breakglass_exempt(headers, "127.0.0.1")


# --------------------------------------------------------------------------
# X-Forwarded-Proto counts only from the trusted hop
# --------------------------------------------------------------------------
def test_forwarded_proto_ignores_the_header_from_an_untrusted_peer():
    headers = {"x-forwarded-proto": "http"}
    assert _network.forwarded_proto(headers, "https", client_host="127.0.0.1") == "http"
    assert _network.forwarded_proto(headers, "https", client_host="203.0.113.9") == "https"
    assert _network.forwarded_proto(headers, client_host="203.0.113.9") == ""
    # No peer given: the historic header-trusting behaviour.
    assert _network.forwarded_proto(headers, "https") == "http"


def test_breakglass_needs_the_scheme_from_the_trusted_hop(monkeypatch):
    """A client reaching the app port directly cannot claim the plaintext
    listener by sending X-Forwarded-Proto itself."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    headers = {"x-forwarded-proto": "http"}
    assert _app_auth.breakglass_exempt({**headers, "x-real-ip": "192.168.1.42"}, "127.0.0.1")
    assert not _app_auth.breakglass_exempt(headers, "192.168.1.42")


# --------------------------------------------------------------------------
# mDNS service file
# --------------------------------------------------------------------------
@pytest.fixture
def avahi_dir(tmp_path, monkeypatch):
    d = tmp_path / "services"
    monkeypatch.setattr(_network, "_AVAHI_SERVICES_DIR", str(d))
    monkeypatch.setattr(_network, "_AVAHI_SERVICE_FILE", str(d / "wsi.service"))
    return d


def test_mdns_file_is_written_whole_and_world_readable(avahi_dir):
    old = os.umask(0o077)
    try:
        assert _network.apply_mdns(True) == (True, "")
    finally:
        os.umask(old)
    target = avahi_dir / "wsi.service"
    assert target.read_text() == _network._AVAHI_SERVICE_XML
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    # Re-applying replaces it in place and leaves no temp file behind.
    assert _network.apply_mdns(True) == (True, "")
    assert sorted(p.name for p in avahi_dir.iterdir()) == ["wsi.service"]
    assert _network.apply_mdns(False) == (True, "")
    assert list(avahi_dir.iterdir()) == []
    # Removing an absent file is not an error.
    assert _network.apply_mdns(False) == (True, "")


def test_mdns_failed_write_leaves_no_temp_file(avahi_dir, monkeypatch):
    avahi_dir.mkdir()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(_network.os, "replace", boom)
    ok, err = _network.apply_mdns(True)
    assert not ok and "disk full" in err
    assert list(avahi_dir.iterdir()) == []
