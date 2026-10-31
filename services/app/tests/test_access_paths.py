"""End-to-end tests for the three documented access paths.

Locks in the invariant that uvicorn runs WITHOUT ``--proxy-headers``, so
every request's TCP peer (``request.client.host``) is always ``127.0.0.1``
regardless of what's in ``X-Forwarded-For``. This is the foundation of:

* ``cf_access._is_trusted_hop`` returning True from the production chain
  without monkey-patching ``TRUSTED_PROXIES`` (which is what
  ``test_cf_access.py`` does for unit-level coverage);
* the nginx ``geo $cf_hop_trusted`` block being the sole trust gate for
  Cf-Access-* / Cf-Connecting-IP headers;
* ``network.get_client_ip`` finding the real client IP from
  ``Cf-Connecting-IP`` / ``X-Real-IP`` rather than from the spoofable peer
  address.

If these tests start failing, the most likely cause is that
``--proxy-headers`` has been added to ``start-wsi-app.sh`` /
``wsi-sync.service`` — uvicorn would then rewrite ``scope["client"]``
based on ``X-Forwarded-For`` and the loopback-peer assumption breaks.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from wsi_app import app_auth as _app_auth
from wsi_app import cf_access as _cf_access
from wsi_app.main import app


@pytest.fixture(scope="module")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "private_pem": private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        "public_pem": private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    }


@pytest.fixture(autouse=True)
def cf_enabled(monkeypatch, rsa_keypair):
    """Configure cf_access as if CF_ACCESS_* envs were set, and stub JWKS.

    Deliberately does NOT widen ``_TRUSTED_PROXIES`` — the whole point of
    this file is to exercise the production loopback-peer invariant
    (``request.client.host == "127.0.0.1"``) where the default
    ``_TRUSTED_PROXIES = {"127.0.0.1", "::1"}`` already matches.
    """
    monkeypatch.setattr(_cf_access, "_TEAM_DOMAIN", "test-team.cloudflareaccess.com")
    monkeypatch.setattr(_cf_access, "_AUDIENCE", "test-aud-tag")

    class _FakeKey:
        def __init__(self, key):
            self.key = key

    class _FakeJWKSClient:
        def __init__(self, public_pem: bytes):
            self._public_pem = public_pem

        def get_signing_key_from_jwt(self, _token: str):
            return _FakeKey(self._public_pem)

    fake = _FakeJWKSClient(rsa_keypair["public_pem"])
    monkeypatch.setattr(_cf_access, "_jwks_client", lambda force_refresh=False: fake)
    yield


@pytest.fixture
def loopback_client() -> TestClient:
    """TestClient whose ``request.client.host`` is ``127.0.0.1``.

    Mirrors the production invariant where uvicorn binds 127.0.0.1 only
    and runs without ``--proxy-headers``, so the TCP peer is always
    loopback irrespective of which of the three access paths produced
    the request. The default TestClient reports ``"testclient"``, which
    would obscure the loopback-only trust gate.
    """
    return TestClient(app, client=("127.0.0.1", 12345))


def _sign_jwt(rsa_keypair, *, email: str = "alice@example.edu",
              aud: str = "test-aud-tag",
              iss: str = "https://test-team.cloudflareaccess.com",
              exp_offset: int = 60) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": iss,
            "aud": aud,
            "iat": now,
            "exp": now + exp_offset,
            "email": email,
            "sub": "user-1234",
        },
        rsa_keypair["private_pem"],
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# Path A: CF Access + Tunnel (verified JWT, real client IP via Cf-Connecting-IP)
# ---------------------------------------------------------------------------
def test_path_a_cf_access_admits_through_loopback(loopback_client, rsa_keypair, db_session):
    """A CF-authenticated request reaches the app and the analytics row
    captures both the verified email and the real public client IP.

    Production header shape: cloudflared on loopback sends
    ``Cf-Connecting-IP`` and ``Cf-Access-Jwt-Assertion``; nginx forwards
    them through the geo gate. uvicorn (without ``--proxy-headers``)
    reports the TCP peer (``127.0.0.1``) as ``request.client.host``, so
    ``_is_trusted_hop`` is True and the JWT verifies normally.
    """
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    token = _sign_jwt(rsa_keypair, email="alice@example.edu")
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={
            _cf_access.CF_JWT_HEADER: token,
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_email == "alice@example.edu"
    assert rows[0].client_ip == "203.0.113.7"


def test_path_a_admin_endpoint_admits_with_valid_cf_jwt(loopback_client, rsa_keypair, admin_headers):
    """Admin-protected endpoint with both an admin Bearer JWT and a
    valid CF JWT: admin token wins (HTTP middleware short-circuits before
    the CF gate). Ensures the loopback peer doesn't change that order.
    """
    token = _sign_jwt(rsa_keypair, email="admin@example.edu")
    r = loopback_client.get(
        "/api/admin/analytics/dashboard",
        headers={
            **admin_headers,
            _cf_access.CF_JWT_HEADER: token,
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Path B: CF Tunnel only / bypass policy (no JWT, but Cf-Connecting-IP set)
# ---------------------------------------------------------------------------
def test_path_b_bypass_admits_with_no_jwt(loopback_client, db_session):
    """Bypass-IP path through CF tunnel: no JWT, only Cf-Connecting-IP
    is set by cloudflared. CF gate sees status="absent" and falls through
    to mode-based auth (default ``"none"`` admits). The recorded row
    captures the Cf-Connecting-IP value as client_ip.
    """
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={_cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.42"},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].client_ip == "203.0.113.42"
    assert rows[0].user_email == ""


# ---------------------------------------------------------------------------
# Path C: LAN-direct (no CF headers; X-Real-IP from on-host nginx)
# ---------------------------------------------------------------------------
def test_path_c_lan_direct_admits_with_xrealip(loopback_client, db_session):
    """LAN-direct path: nginx scrubs Cf-* headers (the geo block) and
    forwards X-Real-IP=$remote_addr. CF gate is "absent", mode-based
    auth admits at default. Recorded row carries the LAN IP.
    """
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].client_ip == "192.168.1.42"


# ---------------------------------------------------------------------------
# Carrying the CF Access identity onto the LAN host (public -> LAN)
# ---------------------------------------------------------------------------
def _carry_set_cookie(response) -> str:
    """Return the raw Set-Cookie line for the app-access session, or ""."""
    for line in response.headers.get_list("set-cookie"):
        if line.startswith(f"{_app_auth.SESSION_COOKIE}="):
            return line
    return ""


def test_carry_cookie_minted_on_path_a_when_domain_configured(
    loopback_client, rsa_keypair, monkeypatch
):
    """With WSI_SESSION_COOKIE_DOMAIN set, a CF-authenticated request gets a
    parent-domain session cookie whose subject is the verified email, so the
    public -> LAN redirect carries the identity without a second login.
    """
    monkeypatch.setattr(_app_auth, "SESSION_COOKIE_DOMAIN", "wsi.example.org")
    token = _sign_jwt(rsa_keypair, email="alice@example.edu")
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={
            _cf_access.CF_JWT_HEADER: token,
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 204
    set_cookie = _carry_set_cookie(r)
    assert set_cookie, "expected an app-access session Set-Cookie"
    assert "domain=wsi.example.org" in set_cookie.lower()
    assert "httponly" in set_cookie.lower()
    # The minted cookie's subject is the bare verified email, matching what
    # the CF JWT path records on the public host. Parse the token out of the
    # raw Set-Cookie line: httpx's jar drops a cookie whose Domain doesn't
    # match the request host, so r.cookies wouldn't expose it.
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    payload = _app_auth.decode_session_token(value)
    assert payload is not None
    assert payload.get("sub") == "alice@example.edu"


def test_no_carry_cookie_when_domain_unset(loopback_client, rsa_keypair, monkeypatch):
    """Default deployment (no shared cookie domain): the CF gate admits the
    request but mints no session cookie — behavior is unchanged.
    """
    monkeypatch.setattr(_app_auth, "SESSION_COOKIE_DOMAIN", "")
    token = _sign_jwt(rsa_keypair, email="alice@example.edu")
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={
            _cf_access.CF_JWT_HEADER: token,
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 204
    assert _carry_set_cookie(r) == ""


def test_carried_cookie_records_email_on_lan_path(loopback_client, db_session):
    """A LAN-direct (Path C) request that carries the session cookie minted
    on Path A records the same email — no CF headers, no second login.
    """
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    session = _app_auth.create_session_token("alice@example.edu")
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-real-ip": "192.168.1.42"},
        cookies={_app_auth.SESSION_COOKIE: session},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_email == "alice@example.edu"
    assert rows[0].client_ip == "192.168.1.42"


# ---------------------------------------------------------------------------
# Break-glass: unauthenticated raw-IP (plaintext, non-tunnel) exemption
# ---------------------------------------------------------------------------
@pytest.fixture
def gated_mode(monkeypatch):
    """Force a gated app-access mode so an unexempt request is 401'd.

    Picks ``google_oauth`` because its raw-IP login genuinely cannot complete
    (private-IP/http redirect URI), which is the motivating case for the
    break-glass exemption.
    """
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth")
    )


def test_breakglass_admits_plaintext_non_tunnel_when_enabled(
    loopback_client, gated_mode, monkeypatch
):
    """Flag on + X-Forwarded-Proto=http + no Cf-Connecting-IP (the raw-IP :80
    server block) bypasses the gate that would otherwise 401."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-forwarded-proto": "http", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 204


