"""Cloudflare Access integration tests.

Covers:

* JWT verification (valid, expired, wrong audience, wrong issuer, bad
  signature, missing header, untrusted hop).
* `probe_identity` precedence: admin JWT > CF Access > app session.
* Middleware behavior:
    - Valid CF JWT → request admitted, identity captured by analytics.
    - Invalid CF JWT → 401 on non-beacon paths, 204 + WARN log on beacon.
    - Absent CF JWT (bypass-IP path) → falls through to existing
      mode-based auth (verified by the `app_auth.mode == "basic"` smoke
      test).
* `network.get_client_ip` honoring `Cf-Connecting-IP`.

The tests sign tokens locally with an RSA key and patch
``cf_access._jwks_client`` so the JWKS network call is replaced by an
in-memory PyJWK constructed from the test key. This keeps the suite
hermetic — no DNS, no Cloudflare round-trip — while still exercising
the real PyJWT signature verification path.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from wsi_app import analytics as _analytics
from wsi_app import app_auth as _app_auth
from wsi_app import cf_access as _cf_access
from wsi_app import network as _network
from wsi_app.auth import create_token, resolve_staff_user


# ---------------------------------------------------------------------------
# RSA key fixture: one keypair per session, reused across tests.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rsa_keypair():
    """RSA-2048 keypair used to sign Cloudflare Access JWTs in tests."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {"private_pem": private_pem, "public_pem": public_pem, "private": private}


@pytest.fixture(autouse=True)
def _fresh_cf_warning_limit():
    """The invalid-assertion warning is rate-limited per process; let every
    test here see its first occurrence logged."""
    _analytics._cf_invalid_warning.reset()


@pytest.fixture(autouse=True)
def cf_enabled(monkeypatch, rsa_keypair):
    """Configure the cf_access module as if `CF_ACCESS_*` envs were set,
    and stub the JWKS client to return the test public key.

    Starlette's TestClient reports ``request.client.host == "testclient"``,
    so TRUSTED_PROXIES is widened to include that sentinel for the duration
    of the test — otherwise the trusted-hop gate would treat every test
    request as if it arrived from an external peer and reject the JWT.
    """
    monkeypatch.setattr(_cf_access, "_TEAM_DOMAIN", "test-team.cloudflareaccess.com")
    monkeypatch.setattr(_cf_access, "_AUDIENCE", "test-aud-tag")
    monkeypatch.setattr(
        _network, "_TRUSTED_PROXIES",
        frozenset(_network._TRUSTED_PROXIES | {"testclient"}),
    )

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sign(rsa_keypair, *, email: str = "alice@example.edu",
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
# verify_jwt + cf_jwt_status
# ---------------------------------------------------------------------------
def test_verify_jwt_valid(rsa_keypair):
    token = _sign(rsa_keypair)
    claims = _cf_access.verify_jwt(token)
    assert claims is not None
    assert claims["email"] == "alice@example.edu"


def test_verify_jwt_rejects_expired(rsa_keypair):
    token = _sign(rsa_keypair, exp_offset=-10)
    assert _cf_access.verify_jwt(token) is None


def test_verify_jwt_rejects_wrong_aud(rsa_keypair):
    token = _sign(rsa_keypair, aud="some-other-app")
    assert _cf_access.verify_jwt(token) is None


def test_verify_jwt_rejects_wrong_issuer(rsa_keypair):
    token = _sign(rsa_keypair, iss="https://attacker.example.com")
    assert _cf_access.verify_jwt(token) is None


def test_verify_jwt_rejects_garbage_signature(rsa_keypair):
    token = _sign(rsa_keypair)
    # Mangle the last few signature bytes — signature verification fails.
    tampered = token[:-4] + ("a" if token[-1] != "a" else "b") * 4
    assert _cf_access.verify_jwt(tampered) is None


def test_cf_jwt_status_absent_when_header_missing():
    headers = {}
    status, claims = _cf_access.cf_jwt_status(headers, "127.0.0.1")
    assert status == "absent"
    assert claims is None


def test_cf_jwt_status_invalid_when_hop_untrusted(rsa_keypair):
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair)}
    # 192.0.2.5 is in TEST-NET-1 — clearly not in TRUSTED_PROXIES.
    status, claims = _cf_access.cf_jwt_status(headers, "192.0.2.5")
    assert status == "invalid"
    assert claims is None


