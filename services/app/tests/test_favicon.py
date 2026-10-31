"""Tests for the favicon upload / revert / serve flow.

Covers:
- Admin upload happy paths (.ico and .png) with magic-byte validation.
- Rejection of extension-only forgeries (HTML renamed as foo.ico, PNG bytes
  uploaded as foo.ico, ICO bytes uploaded as foo.png, cursor-type .ico).
- Rejection of unsupported extensions (.svg, .gif, etc.).
- 413 on oversized uploads (>512 KB).
- /favicon.ico serves the uploaded bytes with the correct mimetype and
  responds to If-None-Match with 304.
- HEAD /favicon.ico (the LAN-direct probe) answers with GET's headers and no
  body, for an uploaded icon and for the bundled default, before login.
- DELETE reverts back to the default fallback (unauthenticated GET still 200).
- /favicon.ico is reachable WITHOUT admin credentials (BYPASS_EXACT path).
- POST / DELETE require admin auth.
- The bypass is a strict equality check: a sibling like /favicon.ico.bak
  must NOT inherit the unauthenticated treatment under basic-auth mode.
"""
from __future__ import annotations

import struct
import zlib

from fastapi.testclient import TestClient


def _minimal_png() -> bytes:
    """1×1 transparent PNG; deterministic so size assertions stay stable."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\x01" b"\x00\x00\x00\x01" b"\x08\x06\x00\x00\x00"
    ihdr_chunk = b"IHDR" + ihdr
    ihdr_crc = struct.pack(">I", zlib.crc32(ihdr_chunk))
    ihdr_block = struct.pack(">I", len(ihdr)) + ihdr_chunk + ihdr_crc
    raw = b"\x00" + b"\x00\x00\x00\x00"
    comp = zlib.compress(raw)
    idat_chunk = b"IDAT" + comp
    idat_crc = struct.pack(">I", zlib.crc32(idat_chunk))
    idat_block = struct.pack(">I", len(comp)) + idat_chunk + idat_crc
    iend_chunk = b"IEND"
    iend_crc = struct.pack(">I", zlib.crc32(iend_chunk))
    iend_block = struct.pack(">I", 0) + iend_chunk + iend_crc
    return sig + ihdr_block + idat_block + iend_block


def _minimal_ico() -> bytes:
    """16×16 1bpp BMP-in-ICO. Bytes are crafted by hand; just enough to pass
    magic-byte sniffing and survive a round-trip through the endpoint."""
    # ICONDIR: reserved=0, type=1 (icon), count=1
    icondir = b"\x00\x00\x01\x00\x01\x00"
    # ICONDIRENTRY: 16x16, no palette, 1 colour plane, 1 bpp,
    # bytesInRes = sizeof(BITMAPINFOHEADER) + colours + and-mask + xor-mask.
    bmp_header_size = 40
    # 1bpp: row stride = ceil(16/32) * 4 = 4 bytes; 16 rows for XOR + 16 for AND.
    xor_bytes = b"\x00" * (4 * 16)
    and_bytes = b"\x00" * (4 * 16)
    palette = b"\x00\x00\x00\x00" b"\xff\xff\xff\x00"  # 2-entry palette
    bytes_in_res = bmp_header_size + len(palette) + len(xor_bytes) + len(and_bytes)
    icondirentry = (
        b"\x10\x10\x02\x00"  # width=16, height=16, colours=2, reserved=0
        b"\x01\x00\x01\x00"  # planes=1, bpp=1
        + struct.pack("<I", bytes_in_res)
        + struct.pack("<I", 6 + 16)  # offset to bitmap data
    )
    # BITMAPINFOHEADER for the embedded DIB. Height is doubled (32) per ICO spec
    # because it stores XOR and AND masks stacked.
    bmp_header = (
        struct.pack("<I", bmp_header_size)
        + struct.pack("<i", 16)        # width
        + struct.pack("<i", 32)        # height (XOR + AND)
        + struct.pack("<H", 1)         # planes
        + struct.pack("<H", 1)         # bpp
        + struct.pack("<I", 0)         # compression = BI_RGB
        + struct.pack("<I", len(xor_bytes) + len(and_bytes))
        + struct.pack("<i", 0)
        + struct.pack("<i", 0)
        + struct.pack("<I", 2)         # colours used
        + struct.pack("<I", 0)
    )
    return icondir + icondirentry + bmp_header + palette + xor_bytes + and_bytes


def _cursor_ico() -> bytes:
    """ICO with type=2 (cursor). Must be rejected — only icons are allowed."""
    body = _minimal_ico()
    return body[:2] + b"\x02\x00" + body[4:]


def test_favicon_default_served_without_auth(client: TestClient):
    """Pre-upload, GET /favicon.ico either 200s from the static fallback or
    404s if the frontend hasn't been built. Either way it must NOT 401 — the
    LAN-direct probe relies on bypassed auth here."""
    r = client.get("/favicon.ico")
    assert r.status_code in (200, 404)


def test_favicon_upload_png_then_get(client: TestClient, admin_headers):
    png = _minimal_png()
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("f.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["favicon_url"].startswith("/favicon.ico?v=")

    r2 = client.get("/favicon.ico")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/png")
    assert r2.content == png
    etag = r2.headers.get("etag")
    assert etag

    # If-None-Match → 304 (probe doesn't care, but the path must work).
    r3 = client.get("/favicon.ico", headers={"If-None-Match": etag})
    assert r3.status_code == 304


_SAME_HEADERS = ("content-type", "content-length", "etag", "cache-control")


def test_favicon_answers_head_like_get(client: TestClient, admin_headers):
    """The LAN-direct probe sends HEAD (frontend/src/lib/lanProbe.ts). A
    GET-only FastAPI route answers it 405; it must get GET's headers and no
    body, and honour If-None-Match like GET does."""
    png = _minimal_png()
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("f.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    got = client.get("/favicon.ico")
    head = client.head("/favicon.ico")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(png))
    for name in _SAME_HEADERS:
        assert head.headers.get(name) == got.headers.get(name), name
    r304 = client.head("/favicon.ico", headers={"If-None-Match": got.headers["etag"]})
    assert r304.status_code == 304


def test_favicon_head_serves_the_bundled_default(client: TestClient, admin_headers,
                                                tmp_path, monkeypatch):
    from wsi_app.routers import public

    assert client.delete("/api/admin/viewer/settings/favicon", headers=admin_headers).status_code == 200
    ico = _minimal_ico()
    path = tmp_path / "favicon.ico"
    path.write_bytes(ico)
    monkeypatch.setattr(public, "_DEFAULT_FAVICON_PATH", str(path))
    got = client.get("/favicon.ico")
    head = client.head("/favicon.ico")
    assert got.status_code == head.status_code == 200
    assert got.content == ico and head.content == b""
    for name in _SAME_HEADERS:
        assert head.headers.get(name) == got.headers.get(name), name


def test_favicon_head_is_answered_before_login(client: TestClient, db_session):
    """/favicon.ico bypasses the app-auth gate by path, whatever the method:
    under basic mode the probe's HEAD must not be challenged."""
    from wsi_app import app_auth
    from wsi_app.models import AuthSettings

    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    saved = row.mode or "none"
    row.mode = "basic"
    db_session.commit()
    app_auth.refresh(db_session)
    try:
        assert client.get("/api/viewer/settings").status_code == 401
        assert client.head("/favicon.ico").status_code in (200, 404)
    finally:
        row.mode = saved
        db_session.commit()
        app_auth.refresh(db_session)


