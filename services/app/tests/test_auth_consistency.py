"""Credential checks and the state they are stamped with must agree.

Each test interleaves a concurrent change between "the credential was
verified" and "the result was recorded" — the window in which a result could
otherwise be recorded against state it was never checked against.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from wsi_app import app_auth as _app_auth
from wsi_app.auth import create_token, hash_password, verify_password
from wsi_app.db import SessionLocal
from wsi_app.models import AuthSettings, AuthUser, User, auth_username_key
from wsi_app.routers import admin_auth as _admin_auth_router


@pytest.fixture(autouse=True)
def _reset_auth_settings():
    yield
    db = SessionLocal()
    try:
        row = db.get(AuthSettings, 1)
        if row is not None:
            row.session_generation = 0
            row.mode = "none"
            db.commit()
        _app_auth.refresh(db)
        db.execute(delete(AuthUser))
        db.commit()
    finally:
        db.close()


def _set_mode(db_session, mode: str, *, generation: int = 0) -> None:
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.mode = mode
    row.session_generation = generation
    db_session.commit()
    _app_auth.refresh(db_session)


# --------------------------------------------------------------------------
# Staff password change: compare-and-swap on the re-auth generation
# --------------------------------------------------------------------------
def test_overlapping_password_changes_cannot_share_a_generation(client, admin_user, monkeypatch):
    """Two changes that both read token_version n must not both store n+1:
    the token minted by the first would stay valid after the second moved the
    password on — and the second would have been authorised by a hash that
    was already superseded. The loser gets 409 and changes nothing."""
    headers = {"Authorization": f"Bearer {create_token(admin_user)}"}
    real_verify = _admin_auth_router.verify_password
    state = {"done": False}

    def verify_then_race(password, hashed):
        ok = real_verify(password, hashed)
        if not state["done"]:
            state["done"] = True
            # Request A commits its change while this request (B) sits
            # between its re-auth and its write.
            db = SessionLocal()
            try:
                u = db.get(User, admin_user.id)
                u.password_hash = hash_password("first-new-pass")
                u.token_version = int(u.token_version or 0) + 1
                db.commit()
            finally:
                db.close()
        return ok

    monkeypatch.setattr(_admin_auth_router, "verify_password", verify_then_race)
    r = client.patch(
        "/api/admin/me",
        json={"current_password": "x", "new_password": "second-new-pass"},
        headers=headers,
    )
    assert r.status_code == 409, r.text

    db = SessionLocal()
    try:
        u = db.get(User, admin_user.id)
        # A's change stands; B's was not applied on top of it.
        assert verify_password("first-new-pass", u.password_hash)
        assert not verify_password("second-new-pass", u.password_hash)
    finally:
        db.close()


def test_password_change_bumps_generation_and_reissues_token(client, admin_user):
    headers = {"Authorization": f"Bearer {create_token(admin_user)}"}
    before = int(admin_user.token_version or 0)
    r = client.patch(
        "/api/admin/me",
        json={"current_password": "x", "new_password": "a-new-password"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.get(User, admin_user.id).token_version == before + 1
    finally:
        db.close()
    # Old token revoked, new one accepted.
    assert client.get("/api/admin/me", headers=headers).status_code == 401
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/admin/me", headers=fresh).status_code == 200


# --------------------------------------------------------------------------
# Basic login: fingerprint from the hash the password was checked against
# --------------------------------------------------------------------------
def test_basic_login_racing_a_reset_gets_the_old_hash_fingerprint(client, db_session, monkeypatch):
    """Were the fingerprint re-read after the password check, a reset
    committing in between would stamp an OLD-password login with the NEW
    hash's fingerprint — a cookie the reset could no longer revoke."""
    db_session.add(AuthUser(username="alice", password_hash=hash_password("old-password")))
    db_session.commit()
    _set_mode(db_session, "basic")

    real_verify = _app_auth._admin_auth.verify_password
    state = {"done": False}

    def verify_then_reset(password, hashed):
        ok = real_verify(password, hashed)
        if not state["done"]:
            state["done"] = True
            db = SessionLocal()
            try:
                row = db.execute(select(AuthUser).where(AuthUser.username == "alice")).scalar_one()
                row.password_hash = hash_password("reset-password")
                db.commit()
            finally:
                db.close()
        return ok

    monkeypatch.setattr(_app_auth._admin_auth, "verify_password", verify_then_reset)
    r = client.post("/api/auth/login", data={"username": "alice", "password": "old-password"})
    monkeypatch.setattr(_app_auth._admin_auth, "verify_password", real_verify)
    assert r.status_code == 200, r.text
    cookie = r.cookies.get(_app_auth.SESSION_COOKIE)
    assert cookie
    # The reset is what this cookie must not survive.
    _app_auth._basic_cred_cache.clear()
    assert _app_auth.decode_session_token(cookie) is None


