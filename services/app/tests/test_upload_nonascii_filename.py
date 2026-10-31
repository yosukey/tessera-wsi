"""Non-ASCII (e.g. Japanese / Shift-JIS) WSI filenames must upload cleanly.

Regression test: ``safe_filename`` must split off the extension *before*
running the disallowed-character replacement and the leading ``._-`` strip on
the name. Otherwise a filename whose stem is entirely non-ASCII (``標本.svs``)
collapses to ``__.svs`` and the strip eats the underscores *and* the separating
dot, yielding ``svs`` with no extension — so ``validated_extension`` rejects
the upload with a 400. The extension must be preserved.
"""
import io
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from wsi_app.admin_helpers import safe_filename


@contextmanager
def _stub_openslide():
    """Patch the OpenSlide-backed helpers so any byte blob "uploads" cleanly."""
    from wsi_app.config import settings
    os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "thumbs"), exist_ok=True)
    from wsi_app import admin_helpers
    from wsi_app.routers import admin_slides

    fake_props = {"width": 100, "height": 100, "mpp_x": 0.25, "mpp_y": 0.25,
                  "vendor": "aperio", "acquired_at": "2024-01-15"}

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image
        Image.new("RGB", (1, 1), (0, 0, 0)).save(out_path, "JPEG")

    # read_associated_images lives in admin_helpers.
    with patch.object(admin_slides, "read_slide_properties", return_value=fake_props), \
         patch.object(admin_slides, "make_thumbnail", side_effect=fake_thumbnail), \
         patch.object(admin_helpers, "read_associated_images", return_value={}):
        yield


def _upload(client, admin_headers, name):
    return client.post(
        "/api/admin/slides/upload",
        headers=admin_headers,
        files={"file": (name, io.BytesIO(b"\x00" * 512), "application/octet-stream")},
    )


@pytest.mark.parametrize(
    "name, expected_ext",
    [
        ("標本.svs", ".svs"),          # all-Japanese stem
        ("①症例.svs", ".svs"),         # circled-number + kanji stem
        ("ＳＶＳ.svs", ".svs"),         # full-width latin stem
        ("病理標本123.ndpi", ".ndpi"),  # mixed stem, trailing ascii
        ("標本 コピー.tiff", ".tiff"),   # space + Japanese
    ],
)
def test_nonascii_stem_preserves_extension(name, expected_ext):
    safe = safe_filename(name)
    assert os.path.splitext(safe)[1].lower() == expected_ext, safe
    # Stem must survive as a non-empty, non-hidden name.
    assert not safe.startswith(".")
    assert safe != expected_ext


def test_safe_filename_still_hardens_traversal_and_hidden():
    # Path components stripped.
    assert "/" not in safe_filename("../../etc/passwd.svs")
    assert "\\" not in safe_filename("..\\..\\win.svs")
    # ".." collapsed, not left as traversal.
    assert ".." not in safe_filename("a..b.svs")
    # ".." right before the extension must not eat the extension's dot.
    assert safe_filename("case12..svs") == "case12.svs"
    assert safe_filename("a.b..tiff") == "a.b.tiff"
    assert safe_filename("a....svs") == "a_.svs"
    # A name of leading dots keeps its extension-preserving result.
    assert safe_filename("...svs") == "file.svs"
    assert safe_filename(".....tiff") == "file.tiff"
    # Leading dots stripped so the result isn't a hidden file.
    assert not safe_filename(".hidden.svs").startswith(".")
    # Empty input has no original stem to disambiguate: bare "file".
    assert safe_filename("") == "file"
    # A fully-non-ASCII stem collapses, but a stable hash of the original stem
    # is appended so distinct originals don't all become the same "file".
    collapsed = safe_filename("標本")  # no extension at all
    assert collapsed.startswith("file") and collapsed != "file"
    assert safe_filename("標本") == collapsed          # stable across calls
    assert safe_filename("検体") != collapsed          # distinct originals differ


def test_two_distinct_nonascii_names_both_register(client, admin_headers):
    """Two different fully-non-ASCII filenames must both upload.

    Both stems sanitise away, so a bare "file" fallback would make
    "標本.svs" and "検体.svs" normalise to the same "file.svs" and
    _assert_unique_wsi_filename would reject the second as a duplicate. A
    stable per-original hash keeps the stored names distinct.
    """
    # Names not reused by any other test in this module (the DB persists across
    # tests, so a duplicate original name would 409 on the second upload).
    with _stub_openslide():
        a = _upload(client, admin_headers, "腫瘍.svs")
        b = _upload(client, admin_headers, "生検.svs")
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert a.json()["slide_id"] != b.json()["slide_id"]


