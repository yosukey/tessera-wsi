"""Tests for the unguessable ``public_id`` that keys every public URL.

Covers the two properties the feature rests on:
1. Enumeration is closed — every public + DZI read surface returns 404 when
   addressed by the numeric PK, and 200 by the ``public_id`` token.
2. Row creation re-mints on a unique collision instead of 500-ing.
"""
from __future__ import annotations

import os

import pytest

from wsi_app.config import settings
from wsi_app.models import Slide, Tour


# ---------------------------------------------------------------------------
# 1. Enumeration closed: PK → 404, token → 200
# ---------------------------------------------------------------------------
@pytest.fixture
def published_with_tour(db_session, attach_wsi):
    s = Slide(title="Pub", catalog_id="P-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    attach_wsi(s)
    t = Tour(slide_id=s.id, title="T", is_published=True)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return s, t


def test_every_public_surface_404s_by_numeric_pk(client, published_with_tour):
    s, t = published_with_tour
    # Each of these is a published, fully-reachable resource — but only by its
    # token. Addressed by the numeric PK, every one must 404 (or 422 for a
    # malformed tile path), never 200.
    assert client.get(f"/api/slides/{s.id}").status_code == 404
    assert client.get(f"/api/slides/{s.id}/macro").status_code == 404
    assert client.get(f"/api/slides/{s.id}/tours").status_code == 404
    assert client.get(f"/api/tours/{t.id}").status_code == 404
    assert client.get(f"/dzi/{s.id}.dzi").status_code == 404
    assert client.get(f"/dzi/{s.id}.szi").status_code == 404
    assert client.get(f"/dzi/{s.id}_files/0/0_0.jpeg").status_code == 404


def test_public_surfaces_resolve_by_token(client, published_with_tour):
    s, t = published_with_tour
    r = client.get(f"/api/slides/{s.public_id}")
    assert r.status_code == 200 and r.json()["public_id"] == s.public_id
    assert client.get(f"/api/slides/{s.public_id}/tours").status_code == 200
    r = client.get(f"/api/tours/{t.public_id}")
    assert r.status_code == 200
    assert r.json()["tour"]["slide_public_id"] == s.public_id


def test_draft_served_to_admin_token_by_public_id(client, admin_headers, db_session):
    """A draft slide/tour 404s anonymously (even by token) but is served to a
    valid admin JWT, so the dashboard can preview it through the same
    token-keyed URLs the viewer uses."""
    s = Slide(title="Draft", catalog_id="D-1", is_published=False)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(slide_id=s.id, title="DraftTour", is_published=False)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    # Anonymous: 404 even with the correct token (draft is hidden).
    assert client.get(f"/api/slides/{s.public_id}").status_code == 404
    assert client.get(f"/api/tours/{t.public_id}").status_code == 404
    # Admin JWT: the draft is served for preview.
    assert client.get(f"/api/slides/{s.public_id}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/tours/{t.public_id}", headers=admin_headers).status_code == 200


def test_draft_preview_works_for_manager_and_instructor(
    client, manager_headers, instructor_headers, db_session
):
    """Draft preview must work for *any* staff role, not just admin — the
    frontend forwards the staff JWT for all of admin/manager/instructor, and
    those roles can already view drafts through the admin endpoints, so an
    admin-only bypass would 404 their own draft previews."""
    s = Slide(title="Draft", catalog_id="D-2", is_published=False)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(slide_id=s.id, title="DraftTour", is_published=False)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    for headers in (manager_headers, instructor_headers):
        assert client.get(f"/api/slides/{s.public_id}", headers=headers).status_code == 200
        assert client.get(f"/api/slides/{s.public_id}/tours", headers=headers).status_code == 200
        assert client.get(f"/api/tours/{t.public_id}", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# 2. Creation re-mints on a unique collision instead of 500-ing
# ---------------------------------------------------------------------------
def test_slide_delete_removes_tour_asset_dir_by_public_id(
    client, admin_headers, db_session
):
    """Deleting a slide must remove its tours' on-disk asset directories, which
    are keyed by the tour's public_id token (not the numeric id)."""
    s = Slide(title="WithAssets", catalog_id="WA-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(slide_id=s.id, title="T", is_published=True)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    # Simulate an uploaded asset living under the token-keyed directory.
    asset_dir = os.path.join(settings.data_dir, "assets", "tours", t.public_id)
    os.makedirs(asset_dir, exist_ok=True)
    with open(os.path.join(asset_dir, "pic.png"), "wb") as fh:
        fh.write(b"x")
    assert os.path.isdir(asset_dir)

    r = client.delete(f"/api/admin/slides/{s.id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    # The token-keyed directory must be gone (not orphaned under the numeric id).
    assert not os.path.exists(asset_dir), "tour asset dir leaked after slide delete"


def test_tour_creation_recovers_from_public_id_collision(
    client, admin_headers, db_session, monkeypatch
):
    s = Slide(title="Coll", catalog_id="C-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)

    # Pre-mint a token, attach it to an existing tour, then force the first
    # mint() in the create loop to collide with it — the retry must recover.
    taken = "collision-token-xyz"
    existing = Tour(slide_id=s.id, title="Existing", public_id=taken)
    db_session.add(existing)
    db_session.commit()

    import wsi_app.routers.admin_tours as at

    calls = {"n": 0}
    real = at.mint_public_id

    def flaky():
        calls["n"] += 1
        return taken if calls["n"] == 1 else real()

    monkeypatch.setattr(at, "mint_public_id", flaky)

    r = client.post(
        f"/api/admin/slides/{s.id}/tours",
        data={"title": "New"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert calls["n"] >= 2, "the first (colliding) mint should have been retried"
    body = r.json()
    assert body["public_id"] and body["public_id"] != taken


# ---------------------------------------------------------------------------
# 3. The id validators are exact: no trailing newline slips through "$"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["abc\n", "abc\n\n", "a" * 32 + "\n", "\n", "abc\r\n", "ab\nc"])
def test_a_public_id_with_a_newline_is_not_valid(bad):
    from wsi_app import admin_helpers

    assert not admin_helpers.valid_public_id(bad)
    assert admin_helpers.PUBLIC_ID_RE.match(bad) is None


@pytest.mark.parametrize("good", ["a", "A-b_9", "a" * 32])
def test_a_plain_public_id_is_valid(good):
    from wsi_app import admin_helpers

    assert admin_helpers.valid_public_id(good)


def test_other_id_shapes_refuse_a_trailing_newline():
    from wsi_app import analytics, jobs, slide_transfer

    assert jobs._JOB_ID_RE.match("0" * 32)
    assert jobs._JOB_ID_RE.match("0" * 32 + "\n") is None
    assert slide_transfer._EXPORT_ID_RE.match("export-" + "a" * 12)
    assert slide_transfer._EXPORT_ID_RE.match("export-" + "a" * 12 + "\n") is None
    assert slide_transfer._IMPORT_ID_RE.match("import-" + "a" * 12 + "\n") is None
    assert analytics.DIGEST_RE.match("f" * 64)
    assert analytics.DIGEST_RE.match("f" * 64 + "\n") is None
