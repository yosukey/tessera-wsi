"""Tests for the RolePermissions singleton and its gating on analytics /
server endpoints.

The RolePermissions row drives ``require_analytics_access`` and
``require_server_access``: admins always pass, managers and instructors
pass only when the corresponding ``{role}_can_view_{analytics|server}``
flag is True. Default is False for every flag (least-privilege).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app.models import RolePermissions


@pytest.fixture(autouse=True)
def _reset_role_permissions(db_session):
    """RolePermissions is a singleton (id=1). Reset to the all-False default
    before each test so flag state set by a previous test cannot leak.
    """
    db_session.execute(delete(RolePermissions))
    db_session.commit()
    yield
    db_session.execute(delete(RolePermissions))
    db_session.commit()


# ---------------------------------------------------------------------------
# Singleton CRUD
# ---------------------------------------------------------------------------
def test_role_permissions_default_off(client, admin_headers):
    """GET on a fresh table lazily creates the singleton with every flag
    set to False (least-privilege default)."""
    r = client.get("/api/admin/role-permissions", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    for flag in (
        "manager_can_view_analytics",
        "manager_can_view_server",
        "instructor_can_view_analytics",
        "instructor_can_view_server",
    ):
        assert flag in body
        assert body[flag] is False


def test_admin_can_toggle_role_permissions(client, admin_headers):
    """Partial PATCH flips only the named flag; subsequent GET reflects it."""
    r = client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_analytics": True},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["manager_can_view_analytics"] is True
    # Other flags untouched.
    assert r.json()["instructor_can_view_analytics"] is False

    body = client.get("/api/admin/role-permissions", headers=admin_headers).json()
    assert body["manager_can_view_analytics"] is True
    assert body["instructor_can_view_analytics"] is False


def test_non_admin_cannot_patch_role_permissions(client, manager_headers, instructor_headers):
    body = {"manager_can_view_analytics": True}
    assert client.patch("/api/admin/role-permissions", json=body, headers=manager_headers).status_code == 403
    assert client.patch("/api/admin/role-permissions", json=body, headers=instructor_headers).status_code == 403


def test_non_admin_cannot_get_role_permissions(client, manager_headers, instructor_headers):
    assert client.get("/api/admin/role-permissions", headers=manager_headers).status_code == 403
    assert client.get("/api/admin/role-permissions", headers=instructor_headers).status_code == 403


# ---------------------------------------------------------------------------
# Analytics access gating
# ---------------------------------------------------------------------------
def test_instructor_blocked_from_analytics_by_default(client, instructor_headers):
    r = client.get("/api/admin/analytics/summary?days=30", headers=instructor_headers)
    assert r.status_code == 403


def test_manager_blocked_from_analytics_by_default(client, manager_headers):
    r = client.get("/api/admin/analytics/summary?days=30", headers=manager_headers)
    assert r.status_code == 403


def test_instructor_allowed_when_flag_on(client, admin_headers, instructor_headers):
    client.patch(
        "/api/admin/role-permissions",
        json={"instructor_can_view_analytics": True},
        headers=admin_headers,
    )
    r = client.get("/api/admin/analytics/summary?days=30", headers=instructor_headers)
    assert r.status_code == 200


def test_manager_allowed_when_flag_on(client, admin_headers, manager_headers):
    client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_analytics": True},
        headers=admin_headers,
    )
    r = client.get("/api/admin/analytics/summary?days=30", headers=manager_headers)
    assert r.status_code == 200


def test_admin_always_sees_analytics(client, admin_headers):
    """Admin role bypasses the flag check entirely."""
    r = client.get("/api/admin/analytics/summary?days=30", headers=admin_headers)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Server access gating
# ---------------------------------------------------------------------------
def test_instructor_blocked_from_server_by_default(client, instructor_headers):
    r = client.get("/api/admin/server/info", headers=instructor_headers)
    assert r.status_code == 403


def test_manager_blocked_from_server_by_default(client, manager_headers):
    r = client.get("/api/admin/server/info", headers=manager_headers)
    assert r.status_code == 403


def test_instructor_allowed_server_when_flag_on(client, admin_headers, instructor_headers):
    client.patch(
        "/api/admin/role-permissions",
        json={"instructor_can_view_server": True},
        headers=admin_headers,
    )
    r = client.get("/api/admin/server/info", headers=instructor_headers)
    assert r.status_code == 200


def test_manager_allowed_server_when_flag_on(client, admin_headers, manager_headers):
    client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_server": True},
        headers=admin_headers,
    )
    r = client.get("/api/admin/server/info", headers=manager_headers)
    assert r.status_code == 200


def test_admin_always_sees_server(client, admin_headers):
    r = client.get("/api/admin/server/info", headers=admin_headers)
    assert r.status_code == 200


def test_storage_preflight_open_to_uploaders(
    client, admin_headers, manager_headers, instructor_headers
):
    """The bulk-upload free-space preflight follows the upload capability.

    Managers upload, so they get the byte counts without the server-info flag
    (behind it the preflight would be silently disabled for them). Instructors
    still need that flag, and the data-dir path is not part of the answer.
    """
    r = client.get("/api/admin/storage", headers=manager_headers)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {
        "total_bytes", "free_bytes", "used_bytes", "upload_space", "max_upload_bytes",
        "allowed_extensions",
    }
    from wsi_app.config import settings
    assert r.json()["allowed_extensions"] == list(settings.wsi_allowed_extensions)
    # Free bytes and the reserve policy only: no paths in the upload figures.
    assert not any(isinstance(v, str) for v in r.json()["upload_space"].values())
    assert client.get("/api/admin/server/info", headers=manager_headers).status_code == 403

    assert client.get("/api/admin/storage", headers=instructor_headers).status_code == 403
    client.patch(
        "/api/admin/role-permissions",
        json={"instructor_can_view_server": True},
        headers=admin_headers,
    )
    assert client.get("/api/admin/storage", headers=instructor_headers).status_code == 200


def test_flag_grants_only_matching_axis(client, admin_headers, instructor_headers):
    """Setting ``instructor_can_view_analytics=True`` must NOT grant access
    to /api/admin/server endpoints (axis-scoped grant)."""
    client.patch(
        "/api/admin/role-permissions",
        json={"instructor_can_view_analytics": True},
        headers=admin_headers,
    )
    # Analytics allowed.
    r = client.get("/api/admin/analytics/summary?days=30", headers=instructor_headers)
    assert r.status_code == 200
    # Server still denied.
    r = client.get("/api/admin/server/info", headers=instructor_headers)
    assert r.status_code == 403


def test_explicit_null_leaves_the_flag_untouched(client, admin_headers):
    """An explicit JSON null means "no change", not "turn it off".

    With `model_dump(exclude_unset=True)` alone, Pydantic v2's "set" means
    *present in the request body* — so a field sent as null would arrive in
    the change set and `bool(None)` would silently revoke it, with a 200 and
    a payload that looked like a successful save. Every field on
    RolePermissionsIn is `Optional[bool] = None`, so None is the schema's
    "not supplied" sentinel and must be dropped on both paths.
    """
    # Turn two flags on.
    r = client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_server": True, "instructor_can_view_analytics": True},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["manager_can_view_server"] is True

    # PATCH one flag while explicitly nulling the other.
    r = client.patch(
        "/api/admin/role-permissions",
        json={
            "manager_can_view_analytics": True,
            "manager_can_view_server": None,
        },
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["manager_can_view_analytics"] is True   # the field actually sent
    assert body["manager_can_view_server"] is True      # null must not revoke it
    assert body["instructor_can_view_analytics"] is True  # untouched

    # And the change is what was persisted, not just what was echoed back.
    stored = client.get("/api/admin/role-permissions", headers=admin_headers).json()
    assert stored["manager_can_view_server"] is True
    assert stored["manager_can_view_analytics"] is True


def test_explicit_false_still_revokes(client, admin_headers):
    """Dropping nulls must not also swallow a deliberate `false`."""
    client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_server": True},
        headers=admin_headers,
    )
    r = client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_server": False},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["manager_can_view_server"] is False


def test_stats_hides_backups_from_non_admin_server_viewers(
    client, admin_headers, manager_headers
):
    """The server-view flag exposes slide/tour stats, not the admin-only
    backup store's archive count and size."""
    client.patch(
        "/api/admin/role-permissions",
        json={"manager_can_view_server": True},
        headers=admin_headers,
    )
    r = client.get("/api/admin/stats", headers=manager_headers)
    assert r.status_code == 200
    assert r.json()["backups"] is None
    assert r.json()["slides"]["total"] >= 0
    r = client.get("/api/admin/stats", headers=admin_headers)
    assert set(r.json()["backups"]) == {"count", "total_size_bytes"}
