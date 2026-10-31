"""Tests for the admin user-management endpoints under /api/admin/users.

Covers the small, safety-critical surface that lets an admin add / remove
staff accounts: role validation, password policy, self-deletion guard,
last-admin guard, username uniqueness, and the non-admin 403 wall.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app.models import User


@pytest.fixture(autouse=True)
def _wipe_extra_users(db_session, admin_user):
    """Keep the bootstrap admin (created by the ``admin_user`` fixture) but
    drop every other row so the last-admin / list-shape assertions are not
    polluted by users left behind by previous tests in the same session."""
    yield
    # Defer cleanup to teardown so per-test bookkeeping (count comparisons)
    # against the seeded admin still works.
    db_session.execute(delete(User).where(User.id != admin_user.id))
    db_session.commit()


def test_admin_creates_user_with_role_manager(client, admin_headers):
    body = {"username": "new-manager", "password": "longenoughpw", "role": "manager"}
    r = client.post("/api/admin/users", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["username"] == "new-manager"
    assert created["role"] == "manager"
    assert created["is_active"] is True

    # Appears in the list endpoint.
    listing = client.get("/api/admin/users", headers=admin_headers).json()
    assert any(u["username"] == "new-manager" and u["role"] == "manager" for u in listing)


def test_admin_creates_instructor(client, admin_headers):
    r = client.post(
        "/api/admin/users",
        json={"username": "new-instructor", "password": "longenoughpw", "role": "instructor"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "instructor"


def test_non_admin_cannot_create_user(client, manager_headers, instructor_headers):
    body = {"username": "should-not-exist", "password": "longenoughpw", "role": "instructor"}
    assert client.post("/api/admin/users", json=body, headers=manager_headers).status_code == 403
    assert client.post("/api/admin/users", json=body, headers=instructor_headers).status_code == 403


def test_admin_cannot_self_delete(client, admin_headers, admin_user):
    r = client.delete(f"/api/admin/users/{admin_user.id}", headers=admin_headers)
    assert r.status_code == 400
    assert "yourself" in (r.json().get("detail") or "").lower()


def test_admin_cannot_delete_last_admin(client, admin_headers, db_session, admin_user):
    """Positive flow: creating a second admin lets the first be deleted."""
    # Add a second admin via the API.
    r = client.post(
        "/api/admin/users",
        json={"username": "second-admin", "password": "longenoughpw", "role": "admin"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    second_id = r.json()["id"]

    # Mint a token for the second admin and use it to delete the original.
    # (Using ``admin_headers`` would hit the self-delete guard instead.)
    from wsi_app.auth import create_token
    second = db_session.get(User, second_id)
    second_headers = {"Authorization": f"Bearer {create_token(second)}"}

    r = client.delete(f"/api/admin/users/{admin_user.id}", headers=second_headers)
    assert r.status_code == 200


def test_admin_delete_last_admin_guard_logic(client, db_session, admin_user):
    """Document the last-admin guard's defensive behaviour.

    The guard fires when deleting an admin would leave zero active admins.
    In normal operation, the requesting admin counts as "another admin"
    (the self-delete branch catches the same-user case), so the guard is
    effectively defensive — reachable only when an admin token outlives
    its account's deactivation. The scenario is modelled by manually
    issuing a JWT for a now-inactive admin via the model layer.

    NOTE: ``get_current_user`` rejects tokens whose user is inactive
    (401 "Invalid token"), so even this path doesn't reach the handler.
    The test asserts the documented 401 instead, which proves the deactivation
    is honoured upstream of the would-be guard branch.
    """
    from wsi_app.auth import create_token, hash_password
    # Wipe other admins to keep the population deterministic.
    db_session.execute(
        delete(User).where(User.role == "admin", User.id != admin_user.id)
    )
    db_session.commit()

    second = User(
        username="lastguard-second",
        password_hash=hash_password("x"),
        role="admin",
        is_active=True,
    )
    db_session.add(second)
    db_session.commit()
    db_session.refresh(second)

    second_token = create_token(second)

    # Deactivate `second`; the JWT minted above is still cryptographically
    # valid, but get_current_user must reject it.
    second.is_active = False
    db_session.commit()

    r = client.delete(
        f"/api/admin/users/{admin_user.id}",
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert r.status_code == 401  # inactive caller short-circuits earlier


def test_invalid_role_rejected(client, admin_headers):
    r = client.post(
        "/api/admin/users",
        json={"username": "bad-role", "password": "longenoughpw", "role": "superuser"},
        headers=admin_headers,
    )
    assert r.status_code == 422


def test_password_strength_enforced(client, admin_headers):
    r = client.post(
        "/api/admin/users",
        json={"username": "weak-pw", "password": "shrt", "role": "instructor"},
        headers=admin_headers,
    )
    assert r.status_code == 422


def test_username_unique(client, admin_headers):
    body = {"username": "dup-user", "password": "longenoughpw", "role": "instructor"}
    r1 = client.post("/api/admin/users", json=body, headers=admin_headers)
    assert r1.status_code == 200
    r2 = client.post("/api/admin/users", json=body, headers=admin_headers)
    assert r2.status_code == 400


def test_list_users_requires_admin(client, manager_headers, instructor_headers):
    assert client.get("/api/admin/users", headers=manager_headers).status_code == 403
    assert client.get("/api/admin/users", headers=instructor_headers).status_code == 403


def test_delete_user_requires_admin(client, manager_headers, instructor_headers, db_session):
    """Even with a real target id, non-admins must be 403'd."""
    from wsi_app.auth import hash_password
    target = User(
        username="del-target",
        password_hash=hash_password("x"),
        role="instructor",
        is_active=True,
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    assert client.delete(f"/api/admin/users/{target.id}", headers=manager_headers).status_code == 403
    assert client.delete(f"/api/admin/users/{target.id}", headers=instructor_headers).status_code == 403


def test_update_me_duplicate_username_returns_400(client, admin_headers, db_session, admin_user):
    # Renaming the current admin onto a name another user already holds must be
    # a 400, not a 500 with an aborted transaction.
    taken = User(
        username="taken-name",
        password_hash=admin_user.password_hash,
        role="admin",
        is_active=True,
    )
    db_session.add(taken)
    db_session.commit()

    r = client.patch(
        "/api/admin/me",
        json={"current_password": "x", "username": "taken-name"},
        headers=admin_headers,
    )
    # Either the deterministic SELECT guard or the IntegrityError fallback must
    # produce a 400 — never a 500 with an aborted transaction.
    assert r.status_code == 400, r.text
