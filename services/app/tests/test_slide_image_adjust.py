"""Tests for the per-slide image-adjustment preset.

Covers:
* PUT /api/admin/slides/{id}/image-adjust stores clamped values and collapses
  the identity value (1.0) to NULL ("no preset").
* PATCH /api/admin/slides/{id} also accepts the preset fields.
* Both admin and public slide responses echo the preset so it reaches every
  viewer.
* Saving the preset requires manager-or-above (instructor is rejected).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app.models import Slide, SlideAttribute, Tour


@pytest.fixture(autouse=True)
def _clean_slides(db_session):
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()


def test_put_image_adjust_stores_values(client, admin_headers, db_session, slide):
    r = client.put(
        f"/api/admin/slides/{slide.id}/image-adjust",
        headers=admin_headers,
        data={"brightness": "1.2", "contrast": "0.9", "saturation": "1.5"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["image_adjust_brightness"] == 1.2
    assert body["image_adjust_contrast"] == 0.9
    assert body["image_adjust_saturation"] == 1.5

    db_session.refresh(slide)
    assert slide.image_adjust_brightness == 1.2
    assert slide.image_adjust_contrast == 0.9
    assert slide.image_adjust_saturation == 1.5


def test_put_image_adjust_identity_collapses_to_null(client, admin_headers, db_session, slide):
    # Seed a preset, then save identity — each identity channel should clear.
    slide.image_adjust_brightness = 1.3
    db_session.commit()
    r = client.put(
        f"/api/admin/slides/{slide.id}/image-adjust",
        headers=admin_headers,
        data={"brightness": "1.0", "contrast": "1", "saturation": "1.00"},
    )
    assert r.status_code == 200
    db_session.refresh(slide)
    assert slide.image_adjust_brightness is None
    assert slide.image_adjust_contrast is None
    assert slide.image_adjust_saturation is None


def test_put_image_adjust_clamps_out_of_range(client, admin_headers, db_session, slide):
    r = client.put(
        f"/api/admin/slides/{slide.id}/image-adjust",
        headers=admin_headers,
        # brightness max 1.5, saturation max 2.0
        data={"brightness": "99", "contrast": "0.1", "saturation": "-5"},
    )
    assert r.status_code == 200
    db_session.refresh(slide)
    assert slide.image_adjust_brightness == 1.5
    assert slide.image_adjust_contrast == 0.5
    assert slide.image_adjust_saturation == 0.0


def test_put_image_adjust_rejects_instructor(client, instructor_headers, slide):
    r = client.put(
        f"/api/admin/slides/{slide.id}/image-adjust",
        headers=instructor_headers,
        data={"brightness": "1.2", "contrast": "1", "saturation": "1"},
    )
    assert r.status_code == 403


def test_put_image_adjust_allows_manager(client, manager_headers, slide):
    r = client.put(
        f"/api/admin/slides/{slide.id}/image-adjust",
        headers=manager_headers,
        data={"brightness": "1.2", "contrast": "1", "saturation": "1"},
    )
    assert r.status_code == 200


def test_patch_metadata_accepts_image_adjust(client, admin_headers, db_session, slide):
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        headers=admin_headers,
        data={
            "catalog_id": slide.catalog_id,
            "title": slide.title,
            "attributes_json": "{}",
            "image_adjust_brightness": "0.8",
            "image_adjust_contrast": "1",  # identity -> NULL
            "image_adjust_saturation": "1.4",
        },
    )
    assert r.status_code == 200
    db_session.refresh(slide)
    assert slide.image_adjust_brightness == 0.8
    assert slide.image_adjust_contrast is None
    assert slide.image_adjust_saturation == 1.4


def test_admin_response_echoes_preset(client, admin_headers, db_session, slide):
    slide.image_adjust_brightness = 1.25
    db_session.commit()
    r = client.get(f"/api/admin/slides/{slide.id}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["image_adjust_brightness"] == 1.25
    assert body["image_adjust_contrast"] is None


def test_public_response_echoes_preset(client, db_session, slide, attach_wsi):
    attach_wsi(slide)
    slide.is_published = True
    slide.image_adjust_brightness = 1.25
    slide.image_adjust_saturation = 0.7
    db_session.commit()
    r = client.get(f"/api/slides/{slide.public_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["image_adjust_brightness"] == 1.25
    assert body["image_adjust_saturation"] == 0.7
    assert body["image_adjust_contrast"] is None
