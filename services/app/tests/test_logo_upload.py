"""Tests for the admin logo upload (/api/admin/viewer/settings/logo).

Logos are magic-byte-validated and SVG is rejected outright: an SVG can carry
an inline <script> and there is no sanitizer, so this mirrors the favicon
stance. Covers the byte sniffer (detect_logo_mimetype) and the endpoint's
extension / content-mismatch / happy-path behaviour.
"""
from __future__ import annotations

from wsi_app.admin_helpers import detect_logo_mimetype

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_GIF = b"GIF89a" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
_SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


def test_detect_logo_mimetype_recognises_raster_formats():
    assert detect_logo_mimetype(_PNG) == "image/png"
    assert detect_logo_mimetype(_JPEG) == "image/jpeg"
    assert detect_logo_mimetype(_GIF) == "image/gif"
    assert detect_logo_mimetype(_WEBP) == "image/webp"


def test_detect_logo_mimetype_rejects_svg_and_garbage():
    # SVG must not resolve to a mimetype: it can carry <script> and there is
    # no sanitizer, so the favicon stance is mirrored for logos.
    assert detect_logo_mimetype(_SVG) is None
    assert detect_logo_mimetype(b"not an image") is None
    assert detect_logo_mimetype(b"") is None


def test_logo_upload_rejects_svg_extension(client, admin_headers):
    r = client.post(
        "/api/admin/viewer/settings/logo",
        files={"file": ("logo.svg", _SVG, "image/svg+xml")},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_logo_upload_rejects_extension_content_mismatch(client, admin_headers):
    # .png extension but SVG bytes — the renamed-payload foothold the favicon
    # path already closes.
    r = client.post(
        "/api/admin/viewer/settings/logo",
        files={"file": ("logo.png", _SVG, "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_logo_upload_accepts_valid_png(client, admin_headers):
    r = client.post(
        "/api/admin/viewer/settings/logo",
        files={"file": ("logo.png", _PNG, "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