def test_cf_jwt_status_valid_from_loopback(rsa_keypair):
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair)}
    status, claims = _cf_access.cf_jwt_status(headers, "127.0.0.1")
    assert status == "valid"
    assert claims["email"] == "alice@example.edu"


def test_cf_jwt_status_invalid_when_disabled(monkeypatch, rsa_keypair):
    """A JWT arriving while CF integration is disabled is treated as
    invalid (rather than absent) so the operator notices the
    misconfiguration via 401s."""
    monkeypatch.setattr(_cf_access, "_TEAM_DOMAIN", "")
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair)}
    status, _ = _cf_access.cf_jwt_status(headers, "127.0.0.1")
    assert status == "invalid"


def test_cf_jwt_status_invalid_when_aud_unset(monkeypatch, rsa_keypair):
    """Refusing to verify when AUD is unset prevents a tenant-wide
    token (any Access app in the team) from being accepted."""
    monkeypatch.setattr(_cf_access, "_AUDIENCE", "")
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair)}
    status, _ = _cf_access.cf_jwt_status(headers, "127.0.0.1")
    assert status == "invalid"


# ---------------------------------------------------------------------------
# is_via_cloudflare_tunnel — independent of CF Access SSO configuration
# ---------------------------------------------------------------------------
def test_is_via_cloudflare_tunnel_path_a_with_jwt(rsa_keypair):
    """Path A: full CF Access SSO. cloudflared sets both Cf-Connecting-IP
    and a verified JWT. The helper looks at Cf-Connecting-IP only, so
    True regardless of the JWT."""
    headers = {
        _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7",
        _cf_access.CF_JWT_HEADER: _sign(rsa_keypair),
    }
    assert _cf_access.is_via_cloudflare_tunnel(headers, "127.0.0.1") is True


