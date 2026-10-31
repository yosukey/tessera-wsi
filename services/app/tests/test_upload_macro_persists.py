"""End-to-end check: upload pipeline persists macro bytes from extractor."""
import io
import os
from unittest.mock import patch


def test_upload_persists_macro_bytes_to_db(client, admin_headers, db_session):
    from wsi_app.config import settings
    os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "thumbs"), exist_ok=True)
    from wsi_app import admin_helpers
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides

    fake_props = {"width": 100, "height": 100, "mpp_x": 0.25, "mpp_y": 0.25,
                  "vendor": "aperio", "acquired_at": "2024-01-15"}
    fake_assoc = {"macro": b"FAKE-MACRO-JPEG", "label": b"FAKE-LABEL-JPEG"}

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image
        Image.new("RGB", (1, 1), (0, 0, 0)).save(out_path, "JPEG")

    # read_associated_images lives in admin_helpers (shared by the upload
    # finalize path and the slide-transfer import finalize path).
    with patch.object(admin_slides, "read_slide_properties", return_value=fake_props), \
         patch.object(admin_slides, "make_thumbnail", side_effect=fake_thumbnail), \
         patch.object(admin_helpers, "read_associated_images", return_value=fake_assoc):
        resp = client.post(
            "/api/admin/slides/upload",
            headers=admin_headers,
            files={"file": ("test.svs", io.BytesIO(b"\x00" * 1024), "application/octet-stream")},
        )
    assert resp.status_code == 200, resp.text
    slide_id = resp.json()["slide_id"]

    db_session.expire_all()
    s = db_session.get(Slide, slide_id)
    assert s.macro_data == b"FAKE-MACRO-JPEG", f"macro_data was {s.macro_data!r}"
    assert s.macro_etag, f"macro_etag empty: {s.macro_etag!r}"
    assert s.label_data == b"FAKE-LABEL-JPEG"
    assert s.acquired_at == "2024-01-15"

    # And the API response surfaces macro_url so the frontend can fetch it.
    list_resp = client.get("/api/admin/slides", headers=admin_headers)
    assert list_resp.status_code == 200
    item = next(x for x in list_resp.json() if x["id"] == slide_id)
    assert item["macro_url"], f"macro_url empty in list response: {item!r}"
    assert item["macro_url"].startswith(f"/api/admin/slides/{slide_id}/macro?v=")

    # And the macro endpoint serves the bytes back.
    macro_resp = client.get(f"/api/admin/slides/{slide_id}/macro", headers=admin_headers)
    assert macro_resp.status_code == 200
    assert macro_resp.content == b"FAKE-MACRO-JPEG"
