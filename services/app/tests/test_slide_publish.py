"""Tests for slide publish/draft state and its cascade to tours.

Covers:
* New slides default to draft (is_published=False).
* The admin publish endpoint flips the state and cascades to tours.
* Re-publishing a slide leaves tours as drafts (admin must republish each).
* Public list / detail / facets / tour endpoints hide draft slides.
* The /api/admin/stats endpoint reports slide published/draft counts.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from wsi_app.models import Slide, SlideAttribute, Tour


@pytest.fixture(autouse=True)
def _clean_slides(db_session):
    """Slide rows leak between session-scoped tests; wipe before each
    case so per-test counts are deterministic."""
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()


@pytest.fixture
def published_slide(db_session) -> Slide:
    s = Slide(title="Visible", catalog_id="V-001", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_new_slide_defaults_to_draft(db_session):
    s = Slide(title="Fresh")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.is_published is False


def test_admin_publish_endpoint_toggles_state(
    client, admin_headers, slide, db_session, attach_wsi
):
    # Slide fixture creates with is_published=False (Python default).
    # Publishing requires the WSI body on disk (see the publish gate).
    attach_wsi(slide)
    r = client.post(
        f"/api/admin/slides/{slide.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["is_published"] is True
    assert body["cascaded_tours"] == 0

    db_session.expire_all()
    assert db_session.get(Slide, slide.id).is_published is True

    # Flip back to draft.
    r = client.post(
        f"/api/admin/slides/{slide.id}/publish",
        data={"publish": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).is_published is False


def test_unpublishing_slide_cascades_published_tours_to_draft(
    client, admin_headers, db_session, published_slide
):
    t1 = Tour(slide_id=published_slide.id, title="T1", is_published=True)
    t2 = Tour(slide_id=published_slide.id, title="T2", is_published=True)
    t3 = Tour(slide_id=published_slide.id, title="T3", is_published=False)
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    r = client.post(
        f"/api/admin/slides/{published_slide.id}/publish",
        data={"publish": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_published"] is False
    # Only the two previously-published tours count toward the cascade.
    assert body["cascaded_tours"] == 2

    db_session.expire_all()
    tours = db_session.execute(
        select(Tour).where(Tour.slide_id == published_slide.id).order_by(Tour.id.asc())
    ).scalars().all()
    assert all(not t.is_published for t in tours)


def test_republishing_slide_does_not_republish_draft_tours(
    client, admin_headers, db_session, slide, attach_wsi
):
    # Slide starts as draft; tour also draft.
    attach_wsi(slide)
    tour = Tour(slide_id=slide.id, title="Stayed draft", is_published=False)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    r = client.post(
        f"/api/admin/slides/{slide.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    # Slide is now published, but the tour stays in its prior (draft) state.
    assert db_session.get(Slide, slide.id).is_published is True
    assert db_session.get(Tour, tour.id).is_published is False


def test_public_list_hides_draft_slides(client, db_session, slide, published_slide):
    # `slide` fixture is a draft (default); `published_slide` is published.
    r = client.get("/api/slides")
    assert r.status_code == 200
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert published_slide.id in ids
    assert slide.id not in ids
    # `total` reflects the filtered count, not the global slide count.
    assert body["total"] == 1


def test_public_detail_404s_for_draft_slide(client, slide):
    # Draft slide: 404 whether addressed by its public_id or (always) by PK.
    r = client.get(f"/api/slides/{slide.public_id}")
    assert r.status_code == 404
    r = client.get(f"/api/slides/{slide.id}")
    assert r.status_code == 404


def test_public_detail_returns_published_slide(client, published_slide):
    r = client.get(f"/api/slides/{published_slide.public_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == published_slide.id
    assert body["public_id"] == published_slide.public_id
    # The publish state must be echoed (not left at the schema default) so the
    # viewer can gate the live-sync presenter launcher, which is only valid on
    # a published slide. Regression guard: a bare SlideDetail(...) that omits
    # is_published returns False for every slide, and the launcher never shows.
    assert body["is_published"] is True


def test_public_detail_draft_preview_reports_unpublished(client, admin_headers, slide):
    # A staff draft-preview (admin token on the token-keyed public URL) gets the
    # draft, and its is_published must report False so the viewer keeps the
    # live-sync launcher hidden — the server refuses to start a session on a
    # draft, so surfacing the button there would be a dead end.
    r = client.get(f"/api/slides/{slide.public_id}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["is_published"] is False


def test_public_tour_404s_when_parent_slide_is_draft(
    client, db_session, slide
):
    # Parent slide is draft; tour is "published" but should still be hidden.
    tour = Tour(slide_id=slide.id, title="Orphan", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    # Per-slide tours endpoint: the parent is a draft, so it 404s (consistent
    # with the slide-detail endpoint, which also hides a draft).
    r = client.get(f"/api/slides/{slide.public_id}/tours")
    assert r.status_code == 404

    # All-tours endpoint
    r = client.get("/api/tours")
    assert r.status_code == 200
    tour_ids = {t["id"] for t in r.json()}
    assert tour.id not in tour_ids

    # Direct tour fetch (by token) — hidden because the parent slide is a draft.
    r = client.get(f"/api/tours/{tour.public_id}")
    assert r.status_code == 404


def test_public_tour_visible_when_both_slide_and_tour_published(
    client, db_session, published_slide
):
    tour = Tour(slide_id=published_slide.id, title="Live", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    r = client.get(f"/api/tours/{tour.public_id}")
    assert r.status_code == 200
    assert r.json()["tour"]["id"] == tour.id


def test_admin_list_includes_is_published(client, admin_headers, slide, published_slide):
    r = client.get("/api/admin/slides", headers=admin_headers)
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()}
    assert items[slide.id]["is_published"] is False
    assert items[published_slide.id]["is_published"] is True


def test_admin_stats_reports_slide_publish_counts(
    client, admin_headers, db_session, slide, published_slide
):
    r = client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    s = r.json()["slides"]
    assert s["published"] >= 1
    assert s["draft"] >= 1
    assert s["published"] + s["draft"] == s["total"]


def test_admin_stats_does_not_load_blob_columns(client, admin_headers, db_session):
    """The stats endpoint must NOT SELECT the LargeBinary macro_data /
    label_data blobs (hundreds of MB for a big catalog) — it selects only
    the scalar columns it aggregates. Regression guard against a
    `select(Slide)` that would materialise every ORM row including the blobs.

    Also asserts the counts are exactly right.
    """
    from sqlalchemy import event
    from wsi_app.db import engine

    # Seed slides carrying real blob payloads plus the scalar attributes the
    # stats aggregate over (vendor, extension, mpp, catalog_id, published).
    big = b"\x00" * (256 * 1024)
    db_session.add_all([
        Slide(
            title="A", catalog_id="C-1", wsi_filename="a.svs", vendor="Aperio",
            mpp_x=0.25, is_published=True, file_size_bytes=100,
            macro_data=big, label_data=big,
        ),
        Slide(
            title="B", catalog_id="", wsi_filename="b.ndpi", vendor="Hamamatsu",
            mpp_x=0.0, mpp_y=0.0, is_published=False, file_size_bytes=300,
            macro_data=big,
        ),
    ])
    db_session.commit()

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        r = client.get("/api/admin/stats", headers=admin_headers)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert r.status_code == 200
    body = r.json()["slides"]
    # Counts are correct from the scalar-only projection.
    assert body["total"] == 2
    assert body["total_file_bytes"] == 400
    assert body["published"] == 1
    assert body["draft"] == 1
    assert body["with_mpp"] == 1          # only slide A has mpp
    assert body["with_catalog_id"] == 1   # only slide A has a catalog_id
    assert body["largest_file_bytes"] == 300
    assert body["largest_file_name"] == "b.ndpi"
    exts = {row["label"]: row["count"] for row in body["by_extension"]}
    assert exts == {"SVS": 1, "NDPI": 1}

    # No statement that reads from the slides table may pull the blob columns.
    slide_reads = [s for s in captured if "FROM slides" in s]
    assert slide_reads, "expected at least one SELECT against slides"
    for stmt in slide_reads:
        assert "macro_data" not in stmt, stmt
        assert "label_data" not in stmt, stmt


def test_publish_endpoint_404s_for_unknown_slide(client, admin_headers):
    r = client.post(
        "/api/admin/slides/999999/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 404


def test_publish_rejected_when_catalog_id_blank(client, admin_headers, db_session):
    s = Slide(title="Has title", catalog_id="")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is False


def test_publish_rejected_when_title_blank(client, admin_headers, db_session):
    s = Slide(title="", catalog_id="HAS-ID")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is False


def test_publish_rejected_when_wsi_file_missing(client, admin_headers, db_session):
    """Complete metadata but no WSI body on disk — the state a slide is left
    in after a metadata-scope restore. Publishing must be refused so a
    gallery card that 404s on every tile request is never exposed."""
    s = Slide(title="Has title", catalog_id="HAS-ID", wsi_filename="ghost.svs")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is False


def test_publish_allowed_once_wsi_file_present(
    client, admin_headers, db_session, attach_wsi
):
    """The same slide becomes publishable once its WSI file is back on disk
    (the re-upload / restore recovery path)."""
    s = Slide(title="Has title", catalog_id="HAS-ID2")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    # Without a file: refused.
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    # With the file present: allowed.
    attach_wsi(s)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is True


# ---------------------------------------------------------------------------
# RBAC parametrisation: who may toggle publish vs upload/delete/patch metadata
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["admin", "manager", "instructor"])
def test_publish_toggle_allowed_for_every_staff_role(
    client, staff_client, db_session, role, attach_wsi
):
    """Every logged-in staff role (admin / manager / instructor) may flip
    a slide's published state. Wired via ``require_staff`` so educators can
    publish their own slides without needing a curator's hand."""
    s = Slide(title="Toggle me", catalog_id="TOGG-001")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    attach_wsi(s)

    headers = staff_client(role)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "1"},
        headers=headers,
    )
    assert r.status_code == 200, f"role={role} got {r.status_code}: {r.text}"
    assert r.json()["is_published"] is True


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_delete_slide_role_matrix(client, staff_client, db_session, role, expected):
    """Slide deletion is ``require_manager_or_above``: admins and managers
    may, instructors must not."""
    s = Slide(title="Del me", catalog_id=f"DEL-{role}")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    headers = staff_client(role)
    r = client.delete(f"/api/admin/slides/{s.id}", headers=headers)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_patch_slide_metadata_role_matrix(client, staff_client, db_session, role, expected):
    """PATCH on a slide is ``require_manager_or_above`` (slide-library write
    surface). Instructors cannot rename / re-tag slides."""
    s = Slide(title="Original", catalog_id=f"PAT-{role}")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    headers = staff_client(role)
    r = client.patch(
        f"/api/admin/slides/{s.id}",
        json={"title": "Renamed"},
        headers=headers,
    )
    assert r.status_code == expected, r.text


