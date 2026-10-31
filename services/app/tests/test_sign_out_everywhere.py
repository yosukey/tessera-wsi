"""POST /api/admin/me/sign-out-everywhere: the server-side sign-out.

Staff tokens are stateless, so the dashboard's "Log out" only forgets the token
in one browser. This ends every session of the account at once by bumping its
token_version - the generation a password change, suspension or two-factor
change already bumps.
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient
from sqlalchemy import select

from wsi_app.auth import create_token, hash_password, known_device_cookie_name
from wsi_app.main import app
from wsi_app.models import AuditLog, User


def _user(db_session, role="manager"):
    u = User(
        username=f"soe-{os.urandom(4).hex()}",
        password_hash=hash_password("owner-password-1"),
        role=role,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_requires_a_signed_in_account(client):
    assert client.post("/api/admin/me/sign-out-everywhere").status_code == 401


def test_every_token_of_the_account_stops_working_including_the_callers(client, db_session):
    user = _user(db_session)
    here, elsewhere = create_token(user), create_token(user)
    assert client.get("/api/admin/me", headers=_h(elsewhere)).status_code == 200

    r = client.post("/api/admin/me/sign-out-everywhere", headers=_h(here))
    assert r.status_code == 200 and r.json() == {"ok": True}

    assert client.get("/api/admin/me", headers=_h(here)).status_code == 401
    assert client.get("/api/admin/me", headers=_h(elsewhere)).status_code == 401
    # Signing in again works, with a token of the new generation.
    r = client.post("/api/admin/login", json={"username": user.username, "password": "owner-password-1"})
    assert r.status_code == 200
    assert client.get("/api/admin/me", headers=_h(r.json()["access_token"])).status_code == 200


def test_other_accounts_are_untouched(client, db_session):
    user, other = _user(db_session), _user(db_session, role="admin")
    other_token = create_token(other)
    client.post("/api/admin/me/sign-out-everywhere", headers=_h(create_token(user)))
    assert client.get("/api/admin/me", headers=_h(other_token)).status_code == 200


def test_it_is_audited(client, db_session):
    user = _user(db_session)
    client.post("/api/admin/me/sign-out-everywhere", headers=_h(create_token(user)))
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.account.sign_out_everywhere", AuditLog.actor_id == user.id)
    ).scalars().first()
    assert row is not None and row.target == str(user.id)


def test_it_also_ends_the_trust_of_known_browsers(client, db_session):
    """The known-device cookie is tied to the same generation, so after a
    sign-out everywhere a browser no longer gets past a standing lockout."""
    from wsi_app.config import settings

    user = _user(db_session)
    owner = TestClient(app)
    r = owner.post("/api/admin/login", json={"username": user.username, "password": "owner-password-1"})
    assert r.status_code == 200 and known_device_cookie_name(user.id) in owner.cookies
    owner.post("/api/admin/me/sign-out-everywhere", headers=_h(r.json()["access_token"]))

    stranger = TestClient(app)
    for _ in range(settings.admin_login_max_failures):
        stranger.post("/api/admin/login", json={"username": user.username, "password": "nope-nope-1"})
    r = owner.post("/api/admin/login", json={"username": user.username, "password": "owner-password-1"})
    assert r.status_code == 429


def test_password_change_token_does_not_outlive_a_sign_out_committed_meanwhile(
    client, db_session, monkeypatch,
):
    """The token a password change hands back carries the generation that
    change committed, not whatever the row holds by the time it is minted: a
    sign-out-everywhere landing in between must end it too."""
    from sqlalchemy import event, update

    from wsi_app.db import SessionLocal

    user = _user(db_session)
    changed_tv = int(user.token_version or 0) + 1
    raced = []

    def sign_out_elsewhere_once_the_change_commits(session):
        # after_commit: the request's row is expired but not yet read back.
        if raced:
            return
        other = SessionLocal()
        try:
            if other.get(User, user.id).token_version != changed_tv:
                return
            raced.append(True)
            other.execute(
                update(User).where(User.id == user.id)
                .values(token_version=User.token_version + 1)
            )
            other.commit()
        finally:
            other.close()

    event.listen(SessionLocal, "after_commit", sign_out_elsewhere_once_the_change_commits)
    try:
        r = client.patch(
            "/api/admin/me",
            json={"current_password": "owner-password-1", "new_password": "fresh-Password-2x"},
            headers=_h(create_token(user)),
        )
    finally:
        event.remove(SessionLocal, "after_commit", sign_out_elsewhere_once_the_change_commits)
    assert r.status_code == 200, r.text
    assert raced
    assert client.get("/api/admin/me", headers=_h(r.json()["access_token"])).status_code == 401
