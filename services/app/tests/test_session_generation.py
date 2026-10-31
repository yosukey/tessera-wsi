"""App-access session bulk-revocation via the ``session_generation`` counter.

An app-access cookie carries a ``gen`` claim (the generation at mint time).
Tightening the auth policy (mode switch, allowed-domain change) or asking for
an explicit revoke bumps ``AuthSettings.session_generation``; every cookie
stamped with an older generation is then rejected on decode, so those actions
invalidate all outstanding sessions at once instead of waiting out each TTL.
"""
from __future__ import annotations

import json
import time

import jwt
import pytest
from sqlalchemy import select

from wsi_app import app_auth as _app_auth
from wsi_app.db import SessionLocal
from wsi_app.models import AuditLog, AuthSettings


def _set_generation(db_session, gen: int, *, mode: str = "none") -> None:
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.session_generation = gen
    row.mode = mode
    db_session.commit()
    _app_auth.refresh(db_session)


@pytest.fixture(autouse=True)
def _reset_auth_settings():
    """Restore the AuthSettings singleton + live snapshot after each test."""
    yield
    db = SessionLocal()
    try:
        row = db.get(AuthSettings, 1)
        if row is not None:
            row.session_generation = 0
            row.mode = "none"
            row.google_allowed_domains = ""
            row.google_allowed_emails = ""
            row.ldap_server_uri = ""
            row.ldap_user_base_dn = ""
            row.ldap_user_filter = "(uid={username})"
            db.commit()
        _app_auth.refresh(db)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Unit: mint / decode round-trip against the generation counter
# --------------------------------------------------------------------------
def test_cookie_valid_within_same_generation(db_session):
    _set_generation(db_session, 0)
    claims = _app_auth.decode_session_token(_app_auth.create_session_token("ldap:alice"))
    assert claims is not None
    assert claims["sub"] == "ldap:alice"
    assert claims["gen"] == 0


def test_generation_bump_invalidates_outstanding_cookie(db_session):
    _set_generation(db_session, 0)
    token = _app_auth.create_session_token("ldap:alice")
    assert _app_auth.decode_session_token(token) is not None

    # Policy tightened -> generation bumped. The already-issued cookie (gen=0)
    # must now be rejected, while a freshly minted one works.
    _set_generation(db_session, 1)
    assert _app_auth.decode_session_token(token) is None
    assert _app_auth.decode_session_token(
        _app_auth.create_session_token("ldap:alice")
    ) is not None


def test_cookie_without_gen_claim_is_rejected(db_session):
    """A cookie carrying no ``gen`` at all must fail closed.

    ``create_session_token`` always stamps the claim, so its absence means the
    cookie is malformed or forged. Reading it as generation 0 would be worse
    than useless here: 0 is the *valid* starting generation, so a stripped
    claim would grant the same standing as a freshly minted cookie until
    someone happened to bump the counter.
    """
    _set_generation(db_session, 0)
    now = int(time.time())
    no_gen = jwt.encode(
        {
            "sub": "basic:alice",
            "aud": _app_auth.SESSION_AUDIENCE,
            "iat": now,
            "exp": now + 3600,
        },
        _app_auth._admin_auth.JWT_SECRET,
        algorithm=_app_auth._admin_auth.JWT_ALG,
    )
    assert _app_auth.decode_session_token(no_gen) is None


def _token_with_gen(gen) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "basic:alice",
            "aud": _app_auth.SESSION_AUDIENCE,
            "iat": now,
            "exp": now + 3600,
            "gen": gen,
        },
        _app_auth._admin_auth.JWT_SECRET,
        algorithm=_app_auth._admin_auth.JWT_ALG,
    )


