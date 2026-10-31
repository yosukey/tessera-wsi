"""Admin user management: list / create / suspend / delete staff accounts
and toggle role-permission flags.

Every endpoint here is gated by ``require_admin`` — only the system
administrator may add/remove/suspend users or flip the ``RolePermissions``
flags. Self-deletion, self-suspension, last-admin deletion, and
last-admin suspension are all refused so the system can't end up locked
out of its own management surface.

Roles are fixed at creation: the design intentionally has no
"change role" endpoint. Operators who get the role wrong delete the
user and re-create them. This keeps audit trails clean (a single
``user.delete`` + ``user.create`` pair rather than a chain of
``user.update`` records to interpret) and avoids the "demote in
flight" race where a freshly demoted manager could still hold an
admin JWT for the rest of its TTL.

Access can be revoked without deletion via the ``is_active`` toggle
(``PATCH /api/admin/users/{id}``): a suspended account is refused at
login and has every in-flight JWT rejected immediately (the suspend
bumps ``User.token_version``; every token path re-checks both
``is_active`` and the version), while preserving the username and audit
trail for a later re-enable.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..admin_helpers import CTRL_CHAR_RE, audit, get_or_create_singleton, staff_username_taken
from ..auth import (
    MAX_USERNAME_LEN,
    hash_password,
    require_admin,
    validate_password_strength,
)
from ..db import get_db
from ..models import RolePermissions, User

router = APIRouter()
_logger = logging.getLogger("wsi_app.admin_users")


# ---------------------------------------------------------------------------
# Pydantic input/output models
# ---------------------------------------------------------------------------
# `Literal[...]` lets FastAPI reject unknown role values at the framework
# layer (422) so the endpoint never sees a malformed payload.
class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=MAX_USERNAME_LEN)
    password: str
    role: Literal["admin", "manager", "instructor"]


class UserUpdateIn(BaseModel):
    """Partial update for a managed user.

    Today only ``is_active`` (suspend / re-enable) is mutable; role stays
    fixed at creation by design (see the module docstring). ``Optional`` so
    the endpoint is a no-op when the flag is omitted rather than coercing it
    to a default.
    """
    is_active: Optional[bool] = None


class RolePermissionsIn(BaseModel):
    """Partial update for the RolePermissions singleton.

    Every field is Optional so the caller may flip any subset of flags
    in one PATCH. Missing fields are left at their current DB value
    (semantics standard for PATCH).
    """
    manager_can_view_analytics: Optional[bool] = None
    manager_can_view_server: Optional[bool] = None
    instructor_can_view_analytics: Optional[bool] = None
    instructor_can_view_server: Optional[bool] = None


def _user_payload(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "is_active": bool(u.is_active),
        "mfa_enabled": u.totp_secret_enc is not None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


_ROLE_PERMISSION_FLAGS = (
    "manager_can_view_analytics",
    "manager_can_view_server",
    "instructor_can_view_analytics",
    "instructor_can_view_server",
)


def role_permissions_payload(db: Session) -> dict:
    """The four operator-controlled role-permission flags.

    Returned by ``GET`` / ``PATCH /api/admin/role-permissions`` and embedded
    in the ``/api/admin/me`` responses. A missing singleton row (a fresh
    install whose flags were never changed) reads as every flag off — the
    model defaults and the least-privilege policy, and exactly how
    ``require_analytics_access`` / ``require_server_access`` read it — so
    reporting the flags never has to write the row.
    """
    rp = db.get(RolePermissions, 1)
    return {f: bool(getattr(rp, f, False)) for f in _ROLE_PERMISSION_FLAGS}


# ---------------------------------------------------------------------------
# Users CRUD
# ---------------------------------------------------------------------------
@router.get("/api/admin/users")
def admin_list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.execute(select(User).order_by(User.id.asc())).scalars().all()
    return [_user_payload(u) for u in users]


@router.post("/api/admin/users")
def admin_create_user(
    body: UserCreateIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="username must not be empty")
    if len(username) > MAX_USERNAME_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"username: at most {MAX_USERNAME_LEN} characters",
        )
    # Reject control characters (C0/DEL), matching the AuthUser username rule in
    # admin_settings.admin_add_auth_user. Staff usernames flow into audit logs,
    # the admin UI and CSV exports; an embedded ESC / NUL could smuggle
    # terminal escapes or truncate the value in those sinks.
    if CTRL_CHAR_RE.search(username):
        raise HTTPException(status_code=422, detail="username: invalid characters")
    # Centralised password policy (same as admin_register, admin_update_me).
    # Raises 422 on too-short / too-long.
    validate_password_strength(body.password)

    # Case-INSENSITIVE duplicate check, though login and the unique index are
    # both case-sensitive. Allowing "Admin" alongside "admin" creates two
    # visually near-identical accounts: an operator who believes they are
    # replacing the first has in fact added a second, audit-history filtering
    # (exact match on actor) silently splits one person's trail across the two,
    # and a suspension applied to the wrong spelling leaves the intended target
    # active. Best-effort — a concurrent create of two case variants can still
    # slip through, which the exact-match IntegrityError backstop below does not
    # catch; the point is to stop an operator reaching the state by hand.
    if staff_username_taken(db, username):
        raise HTTPException(status_code=400, detail="Exists")

    u = User(
        username=username,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(u)
    try:
        db.commit()
    except IntegrityError:
        # Lost the SELECT-then-INSERT race against another concurrent
        # admin creating the same username; surface the same 400 shape
        # the deterministic path produces.
        db.rollback()
        raise HTTPException(status_code=400, detail="Exists")
    db.refresh(u)
    audit(
        admin,
        "user.create",
        target=str(u.id),
        target_username=u.username,
        role=u.role,
    )
    return _user_payload(u)


@router.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    body: UserUpdateIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Suspend or re-enable a staff account.

    A disabled (``is_active=False``) user is refused at login and has every
    in-flight JWT rejected by ``auth.resolve_staff_user`` (the dependency,
    the app-access bypass, and the sync presenter gate all re-check the
    column), so this is the way to revoke access *without* deleting the
    account — the audit trail and the username are preserved, and the user
    can be re-enabled later. Two guards mirror ``admin_delete_user``:
    the actor can't disable themselves (mis-click lockout), and the last
    active admin can't be disabled (cluster lockout).
    """
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    if body.is_active is None:
        # Nothing to change; return the current shape rather than 422 so a
        # future field addition can extend this PATCH without a special case.
        return _user_payload(target)

    new_active = bool(body.is_active)
    if new_active == bool(target.is_active):
        return _user_payload(target)  # no-op

    if not new_active:
        if target.id == admin.id:
            raise HTTPException(status_code=400, detail="Cannot disable yourself")
        # Last-admin guard: refuse to disable the only remaining active admin,
        # matching the delete path so the management surface can't be locked out.
        if target.role == "admin":
            other_admins = db.execute(
                select(func.count())
                .select_from(User)
                .where(User.role == "admin", User.is_active.is_(True), User.id != target.id)
            ).scalar_one()
            if int(other_admins or 0) < 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot disable the last admin",
                )

    target.is_active = new_active
    if not new_active:
        # Bump the revocation generation so any token minted before the
        # suspension is rejected immediately, belt-and-suspenders alongside
        # the is_active re-check on every token path. Incremented in SQL, not
        # from the value read above: a password change committing in between
        # would otherwise be overwritten with the same generation it just
        # minted, and its token would come back to life on re-enable.
        target.token_version = User.token_version + 1
    if not new_active and target.role == "admin":
        # Close the last-admin TOCTOU: the pre-mutation guard above can be
        # passed concurrently by two workers each disabling a *different*
        # admin, and both would then commit → zero active admins. Flush this
        # write into the transaction (SQLite serialises writers) and re-count
        # the *total* remaining active admins; if the invariant broke, roll
        # back and refuse rather than leaving the system with no admin.
        db.flush()
        active_admins = db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        ).scalar_one()
        if int(active_admins or 0) < 1:
            db.rollback()
            raise HTTPException(status_code=400, detail="Cannot disable the last admin")
    db.commit()
    db.refresh(target)
    audit(
        admin,
        "user.update",
        target=str(target.id),
        target_username=target.username,
        role=target.role,
        is_active=new_active,
    )
    return _user_payload(target)