def test_is_via_cloudflare_tunnel_path_b_no_jwt():
    """Path B: bare CF Tunnel + Bypass policy — Cf-Connecting-IP is set
    by cloudflared but no Access JWT is asserted. Must still register
    as 'via tunnel'."""
    headers = {_cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7"}
    assert _cf_access.is_via_cloudflare_tunnel(headers, "127.0.0.1") is True


def test_is_via_cloudflare_tunnel_path_c_lan_direct():
    """Path C: LAN direct. nginx scrubs Cf-Connecting-IP for non-loopback
    peers, so the header is empty even from the trusted hop. Must
    register as not-via-tunnel."""
    headers = {_cf_access.CF_CONNECTING_IP_HEADER: ""}
    assert _cf_access.is_via_cloudflare_tunnel(headers, "127.0.0.1") is False
    assert _cf_access.is_via_cloudflare_tunnel({}, "127.0.0.1") is False


def test_is_via_cloudflare_tunnel_rejects_untrusted_peer():
    """Defense-in-depth: if some misconfigured proxy ever forwards
    Cf-Connecting-IP from a non-loopback peer, the helper still
    refuses to claim 'via tunnel' so a forged header from the LAN
    side cannot trigger the warning banner."""
    headers = {_cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7"}
    assert _cf_access.is_via_cloudflare_tunnel(headers, "192.0.2.5") is False


def test_is_via_cloudflare_tunnel_independent_of_cf_access_disabled(monkeypatch):
    """Detection must work even when CF Access SSO is not configured
    (CF_ACCESS_TEAM_DOMAIN unset). The user cares about the transport
    path, not the auth layer."""
    monkeypatch.setattr(_cf_access, "_TEAM_DOMAIN", "")
    headers = {_cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7"}
    assert _cf_access.is_via_cloudflare_tunnel(headers, "127.0.0.1") is True


# Set by Cloudflare next to the assertion but not covered by its signature.
_CF_EMAIL_HEADER = "cf-access-authenticated-user-email"


def test_cf_email_header_is_ignored_without_an_email_claim(rsa_keypair, db_session):
    """The ``Cf-Access-Authenticated-User-Email`` header is not covered by
    the JWT signature, so a service-token JWT (no ``email`` claim) must NOT
    fall back to it — that would let a caller on the trusted hop forge the
    identity recorded by the analytics beacon. The verified-claim-less case
    records anonymous instead."""
    headers = {
        _cf_access.CF_JWT_HEADER: _sign(rsa_keypair, email=""),
        _CF_EMAIL_HEADER: "svc@example.edu",
    }
    assert _analytics.probe_identity(
        None, headers, "127.0.0.1", db_session,
    ) == (None, "")


def test_cf_email_comes_from_the_verified_claim_not_the_header(rsa_keypair, db_session):
    """The verified JWT's ``email`` claim is the only trusted source."""
    headers = {
        _cf_access.CF_JWT_HEADER: _sign(rsa_keypair, email="real@example.edu"),
        _CF_EMAIL_HEADER: "spoofed@evil.example",
    }
    assert _analytics.probe_identity(
        None, headers, "127.0.0.1", db_session,
    ) == (None, "real@example.edu")


# ---------------------------------------------------------------------------
# network.get_client_ip
# ---------------------------------------------------------------------------
def test_get_client_ip_prefers_cf_connecting_ip_over_x_real_ip():
    headers = {"cf-connecting-ip": "203.0.113.7", "x-real-ip": "10.0.0.1"}
    assert _network.get_client_ip(headers, "127.0.0.1") == "203.0.113.7"


def test_get_client_ip_falls_back_to_x_real_ip():
    """No CF header → use X-Real-IP from a trusted proxy."""
    headers = {"x-real-ip": "10.0.0.5"}
    assert _network.get_client_ip(headers, "127.0.0.1") == "10.0.0.5"


def test_get_client_ip_ignores_headers_from_untrusted_hop():
    """Spoofed forwarded headers from a non-proxy peer must be discarded."""
    headers = {"cf-connecting-ip": "1.2.3.4", "x-real-ip": "5.6.7.8"}
    assert _network.get_client_ip(headers, "192.0.2.5") == "192.0.2.5"


# ---------------------------------------------------------------------------
# probe_identity precedence
# ---------------------------------------------------------------------------
def test_probe_identity_admin_token_wins_over_cf(rsa_keypair, db_session, admin_user):
    """An admin's JWT bearer is the most specific identity available so it
    takes precedence over a CF Access email on the same request — the row
    gets the admin's User.id + username (a stronger attribution than just
    an email string). The beacon routes resolve the bearer once and pass the
    row in as ``staff_user``."""
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair, email="cf-only@example.edu")}
    token = create_token(admin_user)
    user_id, email = _analytics.probe_identity(
        None, headers, "127.0.0.1", db_session,
        staff_user=resolve_staff_user(token, db_session),
    )
    assert user_id == admin_user.id
    assert email == admin_user.username


def test_probe_identity_unresolved_bearer_is_not_an_identity(rsa_keypair, db_session, admin_user):
    """A bearer the caller could not resolve (revoked, expired, its user id
    since reused) attributes nothing: identity falls through to the next
    source instead of being read off the token's ``sub``."""
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair, email="cf-only@example.edu")}
    user_id, email = _analytics.probe_identity(
        None, headers, "127.0.0.1", db_session,
    )
    assert (user_id, email) == (None, "cf-only@example.edu")


def test_probe_identity_uses_cf_email_when_no_admin(rsa_keypair, db_session):
    headers = {_cf_access.CF_JWT_HEADER: _sign(rsa_keypair, email="student@example.edu")}
    user_id, email = _analytics.probe_identity(
        None, headers, "127.0.0.1", db_session,
    )
    assert user_id is None
    assert email == "student@example.edu"