@pytest.mark.parametrize("gen", [None, "", [], {}])
def test_falsy_gen_claim_fails_closed(db_session, gen):
    """A present-but-falsy ``gen`` is rejected rather than read as 0.

    PyJWT's ``require`` only proves the key exists — a JSON ``null``, an empty
    string or an empty list all satisfy it — so the value is type-checked
    separately. An ``or 0`` coercion would map these to generation 0, handing
    a cookie with a blanked-out claim the standing of a valid one.
    """
    _set_generation(db_session, 0)
    assert _app_auth.decode_session_token(_token_with_gen(gen)) is None


@pytest.mark.parametrize("gen", ["x", "3.5", [1], {"a": 1}, True])
def test_non_numeric_gen_claim_fails_closed(db_session, gen):
    """A ``gen`` that is truthy but not an integer is rejected, not fatal.

    This gate is fail-closed: an unexpected claim shape must map to "not
    authenticated" rather than raise. The isinstance check does that without
    ever calling ``int()``, which would otherwise raise ValueError ("x",
    "3.5") or TypeError ([1], {"a": 1}) — and this runs outside the
    try/except around ``jwt.decode``, so it would propagate out of the auth
    middleware and 500 every gated request rather than refusing the cookie.
    """
    _set_generation(db_session, 0)
    assert _app_auth.decode_session_token(_token_with_gen(gen)) is None


# --------------------------------------------------------------------------
# Lowering the session TTL shortens cookies already issued
# --------------------------------------------------------------------------
def _cookie_issued(seconds_ago: int, *, lifetime: int, **extra) -> str:
    now = int(time.time())
    payload = {
        "sub": "ldap:alice",
        "aud": _app_auth.SESSION_AUDIENCE,
        "iat": now - seconds_ago,
        "exp": now - seconds_ago + lifetime,
        "gen": 0,
        **extra,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(
        payload, _app_auth._admin_auth.JWT_SECRET, algorithm=_app_auth._admin_auth.JWT_ALG,
    )


def test_lowered_ttl_ends_cookies_issued_under_the_old_one(monkeypatch):
    """A cookie's ``exp`` is fixed under the TTL at mint time, so judged by
    ``exp`` alone, cutting the TTL from 30 days to an hour (the documented
    lever for a tighter revocation window) would leave every outstanding
    cookie valid for the rest of its 30 days."""
    month = 30 * 24 * 3600
    two_hours_old = _cookie_issued(2 * 3600, lifetime=month)
    fresh = _cookie_issued(60, lifetime=month)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(session_ttl_seconds=month))
    assert _app_auth.decode_session_token(two_hours_old) is not None

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(session_ttl_seconds=3600))
    assert _app_auth.decode_session_token(two_hours_old) is None
    assert _app_auth.decode_session_token(fresh) is not None


def test_raised_ttl_does_not_outlive_the_cookie_exp(monkeypatch):
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(session_ttl_seconds=30 * 24 * 3600)
    )
    assert _app_auth.decode_session_token(_cookie_issued(2 * 3600, lifetime=3600)) is None


@pytest.mark.parametrize("iat", [None, True, "yesterday"])
def test_cookie_without_a_usable_iat_is_rejected(iat):
    """Its age is what the current TTL is measured against."""
    assert _app_auth.decode_session_token(
        _cookie_issued(0, lifetime=3600, iat=iat)
    ) is None


# --------------------------------------------------------------------------
# End-to-end: the auth-settings PATCH bumps the generation
# --------------------------------------------------------------------------
def test_mode_switch_bumps_generation(client, admin_headers, db_session):
    _set_generation(db_session, 0, mode="basic")
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "none"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 1


def test_allowed_domains_change_bumps_generation(client, admin_headers, db_session):
    _set_generation(db_session, 0, mode="google_oauth")
    db_session.get(AuthSettings, 1)
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_domains": "example.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 1


def test_allowed_emails_change_bumps_generation(client, admin_headers, db_session):
    _set_generation(db_session, 0, mode="google_oauth")
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_emails": "friend@gmail.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 1


