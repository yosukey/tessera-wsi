"""Tests for admin endpoints supporting draft preview and WSI download.

Covers:
* GET /api/admin/slides/{id} returns drafts (the public endpoint 404s on them).
* POST /api/admin/slides/{id}/download_url + GET .../download serve the file.
* The download endpoint accepts either an admin Bearer token or a
  signed ?t= query string, and rejects anything else.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import delete

from wsi_app.auth import create_slide_download_token
from wsi_app.config import settings
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


def _write_dummy_wsi(slide_id: int, name: str = "sample.svs", content: bytes = b"WSI-BYTES") -> str:
    """Drop a placeholder file under data_dir/wsi/ so the download endpoint
    has something to stream. The endpoint doesn't open the file with
    OpenSlide — it just FileResponses the bytes — so any payload works.
    """
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    stored_name = f"{slide_id}_{name}"
    path = os.path.join(wsi_dir, stored_name)
    with open(path, "wb") as fh:
        fh.write(content)
    return stored_name


def test_admin_get_slide_returns_draft(client, admin_headers, db_session, slide):
    # Default fixture creates a draft slide.
    r = client.get(f"/api/admin/slides/{slide.id}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == slide.id
    assert body["is_published"] is False


def test_admin_get_slide_404s_for_unknown(client, admin_headers):
    r = client.get("/api/admin/slides/999999", headers=admin_headers)
    assert r.status_code == 404


def test_admin_get_slide_requires_admin(client, slide):
    r = client.get(f"/api/admin/slides/{slide.id}")
    assert r.status_code == 401


def test_download_url_returns_signed_url_and_streams(client, admin_headers, db_session, slide):
    stored_name = _write_dummy_wsi(slide.id, content=b"hello-wsi")
    slide.wsi_filename = stored_name
    db_session.commit()

    r = client.post(
        f"/api/admin/slides/{slide.id}/download_url",
        headers=admin_headers,
    )
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith(f"/api/admin/slides/{slide.id}/download?t=")

    # Token-bearing GET — no Authorization header — succeeds and streams the bytes.
    r = client.get(url)
    assert r.status_code == 200
    assert r.content == b"hello-wsi"
    cd = r.headers.get("content-disposition", "")
    # Filename should be the *original* upload name, not the on-disk
    # `<id>_` storage prefix.
    assert "sample.svs" in cd


def test_download_with_admin_bearer_token_succeeds(client, admin_headers, db_session, slide):
    stored_name = _write_dummy_wsi(slide.id, content=b"bearer-path")
    slide.wsi_filename = stored_name
    db_session.commit()

    r = client.get(f"/api/admin/slides/{slide.id}/download", headers=admin_headers)
    assert r.status_code == 200
    assert r.content == b"bearer-path"


def test_download_rejects_missing_credentials(client, db_session, slide):
    stored_name = _write_dummy_wsi(slide.id, content=b"nope")
    slide.wsi_filename = stored_name
    db_session.commit()

    # No header, no token → 401.
    r = client.get(f"/api/admin/slides/{slide.id}/download")
    assert r.status_code == 401


def test_download_rejects_token_for_other_slide(client, db_session, slide, admin_user):
    stored_name = _write_dummy_wsi(slide.id, content=b"swap")
    slide.wsi_filename = stored_name
    db_session.commit()

    # Token issued for a different slide id must not work here. Download tokens
    # carry the minting user, so one has to be minted on behalf of a real
    # admin row for the slide-id check to be what actually rejects it.
    foreign = create_slide_download_token(slide.id + 999, admin_user)
    r = client.get(f"/api/admin/slides/{slide.id}/download?t={foreign}")
    assert r.status_code == 401


def test_download_refuses_a_filename_outside_the_wsi_dir(client, admin_headers, db_session, slide):
    # A file that exists, but is reached only by escaping wsi/ (a crafted row
    # from a restored DB): the realpath guard answers 404, never the bytes.
    outside = os.path.join(settings.data_dir, "outside-wsi.bin")
    with open(outside, "wb") as fh:
        fh.write(b"secret")
    try:
        for name in ("../outside-wsi.bin", outside):
            slide.wsi_filename = name
            db_session.commit()
            r = client.get(f"/api/admin/slides/{slide.id}/download", headers=admin_headers)
            assert r.status_code == 404, name
            assert b"secret" not in r.content
    finally:
        os.remove(outside)


def test_download_404s_when_the_file_is_missing(client, admin_headers, db_session, slide):
    slide.wsi_filename = f"{slide.id}_gone.svs"
    db_session.commit()
    r = client.get(f"/api/admin/slides/{slide.id}/download", headers=admin_headers)
    assert r.status_code == 404


def test_download_url_404s_when_slide_has_no_file(client, admin_headers, slide):
    # Slide row exists but wsi_filename is empty (e.g. metadata-only restore).
    r = client.post(
        f"/api/admin/slides/{slide.id}/download_url",
        headers=admin_headers,
    )
    assert r.status_code == 404


# Macro / label associated images ----------------------------------------------

def test_macro_label_endpoints_404_when_absent(client, admin_headers, slide):
    # Fresh slide with no associated images stored.
    r = client.get(f"/api/slides/{slide.public_id}/macro")
    assert r.status_code == 404
    r = client.get(f"/api/admin/slides/{slide.id}/macro", headers=admin_headers)
    assert r.status_code == 404
    r = client.get(f"/api/admin/slides/{slide.id}/label", headers=admin_headers)
    assert r.status_code == 404


def test_macro_endpoint_serves_published_only(client, admin_headers, db_session, slide):
    # Stash a tiny "JPEG" payload directly into the row; the endpoint just
    # streams the bytes back, so no real image decoding is needed.
    slide.macro_data = b"FAKE-MACRO"
    slide.macro_mimetype = "image/jpeg"
    slide.macro_etag = "abc123"
    slide.label_data = b"FAKE-LABEL"
    slide.label_mimetype = "image/jpeg"
    slide.label_etag = "def456"
    db_session.commit()

    # Public endpoint hides drafts (consistent with /api/slides/{public_id}).
    r = client.get(f"/api/slides/{slide.public_id}/macro")
    assert r.status_code == 404

    slide.is_published = True
    db_session.commit()
    r = client.get(f"/api/slides/{slide.public_id}/macro")
    assert r.status_code == 200
    assert r.content == b"FAKE-MACRO"
    assert r.headers["etag"] == '"abc123"'

    # Admin endpoint serves drafts too.
    slide.is_published = False
    db_session.commit()
    r = client.get(f"/api/admin/slides/{slide.id}/macro", headers=admin_headers)
    assert r.status_code == 200
    assert r.content == b"FAKE-MACRO"


def test_label_endpoint_admin_only(client, admin_headers, db_session, slide):
    slide.label_data = b"PRIVATE-LABEL"
    slide.label_mimetype = "image/jpeg"
    slide.label_etag = "lbl1"
    slide.is_published = True
    db_session.commit()

    # No public counterpart.
    r = client.get(f"/api/slides/{slide.id}/label")
    assert r.status_code == 404

    # Unauthenticated admin endpoint is 401, not 404 — the resource exists,
    # but nothing is revealed until auth proves admin.
    r = client.get(f"/api/admin/slides/{slide.id}/label")
    assert r.status_code == 401

    r = client.get(f"/api/admin/slides/{slide.id}/label", headers=admin_headers)
    assert r.status_code == 200
    assert r.content == b"PRIVATE-LABEL"


def test_admin_slide_response_carries_macro_and_label_urls(
    client, admin_headers, db_session, slide,
):
    slide.macro_data = b"M"
    slide.macro_mimetype = "image/jpeg"
    slide.macro_etag = "m-etag"
    slide.label_data = b"L"
    slide.label_mimetype = "image/jpeg"
    slide.label_etag = "l-etag"
    db_session.commit()

    r = client.get(f"/api/admin/slides/{slide.id}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    # Admin macro routes through admin endpoint so drafts preview correctly.
    assert body["macro_url"] == f"/api/admin/slides/{slide.id}/macro?v=m-etag"
    assert body["label_url"] == f"/api/admin/slides/{slide.id}/label?v=l-etag"


def test_public_slide_response_omits_label_url(client, db_session, slide):
    slide.macro_data = b"M"
    slide.macro_mimetype = "image/jpeg"
    slide.macro_etag = "m-etag"
    slide.label_data = b"L"
    slide.label_mimetype = "image/jpeg"
    slide.label_etag = "l-etag"
    slide.is_published = True
    db_session.commit()

    r = client.get(f"/api/slides/{slide.public_id}")
    assert r.status_code == 200
    body = r.json()
    # Public clients get the macro overview URL (keyed by public_id) but never
    # the label URL — labels often carry identifying barcodes / patient ids.
    assert body["macro_url"] == f"/api/slides/{slide.public_id}/macro?v=m-etag"
    assert body["label_url"] == ""


# ---------------------------------------------------------------------------
# RBAC role matrix for the admin slide-management surface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_upload_slide_role_matrix(client, staff_client, role, expected):
    """POST /api/admin/slides/upload is gated by ``require_manager_or_above``:
    admin & manager may, instructor must not."""
    headers = staff_client(role)
    # The upload endpoint expects a multipart file. Send a tiny payload
    # with a name that is rejected by the type sniffer further down — so a
    # passing role still gets a 4xx (NOT 403) because the file isn't a real
    # WSI. The key assertion is the 403 boundary: instructors are bounced
    # before the handler runs.
    files = {"file": ("dummy.svs", b"not-a-real-wsi", "application/octet-stream")}
    r = client.post(
        "/api/admin/slides/upload",
        files=files,
        headers=headers,
    )
    if expected == 403:
        assert r.status_code == 403
    else:
        # Anything other than 403 (200 / 400 / 415 / 422) proves the user
        # cleared the gate. The exact code depends on file-content checks
        # not exercised here.
        assert r.status_code != 403, r.text


@pytest.mark.parametrize("role", ["admin", "manager", "instructor"])
def test_admin_get_slide_allowed_for_all_staff(client, staff_client, slide, role):
    """GET /api/admin/slides/{id} is ``require_staff`` — every role may read."""
    headers = staff_client(role)
    r = client.get(f"/api/admin/slides/{slide.id}", headers=headers)
    assert r.status_code == 200, r.text


def test_update_slide_metadata_emits_audit(client, admin_headers, slide, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="wsi_app.audit"):
        r = client.patch(
            f"/api/admin/slides/{slide.id}",
            data={"catalog_id": "C-9", "title": "New Title", "attributes_json": "{}"},
            headers=admin_headers,
        )
    assert r.status_code == 200, r.text
    assert any("slide.update_metadata" in rec.getMessage() for rec in caplog.records), \
        "slide metadata edits must be recorded in the audit trail"
