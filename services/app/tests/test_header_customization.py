"""Tests for the always-on header background + per-slot title color settings.

Covers:
- Default header image upload (PNG/APNG/GIF) + delete + combined reset.
- Default title color hex validation (valid / empty / invalid).
- Per-slot title_color round trip via PATCH.
- Year-wrap rejection on the seasonal PATCH (start > end).
- Seasonal reset returns the slot to factory state with row preserved.
- Public /api/viewer/settings exposes the fields.
"""
from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient


def _minimal_png() -> bytes:
    """1×1 transparent PNG. Bytes are deterministic so tests can assert lengths."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\x01" b"\x00\x00\x00\x01" b"\x08\x06\x00\x00\x00"  # 1x1 RGBA
    ihdr_chunk = b"IHDR" + ihdr
    ihdr_crc = struct.pack(">I", zlib.crc32(ihdr_chunk))
    ihdr_block = struct.pack(">I", len(ihdr)) + ihdr_chunk + ihdr_crc
    raw = b"\x00" + b"\x00\x00\x00\x00"  # filter byte + 1 RGBA pixel
    comp = zlib.compress(raw)
    idat_chunk = b"IDAT" + comp
    idat_crc = struct.pack(">I", zlib.crc32(idat_chunk))
    idat_block = struct.pack(">I", len(comp)) + idat_chunk + idat_crc
    iend_chunk = b"IEND"
    iend_crc = struct.pack(">I", zlib.crc32(iend_chunk))
    iend_block = struct.pack(">I", 0) + iend_chunk + iend_crc
    return sig + ihdr_block + idat_block + iend_block


def _minimal_gif() -> bytes:
    # GIF89a 1x1, single color table entry, single transparent pixel.
    return (
        b"GIF89a"
        b"\x01\x00\x01\x00"  # 1x1
        b"\x80\x00\x00"      # global color table flag + bg index
        b"\x00\x00\x00\xff\xff\xff"  # 2-entry color table
        b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"  # image descriptor
        b"\x02\x02\x44\x01\x00"  # LZW data
        b"\x3b"
    )


def test_default_header_image_upload_delete_reset(client: TestClient, admin_headers):
    png = _minimal_png()
    r = client.post(
        "/api/admin/viewer/settings/header-default-background",
        headers=admin_headers,
        files={"file": ("bg.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "/api/viewer/header-default-background" in body["header_default_image_url"]

    # Public endpoint serves the bytes back.
    r2 = client.get("/api/viewer/header-default-background")
    assert r2.status_code == 200
    assert r2.content == png
    assert r2.headers["content-type"] == "image/png"

    # DELETE clears the image without dropping the row.
    r3 = client.delete(
        "/api/admin/viewer/settings/header-default-background",
        headers=admin_headers,
    )
    assert r3.status_code == 200
    assert client.get("/api/viewer/header-default-background").status_code == 404

    # Upload again, then set a color, then full reset.
    client.post(
        "/api/admin/viewer/settings/header-default-background",
        headers=admin_headers,
        files={"file": ("bg.png", png, "image/png")},
    )
    client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"header_default_title_color": "#abcdef"},
    )
    r4 = client.post("/api/admin/viewer/header-default/reset", headers=admin_headers)
    assert r4.status_code == 200
    settings = client.get("/api/viewer/settings").json()
    assert settings["header_default_image_url"] == ""
    assert settings["header_default_title_color"] == ""


def test_default_header_accepts_gif_rejects_jpeg(client: TestClient, admin_headers):
    gif = _minimal_gif()
    r = client.post(
        "/api/admin/viewer/settings/header-default-background",
        headers=admin_headers,
        files={"file": ("bg.gif", gif, "image/gif")},
    )
    assert r.status_code == 200, r.text
    served = client.get("/api/viewer/header-default-background")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/gif"

    # JPEG magic — should be rejected by the validator.
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
    r2 = client.post(
        "/api/admin/viewer/settings/header-default-background",
        headers=admin_headers,
        files={"file": ("bg.jpg", jpeg, "image/jpeg")},
    )
    assert r2.status_code == 400


def test_default_title_color_validation(client: TestClient, admin_headers):
    ok = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"header_default_title_color": "#1A2B3C"},
    )
    assert ok.status_code == 200
    settings = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert settings["header_default_title_color"] == "#1a2b3c"  # normalized lower

    # Empty string in form data is indistinguishable from "field absent" once
    # FastAPI parses it, so callers explicitly request a clear via the
    # paired clear_ flag (mirrors auth_settings.clear_ldap_bind_password).
    cleared = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"clear_header_default_title_color": "1"},
    )
    assert cleared.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()[
        "header_default_title_color"
    ] == ""

    bad = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"header_default_title_color": "red"},
    )
    assert bad.status_code == 422


def test_seasonal_title_color_roundtrip(client: TestClient, admin_headers):
    r = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/3",
        headers=admin_headers,
        data={"title_color": "#FFAA00", "start_month": 3, "start_day": 1, "end_month": 3, "end_day": 31},
    )
    assert r.status_code == 200, r.text
    assert r.json()["title_color"] == "#ffaa00"

    bad = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/3",
        headers=admin_headers,
        data={"title_color": "not-a-color"},
    )
    assert bad.status_code == 422

    cleared = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/3",
        headers=admin_headers,
        data={"clear_title_color": "1"},
    )
    assert cleared.status_code == 200
    assert cleared.json()["title_color"] == ""


def test_seasonal_year_wrap_rejected(client: TestClient, admin_headers):
    r = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/2",
        headers=admin_headers,
        data={
            "start_month": 12,
            "start_day": 20,
            "end_month": 1,
            "end_day": 5,
        },
    )
    assert r.status_code == 422
    assert "split" in r.json()["detail"].lower()


def test_seasonal_reset_clears_slot(client: TestClient, admin_headers):
    # Populate slot 4 with a non-default state, then reset.
    client.patch(
        "/api/admin/viewer/seasonal-backgrounds/4",
        headers=admin_headers,
        data={
            "enabled": "1",
            "label": "Spring",
            "start_month": 4,
            "start_day": 1,
            "end_month": 4,
            "end_day": 30,
            "title_color": "#112233",
        },
    )
    client.post(
        "/api/admin/viewer/seasonal-backgrounds/4/image",
        headers=admin_headers,
        files={"file": ("p.png", _minimal_png(), "image/png")},
    )

    r = client.post(
        "/api/admin/viewer/seasonal-backgrounds/4/reset",
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["label"] == ""
    assert body["title_color"] == ""
    assert body["start_month"] == 1 and body["end_month"] == 1
    assert body["has_image"] is False
    assert body["image_url"] == ""


def test_public_settings_exposes_header_fields(client: TestClient):
    body = client.get("/api/viewer/settings").json()
    # Always present, even when the ViewerSettings row doesn't exist yet
    # or has the columns at their defaults.
    assert "header_default_image_url" in body
    assert "header_default_title_color" in body
    assert body.get("max_index_items") == 2000
    # The per-slide inline tour cap defaults to 3 listed, the rest behind
    # the expander.
    assert body.get("index_tours_per_slide") == 3
    # Advanced rendering knobs ship at their OSD-equivalent defaults so a
    # fresh install renders with those built-in defaults.
    assert body.get("immediate_render") is True
    assert body.get("max_image_cache_count") == 200
    # Top-page card/filter category lists ship with the built-in
    # defaults so a fresh install renders with those defaults.
    assert body.get("index_card_attrs") == ["stain", "organ", "lab"]
    assert body.get("index_filter_attrs") == ["stain", "organ", "lab", "tags"]
    for slot in body["seasonal_backgrounds"]:
        # Each item should carry the title_color field.
        assert "title_color" in slot


def test_index_attrs_roundtrip_and_sanitise(client: TestClient, admin_headers):
    # Normal PATCH preserves user-defined order, including custom keys
    # that aren't part of BASE_KEYS.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={
            "index_card_attrs": '["organ","stain","custom_key"]',
            "index_filter_attrs": '["tags","stain"]',
        },
    )
    assert r.status_code == 200, r.text
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["index_card_attrs"] == ["organ", "stain", "custom_key"]
    assert body["index_filter_attrs"] == ["tags", "stain"]
    # Public surface mirrors admin.
    pub = client.get("/api/viewer/settings").json()
    assert pub["index_card_attrs"] == ["organ", "stain", "custom_key"]
    assert pub["index_filter_attrs"] == ["tags", "stain"]

    # Duplicates collapse (first-seen wins) and non-string members are
    # dropped silently — a hand-edited row or buggy client can't push
    # garbage into the persisted list.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_card_attrs": '["stain","stain","organ",42,null,"organ"]'},
    )
    assert r.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["index_card_attrs"] == ["stain", "organ"]

    # Malformed JSON collapses to the empty list; the field is then an
    # explicit "show nothing" rather than 500-ing.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_filter_attrs": "not json"},
    )
    assert r.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["index_filter_attrs"] == []


def test_max_index_items_clamps_and_persists(client: TestClient, admin_headers):
    # Out-of-range values are clamped, not rejected, so a typo can't lock
    # the admin out of saving the form (mirrors the max_zoom_pixel_ratio
    # contract).
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_index_items": "99"},  # below the min
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_index_items"] == 100

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_index_items": "999999"},  # above the max
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_index_items"] == 5000

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_index_items": "1500"},  # in-range
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_index_items"] == 1500
    # Public surface mirrors the admin-saved value.
    assert client.get("/api/viewer/settings").json()["max_index_items"] == 1500


@pytest.mark.parametrize("raw,expected", [("nan", 1.0), ("inf", 2.0), ("-inf", 1.0), ("1.44", 1.4)])
def test_max_zoom_pixel_ratio_non_finite_is_clamped_not_500(
    client: TestClient, admin_headers, raw, expected
):
    # float("nan") parses but slips past the < / > clamps; it must fall back
    # to the default instead of crashing round() into a 500.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_zoom_pixel_ratio": raw},
    )
    assert r.status_code == 200, r.text
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["max_zoom_pixel_ratio"] == expected


def test_index_tours_per_slide_clamps_and_persists(client: TestClient, admin_headers):
    # 0 is a legitimate setting ("list no tours inline; everything behind the
    # expander"), so it must survive the round-trip rather than being treated
    # as "unset" and rewritten to the default.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "0"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["index_tours_per_slide"] == 0
    assert client.get("/api/viewer/settings").json()["index_tours_per_slide"] == 0

    # Out-of-range values clamp rather than 4xx, same contract as
    # max_index_items / max_zoom_pixel_ratio.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "-5"},
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["index_tours_per_slide"] == 0

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "9999"},
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["index_tours_per_slide"] == 50

    # Garbage falls back to the default instead of 500-ing or persisting NULL.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "not a number"},
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["index_tours_per_slide"] == 3

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "8"},  # in-range
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["index_tours_per_slide"] == 8
    # Public surface mirrors the admin-saved value.
    assert client.get("/api/viewer/settings").json()["index_tours_per_slide"] == 8


def test_index_tours_per_slide_survives_unrelated_patch(client: TestClient, admin_headers):
    """PATCH semantics: a save that omits the field must leave it stored.

    0 is the value this matters most for — an "unset means default" handler
    would quietly restore 3 on the next unrelated save, and the admin would
    watch their setting undo itself. The reverse direction is asserted too, so
    a future edit that starts writing this field unconditionally can't clobber
    a sibling. Same contract test_settings_partial_update.py pins for the
    network settings.
    """
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "0", "max_index_items": "1500"},
    )
    assert r.status_code == 200, r.text

    # A save that mentions neither field.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"site_title": "Partial update probe"},
    )
    assert r.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["index_tours_per_slide"] == 0
    assert body["max_index_items"] == 1500

    # A save that mentions only the new field leaves its neighbour alone.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_tours_per_slide": "6"},
    )
    assert r.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["index_tours_per_slide"] == 6
    assert body["max_index_items"] == 1500
    assert body["site_title"] == "Partial update probe"


def test_advanced_rendering_settings_clamp_and_persist(client: TestClient, admin_headers):
    # immediate_render is a plain bool round-trip (mirrors show_admin_link).
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"immediate_render": "0"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["immediate_render"] is False
    assert client.get("/api/viewer/settings").json()["immediate_render"] is False

    # max_image_cache_count clamps to [100, 1000] on both read and write so a
    # typo can't lock the admin out and a hand-edited row can't ship an
    # out-of-range value to the viewer.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_image_cache_count": "50"},  # below the min
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_image_cache_count"] == 100

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_image_cache_count": "100000"},  # above the max
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_image_cache_count"] == 1000

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"max_image_cache_count": "350"},  # in-range
    )
    assert r.status_code == 200
    assert client.get("/api/admin/viewer/settings", headers=admin_headers).json()["max_image_cache_count"] == 350
    # Public surface mirrors the admin-saved value.
    assert client.get("/api/viewer/settings").json()["max_image_cache_count"] == 350


def test_slides_per_page_accepts_up_to_hard_cap(client: TestClient):
    # The per_page query param is the floor that ultimately bounds the
    # top-page render. The API should honour any value up to the hard cap
    # (5000) so an admin who raises max_index_items past 500 actually
    # gets the requested rows, not a silently-clamped 500.
    r = client.get("/api/slides?per_page=5000")
    assert r.status_code == 200
    assert r.json()["per_page"] == 5000

    r = client.get("/api/slides?per_page=5001")
    assert r.status_code == 422  # FastAPI enforces le=5000


def test_free_text_settings_strip_controls_and_cap_length(client: TestClient, admin_headers):
    # site_title / copyright_msg are single-line: every control char is
    # dropped. footer_text is multiline: newlines/tabs survive, other
    # control chars are stripped. All three are capped to their column
    # widths so SQLite (which does not enforce String(N)) can't persist an
    # unbounded value that then ships to every public client.
    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={
            "site_title": "A" * 300 + "\x00\x07bad",
            "copyright_msg": "C" * 600 + "\nnewline",
            "footer_text": "line1\nline2\tend\x00" + "F" * 2000,
        },
    )
    assert r.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    # site_title: controls removed, capped at 256.
    assert body["site_title"] == "A" * 256
    # copyright_msg: the embedded newline (a control char) is stripped on a
    # single-line field, then capped at 512.
    assert "\n" not in body["copyright_msg"]
    assert body["copyright_msg"] == "C" * 512
    # footer_text: newline + tab preserved, NUL stripped, capped at 1024.
    assert body["footer_text"].startswith("line1\nline2\tend")
    assert "\x00" not in body["footer_text"]
    assert len(body["footer_text"]) == 1024
    # Public surface mirrors the sanitised values.
    pub = client.get("/api/viewer/settings").json()
    assert pub["site_title"] == "A" * 256
    assert pub["footer_text"] == body["footer_text"]


def test_seasonal_label_strips_control_characters(client: TestClient, admin_headers):
    """The seasonal label gets the same hygiene as every other free-text field.

    A bare ``[:128]`` slice is not enough: every other admin free-text setting
    goes through ``_clean_free_text``. The value ships to every public client
    in the viewer-settings payload (and into the audit record), so an embedded
    NUL/ESC must not survive the round trip.
    """
    r = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/4",
        headers=admin_headers,
        data={"label": "Sp\x00ri\x1bng\x07"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Spring"


def test_seasonal_label_length_is_still_capped(client: TestClient, admin_headers):
    r = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/4",
        headers=admin_headers,
        data={"label": "x" * 300},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["label"]) == 128


def test_blank_header_title_color_is_not_a_clear(client: TestClient, admin_headers):
    """A whitespace-only color means "no change", not "clear".

    A guard testing the raw value's truthiness would let "   " pass,
    ``_normalize_title_color`` map it to "", and the stored color be
    cleared — bypassing the ``clear_`` flag that owns that transition, the
    same tri-state contract the rest of the handler follows.
    """
    def stored() -> str:
        return client.get(
            "/api/admin/viewer/settings", headers=admin_headers
        ).json()["header_default_title_color"]

    set_it = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"header_default_title_color": "#123456"},
    )
    assert set_it.status_code == 200, set_it.text
    assert stored() == "#123456"

    blank = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"header_default_title_color": "   "},
    )
    assert blank.status_code == 200, blank.text
    assert stored() == "#123456"

    # The explicit flag still clears it.
    cleared = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"clear_header_default_title_color": "1"},
    )
    assert cleared.status_code == 200
    assert stored() == ""


def test_ldap_test_endpoint_caps_credential_field_length(client: TestClient, admin_headers):
    """The LDAP probe's Form fields are capped like every sibling credential.

    Without a max_length FastAPI buffers the whole value in memory before the
    handler runs, so a multi-MB field would be materialised only to be handed
    to a bind that could never succeed.
    """
    r = client.post(
        "/api/admin/auth/ldap/test",
        headers=admin_headers,
        data={"username": "u" * 600, "password": "p"},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/admin/auth/ldap/test",
        headers=admin_headers,
        data={"username": "u", "password": "p" * 2000},
    )
    assert r.status_code == 422


def test_blank_seasonal_title_color_is_not_a_clear(client: TestClient, admin_headers):
    """The seasonal slot's title_color follows the same tri-state contract as
    header_default_title_color: blank means "no change", only the clear_ flag
    clears. A guard testing the raw value's truthiness would let a
    whitespace-only field normalize to "" and silently wipe the stored color."""
    r = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/5",
        headers=admin_headers,
        data={"title_color": "#0f0f0f"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["title_color"] == "#0f0f0f"

    blank = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/5",
        headers=admin_headers,
        data={"title_color": "  "},
    )
    assert blank.status_code == 200, blank.text
    assert blank.json()["title_color"] == "#0f0f0f"

    cleared = client.patch(
        "/api/admin/viewer/seasonal-backgrounds/5",
        headers=admin_headers,
        data={"clear_title_color": "1"},
    )
    assert cleared.status_code == 200
    assert cleared.json()["title_color"] == ""


def test_admin_previews_a_disabled_seasonal_slot(client: TestClient, admin_headers):
    """An uploaded image sits in a disabled slot until the admin enables it;
    the public route 404s it, so the dashboard previews it through the admin
    route its responses point at."""
    png = _minimal_png()
    client.post("/api/admin/viewer/seasonal-backgrounds/3/reset", headers=admin_headers)
    r = client.post(
        "/api/admin/viewer/seasonal-backgrounds/3/image",
        headers=admin_headers,
        files={"file": ("p.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    url = body["image_url"]
    assert url.startswith("/api/admin/viewer/seasonal-backgrounds/3/image?v=")
    listed = client.get("/api/admin/viewer/seasonal-backgrounds", headers=admin_headers).json()
    assert [sb["image_url"] for sb in listed if sb["slot"] == 3] == [url]

    assert client.get("/api/viewer/seasonal-background/3").status_code == 404
    assert client.get(url).status_code == 401
    got = client.get(url, headers=admin_headers)
    assert got.status_code == 200
    assert got.content == png
    assert got.headers["content-type"] == "image/png"
    assert client.get(
        url, headers={**admin_headers, "If-None-Match": got.headers["etag"]}
    ).status_code == 304

    client.post("/api/admin/viewer/seasonal-backgrounds/3/reset", headers=admin_headers)
    assert client.get(url, headers=admin_headers).status_code == 404
    assert client.get(
        "/api/admin/viewer/seasonal-backgrounds/9/image", headers=admin_headers
    ).status_code == 404


def test_public_settings_do_not_load_image_blobs(client: TestClient, admin_headers):
    """/api/viewer/settings needs only whether each image exists; selecting
    the blobs cost up to ~14.5 MB per request."""
    from sqlalchemy import event

    from wsi_app.db import engine

    png = _minimal_png()
    for path in (
        "/api/admin/viewer/settings/logo",
        "/api/admin/viewer/settings/header-default-background",
    ):
        r = client.post(path, headers=admin_headers, files={"file": ("p.png", png, "image/png")})
        assert r.status_code == 200, r.text
    for slot, enabled in ((1, "1"), (2, "0")):
        client.post(f"/api/admin/viewer/seasonal-backgrounds/{slot}/reset", headers=admin_headers)
        client.post(
            f"/api/admin/viewer/seasonal-backgrounds/{slot}/image",
            headers=admin_headers,
            files={"file": ("p.png", png, "image/png")},
        )
        client.patch(
            f"/api/admin/viewer/seasonal-backgrounds/{slot}",
            headers=admin_headers,
            data={"enabled": enabled},
        )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        body = client.get("/api/viewer/settings").json()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        for slot in (1, 2):
            client.post(f"/api/admin/viewer/seasonal-backgrounds/{slot}/reset", headers=admin_headers)
        client.post("/api/admin/viewer/header-default/reset", headers=admin_headers)
        client.delete("/api/admin/viewer/settings/logo", headers=admin_headers)

    assert body["logo_url"].startswith("/api/viewer/logo?v=")
    assert body["header_default_image_url"].startswith("/api/viewer/header-default-background?v=")
    # Only enabled slots with an image reach the public payload.
    assert [sb["slot"] for sb in body["seasonal_backgrounds"]] == [1]
    assert body["seasonal_backgrounds"][0]["image_url"].startswith("/api/viewer/seasonal-background/1?v=")
    for blob in (
        "viewer_settings.logo_data", "viewer_settings.favicon_data",
        "viewer_settings.header_default_image_data", "seasonal_backgrounds.image_data",
    ):
        for stmt in statements:
            assert blob not in stmt.replace(f"length({blob})", ""), stmt


def test_public_settings_defaults_without_a_row(client: TestClient):
    """No ViewerSettings row yet: the payload reports the model's defaults."""
    from wsi_app._serializers import viewer_settings_to_dict

    body = viewer_settings_to_dict(None)
    assert body == {
        "logo_url": "", "favicon_url": "", "copyright_msg": "", "extra_attrs": [],
        "index_card_attrs": ["stain", "organ", "lab"],
        "index_filter_attrs": ["stain", "organ", "lab", "tags"],
        "site_title": "", "footer_text": "",
        "show_zoom_indicator": False, "show_scale_bar": True, "show_metadata_panel": True,
        "default_index_type": "all", "show_filter_button": False,
        "default_view_mode": "tile", "default_sort_field": "catalog_id",
        "filter_sort_order": "count", "max_zoom_pixel_ratio": 1.0,
        "show_admin_link": False, "default_thumbnail_size": "lg",
        "max_index_items": 2000, "index_tours_per_slide": 3,
        "immediate_render": True, "max_image_cache_count": 200,
        "header_default_image_url": "", "header_default_title_color": "",
    }