# --------------------------------------------------------------------------
# LDAP / app-session generation: stamp the policy the login was checked under
# --------------------------------------------------------------------------
def test_session_token_keeps_the_generation_it_was_verified_under(db_session):
    _set_mode(db_session, "ldap", generation=0)
    stale = _app_auth.create_session_token("ldap:alice", generation=0)
    _set_mode(db_session, "ldap", generation=1)
    assert _app_auth.decode_session_token(stale) is None
    assert _app_auth.decode_session_token(_app_auth.create_session_token("ldap:alice")) is not None


def test_ldap_login_racing_a_policy_tightening_is_refused(client, db_session, monkeypatch):
    """A login verified under the old LDAP filter must not be minted with the
    generation the tightening has just bumped, or the revocation check would
    accept a result the new policy refuses."""
    _set_mode(db_session, "ldap", generation=0)
    seen_cfg = []

    def verify_while_policy_changes(username, password, cfg=None):
        seen_cfg.append(cfg)
        db = SessionLocal()
        try:
            _set_mode(db, "ldap", generation=1)
        finally:
            db.close()
        return True

    monkeypatch.setattr(_app_auth, "verify_ldap", verify_while_policy_changes)
    r = client.post("/api/auth/login", data={"username": "alice", "password": "pw"})
    assert r.status_code == 409, r.text
    assert _app_auth.SESSION_COOKIE not in r.cookies
    # The credential was checked against the snapshot the login started with.
    assert seen_cfg and seen_cfg[0] is not None


def test_ldap_login_without_a_policy_change_succeeds(client, db_session, monkeypatch):
    _set_mode(db_session, "ldap", generation=3)
    monkeypatch.setattr(_app_auth, "verify_ldap", lambda u, p, cfg=None: True)
    r = client.post("/api/auth/login", data={"username": "alice", "password": "pw"})
    assert r.status_code == 200, r.text
    claims = _app_auth.decode_session_token(r.cookies.get(_app_auth.SESSION_COOKIE))
    assert claims and claims["gen"] == 3


