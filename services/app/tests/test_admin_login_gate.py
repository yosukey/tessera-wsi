"""The dashboard sign-in sits inside the app-access gate.

With an app-access mode on (basic / ldap / google_oauth), a visitor has to be
admitted by it (an app session, Basic credentials, Cloudflare Access or an
exemption) before ``/api/admin/login`` or its two-factor code step will even
look at a staff password. A staff token already issued keeps bypassing the
gate, so an admin who saved a broken configuration can still fix it.
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import delete

from wsi_app import app_auth as _app_auth
from wsi_app.auth import create_token, hash_password
from wsi_app.models import AuthSettings, AuthUser, User

PW = "staff-gate-password"


def _set_mode(db_session, mode: str) -> None:
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.mode = mode
    db_session.commit()
    _app_auth.refresh(db_session)


@pytest.fixture
def staff(db_session):
    u = User(username=f"gate-{os.urandom(4).hex()}", password_hash=hash_password(PW), role="admin", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def gated(db_session):
    """Run the test body under the given app-access mode, then back to none."""
    def _enter(mode):
        _set_mode(db_session, mode)
    yield _enter
    _set_mode(db_session, "none")


def _login(client, user, **kw):
    return client.post("/api/admin/login", json={"username": user.username, "password": PW}, **kw)


@pytest.mark.parametrize("mode", ["ldap", "google_oauth"])
def test_cookie_modes_require_app_sign_in_first(client, staff, gated, mode):
    gated(mode)
    r = _login(client, staff)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "admin.err.appAuthRequired"
    r = client.post("/api/admin/login/mfa", json={"mfa_token": "x", "code": "123456"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "admin.err.appAuthRequired"

    client.cookies.set(_app_auth.SESSION_COOKIE, _app_auth.create_session_token(f"{mode}:alice"))
    r = _login(client, staff)
    assert r.status_code == 200 and "access_token" in r.json()


def test_refused_attempt_charges_nothing(client, db_session, staff, gated):
    """A request the gate refuses never reaches the password check, so it
    cannot lock the account out either."""
    gated("google_oauth")
    for _ in range(8):
        assert client.post(
            "/api/admin/login", json={"username": staff.username, "password": "wrong"},
        ).status_code == 401
    db_session.expire_all()
    row = db_session.get(User, staff.id)
    assert row.failed_login_count == 0 and row.lockout_until is None


def test_basic_mode_needs_basic_credentials(client, db_session, staff, gated):
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="visitor", password_hash=hash_password("visitor-pass")))
    db_session.commit()
    try:
        gated("basic")
        r = _login(client, staff)
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "admin.err.appAuthRequired"
        basic = "Basic " + base64.b64encode(b"visitor:visitor-pass").decode()
        r = _login(client, staff, headers={"Authorization": basic})
        assert r.status_code == 200 and "access_token" in r.json()
    finally:
        db_session.execute(delete(AuthUser))
        db_session.commit()


def test_staff_token_still_passes_the_gate(client, staff, gated):
    gated("google_oauth")
    r = client.get("/api/admin/me", headers={"Authorization": f"Bearer {create_token(staff)}"})
    assert r.status_code == 200


def test_mode_none_is_unchanged(client, staff):
    r = _login(client, staff)
    assert r.status_code == 200 and "access_token" in r.json()


def test_login_paths_are_not_bypass_paths():
    for path in _app_auth.ADMIN_LOGIN_PATHS:
        assert not _app_auth.is_bypass_path(path)
    # The app's own sign-in, and the health check, stay reachable.
    assert _app_auth.is_bypass_path("/api/auth/login")
    assert _app_auth.is_bypass_path("/api/health")