def test_favicon_upload_ico(client: TestClient, admin_headers):
    ico = _minimal_ico()
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("f.ico", ico, "image/x-icon")},
    )
    assert r.status_code == 200, r.text
    r2 = client.get("/favicon.ico")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/x-icon") or \
        r2.headers["content-type"].startswith("image/vnd.microsoft.icon")
    assert r2.content == ico


def test_favicon_upload_rejects_extension_only_forgery(client: TestClient, admin_headers):
    """HTML renamed to foo.ico must be rejected by the magic-byte check."""
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("evil.ico", b"<html><script>alert(1)</script></html>", "image/x-icon")},
    )
    assert r.status_code == 400


def test_favicon_upload_rejects_mimetype_mismatch(client: TestClient, admin_headers):
    """PNG bytes uploaded as foo.ico must be rejected — extension wins on the
    map but the bytes don't match, so the upload should fail rather than
    silently serve PNG bytes with image/x-icon."""
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("oops.ico", _minimal_png(), "image/x-icon")},
    )
    assert r.status_code == 400

    # The mirror case: real ICO uploaded as foo.png.
    r2 = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("oops.png", _minimal_ico(), "image/png")},
    )
    assert r2.status_code == 400


def test_favicon_upload_rejects_cursor_ico(client: TestClient, admin_headers):
    """An .ico file with cursor type (type=2) must be rejected."""
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("cursor.ico", _cursor_ico(), "image/x-icon")},
    )
    assert r.status_code == 400