def test_probe_identity_falls_back_to_session_when_no_cf(db_session):
    """Path B (bypass IP path) — no CF JWT, identity comes from the
    app's own session cookie."""
    cookie = _app_auth.create_session_token("internal-user@example.edu")
    user_id, email = _analytics.probe_identity(
        cookie, {}, "127.0.0.1", db_session,
    )
    assert user_id is None
    assert email == "internal-user@example.edu"


def test_probe_identity_invalid_cf_jwt_logs_and_returns_anonymous(
    rsa_keypair, db_session, caplog
):
    """Invalid CF JWT + no other identity → anonymous + WARN log."""
    expired = _sign(rsa_keypair, exp_offset=-10)
    headers = {_cf_access.CF_JWT_HEADER: expired}
    with caplog.at_level("WARNING", logger="wsi_app.analytics"):
        user_id, email = _analytics.probe_identity(
            None, headers, "127.0.0.1", db_session,
        )
    assert user_id is None
    assert email == ""
    assert any("CF Access JWT verification failed" in m for m in caplog.messages)


def test_probe_identity_attributes_only_the_verified_basic_user(db_session, monkeypatch):
    """Path 4 uses the username the middleware verified on this request
    (request.state.verified_basic_user), never the raw Basic header: a request
    admitted by CF Access or an exemption carries an unchecked header, and
    trusting it would let a caller write rows in any learner's name. The raw
    header is not even an input of probe_identity."""
    snap = _app_auth._AuthSnapshot(mode="basic")
    monkeypatch.setattr(_app_auth, "_snapshot", snap)
    user_id, email = _analytics.probe_identity(
        None, {}, "127.0.0.1", db_session,
        verified_basic_user="alice",
    )
    assert user_id is None
    assert email == "basic:alice"

    # The same request without a verified user is not an identity.
    user_id, email = _analytics.probe_identity(
        None, {}, "127.0.0.1", db_session,
    )
    assert (user_id, email) == (None, "")


def test_probe_identity_session_cookie_wins_over_basic_in_basic_mode(db_session, monkeypatch):
    """When both a session cookie and a verified Basic user are present (e.g.
    a user who form-logged-in but whose browser still caches Basic creds
    from an earlier session), the cookie is the more authoritative
    identity and wins."""
    snap = _app_auth._AuthSnapshot(mode="basic")
    monkeypatch.setattr(_app_auth, "_snapshot", snap)
    # No AuthUser row backs this subject; the credential binding is covered
    # in test_auth_guards, so treat the cookie's credential as live.
    monkeypatch.setattr(_app_auth, "_basic_session_live", lambda _u, _f, **_kw: True)
    cookie = _app_auth.create_session_token("basic:cookie-user", credential="x")
    user_id, email = _analytics.probe_identity(
        cookie, {}, "127.0.0.1", db_session,
        verified_basic_user="alice",
    )
    assert user_id is None
    assert email == "basic:cookie-user"


def test_probe_identity_invalid_cf_jwt_does_not_fall_through_to_session(
    rsa_keypair, db_session
):
    """An invalid CF JWT classifies the request as untrustworthy at the
    CF gate. The HTTP middleware 401s the same request on every non-beacon
    path; for the beacon (best-effort) path the row is recorded as anonymous instead of
    silently attributing the row to whatever app-session cookie happens to
    be on the same request. Both layers individually require JWT_SECRET so
    this is defense-in-depth rather than a hard security boundary, but
    keeping the failure mode consistent with the middleware avoids
    surprising operator-facing analytics."""
    expired = _sign(rsa_keypair, exp_offset=-10)
    headers = {_cf_access.CF_JWT_HEADER: expired}
    cookie = _app_auth.create_session_token("session-user@example.edu")
    user_id, email = _analytics.probe_identity(
        cookie, headers, "127.0.0.1", db_session,
    )
    assert email == ""
    assert user_id is None