def test_breakglass_does_not_open_tls_lan_host(
    loopback_client, gated_mode, monkeypatch
):
    """The TLS LAN-direct vhost (X-Forwarded-Proto=https) stays gated even with
    the flag on, so normal LAN use keeps its app-access login."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-forwarded-proto": "https", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 401


def test_breakglass_does_not_open_tunnel_path(
    loopback_client, gated_mode, monkeypatch
):
    """A tunnel request (Cf-Connecting-IP present) is excluded even when the
    edge scheme is http, so the public hostname never becomes unauthenticated."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={
            "x-forwarded-proto": "http",
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 401


def test_breakglass_does_not_open_plaintext_to_a_public_client(
    loopback_client, gated_mode, monkeypatch
):
    """nginx's plain-HTTP listener is a default_server for every name, so a
    plaintext request can also come from the Internet (http://<public-IP>/
    where port 80 is reachable). The break-glass route is a LAN fallback: a
    public client stays gated even with the flag on."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    for client_ip in ("203.0.113.9", "8.8.8.8", "2001:db8::5", "100.64.0.1"):
        r = loopback_client.post(
            "/api/analytics/pageview",
            json={"kind": "top"},
            headers={"x-forwarded-proto": "http", "x-real-ip": client_ip},
        )
        assert r.status_code == 401, client_ip
    # A forged X-Real-IP from an untrusted (non-loopback) peer is ignored: the
    # peer's own public address decides.
    public_peer = TestClient(app, client=("203.0.113.9", 40000))
    r = public_peer.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-forwarded-proto": "http", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 401
    # Every LAN range still qualifies.
    for client_ip in ("10.1.2.3", "172.16.0.9", "192.168.1.42", "fd00::7", "fe80::1"):
        r = loopback_client.post(
            "/api/analytics/pageview",
            json={"kind": "top"},
            headers={"x-forwarded-proto": "http", "x-real-ip": client_ip},
        )
        assert r.status_code == 204, client_ip


def test_breakglass_disabled_by_default_keeps_plaintext_gated(
    loopback_client, gated_mode, monkeypatch
):
    """Flag off (the default): the same plaintext non-tunnel request is gated,
    so existing deployments are unaffected."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", False)
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"x-forwarded-proto": "http", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 401


