"""Auth guards across the staff, app-access and download paths.

Each test pins the *observable* behaviour; its docstring names the hazard
being guarded so the reason for the assertion survives codebase drift.
"""
from __future__ import annotations

import time
from datetime import timedelta

import jwt as _jwt
import pytest
from sqlalchemy import delete

from wsi_app import app_auth as _app_auth
from wsi_app.admin_helpers import utcnow
from wsi_app.auth import (
    JWT_ALG,
    JWT_SECRET,
    create_token,
    hash_password,
    resolve_staff_user,
)
from wsi_app.config import settings
from wsi_app.models import AuthSettings, AuthUser, User


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _set_mode(db_session, mode: str) -> None:
    """Force AuthSettings to a particular mode and refresh the snapshot.

    The DB-backed singleton mirrors what the dashboard would write; the
    in-memory snapshot is what the middleware actually reads.
    """
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.mode = mode
    db_session.commit()
    _app_auth.refresh(db_session)


# --------------------------------------------------------------------------
# basic-mode form login must produce a usable session cookie
# --------------------------------------------------------------------------
def test_basic_mode_form_login_cookie_is_honored(client, db_session):
    """Logging in via /api/auth/login under basic mode must let the cookie
    carry future requests; the middleware must honor it rather than ignore
    it and 401 every follow-up call."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        r = client.post(
            "/api/auth/login",
            data={"username": "alice", "password": "secret-pass"},
        )
        assert r.status_code == 200
        assert _app_auth.SESSION_COOKIE in r.cookies

        # Cookie is preserved by TestClient; the next request should succeed
        # without an Authorization: Basic header.
        r2 = client.get("/api/auth/state")
        assert r2.status_code == 200
        assert r2.json()["authenticated"] is True

        # And the gated surface admits the cookie-bearer directly.
        r3 = client.get("/api/slides")
        assert r3.status_code == 200
    finally:
        _set_mode(db_session, "none")


# --------------------------------------------------------------------------
# locked-out branch must also exercise the password verifier
# --------------------------------------------------------------------------
def test_locked_branch_runs_dummy_verify(monkeypatch, client, db_session):
    """The 429 response for a locked user must still exercise passlib, so it
    does not leak 'user exists' via response latency relative to the
    unknown-username path (which always pays the dummy_verify cost)."""
    user = User(
        username=f"locked-{int(time.time()*1000)}",
        password_hash=hash_password("anything"),
        role="admin",
        is_active=True,
        lockout_until=utcnow() + timedelta(minutes=5),
    )
    db_session.add(user)
    db_session.commit()

    calls = {"n": 0}
    import wsi_app.routers.admin_auth as _ar

    real_dummy = _ar.dummy_verify_password

    def _counting_dummy(pw):
        calls["n"] += 1
        return real_dummy(pw)

    monkeypatch.setattr(_ar, "dummy_verify_password", _counting_dummy)

    r = client.post(
        "/api/admin/login",
        json={"username": user.username, "password": "wrong"},
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# stale admin JWT must not bypass the app-auth gate
# --------------------------------------------------------------------------
def test_disabled_staff_token_does_not_bypass_app_auth(client, db_session):
    """Disabling a user, or bumping their token_version (password reset),
    must revoke their JWT's app-auth bypass immediately, not leave it
    effective for the rest of its 24h TTL.

    Note the per-user revocation lever is disable / password-reset, NOT role:
    every staff role (admin / manager / instructor) bypasses the app-access
    gate (see the middleware's staff_token_valid check and its dedicated
    coverage in test_edge_authz), so a demotion between staff roles does NOT
    deny — that case is asserted separately below."""
    # Issue a valid admin token first, *then* disable the account.
    user = User(
        username=f"bypass-{int(time.time()*1000)}",
        password_hash=hash_password("x"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_token(user)

    _set_mode(db_session, "ldap")  # any gated, non-basic mode
    try:
        # Sanity: while active, the bypass works.
        r_ok = client.get(
            "/api/slides", headers={"Authorization": f"Bearer {token}"}
        )
        assert r_ok.status_code == 200

        user.is_active = False
        db_session.commit()

        r = client.get(
            "/api/slides", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401

        # Re-enable and demote to another staff role: this now STILL bypasses,
        # because the gate admits every staff role, not admin only.
        user.is_active = True
        user.role = "instructor"
        db_session.commit()
        r2 = client.get(
            "/api/slides", headers={"Authorization": f"Bearer {token}"}
        )
        assert r2.status_code == 200

        # Bump token_version (what a password reset does): the old token's tv
        # claim no longer matches, so the bypass is denied immediately.
        user.token_version = int(user.token_version or 0) + 1
        db_session.commit()
        r3 = client.get(
            "/api/slides", headers={"Authorization": f"Bearer {token}"}
        )
        assert r3.status_code == 401
    finally:
        user.is_active = True
        user.role = "admin"
        db_session.commit()
        _set_mode(db_session, "none")


# --------------------------------------------------------------------------
# /api/admin/register stays reachable under a gated mode
# --------------------------------------------------------------------------
def test_admin_register_bypasses_app_auth_gate(client, db_session):
    """When restoring a DB whose AuthSettings.mode is already ldap/oauth
    but the users table is empty, the first-launch bootstrap call needs
    to slip through the app-auth gate.

    ALLOW_ADMIN_REGISTER is not flipped on (the settings dataclass is
    frozen): a handler-level 403 ('Disabled') still proves the middleware
    admitted the request, which is the only thing the bypass list controls.
    """
    _set_mode(db_session, "ldap")
    try:
        r = client.post(
            "/api/admin/register",
            json={"username": "first-admin", "password": "longenoughpw"},
        )
        # The app-auth middleware must not 401 this; it must reach the
        # handler, which (with ALLOW_ADMIN_REGISTER unset) responds 403
        # Disabled.
        assert r.status_code != 401, r.text
    finally:
        _set_mode(db_session, "none")


# --------------------------------------------------------------------------
# inactive user's failed_login_count must not change
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# oversized password / username inputs are rejected before pbkdf2
# --------------------------------------------------------------------------
def test_oversize_admin_login_password_rejected_with_422(client):
    """JSON admin login with a multi-KB password is rejected by the schema
    cap at the validation boundary, before it reaches passlib."""
    r = client.post(
        "/api/admin/login",
        json={"username": "anyone", "password": "x" * 2048},
    )
    assert r.status_code == 422


def test_oversize_basic_header_is_rejected(client, db_session):
    """parse_basic_header must refuse to base64-decode and pbkdf2-verify
    multi-MB Authorization values."""
    import base64 as _b64
    _set_mode(db_session, "basic")
    try:
        # >4096 bytes triggers the early reject.
        huge = "u:" + ("p" * 8000)
        encoded = _b64.b64encode(huge.encode()).decode()
        r = client.get("/api/slides", headers={"Authorization": f"Basic {encoded}"})
        assert r.status_code == 401  # rejected; no server-side hash cost
    finally:
        _set_mode(db_session, "none")


def test_oversize_auth_user_form_password_rejected(client, admin_headers):
    """admin_add_auth_user Form() caps password at 1024 chars."""
    r = client.post(
        "/api/admin/auth/users",
        data={"username": "u1", "password": "x" * 4096},
        headers=admin_headers,
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------
# bearer_token tolerates leading whitespace on the header value
# --------------------------------------------------------------------------
def test_bearer_token_strips_leading_whitespace():
    from wsi_app.auth import bearer_token
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("  Bearer abc") == "abc"
    assert bearer_token("\tBearer abc") == "abc"
    assert bearer_token("") is None
    assert bearer_token("Basic abc") is None


def test_inactive_user_failed_login_does_not_mutate_counter(client, db_session):
    """A login attempt against a disabled account must not increment
    failed_login_count or set lockout_until, which would leave stale
    rate-limit state behind after the account is re-enabled."""
    user = User(
        username=f"inactive-{int(time.time()*1000)}",
        password_hash=hash_password("real-pass"),
        role="admin",
        is_active=False,
        failed_login_count=0,
        lockout_until=None,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    for _ in range(settings.admin_login_max_failures + 2):
        r = client.post(
            "/api/admin/login",
            json={"username": user.username, "password": "any-wrong"},
        )
        assert r.status_code == 401

    db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.lockout_until is None


# --------------------------------------------------------------------------
# WSI_PUBLIC_BASE_URL must not repin the OAuth callback onto the public host
# for a login that started on a different (e.g. LAN-direct) host
# --------------------------------------------------------------------------
class _FakeUrl:
    def __init__(self, scheme: str, netloc: str):
        self.scheme = scheme
        self.netloc = netloc


class _FakeRequest:
    """Minimal stand-in for starlette Request for _oauth_redirect_uri.

    The helper only reads request.headers.get(...) and request.url.{scheme,
    netloc}; a dict of headers plus a tiny url object is enough.
    """

    def __init__(self, headers: dict, scheme: str = "https", netloc: str = ""):
        self.headers = headers
        self.url = _FakeUrl(scheme, netloc)


def _redirect_uri_for(monkeypatch, host: str, public_base_url: str) -> str:
    import dataclasses

    import wsi_app.routers.app_auth as _router

    patched = dataclasses.replace(_router.settings, public_base_url=public_base_url)
    monkeypatch.setattr(_router, "settings", patched)
    req = _FakeRequest(
        headers={"host": host, "x-forwarded-proto": "https"},
        netloc=host,
    )
    return _router._oauth_redirect_uri(req)


def test_oauth_redirect_uri_pins_only_on_the_pinned_host(monkeypatch):
    """Guards against an "OAuth state mismatch" when WSI_PUBLIC_BASE_URL is
    set on a deployment that is also reachable on a LAN-direct host.

    The OAuth state cookie is host-scoped: /oauth/start sets it on the host the
    browser is on and /oauth/callback reads it back on that same host. Pinning
    the redirect_uri unconditionally would force the callback onto the public
    host even for a login that started on the LAN host, so the state cookie
    would be absent at the callback and the login would fail.

    The redirect_uri must therefore only adopt the pinned public base URL when
    the request is actually ON the pinned host; on any other host it must
    follow the request host so both OAuth legs stay together.
    """
    public = "https://wsi.example.org"

    # On the pinned host: still uses the pinned value verbatim (the
    # anti-Host-spoof benefit is preserved here).
    on_public = _redirect_uri_for(monkeypatch, "wsi.example.org", public)
    assert on_public == "https://wsi.example.org/api/auth/oauth/callback"

    # On the LAN-direct host: derive from the current host so /oauth/start and
    # /oauth/callback run together and the state cookie survives.
    on_lan = _redirect_uri_for(monkeypatch, "lan.wsi.example.org", public)
    assert on_lan == "https://lan.wsi.example.org/api/auth/oauth/callback"


def test_oauth_redirect_uri_derives_when_unpinned(monkeypatch):
    """With WSI_PUBLIC_BASE_URL unset the callback is reconstructed from the
    inbound host — the default the LAN-direct guidance relies on."""
    derived = _redirect_uri_for(monkeypatch, "lan.wsi.example.org", "")
    assert derived == "https://lan.wsi.example.org/api/auth/oauth/callback"


# --------------------------------------------------------------------------
# app-access login is rate-limited (form + HTTP Basic share one IP bucket)
# --------------------------------------------------------------------------
import contextlib as _contextlib


@_contextlib.contextmanager
def _override_app_settings(**values):
    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


def test_app_form_login_is_ip_throttled(client, db_session):
    """POST /api/auth/login under basic mode bounds failed attempts per IP and
    returns 429 + Retry-After once the window fills, rather than allowing
    unbounded attempts that each pay a full pbkdf2 verify."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        with _override_app_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
            for _ in range(3):
                r = client.post(
                    "/api/auth/login",
                    data={"username": "alice", "password": "wrong"},
                )
                assert r.status_code == 401, r.text
            r = client.post(
                "/api/auth/login",
                data={"username": "alice", "password": "wrong"},
            )
            assert r.status_code == 429
            assert "Retry-After" in r.headers
    finally:
        _set_mode(db_session, "none")