@router.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        # Self-deletion would leave the actor unable to recover from a
        # mis-click. The dashboard hides the affordance, but a direct
        # API call must also fail closed.
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    # Last-admin guard: count *other* active admins. Deleting the only
    # remaining admin (active or otherwise) would lock the system out of
    # its own management surface — the bootstrap endpoint
    # /api/admin/register is only reachable when ALLOW_ADMIN_REGISTER=1
    # AND no admin exists, so re-entry would require operator action on
    # the host.
    if target.role == "admin":
        other_admins = db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True), User.id != target.id)
        ).scalar_one()
        if int(other_admins or 0) < 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last admin",
            )
    target_username = target.username
    target_role = target.role
    db.delete(target)
    if target_role == "admin":
        # Close the last-admin TOCTOU (see admin_update_user): flush the delete
        # so SQLite holds the write lock, then re-count the remaining active
        # admins inside the same transaction. Two workers each deleting a
        # different admin could both pass the pre-mutation guard above; the
        # re-check catches the case where the invariant would break and rolls
        # back rather than committing a zero-admin state.
        db.flush()
        active_admins = db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        ).scalar_one()
        if int(active_admins or 0) < 1:
            db.rollback()
            raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    db.commit()
    audit(
        admin,
        "user.delete",
        target=str(user_id),
        target_username=target_username,
        role=target_role,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Role permissions singleton
# ---------------------------------------------------------------------------
@router.get("/api/admin/role-permissions")
def admin_get_role_permissions(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return role_permissions_payload(db)


@router.patch("/api/admin/role-permissions")
def admin_update_role_permissions(
    body: RolePermissionsIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Race-safe first creation of the singleton (SAVEPOINT + re-read), so two
    # workers creating it concurrently don't 500 the loser on the PK.
    rp = get_or_create_singleton(db, RolePermissions)
    # Apply only fields the caller actually supplied. Pydantic v2's
    # `model_dump(exclude_unset=True)` reflects "set" vs "default";
    # iterating means a future field addition is picked up automatically.
    #
    # `exclude_none` is the companion half: "set" in Pydantic v2 means
    # *present in the input*, so an explicit JSON null arrives in `changes`
    # and `bool(None)` would silently revoke the flag. Every field here is
    # `Optional[bool] = None`, so None is this schema's "not supplied"
    # sentinel on both paths — dropping it keeps the documented PATCH
    # semantics (missing field -> current DB value) for null as well.
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(rp, field, bool(value))
    db.commit()
    db.refresh(rp)
    payload = role_permissions_payload(db)
    audit(admin, "role_permissions.update", **payload)
    return payload