def test_breakglass_auth_state_reports_ungated(
    loopback_client, gated_mode, monkeypatch
):
    """On the break-glass path, /api/auth/state reports an un-gated state so the
    SPA does not bounce the visitor to a login flow that cannot complete."""
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    r = loopback_client.get(
        "/api/auth/state", headers={"x-forwarded-proto": "http"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "none"
    assert body["authenticated"] is True
    # Same request over the TLS vhost still advertises the real gated mode.
    r2 = loopback_client.get(
        "/api/auth/state", headers={"x-forwarded-proto": "https"}
    )
    assert r2.json()["mode"] == "google_oauth"


# ---------------------------------------------------------------------------
# Direct LAN exemption: a tri-state level chooses how far the skip reaches.
#   "lan" — any non-tunnel LAN-client request (bare IP or hostname/vhost)
#   "ip"  — only a bare private-IP literal Host (the narrow level)
#   "off" — never (default)
# Both non-off levels stay anchored on the two non-forgeable signals (not via
# the tunnel + private real client IP), so the public path is never opened.
# ---------------------------------------------------------------------------
@pytest.fixture
def lan_direct_lan_mode(monkeypatch):
    """Gated google_oauth mode with the exemption at the broad "lan" level."""
    monkeypatch.setattr(
        _app_auth,
        "_snapshot",
        _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan"),
    )


@pytest.fixture
def lan_direct_ip_mode(monkeypatch):
    """Gated google_oauth mode with the exemption at the narrow "ip" level."""
    monkeypatch.setattr(
        _app_auth,
        "_snapshot",
        _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="ip"),
    )


def test_lan_direct_lan_admits_private_ip_host_from_lan_client(
    loopback_client, lan_direct_lan_mode
):
    """"lan" level + private-IP Host + private real client IP + no tunnel: the
    bare-IP access bypasses the gate that would otherwise 401."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "192.168.1.50", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 204


def test_lan_direct_lan_admits_hostname_host_from_lan_client(
    loopback_client, lan_direct_lan_mode
):
    """At the "lan" level the Host header is not consulted: a LAN-direct
    hostname/vhost request from a LAN client (no tunnel) is admitted just like
    a bare-IP one, so the normal LAN-direct login flow is also exempted."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "lan.wsi.example.org", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 204


def test_lan_direct_ip_admits_bare_ip_host_from_lan_client(
    loopback_client, lan_direct_ip_mode
):
    """At the narrow "ip" level a bare private-IP literal Host from a LAN
    client is still admitted (the direct-IP route)."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "192.168.1.50", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 204


def test_lan_direct_ip_gates_hostname_host(
    loopback_client, lan_direct_ip_mode
):
    """At the "ip" level a hostname/vhost Host is NOT a bare-IP literal, so it
    stays gated even from a LAN client — the regular login UX is preserved."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "lan.wsi.example.org", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 401


def test_lan_direct_does_not_open_public_client_ip(
    loopback_client, lan_direct_lan_mode
):
    """Even with a private-IP Host, a public real client IP (the spoofed
    `Host: <private-IP>` against the public endpoint) stays gated."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "192.168.1.50", "x-real-ip": "203.0.113.7"},
    )
    assert r.status_code == 401


def test_lan_direct_does_not_open_tunnel_path(
    loopback_client, lan_direct_lan_mode
):
    """A tunnel request (Cf-Connecting-IP present) is excluded, so the public
    hostname never becomes unauthenticated via this exemption."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={
            "host": "192.168.1.50",
            _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        },
    )
    assert r.status_code == 401