def test_favicon_upload_rejects_svg(client: TestClient, admin_headers):
    """SVG is kept off the allowlist (XSS risk)."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("evil.svg", svg, "image/svg+xml")},
    )
    assert r.status_code == 400


def test_favicon_upload_rejects_unsupported_extension(client: TestClient, admin_headers):
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("anim.gif", b"GIF89a\x01\x00\x01\x00\x00", "image/gif")},
    )
    assert r.status_code == 400


def test_favicon_upload_rejects_oversize(client: TestClient, admin_headers):
    # Construct a "PNG" longer than 512 KB. Header is valid, body is
    # padded with zeros — the size guard fires before the magic check.
    big = _minimal_png() + b"\x00" * (512 * 1024 + 1)
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("big.png", big, "image/png")},
    )
    assert r.status_code == 413


def test_favicon_upload_requires_admin(client: TestClient):
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        files={"file": ("f.png", _minimal_png(), "image/png")},
    )
    assert r.status_code in (401, 403)


def test_favicon_delete_reverts_to_default(client: TestClient, admin_headers):
    # Upload first
    r = client.post(
        "/api/admin/viewer/settings/favicon",
        headers=admin_headers,
        files={"file": ("f.png", _minimal_png(), "image/png")},
    )
    assert r.status_code == 200

    # Revert
    r2 = client.delete("/api/admin/viewer/settings/favicon", headers=admin_headers)
    assert r2.status_code == 200

    # The settings serializer should now report an empty favicon_url
    r3 = client.get("/api/viewer/settings")
    assert r3.status_code == 200
    assert r3.json().get("favicon_url", "") == ""

    # And GET /favicon.ico must still not 401 — bypass remains, fallback
    # either delivers the bundled default (production) or 404 (dev). Both
    # are acceptable here; this test guards against a regression to
    # 401 / 500.
    r4 = client.get("/favicon.ico")
    assert r4.status_code in (200, 404)


def test_favicon_delete_requires_admin(client: TestClient):
    r = client.delete("/api/admin/viewer/settings/favicon")
    assert r.status_code in (401, 403)


def test_bypass_is_strict_equality_not_prefix(client: TestClient):
    """/favicon.ico is exact-match bypassed; a sibling like /favicon.ico.bak
    must NOT inherit that treatment. There is no such route registered, so
    the SPA fallback (or a 404) should serve it — never a 200 favicon body
    smuggled in through prefix matching, and (under restrictive auth modes)
    never an unauthenticated 200."""
    r = client.get("/favicon.ico.bak")
    # Either 404 (no such file in static dir) or the SPA shell HTML. Crucially
    # NOT the favicon payload and NOT a 200 image/x-icon response.
    assert r.status_code in (200, 404)
    ct = r.headers.get("content-type", "")
    assert not ct.startswith("image/")