def test_attr_lists_are_bounded(client: TestClient, admin_headers):
    """The attribute-key lists ship to every public client: keys get the
    free-text hygiene, and over-long keys or too many keys are refused."""
    import json

    from wsi_app._serializers import ATTR_LIST_MAX_ITEMS
    from wsi_app.admin_helpers import ATTR_KEY_MAX_LEN

    r = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"extra_attrs": json.dumps([" sta\x1bin ", "stain", "\x07", "or\ngan"])},
    )
    assert r.status_code == 200, r.text
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert body["extra_attrs"] == ["stain", "organ"]

    long_key = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"extra_attrs": json.dumps(["k" * (ATTR_KEY_MAX_LEN + 1)])},
    )
    assert long_key.status_code == 422
    too_many = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_filter_attrs": json.dumps([f"k{i}" for i in range(ATTR_LIST_MAX_ITEMS + 1)])},
    )
    assert too_many.status_code == 422
    at_cap = client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"index_filter_attrs": json.dumps([f"k{i}" for i in range(ATTR_LIST_MAX_ITEMS)])},
    )
    assert at_cap.status_code == 200
    body = client.get("/api/admin/viewer/settings", headers=admin_headers).json()
    assert len(body["index_filter_attrs"]) == ATTR_LIST_MAX_ITEMS
    assert body["extra_attrs"] == ["stain", "organ"]

    client.patch(
        "/api/admin/viewer/settings",
        headers=admin_headers,
        data={"extra_attrs": "[]", "index_filter_attrs": '["stain","organ","lab","tags"]'},
    )


def test_stored_attr_lists_are_bounded_on_read():
    """A row stored without the PATCH's bounds (or written by hand) can't
    push oversized or control-character keys into the public payload."""
    import json

    from wsi_app._serializers import ATTR_LIST_MAX_ITEMS, parse_attr_list
    from wsi_app.admin_helpers import ATTR_KEY_MAX_LEN

    raw = json.dumps(["ok", "x" * (ATTR_KEY_MAX_LEN + 1), "bad\x1b", "", 7, "ok"]
                     + [f"k{i}" for i in range(2 * ATTR_LIST_MAX_ITEMS)])
    out = parse_attr_list(raw)
    assert out[0] == "ok"
    assert "bad\x1b" not in out and "" not in out
    assert len(out) == ATTR_LIST_MAX_ITEMS
    assert len(set(out)) == len(out)