def test_japanese_filename_uploads_successfully(client, admin_headers):
    with _stub_openslide():
        resp = _upload(client, admin_headers, "標本.svs")
    assert resp.status_code == 200, resp.text
    slide_id = resp.json()["slide_id"]

    # Stored on disk as "{slide_id}_{safe_name}" with the extension intact.
    from wsi_app.config import settings
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    stored = [f for f in os.listdir(wsi_dir) if f.startswith(f"{slide_id}_")]
    assert stored, os.listdir(wsi_dir)
    assert stored[0].endswith(".svs"), stored


def test_partially_dropped_names_stay_distinct_and_nfc_nfd_agree():
    """Dropping the non-ASCII part of "胃生検0a1b.svs" and
    "大腸生検0a1b.svs" would leave both as "0a1b.svs" (a false 409 on the second),
    while the NFD spelling macOS sends would hash differently from the NFC one,
    so the same file could register twice."""
    import unicodedata

    a, b = safe_filename("胃生検0a1b.svs"), safe_filename("大腸生検0a1b.svs")
    assert a != b
    assert a.startswith("0a1b_") and a.endswith(".svs")
    for name in ("がん0a1b.svs", "ガーゼ.svs"):
        nfc, nfd = unicodedata.normalize("NFC", name), unicodedata.normalize("NFD", name)
        assert nfc != nfd
        assert safe_filename(nfc) == safe_filename(nfd)
    # ASCII-only names are unchanged (existing slides keep their stored name),
    # outputs are idempotent (bundle import re-checks names this way), and a
    # long name keeps its hash when trimmed.
    assert safe_filename("slide 01.svs") == "slide_01.svs"
    for name in (a, b, safe_filename("標本.svs")):
        assert safe_filename(name) == name
    long_name = safe_filename("標" + "x" * 300 + ".svs")
    assert len(long_name.encode()) <= 200
    assert long_name != safe_filename("本" + "x" * 300 + ".svs")
    # Duplicate detection is case-insensitive, so names differing only in
    # letter case must still collide once hashed ("A_<h>" vs "a_<h>").
    upper, lower = safe_filename("標本A.svs"), safe_filename("標本a.svs")
    assert upper != lower and upper.casefold() == lower.casefold()


def test_partially_dropped_names_both_register(client, admin_headers):
    with _stub_openslide():
        a = _upload(client, admin_headers, "胃生検0a1b.svs")
        b = _upload(client, admin_headers, "大腸ポリープ生検0a1b.svs")
        # The same name in NFD ("プ" as "フ" + U+309A) is the same file: a
        # duplicate, not a new slide.
        import unicodedata
        dup = _upload(client, admin_headers, unicodedata.normalize("NFD", "大腸ポリープ生検0a1b.svs"))
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert dup.status_code == 409, dup.text


def test_safe_filename_cuts_an_overlong_extension():
    """Capping only the stem is not enough: a long "extension" would keep the
    name past the filesystem's 255-byte limit (ENAMETOOLONG -> 500 on a tour
    asset)."""
    from wsi_app.admin_helpers import _FILENAME_EXT_MAX_BYTES, _FILENAME_MAX_BYTES

    name = safe_filename("a." + "b" * 300)
    assert name == "a." + "b" * (_FILENAME_EXT_MAX_BYTES - 1)
    long_both = safe_filename("s" * 150 + "." + "e" * 100)
    assert long_both == "s" * 150 + "." + "e" * (_FILENAME_EXT_MAX_BYTES - 1)
    for n in (name, long_both, safe_filename("標" + "." + "x" * 400)):
        assert len(n.encode()) <= _FILENAME_MAX_BYTES
        assert safe_filename(n) == n  # stable on a second pass
    # A long extension that still fits is left alone, and so is a short one
    # on a long name: only what would not fit changes.
    assert safe_filename("report.final-reviewed-version") == "report.final-reviewed-version"
    assert safe_filename("x" * 300 + ".svs") == "x" * 196 + ".svs"