def test_lan_direct_disabled_by_default_keeps_gated(
    loopback_client, gated_mode
):
    """Level "off" (the default snapshot): the same bare-IP request is gated,
    so existing deployments are unaffected."""
    r = loopback_client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"host": "192.168.1.50", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 401


def test_lan_direct_auth_state_reports_ungated(
    loopback_client, lan_direct_lan_mode
):
    """On the direct-LAN-IP path, /api/auth/state reports an un-gated state so
    the SPA does not bounce the visitor to a login flow that cannot complete
    against a private-IP origin."""
    r = loopback_client.get(
        "/api/auth/state",
        headers={"host": "192.168.1.50", "x-real-ip": "192.168.1.42"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "none"
    assert body["authenticated"] is True
    # A public real client IP (spoofed Host against the public endpoint) is
    # not on the LAN, so it still advertises the real gated mode regardless of
    # the Host header.
    r2 = loopback_client.get(
        "/api/auth/state",
        headers={"host": "192.168.1.50", "x-real-ip": "203.0.113.7"},
    )
    assert r2.json()["mode"] == "google_oauth"


# ---------------------------------------------------------------------------
# Guard: uvicorn must not run with --proxy-headers
# ---------------------------------------------------------------------------
def test_proxy_headers_simulated_public_peer_rejects_cf_jwt(rsa_keypair):
    """Simulates the broken state that ``--proxy-headers`` would cause.

    With ``--proxy-headers --forwarded-allow-ips=127.0.0.1`` enabled,
    uvicorn rewrites ``scope["client"]`` from ``X-Forwarded-For``. For a
    CF Tunnel request whose XFF chain leaks the public client IP (e.g.,
    cloudflared adding ``X-Forwarded-For: <public IP>``), this lands on
    ``request.client.host = <public IP>`` — which is **not** in
    ``_TRUSTED_PROXIES``, so ``cf_access._is_trusted_hop`` returns False
    and the middleware 401s a perfectly legitimate request.

    Pin the failure shape here so adding ``--proxy-headers`` to the systemd
    unit is loud rather than silent.
    """
    public_peer_client = TestClient(app, client=("203.0.113.99", 12345))
    token = _sign_jwt(rsa_keypair)
    r = public_peer_client.get(
        "/api/admin/analytics/dashboard",
        headers={_cf_access.CF_JWT_HEADER: token},
    )
    assert r.status_code == 401