# ---------------------------------------------------------------------------
# Middleware: end-to-end behavior via TestClient
# ---------------------------------------------------------------------------
def test_middleware_admits_valid_cf_jwt_in_mode_none(client, rsa_keypair, db_session):
    """mode=none default: a CF-authenticated request reaches the app and
    the analytics beacon records the verified email."""
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    token = _sign(rsa_keypair, email="alice@example.edu")
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={_cf_access.CF_JWT_HEADER: token},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_email == "alice@example.edu"


def test_middleware_rejects_invalid_cf_jwt_on_non_beacon_paths(client, rsa_keypair):
    """Expired/tampered CF JWT → 401 for a real API call."""
    expired = _sign(rsa_keypair, exp_offset=-10)
    r = client.get(
        "/api/health",
        headers={_cf_access.CF_JWT_HEADER: expired},
    )
    # /api/health is in BYPASS_PREFIXES so it returns 200 regardless.
    # Use an admin-protected endpoint to confirm the gate fires.
    assert r.status_code == 200

    r = client.get(
        "/api/admin/analytics/dashboard",
        headers={_cf_access.CF_JWT_HEADER: expired},
    )
    assert r.status_code == 401


def test_middleware_admits_beacon_with_invalid_cf_jwt_but_records_anonymous(
    client, rsa_keypair, db_session, caplog,
):
    """Beacon is best-effort: an invalid JWT does not 4xx the request
    (so the SPA's unauthenticated beacon never breaks), but the row
    records anonymous and a WARN log fires."""
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()

    expired = _sign(rsa_keypair, exp_offset=-10)
    with caplog.at_level("WARNING", logger="wsi_app.analytics"):
        r = client.post(
            "/api/analytics/pageview",
            json={"kind": "top"},
            headers={_cf_access.CF_JWT_HEADER: expired},
        )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_email == ""
    assert any("CF Access JWT verification failed" in m for m in caplog.messages)


def test_middleware_admits_every_beacon_with_invalid_cf_jwt(client, rsa_keypair):
    """The dwell and tour beacons share the pageview beacon's contract.

    All three are fire-and-forget and must never 4xx the SPA, and the admin
    dashboard shows their streams side by side — so exempting only the pageview
    path silently drops dwell/tour data for exactly the CF-fronted users whose
    assertion went stale, and the three views stop agreeing.
    """
    expired = _sign(rsa_keypair, exp_offset=-10)
    headers = {_cf_access.CF_JWT_HEADER: expired}

    # An empty body is silently dropped by both handlers (204, nothing
    # persisted); what matters here is that the middleware let it through.
    dwell = client.post("/api/analytics/slide-dwell", json={}, headers=headers)
    assert dwell.status_code == 204, dwell.text

    tour = client.post("/api/analytics/tour-event", json={}, headers=headers)
    assert tour.status_code == 204, tour.text


def test_middleware_passes_through_with_no_cf_jwt(client, db_session):
    """Bypass-IP path (no CF JWT, mode=none): existing behavior unchanged."""
    from sqlalchemy import delete, select
    from wsi_app.models import PageView
    db_session.execute(delete(PageView))
    db_session.commit()
    r = client.post("/api/analytics/pageview", json={"kind": "top"})
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_email == ""