def test_app_form_login_failure_is_audited_without_password(client, db_session):
    import json
    from wsi_app.models import AuditLog

    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="bob", password_hash=hash_password("correct-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        r = client.post("/api/auth/login", data={"username": "bob", "password": "wrong-guess-1"})
        assert r.status_code == 401
        row = db_session.query(AuditLog).filter(AuditLog.action == "app.login.failure").order_by(AuditLog.id.desc()).first()
        assert row is not None and row.target == "bob"
        details = json.loads(row.details)
        assert details["mode"] == "basic" and details["ip"]
        assert "wrong-guess-1" not in row.details
    finally:
        _set_mode(db_session, "none")


def test_app_form_login_failure_masks_a_name_that_is_no_account(client, db_session):
    """A "username" no local account has is often a password pasted into the
    wrong field: the history (and its CSV export) must not carry it."""
    from wsi_app.models import AuditLog

    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="carol", password_hash=hash_password("correct-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        typed = "Hunter2-my-real-password"
        r = client.post("/api/auth/login", data={"username": typed, "password": "x"})
        assert r.status_code == 401
        row = db_session.query(AuditLog).filter(AuditLog.action == "app.login.failure").order_by(AuditLog.id.desc()).first()
        assert row is not None
        assert typed not in (row.target or "") and typed not in (row.details or "")
        assert row.target.startswith("H") and f"({len(typed)} chars)" in row.target
    finally:
        _set_mode(db_session, "none")


def test_app_form_login_success_clears_throttle(client, db_session):
    """A correct app login forgives the IP's recent failures against THAT
    username (the typo-then-success UX), and only those."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="bob", password_hash=hash_password("correct-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        with _override_app_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
            for _ in range(2):
                assert client.post(
                    "/api/auth/login",
                    data={"username": "bob", "password": "nope"},
                ).status_code == 401
            assert client.post(
                "/api/auth/login",
                data={"username": "bob", "password": "correct-pass"},
            ).status_code == 200
            # Counter reset: three more failures allowed before a block.
            for _ in range(3):
                assert client.post(
                    "/api/auth/login",
                    data={"username": "bob", "password": "nope"},
                ).status_code == 401
    finally:
        _set_mode(db_session, "none")


def test_app_login_success_does_not_reset_failures_against_other_users(client, db_session):
    """An attacker holding one valid account must not reset the per-IP budget
    by alternating a success with guesses at other accounts."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="mallory", password_hash=hash_password("mine-pass")))
    db_session.add(AuthUser(username="victim", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        with _override_app_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
            statuses = []
            for i in range(3):
                statuses.append(client.post(
                    "/api/auth/login",
                    data={"username": "victim", "password": f"guess-{i}"},
                ).status_code)
                statuses.append(client.post(
                    "/api/auth/login",
                    data={"username": "mallory", "password": "mine-pass"},
                ).status_code)
            # Three victim failures filled the window despite the successes
            # in between, so the third success attempt was already refused.
            assert statuses == [401, 200, 401, 200, 401, 429]
            assert client.post(
                "/api/auth/login",
                data={"username": "victim", "password": "secret-pass"},
            ).status_code == 429
    finally:
        _set_mode(db_session, "none")


def test_app_basic_header_is_ip_throttled_sharing_form_bucket(client, db_session):
    """The middleware's HTTP Basic branch throttles on the SAME IP bucket as
    the form endpoint: failures on one count toward the other."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="carol", password_hash=hash_password("carol-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        with _override_app_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
            import base64 as _b64

            def headers(pw: str) -> dict:
                return {"Authorization": "Basic " + _b64.b64encode(f"carol:{pw}".encode()).decode()}

            # Two bad Basic headers (distinct guesses: an identical repeat
            # counts only once)...
            for i in range(2):
                assert client.get("/api/slides", headers=headers(f"wrong-{i}")).status_code == 401
            # ...plus one bad form login fills the shared 3-failure window.
            assert client.post(
                "/api/auth/login",
                data={"username": "carol", "password": "wrong"},
            ).status_code == 401
            # The next Basic-header request is throttled (429), proving the
            # form and Basic surfaces share one per-IP bucket.
            r = client.get("/api/slides", headers=headers("wrong-2"))
            assert r.status_code == 429
            assert "Retry-After" in r.headers
    finally:
        _set_mode(db_session, "none")


# --------------------------------------------------------------------------
# The `tv` (token_version) claim is mandatory on every staff JWT
# --------------------------------------------------------------------------
def _staff_token(claims: dict) -> str:
    """Mint a staff JWT with hand-written claims (no `aud`, so it reads as staff)."""
    now = int(time.time())
    return _jwt.encode(
        {"iat": now, "exp": now + 3600, **claims},
        JWT_SECRET,
        algorithm=JWT_ALG,
    )


@pytest.mark.parametrize(
    "tv_claim",
    [
        pytest.param(..., id="absent"),
        pytest.param(None, id="null"),
        pytest.param("0", id="string"),
        pytest.param([0], id="list"),
        pytest.param(True, id="bool"),
    ],
)
def test_staff_token_without_a_well_formed_tv_is_rejected(db_session, tv_claim):
    """A staff JWT whose ``tv`` is missing or malformed must not authenticate.

    Reading ``tv`` as ``int(payload.get("tv", 0) or 0)`` would default to 0 —
    and 0 is the *valid* starting token_version, so stripping or blanking the
    claim would produce a token that passed for any user who had never had
    their tokens revoked. It is required and type-checked, which
    also excludes ``bool`` (an ``int`` subclass, so ``True`` would read as
    version 1).
    """
    user = User(
        username=f"tvprobe-{tv_claim!r}",
        password_hash=hash_password("correct-horse"),
        role="instructor",
        is_active=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    try:
        claims = {"sub": str(user.id), "username": user.username, "role": user.role}
        if tv_claim is not ...:
            claims["tv"] = tv_claim
        assert resolve_staff_user(_staff_token(claims), db_session) is None

        # Control: the real minter stamps a well-formed tv and resolves fine.
        assert resolve_staff_user(create_token(user), db_session) is not None
    finally:
        db_session.delete(user)
        db_session.commit()


# --------------------------------------------------------------------------
# users.id reuse must not resurrect a deleted user's token
# --------------------------------------------------------------------------
def test_deleted_users_token_does_not_authenticate_id_reuse(client, db_session):
    """users.id has no AUTOINCREMENT, so SQLite reuses a deleted max-id row's
    id for the next insert. A token still held by the deleted user carries
    that ``sub``; with every account starting at ``token_version`` 0 its
    ``tv`` would also match, and it would authenticate as the new account
    (here an admin). New rows start at a random generation, so the old ``tv`` misses.
    """
    import os

    guest = User(
        username=f"idreuse-guest-{os.urandom(4).hex()}",
        password_hash=hash_password("x" * 16),
        role="instructor",
    )
    db_session.add(guest)
    db_session.commit()
    db_session.refresh(guest)
    guest_id = guest.id
    stale = create_token(guest)

    db_session.execute(delete(User).where(User.id == guest_id))
    db_session.commit()

    carol = User(
        username=f"idreuse-carol-{os.urandom(4).hex()}",
        password_hash=hash_password("x" * 16),
        role="admin",
    )
    db_session.add(carol)
    db_session.commit()
    db_session.refresh(carol)
    # The precondition this test exists for: SQLite handed out the same id.
    assert carol.id == guest_id

    r = client.get("/api/admin/me", headers={"Authorization": f"Bearer {stale}"})
    assert r.status_code == 401
    assert resolve_staff_user(stale, db_session) is None

    fresh = create_token(carol)
    r = client.get("/api/admin/me", headers={"Authorization": f"Bearer {fresh}"})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# A verified HTTP Basic request gets a session cookie, and only
# the verified username becomes an analytics identity
# --------------------------------------------------------------------------
def _basic(user: str, pw: str) -> dict:
    import base64 as _b64

    return {"Authorization": "Basic " + _b64.b64encode(f"{user}:{pw}".encode()).decode()}


def _last_pageview_identity() -> str:
    from wsi_app.db import SessionLocal
    from wsi_app.models import PageView

    db = SessionLocal()
    try:
        row = db.query(PageView).order_by(PageView.id.desc()).first()
        return row.user_email if row is not None else None
    finally:
        db.close()


def test_basic_header_success_mints_session_cookie(client, db_session, monkeypatch):
    """A browser answering the Basic dialog re-sends the header on every
    request; without a session cookie each one would pay a full pbkdf2 verify
    (and nginx bypasses the shared tile cache for it). A successful Basic
    check sets wsi_session, and the follow-up request rides the cookie without a
    second verify."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    calls = []
    real_verify = _app_auth._verified_basic_hash

    def counting_verify(u, p, db):
        calls.append(u)
        return real_verify(u, p, db)

    monkeypatch.setattr(_app_auth, "_verified_basic_hash", counting_verify)
    try:
        r = client.get("/api/slides", headers=_basic("alice", "secret-pass"))
        assert r.status_code == 200
        cookie = r.cookies.get(_app_auth.SESSION_COOKIE)
        assert cookie
        assert _app_auth.decode_session_token(cookie)["sub"] == "basic:alice"
        assert "httponly" in r.headers["set-cookie"].lower()
        assert calls == ["alice"]

        # TestClient keeps the cookie: same header again, no second verify.
        r2 = client.get("/api/slides", headers=_basic("alice", "secret-pass"))
        assert r2.status_code == 200
        assert calls == ["alice"]
        assert "set-cookie" not in r2.headers

        # A wrong password mints nothing.
        client.cookies.clear()
        r3 = client.get("/api/slides", headers=_basic("alice", "wrong"))
        assert r3.status_code == 401
        assert _app_auth.SESSION_COOKIE not in r3.cookies
    finally:
        _set_mode(db_session, "none")


def test_basic_beacon_attributes_verified_user(client, db_session):
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        r = client.post(
            "/api/analytics/pageview", json={"kind": "top"},
            headers=_basic("alice", "secret-pass"),
        )
        assert r.status_code == 204
        assert _last_pageview_identity() == "basic:alice"
    finally:
        _set_mode(db_session, "none")


def test_exempted_request_cannot_forge_basic_identity(monkeypatch):
    """A request admitted by the LAN-direct exemption never had its Basic
    password checked, so the header's username must not reach analytics —
    otherwise anyone on the LAN could write rows in any learner's name."""
    from fastapi.testclient import TestClient

    from wsi_app.main import app

    monkeypatch.setattr(
        _app_auth, "_snapshot",
        _app_auth._AuthSnapshot(mode="basic", lan_direct_noauth_mode="lan"),
    )
    lan_client = TestClient(app, client=("127.0.0.1", 12345))
    headers = {"host": "192.168.1.50", "x-real-ip": "192.168.1.42"}
    headers.update(_basic("victim", "not-checked"))
    r = lan_client.post("/api/analytics/pageview", json={"kind": "top"}, headers=headers)
    assert r.status_code == 204
    assert _last_pageview_identity() == ""


@pytest.mark.parametrize("action", ["delete", "password_reset"])
def test_basic_session_cookie_dies_with_its_credential(client, db_session, admin_headers, action):
    """A basic session cookie (form login or the one minted on a Basic-header
    success) must stop working once the account behind it is deleted or its
    password reset — not live on for the rest of the session TTL."""
    db_session.execute(delete(AuthUser))
    row = AuthUser(username="alice", password_hash=hash_password("secret-pass"))
    db_session.add(row)
    # A second user: the last one cannot be deleted while the mode is basic.
    db_session.add(AuthUser(username="bob", password_hash=hash_password("secret-pass")))
    db_session.commit()
    user_id = row.id
    _set_mode(db_session, "basic")
    try:
        r = client.get("/api/slides", headers=_basic("alice", "secret-pass"))
        assert r.status_code == 200
        cookie = r.cookies.get(_app_auth.SESSION_COOKIE)
        assert cookie
        client.cookies.clear()
        cookies = {_app_auth.SESSION_COOKIE: cookie}
        assert client.get("/api/slides", cookies=cookies).status_code == 200

        if action == "delete":
            resp = client.delete(f"/api/admin/auth/users/{user_id}", headers=admin_headers)
        else:
            resp = client.patch(
                f"/api/admin/auth/users/{user_id}",
                data={"password": "another-Secret-9"},
                headers=admin_headers,
            )
        assert resp.status_code == 200, resp.text
        assert client.get("/api/slides", cookies=cookies).status_code == 401
        assert _app_auth.decode_session_token(cookie) is None
    finally:
        _set_mode(db_session, "none")


def test_basic_session_cookie_without_credential_claim_is_rejected(db_session):
    """A basic: cookie that carries no credential fingerprint (minted without
    the binding, or forged without it) fails closed."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    assert _app_auth.decode_session_token(_app_auth.create_session_token("basic:alice")) is None
    assert _app_auth.decode_session_token(
        _app_auth.create_session_token("basic:alice", credential="0" * 16)
    ) is None


# --------------------------------------------------------------------------
# Staff bearers under the gated modes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["ldap", "google_oauth"])
def test_auth_state_counts_a_valid_staff_bearer(client, db_session, manager_user, mode):
    """Staff are never asked for a second, app-access login: the middleware
    admits any valid staff bearer, so /api/auth/state (which the SPA's gate
    asks before rendering a public page) must say so too. Otherwise a manager
    holding no app cookie — every dashboard user under ldap / google_oauth
    who never signed in as a viewer — is bounced to /login when opening a
    draft preview or the presenter view."""
    token = create_token(manager_user)
    _set_mode(db_session, mode)
    try:
        r = client.get("/api/auth/state", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["authenticated"] is True
        # ... but it is no app session that signing out could end.
        assert r.json()["app_session"] is False
        assert client.get("/api/auth/state").json()["authenticated"] is False

        # A revoked token no longer counts, matching the middleware.
        manager_user.token_version = int(manager_user.token_version or 0) + 1
        db_session.commit()
        r = client.get("/api/auth/state", headers={"Authorization": f"Bearer {token}"})
        assert r.json()["authenticated"] is False
    finally:
        _set_mode(db_session, "none")


@pytest.mark.parametrize("mode", ["ldap", "google_oauth"])
def test_auth_state_reports_the_app_session_apart_from_a_staff_bearer(
    client, db_session, manager_user, mode
):
    """The public pages offer "Sign out" on ``app_session``, not on
    ``authenticated``: a staff bearer counts as authenticated, but logging out
    cannot end it, so the button would only bounce off /login back to the page."""
    staff = {"Authorization": f"Bearer {create_token(manager_user)}"}
    sub = "ldap:alice" if mode == "ldap" else "alice@example.edu"
    _set_mode(db_session, mode)
    try:
        client.cookies.set(_app_auth.SESSION_COOKIE, _app_auth.create_session_token(sub))
        body = client.get("/api/auth/state", headers=staff).json()
        assert (body["authenticated"], body["app_session"]) == (True, True)

        assert client.post("/api/auth/logout").status_code in (200, 204)
        client.cookies.clear()
        body = client.get("/api/auth/state", headers=staff).json()
        assert (body["authenticated"], body["app_session"]) == (True, False)
        body = client.get("/api/auth/state").json()
        assert (body["authenticated"], body["app_session"]) == (False, False)
    finally:
        client.cookies.clear()
        _set_mode(db_session, "none")


def test_auth_state_reports_no_oauth_login_url(client, db_session):
    """The login page links /api/auth/oauth/start itself; the state endpoint
    carries no second copy of that URL."""
    _set_mode(db_session, "google_oauth")
    try:
        body = client.get("/api/auth/state").json()
        assert body["mode"] == "google_oauth"
        assert "oauth_login_url" not in body
    finally:
        _set_mode(db_session, "none")


@pytest.mark.parametrize("mode", ["none", "ldap", "google_oauth", "basic"])
def test_rejected_staff_bearer_on_admin_api_reads_invalid_token(
    client, db_session, other_admin, mode
):
    """A revoked / expired / malformed staff bearer on /api/admin/* must get
    the same 401 "Invalid token" in every mode, without a WWW-Authenticate
    challenge. The dashboard keys its return-to-login on that detail; were
    the app-access gate under the gated modes to answer first with its own
    "Authentication required" (plus, under basic, the browser's native
    credential dialog), an admin whose token died would be left on a page that
    failed every action."""
    revoked = create_token(other_admin)
    other_admin.token_version = int(other_admin.token_version or 0) + 1
    db_session.commit()
    _set_mode(db_session, mode)
    try:
        for header in (f"Bearer {revoked}", "Bearer not-a-jwt", "Bearer null"):
            r = client.get("/api/admin/users", headers={"Authorization": header})
            assert r.status_code == 401, (header, r.text)
            assert r.json()["detail"] == "Invalid token"
            assert "www-authenticate" not in r.headers

        if mode != "none":
            # Outside the admin API the gate's own answer is unchanged.
            r = client.get("/api/slides", headers={"Authorization": f"Bearer {revoked}"})
            assert r.status_code == 401
            assert r.json()["detail"] == "Authentication required"
            assert ("www-authenticate" in r.headers) == (mode == "basic")
    finally:
        _set_mode(db_session, "none")


@pytest.mark.parametrize("mode", ["none", "ldap", "google_oauth", "basic"])
def test_tokenless_admin_api_request_reads_missing_token(client, db_session, mode):
    """The dashboard sends no Authorization header before its staff login, so a
    bookmarked or newly opened /admin/... page's first API call carries none.
    It must get the "Missing token" mode=none gives, in every mode and without
    a WWW-Authenticate challenge, so the dashboard returns to its login screen
    — not the app-access gate's "Authentication required", which it shows as a
    raw error (and which, under basic, pops the browser's credential dialog)."""
    _set_mode(db_session, mode)
    try:
        r = client.get("/api/admin/tours")
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == "Missing token"
        assert "www-authenticate" not in r.headers
        # Form uploads too (the gate answers before the pre-auth middleware).
        r = client.post("/api/admin/slides/upload", files={"file": ("a.svs", b"x")})
        assert (r.status_code, r.json()["detail"]) == (401, "Missing token")
        assert "www-authenticate" not in r.headers
        # A wrong cached Basic credential is no staff token either.
        r = client.get("/api/admin/tours", headers=_basic("nobody", "wrong-pass"))
        assert (r.status_code, r.json()["detail"]) == (401, "Missing token")
        assert "www-authenticate" not in r.headers

        if mode != "none":
            # Outside the admin API the gate's own answer is unchanged.
            r = client.get("/api/slides")
            assert (r.status_code, r.json()["detail"]) == (401, "Authentication required")
            assert ("www-authenticate" in r.headers) == (mode == "basic")
    finally:
        _set_mode(db_session, "none")


@pytest.mark.parametrize("mode", ["none", "ldap", "google_oauth", "basic"])
def test_signed_download_link_needs_no_app_session(
    client, db_session, admin_user, attach_wsi, slide, mode
):
    """The dashboard starts a slide / backup / export download by navigating
    the browser to the ``…/download?t=`` URL /download_url minted, so the
    request carries no Authorization header — and under the gated modes a
    staff member who never signed in to the app itself has no app session
    either. A gate that refused it ("Authentication required") before the
    route could look at the token would fail every signed download for staff
    without an app cookie. A valid token is a staff credential of its own and
    must reach the route; a forged, stale or misplaced one must not."""
    from wsi_app.auth import (
        create_backup_download_token,
        create_export_download_token,
        create_slide_download_token,
    )

    attach_wsi(slide)
    slide_t = create_slide_download_token(slide.id, admin_user)
    # Valid tokens for ids with nothing behind them: the route's 404 shows the
    # gate let the request through to it.
    backup_t = create_backup_download_token("no-such-backup", admin_user)
    export_t = create_export_download_token("export-000000000000", admin_user)
    _set_mode(db_session, mode)
    try:
        client.cookies.clear()
        r = client.get(f"/api/admin/slides/{slide.id}/download?t={slide_t}")
        assert r.status_code == 200, r.text
        r = client.get(f"/api/admin/backup/no-such-backup/download?t={backup_t}")
        assert (r.status_code, r.json()["detail"]) == (404, "backup not found")
        r = client.get(f"/api/admin/slides/export/export-000000000000/download?t={export_t}")
        assert r.status_code == 404, r.text

        if mode == "none":
            # The route's own validation answers an id past int()'s digit limit.
            r = client.get(f"/api/admin/slides/{'9' * 4301}/download?t={slide_t}")
            assert r.status_code == 422, r.text
            return
        # Refused at the gate, with the admin API's answer (no app-access
        # challenge, which signing in to the app could not satisfy): a token
        # for another id, another purpose, a forged one, none at all, or a
        # valid one presented to a route that does not take it.
        for url in (
            f"/api/admin/slides/{slide.id + 1}/download?t={slide_t}",
            f"/api/admin/backup/no-such-backup/download?t={slide_t}",
            f"/api/admin/slides/{slide.id}/download?t=forged",
            f"/api/admin/slides/{slide.id}/download",
            f"/api/admin/slides?t={slide_t}",
        ):
            r = client.get(url)
            assert (r.status_code, r.json()["detail"]) == (401, "Missing token"), url
            assert "www-authenticate" not in r.headers
        r = client.post(f"/api/admin/slides/{slide.id}/download?t={slide_t}")
        assert r.status_code == 401

        # An id past int()'s digit limit is no slide, not a server error.
        r = client.get(f"/api/admin/slides/{'9' * 4301}/download?t={slide_t}")
        assert (r.status_code, r.json()["detail"]) == (401, "Missing token")

        # A password change (token_version bump) revokes the link at the gate.
        admin_user.token_version = int(admin_user.token_version or 0) + 1
        db_session.commit()
        r = client.get(f"/api/admin/slides/{slide.id}/download?t={slide_t}")
        assert (r.status_code, r.json()["detail"]) == (401, "Missing token")
    finally:
        _set_mode(db_session, "none")


# --------------------------------------------------------------------------
# A stale cached Basic credential must not lock out the whole IP
# --------------------------------------------------------------------------
def test_stale_basic_credential_does_not_lock_out_the_ip(
    client, db_session, admin_headers, monkeypatch
):
    """After a shared basic password is rotated, a tab still replaying the old
    one sends it with every request — each tile's /api/edge-authz subrequest
    included. Counting each as a failed login would fill the IP's budget
    within seconds and 429 everyone behind the same NAT address, the correct
    new password included, for as long as the tab stays open. A repeat of one
    exact credential counts once per window (and is refused from memory after
    the first pbkdf2 verify); distinct guesses still count."""
    db_session.execute(delete(AuthUser))
    row = AuthUser(username="student", password_hash=hash_password("old-password-1"))
    db_session.add(row)
    db_session.commit()
    user_id = row.id
    _set_mode(db_session, "basic")
    verifies = {"n": 0}
    real_verify = _app_auth._admin_auth.verify_password

    def counting_verify(pw, pw_hash):
        verifies["n"] += 1
        return real_verify(pw, pw_hash)

    try:
        with _override_app_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
            r = client.get("/api/slides", headers=_basic("student", "old-password-1"))
            assert r.status_code == 200
            stale_cookie = r.cookies.get(_app_auth.SESSION_COOKIE)
            assert stale_cookie
            r = client.patch(
                f"/api/admin/auth/users/{user_id}",
                data={"password": "new-password-1"},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            client.cookies.clear()

            monkeypatch.setattr(_app_auth._admin_auth, "verify_password", counting_verify)
            for i in range(20):
                r = client.get(
                    "/api/edge-authz",
                    headers={
                        **_basic("student", "old-password-1"),
                        "Cookie": f"{_app_auth.SESSION_COOKIE}={stale_cookie}",
                        "X-Original-URI": f"/thumbs/{i}.jpg",
                    },
                )
                assert r.status_code == 401
            assert verifies["n"] == 1

            # Someone else behind the same address signs in with the new one.
            r = client.get("/api/slides", headers=_basic("student", "new-password-1"))
            assert r.status_code == 200, r.headers.get("retry-after")
            client.cookies.clear()

            # Rotating back to the old password re-verifies at once: the
            # remembered rejection was tied to the hash it was checked against.
            r = client.patch(
                f"/api/admin/auth/users/{user_id}",
                data={"password": "old-password-1"},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            r = client.get("/api/slides", headers=_basic("student", "old-password-1"))
            assert r.status_code == 200
            client.cookies.clear()

            # A brute-forcer varies the password, and every guess counts.
            codes = [
                client.get("/api/slides", headers=_basic("student", f"guess-{i}")).status_code
                for i in range(4)
            ]
            assert codes == [401, 401, 401, 429]
    finally:
        _set_mode(db_session, "none")
