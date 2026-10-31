"""Regression tests for signed-download token_version (tv) revocation.

The three admin download endpoints (slide / backup / export) authorise a Bearer
JWT through resolve_staff_user, which enforces the ``tv`` (token_version)
claim — so a JWT revoked by a password change stops working immediately. This
test pins the slide-download endpoint (the shared code path); the
backup/export endpoints use the identical resolve_staff_user check.
"""
from __future__ import annotations

from wsi_app.auth import create_token
from wsi_app.db import SessionLocal
from wsi_app.models import Slide, User


def _admin_with_slide(db_session) -> tuple[User, Slide]:
    admin = User(username=f"dl-admin-{__import__('os').urandom(4).hex()}",
                 password_hash="x", role="admin", is_active=True)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    slide = Slide(title="DL", catalog_id="DL-001")
    db_session.add(slide)
    db_session.commit()
    db_session.refresh(slide)
    return admin, slide


def test_valid_admin_token_downloads(client, db_session, attach_wsi):
    admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    token = create_token(admin)
    r = client.get(
        f"/api/admin/slides/{slide.id}/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_token_revoked_by_version_bump_is_rejected(client, db_session, attach_wsi):
    """A token minted before a password change (token_version bump) must 401."""
    admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    # Mint the token at the current token_version, then bump it (as a password
    # change / forced logout does) so the token's tv claim is now stale.
    stale_token = create_token(admin)

    db = SessionLocal()
    try:
        row = db.get(User, admin.id)
        row.token_version = (row.token_version or 0) + 1
        db.commit()
    finally:
        db.close()

    r = client.get(
        f"/api/admin/slides/{slide.id}/download",
        headers={"Authorization": f"Bearer {stale_token}"},
    )
    assert r.status_code == 401

    # A freshly minted token at the new version still works.
    db = SessionLocal()
    try:
        fresh = create_token(db.get(User, admin.id))
    finally:
        db.close()
    r2 = client.get(
        f"/api/admin/slides/{slide.id}/download",
        headers={"Authorization": f"Bearer {fresh}"},
    )
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Query-string capability tokens (?t=)
# ---------------------------------------------------------------------------
# These ride in the URL — nginx access logs, browser history, Referer — so a
# leak is materially more likely than for a header-borne JWT. They carry
# the minting user's id and token_version and are re-checked against the live
# row, so disabling the account or resetting the password kills an
# already-issued link instead of leaving it good for the rest of its TTL.

def test_query_token_download_works_for_active_admin(client, db_session, attach_wsi):
    admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    token = create_token(admin)
    r = client.post(
        f"/api/admin/slides/{slide.id}/download_url",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    r2 = client.get(r.json()["url"])
    assert r2.status_code == 200


def test_query_token_dies_with_token_version_bump(client, db_session, attach_wsi):
    """A password change / forced logout revokes an outstanding download URL."""
    admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    token = create_token(admin)
    url = client.post(
        f"/api/admin/slides/{slide.id}/download_url",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["url"]
    assert client.get(url).status_code == 200

    db = SessionLocal()
    try:
        row = db.get(User, admin.id)
        row.token_version = (row.token_version or 0) + 1
        db.commit()
    finally:
        db.close()

    assert client.get(url).status_code == 401


def test_query_token_dies_when_account_disabled(client, db_session, attach_wsi):
    admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    token = create_token(admin)
    url = client.post(
        f"/api/admin/slides/{slide.id}/download_url",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["url"]
    assert client.get(url).status_code == 200

    db = SessionLocal()
    try:
        db.get(User, admin.id).is_active = False
        db.commit()
    finally:
        db.close()

    assert client.get(url).status_code == 401


def test_unbound_download_token_is_refused(client, db_session, attach_wsi):
    """A token without a ``uid`` claim (crafted or malformed) fails closed.

    Honouring it would leave the token un-revocable for the remainder of its
    TTL.
    """
    import time as _time

    import jwt as _jwt

    from wsi_app.auth import DOWNLOAD_TOKEN_TTL_SECONDS, JWT_ALG, JWT_SECRET

    _admin, slide = _admin_with_slide(db_session)
    attach_wsi(slide)
    now = int(_time.time())
    unbound = _jwt.encode(
        {
            "purpose": "slide_download",
            "slide_id": slide.id,
            "iat": now,
            "exp": now + DOWNLOAD_TOKEN_TTL_SECONDS,
        },
        JWT_SECRET,
        algorithm=JWT_ALG,
    )
    r = client.get(f"/api/admin/slides/{slide.id}/download?t={unbound}")
    assert r.status_code == 401