def test_middleware_admin_token_bypasses_cf_gate(client, rsa_keypair, admin_headers):
    """An admin with a valid bearer token reaches the app even when
    the request also carries an invalid CF JWT (e.g. the admin's CF
    session expired but their admin token is still fresh)."""
    expired = _sign(rsa_keypair, exp_offset=-10)
    r = client.get(
        "/api/admin/analytics/dashboard",
        headers={**admin_headers, _cf_access.CF_JWT_HEADER: expired},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /api/admin/network/my-ip — surfaces the tunnel detection to the dashboard
# ---------------------------------------------------------------------------
def test_my_ip_reports_via_cloudflare_tunnel_when_header_present(client, admin_headers):
    r = client.get(
        "/api/admin/network/my-ip",
        headers={**admin_headers, _cf_access.CF_CONNECTING_IP_HEADER: "203.0.113.7"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["via_cloudflare_tunnel"] is True
    assert "ip" in body


def test_my_ip_reports_not_via_tunnel_for_lan_direct(client, admin_headers):
    r = client.get("/api/admin/network/my-ip", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["via_cloudflare_tunnel"] is False


def test_beacon_with_invalid_cf_jwt_is_gated_by_mode(client, rsa_keypair, monkeypatch):
    """A forged CF assertion must not become a way *past* the app-access gate.

    The beacon is best-effort, so an invalid CF JWT does not 4xx it — but that
    must not be an unconditional short-circuit, which would make a junk
    Cf-Access-Jwt-Assertion header strictly more powerful than sending no header
    at all: junk classifying as "invalid" (admitted) while absent falls through
    to the mode gate (401) would let anyone write analytics rows on a gated
    install. The beacon falls through to the gate like every other path.
    """
    snap = _app_auth._AuthSnapshot(mode="basic")
    monkeypatch.setattr(_app_auth, "_snapshot", snap)
    expired = _sign(rsa_keypair, exp_offset=-10)

    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={_cf_access.CF_JWT_HEADER: expired},
    )
    assert r.status_code == 401, r.text

    # Sending no CF header at all is gated the same way — the two cases must
    # not be treated differently.
    r2 = client.post("/api/analytics/pageview", json={"kind": "top"})
    assert r2.status_code == 401, r2.text


def test_auth_state_reports_valid_cf_jwt_as_authenticated(client, rsa_keypair, monkeypatch):
    """/api/auth/state bypasses the middleware, so it has to recognise a
    verified CF Access JWT itself. Otherwise useAuthGate bounces an external
    user whom CF Access (and the middleware) already admitted to /login, where
    an LDAP / Google login they have no account for is their only way on."""
    for mode in ("ldap", "google_oauth"):
        monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode=mode))
        r = client.get(
            "/api/auth/state",
            headers={_cf_access.CF_JWT_HEADER: _sign(rsa_keypair)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == mode
        assert r.json()["authenticated"] is True
        # Not an app session: the app's sign-out cannot end a CF Access one.
        assert r.json()["app_session"] is False

        # An invalid (expired) assertion and no assertion stay unauthenticated.
        expired = _sign(rsa_keypair, exp_offset=-10)
        r = client.get("/api/auth/state", headers={_cf_access.CF_JWT_HEADER: expired})
        assert r.json()["authenticated"] is False
        assert client.get("/api/auth/state").json()["authenticated"] is False


def test_an_unreachable_jwks_endpoint_is_an_outage_for_rechecks(monkeypatch, rsa_keypair):
    """PyJWT reports a failed key-set fetch as PyJWKClientConnectionError (a
    PyJWKClientError). For a live socket's re-check that must surface as
    VerifierUnavailable — retried — not as an invalid assertion, which
    closes the socket for good. Every other caller still fails closed."""
    import jwt as _jwt

    calls = []

    class _Down:
        def get_signing_key_from_jwt(self, token):
            calls.append(token)
            raise _jwt.PyJWKClientConnectionError("Fail to fetch data from the url")

    monkeypatch.setattr(_cf_access, "_jwks_client", lambda force_refresh=False: _Down())
    token = _sign(rsa_keypair)
    with pytest.raises(_cf_access.VerifierUnavailable):
        _cf_access.verify_jwt(token, raise_unavailable=True)
    assert len(calls) == 2  # retried once with a fresh client
    assert _cf_access.verify_jwt(token) is None
    status, _claims = _cf_access.cf_jwt_status({_cf_access.CF_JWT_HEADER: token}, "127.0.0.1")
    assert status == "invalid"
