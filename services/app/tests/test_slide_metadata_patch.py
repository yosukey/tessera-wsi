"""Tests for attribute handling on PATCH /api/admin/slides/{id}.

attributes_json drives rebuild_slide_attributes(). The validation contract:
  * values must be lists of strings — a bare string must not split into one
    row per character (stain=H, stain=E), and a non-iterable must not 500 on
    ``for val in values``;
  * malformed / non-object JSON is a 4xx that leaves existing attributes
    intact, never a silent fall-back to {} that wipes them.
"""
from __future__ import annotations

from wsi_app.models import SlideAttribute


def test_patch_metadata_rejects_string_attribute_value(client, admin_headers, slide):
    # A bare string value must not be accepted (it would split into one row
    # per character: stain=H, stain=E).
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={
            "catalog_id": "C1",
            "title": "T1",
            "attributes_json": '{"stain": "HE"}',
        },
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_patch_metadata_rejects_null_attribute_value(client, admin_headers, slide):
    # A non-iterable value must not 500 on `for val in values`.
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={
            "catalog_id": "C1",
            "title": "T1",
            "attributes_json": '{"stain": null}',
        },
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_patch_metadata_rejects_corrupt_json_without_wiping_attributes(
    client, admin_headers, slide, db_session
):
    """A malformed attributes_json must 400, not silently fall back to {} and
    wipe the slide's existing attributes via rebuild_slide_attributes()."""
    # Seed an existing attribute.
    ok = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": "C1", "title": "T1", "attributes_json": '{"stain": ["HE"]}'},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text

    # Now send a corrupt body — must be rejected.
    bad = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": "C1", "title": "T1", "attributes_json": '{"stain": ['},
        headers=admin_headers,
    )
    assert bad.status_code == 400, bad.text

    # The seeded attribute must survive the rejected request.
    db_session.expire_all()
    vals = sorted(
        a.value for a in db_session.query(SlideAttribute).filter_by(slide_id=slide.id, key="stain")
    )
    assert vals == ["HE"], "corrupt request must not have wiped existing attributes"


def test_patch_metadata_rejects_non_object_json(client, admin_headers, slide):
    """attributes_json that parses to a non-object (list, scalar) is a 400,
    not a silent attribute wipe."""
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": "C1", "title": "T1", "attributes_json": '["HE"]'},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text


def test_patch_metadata_accepts_list_attribute_value(client, admin_headers, slide, db_session):
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={
            "catalog_id": "C1",
            "title": "T1",
            "attributes_json": '{"stain": ["HE", "PAS"]}',
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    vals = sorted(
        a.value for a in db_session.query(SlideAttribute).filter_by(slide_id=slide.id, key="stain")
    )
    assert vals == ["HE", "PAS"]


def test_patch_clear_flags_blank_catalog_id_and_title_on_a_draft(
    client, manager_headers, slide, db_session
):
    """An empty form value reads as "leave alone", so the dashboard sends
    clear_catalog_id / clear_title for a blanked field. Without the flags a
    cleared field would answer 200 while keeping the old value."""
    slide.is_published = False
    db_session.commit()
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"clear_catalog_id": "1", "clear_title": "1"},
        headers=manager_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    fresh = db_session.get(type(slide), slide.id)
    assert (fresh.catalog_id, fresh.title) == ("", "")


def test_patch_clear_flag_refused_on_a_published_slide(
    client, admin_headers, slide, attach_wsi, db_session
):
    """Blanking a required field on a published slide is a 400, not a silent
    "Slide #N" in the public gallery."""
    attach_wsi(slide)
    slide.is_published = True
    db_session.commit()
    before = slide.title
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": slide.catalog_id, "clear_title": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    db_session.expire_all()
    assert db_session.get(type(slide), slide.id).title == before


def test_patch_published_slide_with_already_blank_title_accepts_other_edits(
    client, admin_headers, slide, attach_wsi, db_session
):
    """A published slide can carry a blank title (bundle import skips the
    publish gate). Saving an unrelated field — the dashboard omits the blank
    title — must not be refused as "blanking" a required field."""
    attach_wsi(slide)
    slide.is_published = True
    slide.title = ""
    db_session.commit()
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": slide.catalog_id, "attributes_json": "{}", "show_scale_bar": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    fresh = db_session.get(type(slide), slide.id)
    assert fresh.title == ""
    assert fresh.show_scale_bar is False


def test_patch_metadata_attribute_race_is_409_not_500(client, admin_headers, slide, monkeypatch):
    """Two admins saving the same slide's attributes at once collide on
    uq_slide_attr_triplet; the loser must get a retryable 409, not a 500."""
    from sqlalchemy.exc import IntegrityError
    from wsi_app.routers import admin_slides

    def _collide(*_a, **_kw):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: slide_attributes"))

    monkeypatch.setattr(admin_slides, "rebuild_slide_attributes", _collide)
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"catalog_id": "C1", "title": "T1", "attributes_json": '{"stain": ["HE"]}'},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.slideConflict"
