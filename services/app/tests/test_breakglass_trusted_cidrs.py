"""WSI_BREAKGLASS_TRUSTED_CIDRS: the client ranges the break-glass exemption admits.

Unset, it is the LAN ranges the dashboard's direct-LAN exemption uses. Set,
it replaces them, so a campus that hands out global addresses can list its
own range (beside the defaults) and an operator can also narrow the default.
"""
from __future__ import annotations

import ipaddress
import logging

import pytest

from wsi_app import app_auth as _app_auth
from wsi_app.config import LAN_CIDRS, breakglass_trusted_networks_from_env


def _nets(*cidrs):
    return tuple(ipaddress.ip_network(c) for c in cidrs)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_is_the_lan_ranges(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("WSI_BREAKGLASS_TRUSTED_CIDRS", raising=False)
    else:
        monkeypatch.setenv("WSI_BREAKGLASS_TRUSTED_CIDRS", raw)
    assert breakglass_trusted_networks_from_env() == _nets(*LAN_CIDRS)


def test_set_value_replaces_the_default(monkeypatch, caplog):
    monkeypatch.setenv(
        "WSI_BREAKGLASS_TRUSTED_CIDRS", "198.51.100.0/24, 192.168.0.0/16\n10.0.0.5  2001:db8::/32"
    )
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        nets = breakglass_trusted_networks_from_env()
    assert nets == _nets("198.51.100.0/24", "192.168.0.0/16", "10.0.0.5/32", "2001:db8::/32")
    assert not caplog.records


def test_invalid_entries_are_skipped_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("WSI_BREAKGLASS_TRUSTED_CIDRS", "bogus, 198.51.100.0/24, 10.0.0.0/33")
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        assert breakglass_trusted_networks_from_env() == _nets("198.51.100.0/24")
    assert len(caplog.records) == 2


def test_nothing_valid_admits_no_one(monkeypatch, caplog):
    monkeypatch.setenv("WSI_BREAKGLASS_TRUSTED_CIDRS", "bogus")
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        assert breakglass_trusted_networks_from_env() == ()
    assert any("admits no one" in r.getMessage() for r in caplog.records)


def test_a_zero_prefix_is_honoured_but_warned_about(monkeypatch, caplog):
    monkeypatch.setenv("WSI_BREAKGLASS_TRUSTED_CIDRS", "0.0.0.0/0")
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        assert breakglass_trusted_networks_from_env() == _nets("0.0.0.0/0")
    assert any("every address" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# The exemption itself, with a configured range
# --------------------------------------------------------------------------
@pytest.fixture
def breakglass_on(monkeypatch):
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)


def _plaintext_from(ip):
    # nginx on the trusted loopback hop: the real client in X-Real-IP.
    return {"x-forwarded-proto": "http", "x-real-ip": ip}


def test_a_campus_global_range_can_be_trusted(breakglass_on, monkeypatch):
    monkeypatch.setattr(
        _app_auth, "BREAKGLASS_TRUSTED_NETWORKS", _nets(*LAN_CIDRS, "198.51.100.0/24")
    )
    assert _app_auth.breakglass_exempt(_plaintext_from("198.51.100.42"), "127.0.0.1")
    assert _app_auth.breakglass_exempt(_plaintext_from("192.168.1.42"), "127.0.0.1")
    # Outside every listed range: still gated.
    assert not _app_auth.breakglass_exempt(_plaintext_from("198.51.101.1"), "127.0.0.1")
    # The other two conditions still apply to a trusted address.
    assert not _app_auth.breakglass_exempt(
        {"x-forwarded-proto": "https", "x-real-ip": "198.51.100.42"}, "127.0.0.1"
    )


def test_a_narrowed_list_drops_the_default_ranges(breakglass_on, monkeypatch):
    monkeypatch.setattr(_app_auth, "BREAKGLASS_TRUSTED_NETWORKS", _nets("192.168.10.0/24"))
    assert _app_auth.breakglass_exempt(_plaintext_from("192.168.10.7"), "127.0.0.1")
    assert not _app_auth.breakglass_exempt(_plaintext_from("192.168.1.42"), "127.0.0.1")
    assert not _app_auth.breakglass_exempt(_plaintext_from("10.1.2.3"), "127.0.0.1")


def test_the_direct_lan_setting_keeps_its_own_ranges(monkeypatch):
    """Only the break-glass exemption reads the variable; the dashboard's
    direct-LAN exemption keeps the LAN ranges."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_TRUSTED_NETWORKS", _nets("198.51.100.0/24"))
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan")
    )
    headers = {"x-forwarded-proto": "https", "x-real-ip": "198.51.100.42"}
    assert not _app_auth.lan_direct_exempt(headers, "127.0.0.1")


# --------------------------------------------------------------------------
# Behind a further proxy, the TCP peer is not the client
# --------------------------------------------------------------------------
# nginx writes X-Real-IP from its own TCP peer. When another proxy (cloudflared
# on a different host, a container port mapping, a load balancer) sits in front
# of it, that peer is the proxy's private address and every Internet client
# would pass for "on the LAN" - unless the forwarded chain, which such a proxy
# fills with the real client, is checked as well.
def _behind_proxy(real_client, *, scheme="http", peer="192.168.1.5"):
    return {
        "x-forwarded-proto": scheme,
        "x-real-ip": peer,
        "x-forwarded-for": f"{real_client}, {peer}",
    }


def test_breakglass_refuses_a_public_client_behind_a_private_proxy(breakglass_on):
    assert _app_auth.breakglass_exempt(_plaintext_from("192.168.1.42"), "127.0.0.1")
    assert not _app_auth.breakglass_exempt(_behind_proxy("203.0.113.9"), "127.0.0.1")


def test_breakglass_allows_a_lan_chain_and_a_configured_campus_chain(breakglass_on, monkeypatch):
    assert _app_auth.breakglass_exempt(_behind_proxy("192.168.1.42"), "127.0.0.1")
    monkeypatch.setattr(
        _app_auth, "BREAKGLASS_TRUSTED_NETWORKS", _nets(*LAN_CIDRS, "198.51.100.0/24")
    )
    assert _app_auth.breakglass_exempt(_behind_proxy("198.51.100.42"), "127.0.0.1")
    assert not _app_auth.breakglass_exempt(_behind_proxy("198.51.101.42"), "127.0.0.1")


@pytest.mark.parametrize("chain", ["not-an-ip, 192.168.1.5", "192.168.1.5, 8.8.8.8", "[2001:db8::1]:443"])
def test_breakglass_treats_any_untrusted_or_unparseable_hop_as_outside(breakglass_on, chain):
    headers = {"x-forwarded-proto": "http", "x-real-ip": "192.168.1.5", "x-forwarded-for": chain}
    assert not _app_auth.breakglass_exempt(headers, "127.0.0.1")


def test_a_client_cannot_use_the_header_to_gain_the_exemption(breakglass_on):
    # Extra hops only ever take the exemption away: a forged LAN-looking hop
    # on a request from a public address does not matter, X-Real-IP decides.
    headers = {"x-forwarded-proto": "http", "x-real-ip": "203.0.113.9", "x-forwarded-for": "192.168.1.5"}
    assert not _app_auth.breakglass_exempt(headers, "127.0.0.1")


def test_direct_lan_refuses_a_public_client_behind_a_private_proxy(monkeypatch):
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan")
    )
    assert _app_auth.lan_direct_exempt({"x-forwarded-proto": "https", "x-real-ip": "192.168.1.42"}, "127.0.0.1")
    assert not _app_auth.lan_direct_exempt(_behind_proxy("203.0.113.9", scheme="https"), "127.0.0.1")
    assert _app_auth.lan_direct_exempt(_behind_proxy("192.168.1.42", scheme="https"), "127.0.0.1")


def test_a_repeated_forwarded_header_is_read_in_full(breakglass_on):
    from starlette.datastructures import Headers

    headers = Headers(raw=[
        (b"x-forwarded-proto", b"http"),
        (b"x-real-ip", b"192.168.1.5"),
        (b"x-forwarded-for", b"192.168.1.42"),
        (b"x-forwarded-for", b"203.0.113.9"),
    ])
    assert not _app_auth.breakglass_exempt(headers, "127.0.0.1")