@pytest.mark.parametrize(
    "field", ["ldap_server_uri", "ldap_user_base_dn", "ldap_user_filter"]
)
def test_ldap_scope_change_bumps_generation_in_ldap_mode(
    client, admin_headers, db_session, field
):
    """Repointing LDAP can drop previously-admitted users, like a narrowed
    Google domain list, so it must invalidate outstanding cookies."""
    _set_generation(db_session, 0, mode="ldap")
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={field: "changed-value"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 1


def test_ldap_scope_change_outside_ldap_mode_does_not_bump(
    client, admin_headers, db_session
):
    """Editing dormant LDAP config while another mode is active admits or
    drops no one, so it must not log everybody out."""
    _set_generation(db_session, 0, mode="basic")
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={"ldap_user_filter": "(uid={username})"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start


def test_explicit_revoke_flag_bumps_generation_without_policy_change(
    client, admin_headers, db_session
):
    _set_generation(db_session, 0, mode="basic")
    start = db_session.get(AuthSettings, 1).session_generation

    r = client.patch(
        "/api/admin/auth/settings",
        data={"revoke_app_sessions": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 1


def test_unrelated_patch_does_not_bump_generation(client, admin_headers, db_session):
    _set_generation(db_session, 0, mode="basic")
    start = db_session.get(AuthSettings, 1).session_generation

    # Touch only a login-screen override: no policy change, no revoke flag.
    r = client.patch(
        "/api/admin/auth/settings",
        data={"login_button_label": "Sign in"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start


def test_cleared_ldap_filter_round_trip_does_not_bump_or_audit(
    client, admin_headers, db_session
):
    """Clearing the LDAP user filter stores "" while GET reports the default
    filter it stands for, and the dashboard sends back what GET showed. That
    round trip is no scope change: it must neither log every app user out nor
    audit a filter change."""
    _set_generation(db_session, 0, mode="ldap")
    base = {"ldap_server_uri": "ldap://ldap.example", "ldap_user_base_dn": "ou=p,dc=x"}
    r = client.patch(
        "/api/admin/auth/settings",
        data={**base, "clear_ldap_user_filter": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    shown = r.json()["ldap_user_filter"]
    assert shown == "(uid={username})"
    db_session.expire_all()
    start = db_session.get(AuthSettings, 1).session_generation

    for message in ("hello", "hello again"):
        r = client.patch(
            "/api/admin/auth/settings",
            data={**base, "ldap_user_filter": shown, "login_message": message},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ldap_user_filter"] == shown
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start

    rows = db_session.execute(
        select(AuditLog)
        .where(AuditLog.action == "auth.settings.update")
        .order_by(AuditLog.id.desc())
        .limit(2)
    ).scalars().all()
    assert len(rows) == 2
    for row in rows:
        details = json.loads(row.details)
        assert "ldap_user_filter" not in details["changes"]
        assert details["sessions_revoked"] is False


def test_overlapping_saves_each_advance_the_generation(
    client, admin_headers, db_session, monkeypatch
):
    """Two saves that both read generation n must not both store n+1: a
    cookie minted at n+1 between their commits (under the policy the first
    save left) would then survive the second save's tightening."""
    from wsi_app.routers import admin_settings

    _set_generation(db_session, 0, mode="google_oauth")
    start = db_session.get(AuthSettings, 1).session_generation
    real_snapshot = admin_settings._auth_audit_snapshot
    raced = []

    def snapshot_then_other_save_commits(s):
        out = real_snapshot(s)
        if not raced:
            # This request has read the row; another worker's save now lands.
            raced.append(True)
            other = SessionLocal()
            try:
                other.get(AuthSettings, 1).session_generation = start + 1
                other.commit()
            finally:
                other.close()
        return out

    monkeypatch.setattr(admin_settings, "_auth_audit_snapshot", snapshot_then_other_save_commits)
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_domains": "example.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert raced
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == start + 2
