"""Slide state that must stay consistent under concurrent edits, and slide
metadata that must stay JSON-serialisable."""
from __future__ import annotations

import json
import math
from unittest.mock import patch

import openslide
import pytest
from sqlalchemy import delete, event, select

from wsi_app.db import SessionLocal, engine
from wsi_app.models import MetadataOption, Slide, SlideAttribute, Tour
from wsi_app.openslide_utils import read_slide_properties
from wsi_app.routers import admin_slides as _admin_slides
from wsi_app.schemas import SlideOut


@pytest.fixture(autouse=True)
def _clean(db_session):
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(MetadataOption).where(MetadataOption.key == "race_key"))
    db_session.execute(delete(Slide))
    db_session.commit()


def _commit_on_other_session(slide_id: int, **values) -> None:
    db = SessionLocal()
    try:
        row = db.get(Slide, slide_id)
        for k, v in values.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Publish vs metadata edit
# --------------------------------------------------------------------------
def test_clear_title_racing_a_publish_is_refused(client, admin_headers, slide, monkeypatch):
    """The PATCH decides "draft, clearing is fine" from the row it read; a
    publish committing after that read must not leave a published slide with
    an empty title."""
    real_validate = _admin_slides.validate_slide_metadata

    def validate_then_publish(*a, **kw):
        real_validate(*a, **kw)
        _commit_on_other_session(slide.id, is_published=True)

    monkeypatch.setattr(_admin_slides, "validate_slide_metadata", validate_then_publish)
    r = client.patch(
        f"/api/admin/slides/{slide.id}", data={"clear_title": "1"}, headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.publishedRequiredFields"
    db = SessionLocal()
    try:
        row = db.get(Slide, slide.id)
        assert row.is_published is True
        assert row.title == "Test Slide"
    finally:
        db.close()


def test_publish_racing_a_title_clear_is_refused(client, admin_headers, slide, attach_wsi, monkeypatch):
    """The reverse order: publish checked the fields, then a draft-time clear
    committed before publish wrote is_published."""
    attach_wsi(slide)
    real_present = _admin_slides._wsi_file_present

    def present_then_clear(s):
        _commit_on_other_session(slide.id, title="")
        return real_present(s)

    monkeypatch.setattr(_admin_slides, "_wsi_file_present", present_then_clear)
    r = client.post(f"/api/admin/slides/{slide.id}/publish", data={"publish": "1"}, headers=admin_headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.publishMissingFields"
    db = SessionLocal()
    try:
        row = db.get(Slide, slide.id)
        assert row.is_published is False
    finally:
        db.close()


def test_bulk_publish_racing_a_title_clear_reports_missing_fields(client, admin_headers, slide, attach_wsi, monkeypatch):
    attach_wsi(slide)
    real_present = _admin_slides._wsi_file_present

    def present_then_clear(s):
        _commit_on_other_session(slide.id, catalog_id="")
        return real_present(s)

    monkeypatch.setattr(_admin_slides, "_wsi_file_present", present_then_clear)
    r = client.post(
        "/api/admin/slides/bulk/publish",
        json={"slide_ids": [slide.id], "publish": True},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["error"] == "missing_fields"
    db = SessionLocal()
    try:
        assert db.get(Slide, slide.id).is_published is False
    finally:
        db.close()


def test_clearing_a_draft_and_an_already_blank_published_field_still_work(client, admin_headers, db_session, slide):
    r = client.patch(f"/api/admin/slides/{slide.id}", data={"clear_catalog_id": "1"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).catalog_id == ""
    # Published with the field already blank (restored DB): the no-op clear
    # alongside an unrelated edit is still accepted.
    _commit_on_other_session(slide.id, is_published=True)
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"clear_catalog_id": "1", "title": "Renamed"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).title == "Renamed"


# --------------------------------------------------------------------------
# Metadata rename losing the race to a new attribute
# --------------------------------------------------------------------------
def test_rename_collision_raised_by_the_update_is_a_409(client, admin_headers, db_session, slide):
    """SQLite raises the UNIQUE(slide_id, key, value) violation on the bulk
    UPDATE itself, so the UPDATE must sit inside the try/except, not 500."""
    db_session.add(MetadataOption(key="race_key", value="old"))
    db_session.add(SlideAttribute(slide_id=slide.id, key="race_key", value="old"))
    db_session.commit()

    fired = []

    def insert_target_first(conn, cursor, statement, params, context, executemany):
        if fired or not statement.lstrip().upper().startswith("UPDATE SLIDE_ATTRIBUTES"):
            return
        fired.append(True)
        other = SessionLocal()
        try:
            other.add(SlideAttribute(slide_id=slide.id, key="race_key", value="new"))
            other.commit()
        finally:
            other.close()

    event.listen(engine, "before_cursor_execute", insert_target_first)
    try:
        r = client.patch(
            "/api/admin/metadata/options",
            data={"key": "race_key", "old_value": "old", "new_value": "new"},
            headers=admin_headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", insert_target_first)
    assert fired
    assert r.status_code == 409, r.text
    assert r.json()["raced"] is True
    db_session.expire_all()
    vals = sorted(
        v for (v,) in db_session.execute(
            select(SlideAttribute.value).where(SlideAttribute.slide_id == slide.id)
        )
    )
    assert vals == ["new", "old"]  # nothing half-renamed


# --------------------------------------------------------------------------
# Draft macro image must not be licensed for shared caches
# --------------------------------------------------------------------------
def _with_macro(db_session, slide, *, published: bool) -> None:
    slide.macro_data = b"FAKE-MACRO"
    slide.macro_mimetype = "image/jpeg"
    slide.macro_etag = "m1"
    slide.is_published = published
    db_session.commit()


def test_draft_macro_for_staff_is_private(client, admin_headers, db_session, slide):
    _with_macro(db_session, slide, published=False)
    r = client.get(f"/api/slides/{slide.public_id}/macro", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["cache-control"].startswith("private")
    r = client.get(
        f"/api/slides/{slide.public_id}/macro",
        headers={**admin_headers, "If-None-Match": '"m1"'},
    )
    assert r.status_code == 304
    assert r.headers["cache-control"].startswith("private")


def test_published_macro_on_an_open_install_stays_shareable(client, db_session, slide):
    _with_macro(db_session, slide, published=True)
    r = client.get(f"/api/slides/{slide.public_id}/macro")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=300"


def test_published_macro_behind_cloudflare_access_is_private(client, db_session, slide, monkeypatch):
    from wsi_app.routers import public as _public

    _with_macro(db_session, slide, published=True)
    monkeypatch.setattr(_public._cf_access, "is_enabled", lambda: True)
    r = client.get(f"/api/slides/{slide.public_id}/macro")
    assert r.status_code == 200
    assert r.headers["cache-control"].startswith("private")


# --------------------------------------------------------------------------
# Non-finite numbers in file metadata
# --------------------------------------------------------------------------
class _PropsSlide:
    def __init__(self, props):
        self.properties = props
        self.dimensions = (256, 128)
        self.level_dimensions = ((256, 128),)

    def close(self):
        pass


@pytest.mark.parametrize("raw", ["Infinity", "inf", "NaN", "-0.25", "1e9"])
def test_non_finite_or_absurd_file_numbers_read_as_unknown(tmp_path, raw):
    path = tmp_path / "slide.svs"
    path.write_bytes(b"not-really-a-slide")
    props = {"openslide.mpp-x": raw, "openslide.mpp-y": "0.25", "openslide.objective-power": raw}
    with patch.object(openslide, "OpenSlide", return_value=_PropsSlide(props)):
        out = read_slide_properties(str(path))
    assert out["mpp_x"] == 0.0
    assert out["mpp_y"] == 0.25
    assert out["objective_power"] == 0.0
    json.dumps(out, allow_nan=False)


def test_slide_out_reports_stored_non_finite_values_as_unknown():
    out = SlideOut(
        id=1, title="t", thumb_url="", width=1, height=1,
        mpp_x=math.inf, mpp_y=math.nan, objective_power_file=math.inf,
    )
    dumped = out.model_dump(mode="json")
    assert dumped["mpp_x"] == 0.0 and dumped["mpp_y"] == 0.0
    assert dumped["objective_power_file"] == 0.0
    json.dumps(dumped, allow_nan=False)