# --------------------------------------------------------------------------
# Basic usernames: one case-insensitive identity, Unicode included
# --------------------------------------------------------------------------
def test_unicode_case_variant_auth_user_is_refused(client, admin_headers):
    """SQLite's lower() folds only ASCII, so relying on it would let "älice"
    register next to "Älice" — and both would hash to one pseudonymous
    analytics identity."""
    r = client.post("/api/admin/auth/users", data={"username": "Älice", "password": "first-pass-1"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/admin/auth/users", data={"username": "älice", "password": "second-pass-2"}, headers=admin_headers)
    assert r.status_code == 409, r.text
    r = client.post("/api/admin/auth/users", data={"username": "ALICE", "password": "third-pass-3"}, headers=admin_headers)
    assert r.status_code == 200, r.text  # a different name, not a case variant


def test_auth_username_key_matches_the_analytics_normalisation():
    from wsi_app import analytics_hash

    for a, b in [("Älice", "älice"), ("Alice", "alice"), ("ÉMILE", "émile")]:
        assert auth_username_key(a) == auth_username_key(b)
        assert analytics_hash.compute_user_hash(f"basic:{a}") == analytics_hash.compute_user_hash(f"basic:{b}")


def test_staff_unicode_case_variant_is_refused(client, admin_headers):
    r = client.post("/api/admin/users", json={"username": "Ödön", "password": "a-strong-pass", "role": "instructor"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/admin/users", json={"username": "ödön", "password": "a-strong-pass", "role": "instructor"}, headers=admin_headers)
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------
# Basic-header login vs. a concurrent mode switch; the edge probe's throttle
# --------------------------------------------------------------------------
def test_basic_header_login_racing_a_mode_switch_mints_a_stale_cookie(
    client, db_session, monkeypatch
):
    """The middleware verifies a Basic header under the mode it read at the
    top of the request. A basic->ldap switch (which bumps the generation to
    revoke every basic cookie) landing mid-request must not yield a cookie
    stamped with the NEW generation — one the ldap gate would then accept."""
    import base64

    db_session.add(AuthUser(username="alice", password_hash=hash_password("pw-alice-1")))
    db_session.commit()
    _set_mode(db_session, "basic", generation=0)

    real = _app_auth.verify_basic_credential
    fired = []

    def verify_while_the_mode_switches(username, password, db):
        if not fired:
            fired.append(1)
            other = SessionLocal()
            try:
                _set_mode(other, "ldap", generation=1)
            finally:
                other.close()
        return real(username, password, db)

    monkeypatch.setattr(_app_auth, "verify_basic_credential", verify_while_the_mode_switches)
    hdr = "Basic " + base64.b64encode(b"alice:pw-alice-1").decode()
    r = client.get("/api/slides", headers={"Authorization": hdr})
    assert fired and _app_auth.current_mode() == "ldap"
    cookie = r.cookies.get(_app_auth.SESSION_COOKIE)
    if cookie:
        assert _app_auth.decode_session_token(cookie) is None
        client.cookies.clear()
        assert client.get("/api/slides", cookies={_app_auth.SESSION_COOKIE: cookie}).status_code == 401


def test_a_basic_cookie_is_refused_outside_basic_mode(db_session):
    """Backstop: even a basic: cookie carrying the current generation means
    nothing once the mode is no longer basic."""
    db_session.add(AuthUser(username="bob", password_hash=hash_password("pw-bob-123")))
    db_session.commit()
    stored = db_session.execute(
        select(AuthUser.password_hash).where(AuthUser.username == "bob")
    ).scalar_one()
    _set_mode(db_session, "basic", generation=3)
    token = _app_auth.create_session_token(
        "basic:bob",
        credential=_app_auth.basic_credential_fingerprint(stored),
        generation=3,
    )
    assert _app_auth.decode_session_token(token) is not None
    _set_mode(db_session, "ldap", generation=3)
    assert _app_auth.decode_session_token(token) is None


def test_edge_authz_answers_401_not_429_when_the_basic_throttle_trips(
    client, db_session, monkeypatch
):
    """nginx's auth_request turns anything but 2xx / 401 / 403 into a 500 for
    the gated tile, thumbnail or asset."""
    import base64

    from wsi_app import login_throttle

    _set_mode(db_session, "basic", generation=0)
    monkeypatch.setattr(login_throttle, "retry_after_seconds", lambda *a, **k: 30)
    hdr = "Basic " + base64.b64encode(b"alice:wrong").decode()
    r = client.get(
        "/api/edge-authz",
        headers={"Authorization": hdr, "X-Original-URI": "/thumbs/x.jpg"},
    )
    assert r.status_code == 401
    # The interactive surfaces keep their 429 + Retry-After.
    assert client.get("/api/slides", headers={"Authorization": hdr}).status_code == 429