def test_unpublish_rotates_thumbnail_token(client, admin_headers, db_session):
    """Unpublishing rotates the public thumbnail's capability token so a URL
    handed out while the slide was live stops resolving (the /thumbs mount is
    ungated). The on-disk file is renamed to the new token."""
    import os

    from wsi_app.config import settings

    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)

    s = Slide(title="T", catalog_id="THUMB-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    old_thumb = f"{s.id}-deadbeefdeadbeef.jpg"
    s.thumb_filename = old_thumb
    db_session.commit()
    old_path = os.path.join(thumbs_dir, old_thumb)
    with open(old_path, "wb") as fh:
        fh.write(b"jpeg")

    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    new_thumb = db_session.get(Slide, s.id).thumb_filename
    assert new_thumb and new_thumb != old_thumb
    assert not os.path.exists(old_path)  # the leaked URL no longer resolves
    assert os.path.exists(os.path.join(thumbs_dir, new_thumb))


def test_unpublish_allowed_even_when_fields_blank(client, admin_headers, db_session):
    # A published slide that was later cleared shouldn't be locked into
    # published: the admin must still be able to take it back to draft.
    s = Slide(title="", catalog_id="", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    r = client.post(
        f"/api/admin/slides/{s.id}/publish",
        data={"publish": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is False
