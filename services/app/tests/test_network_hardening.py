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
