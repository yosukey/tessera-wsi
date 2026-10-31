"""Coverage for openslide_utils helpers that don't need a real WSI on disk.

These tests stub out openslide.OpenSlide so they exercise the wrapper logic
(name normalisation, error swallowing) without depending on the OpenSlide
test corpus or network access.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from unittest.mock import patch

import openslide
import pytest
from PIL import Image

from wsi_app import openslide_utils as ou


class _StubAssoc(Mapping):
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def __iter__(self):
        return iter(self._names)

    def __len__(self):
        return len(self._names)

    def __getitem__(self, k):
        if k not in self._names:
            raise KeyError(k)
        return Image.new("RGB", (200, 100), (128, 0, 0))


class _StubSlide:
    def __init__(self, names: list[str]) -> None:
        self._assoc = _StubAssoc(names)

    @property
    def associated_images(self):
        return self._assoc

    def close(self) -> None:
        pass


def _run(names: list[str]) -> dict[str, bytes]:
    with patch.object(openslide, "OpenSlide", return_value=_StubSlide(names)):
        return ou.read_associated_images("/fake/path.svs")


def test_canonical_macro_and_label_extracted():
    out = _run(["macro", "label"])
    assert set(out.keys()) == {"macro", "label"}
    assert out["macro"].startswith(b"\xff\xd8")  # JPEG SOI


def test_only_macro_present():
    out = _run(["macro"])
    assert set(out.keys()) == {"macro"}


def test_no_associated_images_returns_empty():
    assert _run([]) == {}


def test_trailing_space_names_are_recognised():
    # The Aperio convention tags IFDs with an ImageDescription "macro " /
    # "label " prefix (with trailing space). OpenSlide is meant to strip the
    # space when normalising into associated_images keys, but some writers
    # produce names that survive verbatim — extract them anyway rather than
    # silently dropping a slide overview the operator can see in other tools.
    out = _run(["macro ", "label "])
    assert set(out.keys()) == {"macro", "label"}


def test_uppercase_names_are_recognised():
    out = _run(["Macro", "LABEL"])
    assert set(out.keys()) == {"macro", "label"}


def test_unrelated_names_are_ignored():
    out = _run(["thumbnail", "preview"])
    assert out == {}


# TIFF-IFD fallback ------------------------------------------------------------
#
# These exercise the recovery path that fires when OpenSlide misidentifies an
# Aperio-convention TIFF and its associated_images doesn't expose macro/label.
# Each test writes a real multi-IFD TIFF to disk (so PIL has something to
# walk) and stubs openslide.OpenSlide so the OpenSlide-side path returns
# whatever the test wants — usually [`thumbnail`] only, the failure mode
# this code path guards against.


def _build_aperio_convention_tiff(path, *, with_macro=True, with_label=True):
    """Write a multi-IFD TIFF that follows the Aperio macro/label convention.

    Pillow's save_all only stamps tiffinfo onto IFD 0, so tifffile (a
    dev dep) is used to control ImageDescription per directory. The bytes
    are not a *real* SVS — no tile pyramid, no MPP tags — but they're enough
    for the IFD walker to find the macro/label pages.
    """
    import numpy as np
    import tifffile

    with tifffile.TiffWriter(str(path)) as tw:
        tw.write(
            np.full((16, 16, 3), 255, dtype=np.uint8),
            description="Aperio Image Library v12.0\nfake",
            photometric="rgb",
            metadata=None,
        )
        if with_macro:
            tw.write(
                np.full((64, 32, 3), 64, dtype=np.uint8),
                description="macro 64x32 (0.5,1.0) JPEG/RGB Q=70",
                subfiletype=9,
                photometric="rgb",
                metadata=None,
            )
        if with_label:
            tw.write(
                np.full((40, 40, 3), 128, dtype=np.uint8),
                description="label 40x40 -> 40x40 JPEG/RGB Q=70",
                subfiletype=9,
                photometric="rgb",
                metadata=None,
            )


def test_tiff_fallback_recovers_macro_when_openslide_misses_it(tmp_path):
    """OpenSlide-level path returned only ['thumbnail'] — fallback wins.

    Covers a known failure: an Aperio-convention SVS whose
    IFD 0 lacks the OpenSlide-required "Aperio Image Library" banner makes
    OpenSlide fall back to its generic TIFF reader, which exposes a
    thumbnail but neither macro nor label. The IFD-walking fallback
    bypasses OpenSlide's vendor detection so the macro/label pages reach
    the dashboard regardless.
    """
    svs = tmp_path / "aperio_no_banner.svs"
    _build_aperio_convention_tiff(svs)
    with patch.object(openslide, "OpenSlide", return_value=_StubSlide(["thumbnail"])):
        out = ou.read_associated_images(str(svs))
    assert set(out.keys()) == {"macro", "label"}
    for v in out.values():
        assert v.startswith(b"\xff\xd8")  # JPEG SOI


def test_tiff_fallback_skips_when_openslide_already_supplied_both(tmp_path):
    """Don't re-walk the TIFF if OpenSlide already produced macro+label.

    OpenSlide's extraction is the preferred path because it knows about
    each vendor's per-IFD geometry / colour profile quirks. The fallback
    should only fire when something is missing.
    """
    svs = tmp_path / "ok.svs"
    _build_aperio_convention_tiff(svs)
    with patch.object(openslide, "OpenSlide", return_value=_StubSlide(["macro", "label"])):
        out = ou.read_associated_images(str(svs))
    # Both came from OpenSlide's red-tinted stub image, not the green/blue
    # ones written into the TIFF — confirming the fallback didn't override.
    assert set(out.keys()) == {"macro", "label"}


def test_tiff_fallback_fills_only_the_missing_key(tmp_path):
    """If OpenSlide produced label but missed macro, only macro is recovered."""
    svs = tmp_path / "partial.svs"
    _build_aperio_convention_tiff(svs)
    # The stub _StubAssoc returns a tinted PIL Image for any name in its
    # list, so this simulates "OpenSlide produced a label but no macro".
    with patch.object(openslide, "OpenSlide", return_value=_StubSlide(["label"])):
        out = ou.read_associated_images(str(svs))
    assert set(out.keys()) == {"macro", "label"}


def test_tiff_fallback_noops_on_szi(tmp_path):
    """SZI doesn't go through the TIFF fallback (it's a ZIP, not a TIFF)."""
    szi = tmp_path / "x.szi"
    szi.write_bytes(b"not a tiff")
    out = ou.read_associated_images(str(szi))
    assert out == {}


def test_tiff_fallback_bypasses_decompression_bomb_guard(tmp_path):
    """IFD 0 of a real WSI is multi-gigapixel — well past Pillow's default
    ``MAX_IMAGE_PIXELS`` of ~178M. The bomb check fires inside ``Image.open``
    based on IFD 0's declared size, before the code can ``seek`` to the
    smaller macro/label IFDs. Reproduce the failure by temporarily lowering
    the cap below the test fixture's IFD 0 size and confirm the fallback
    still recovers macro/label, and that the global cap is restored after.
    """
    svs = tmp_path / "bombsized.svs"
    _build_aperio_convention_tiff(svs)
    original_cap = Image.MAX_IMAGE_PIXELS
    # Fixture IFD 0 is 16×16 = 256 pixels; cap it to 1 so Image.open() would
    # raise DecompressionBombError without the bypass.
    Image.MAX_IMAGE_PIXELS = 1
    try:
        with patch.object(openslide, "OpenSlide", return_value=_StubSlide(["thumbnail"])):
            out = ou.read_associated_images(str(svs))
        assert set(out.keys()) == {"macro", "label"}
        # The bypass must restore the cap once the fallback finishes so
        # the process-wide setting isn't permanently weakened.
        assert Image.MAX_IMAGE_PIXELS == 1
    finally:
        Image.MAX_IMAGE_PIXELS = original_cap


def test_tiff_fallback_skips_oversized_pages_before_decoding(tmp_path, monkeypatch):
    """The IFD walk runs with the bomb guard raised to 1e12, so a crafted
    16000² "label" page decoded whole would take ~2 GB. Pages over the
    associated-image cap are skipped on their tag-declared size, before
    anything is decoded."""
    svs = tmp_path / "bigpages.svs"
    _build_aperio_convention_tiff(svs)  # macro 32x64 = 2048 px, label 40x40 = 1600 px
    monkeypatch.setattr(ou, "_ASSOCIATED_MAX_PIXELS", 2000)
    decoded = []
    real_encode = ou._encode_associated_jpeg
    monkeypatch.setattr(ou, "_encode_associated_jpeg", lambda img: decoded.append(img.size) or real_encode(img))
    with patch.object(openslide, "OpenSlide", return_value=_StubSlide(["thumbnail"])):
        out = ou.read_associated_images(str(svs))
    assert set(out) == {"label"}
    assert decoded == [(40, 40)]


class _StubSlideWithProps(_StubSlide):
    def __init__(self, names, props):
        super().__init__(names)
        self.properties = props


def test_openslide_path_skips_oversized_associated_images(monkeypatch):
    """OpenSlide's read_associated_image allocates w*h*4 bytes, so the
    size it publishes as properties is checked first."""
    props = {
        "openslide.associated.macro.width": "16000", "openslide.associated.macro.height": "16000",
        "openslide.associated.label.width": "200", "openslide.associated.label.height": "100",
    }
    slide = _StubSlideWithProps(["macro", "label"], props)
    touched = []
    real_getitem = _StubAssoc.__getitem__
    monkeypatch.setattr(_StubAssoc, "__getitem__", lambda self, k: touched.append(k) or real_getitem(self, k))
    with patch.object(openslide, "OpenSlide", return_value=slide):
        out = ou.read_associated_images("/fake/path.svs")
    assert set(out) == {"label"}
    assert touched == ["label"]


# ---------------------------------------------------------------------------
# _read_level_bounded: bounded-memory thumbnail read for non-pyramidal slides
# ---------------------------------------------------------------------------
class _FakeReadSlide:
    """Minimal OpenSlide stand-in whose lowest level is the full base image.

    Records the largest single read_region() size so a test can assert the
    thumbnail reader never attempts a full-frame allocation on a giant
    single-level slide.
    """

    def __init__(self, width: int, height: int):
        self._w = width
        self._h = height
        self.level_dimensions = [(width, height)]
        self.level_downsamples = [1.0]
        self.max_region_px = 0
        self.read_calls = 0

    @property
    def level_count(self) -> int:
        return 1

    def read_region(self, location, level, size):
        w, h = int(size[0]), int(size[1])
        self.read_calls += 1
        self.max_region_px = max(self.max_region_px, w * h)
        # Return an opaque RGBA tile; content is irrelevant to the assertions.
        return Image.new("RGBA", (w, h), (128, 128, 128, 255))

    def close(self):
        pass


def test_read_level_bounded_small_level_single_read():
    slide = _FakeReadSlide(400, 300)
    img = ou._read_level_bounded(slide, 0, (400, 300))
    try:
        assert slide.read_calls == 1
        assert img.size == (400, 300)
        assert img.mode == "RGB"
    finally:
        img.close()


# The strip walk depends only on a level's size relative to
# _THUMB_READ_BUDGET_PX, so the "giant" cases below shrink the budget and the
# level together: a real 100k x 100k level meant painting and resampling 10^10
# pixels, minutes of CPU per test, to check the same geometry.


def test_read_level_bounded_giant_single_level_stays_under_budget(monkeypatch):
    # A 100k x 100k single-level slide (a naive full read is ~40 GB of RGBA)
    # against the real 64 Mpx budget, at 1/10 scale on each side: the same
    # 156x the budget, so the same downsample factor.
    monkeypatch.setattr(ou, "_THUMB_READ_BUDGET_PX", 640_000)
    w = h = 10_000
    slide = _FakeReadSlide(w, h)
    img = ou._read_level_bounded(slide, 0, (w, h))
    try:
        # No single read may exceed the per-read budget, and it must have been
        # split into several strips rather than one full-frame read.
        assert slide.read_calls > 1
        assert slide.max_region_px <= ou._THUMB_READ_BUDGET_PX
        # Output preserves the square aspect ratio and is a modest intermediate.
        assert img.mode == "RGB"
        assert img.size[0] == img.size[1]
        assert img.size[0] * img.size[1] <= ou._THUMB_READ_BUDGET_PX
    finally:
        img.close()


def test_make_thumbnail_giant_single_level_bounded(tmp_path, monkeypatch):
    # 80k x 40k against the real budget, at 1/10 scale (50x the budget either
    # way; the intermediate image still exceeds the 512 box).
    monkeypatch.setattr(ou, "_THUMB_READ_BUDGET_PX", 640_000)
    out = tmp_path / "thumb.jpg"
    slide = _FakeReadSlide(8_000, 4_000)
    with patch.object(openslide, "OpenSlide", return_value=slide):
        ou.make_thumbnail("ignored.tiff", str(out), max_size=(512, 512))
    assert slide.max_region_px <= ou._THUMB_READ_BUDGET_PX
    with Image.open(out) as im:
        # Final thumbnail is capped to the 512 box, aspect preserved (2:1).
        assert max(im.size) <= 512
        assert im.size == (512, 256)


# ---------------------------------------------------------------------------
# save_thumbnail_atomic — publish via tmp + os.replace
# ---------------------------------------------------------------------------

def test_thumbnail_publish_is_atomic_on_save_failure(tmp_path, monkeypatch):
    """A failed encode must leave the previous thumbnail intact.

    A thumbnail written straight to the final name is unsafe: ``/thumbs`` is a
    public static mount and the slide row already references the file by
    name, so a crash or ENOSPC part-way through ``Image.save`` would publish a
    truncated JPEG that renders as a broken image in the gallery until an
    operator re-runs rebuild-thumbnails.
    """
    from PIL import Image

    import wsi_app.openslide_utils as ou

    out = tmp_path / "thumbs" / "7-deadbeef.jpg"
    out.parent.mkdir()
    out.write_bytes(b"previous-thumbnail")

    img = Image.new("RGB", (8, 8), (1, 2, 3))

    real_save = Image.Image.save

    def _boom(self, fp, *a, **kw):
        # Write a partial file first, then fail — exactly the shape of an
        # interrupted encode, and what a non-atomic path would leave behind.
        with open(fp, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0truncated")
        raise OSError("no space left on device")

    monkeypatch.setattr(Image.Image, "save", _boom)
    try:
        with pytest.raises(OSError):
            ou.save_thumbnail_atomic(img, str(out))
    finally:
        monkeypatch.setattr(Image.Image, "save", real_save)

    # The old thumbnail is still the one being served ...
    assert out.read_bytes() == b"previous-thumbnail"
    # ... and the failed attempt left no debris behind for the backup
    # enumerator or a later sweep to trip over.
    assert [p.name for p in out.parent.iterdir()] == [out.name]


def test_thumbnail_publish_replaces_previous_on_success(tmp_path):
    """The happy path still ends with a complete, readable JPEG at out_path."""
    from PIL import Image

    import wsi_app.openslide_utils as ou

    out = tmp_path / "thumbs" / "9-cafe.jpg"
    ou.save_thumbnail_atomic(Image.new("RGB", (16, 16), (200, 10, 10)), str(out))

    assert out.exists()
    with Image.open(out) as got:
        got.load()
        assert got.format == "JPEG"
        assert got.size == (16, 16)
    # No temp sibling survives a successful publish.
    assert [p.name for p in out.parent.iterdir()] == [out.name]


@pytest.mark.parametrize(
    "w,h,scale",
    [
        (10_000, 10_000, 100),  # h a clean multiple of the scale factor
        (10_000, 10_001, 100),  # h not a multiple of the scale factor
        (12_345, 33_333, 5),    # both odd, strip boundary lands mid-row
        (20_000, 40_001, 100),  # final strip shorter than one output row
        (65_536, 99_999, 64),   # large, many strips
    ],
)
def test_read_level_bounded_paints_every_canvas_row(w, h, scale, monkeypatch):
    """The strip walk must cover the whole output canvas, bottom row included.

    The strip loop scales each strip independently (``max(1, rh // f)``) and
    skips a final strip that would land past ``out_h``, which reads like it
    could leave the last rows of the canvas at the ``Image.new`` default. It
    cannot: ``rows_per_strip`` is a multiple of ``f``, so the full strips
    already paint exactly ``h // f == out_h`` rows and the skipped remainder is
    worth less than one output row. This pins that as a checkable property
    rather than an argument, across the geometries where it is least obvious.

    Each geometry is the real budget against a w x h level, run with the width
    and the budget both divided by ``scale`` (a common divisor): the
    downsample factor, the rows per strip and so every strip boundary stay
    exactly those of the full-size case, at 1/scale of the pixels to paint.
    """
    budget = ou._THUMB_READ_BUDGET_PX
    assert w % scale == 0 and budget % scale == 0
    sw, sbudget = w // scale, budget // scale

    def geometry(width, b):
        f = max(1, math.ceil(math.sqrt((width * h) / b)))
        return f, max(f, (max(1, b // width) // f) * f)

    assert geometry(sw, sbudget) == geometry(w, budget)
    monkeypatch.setattr(ou, "_THUMB_READ_BUDGET_PX", sbudget)
    slide = _FakeReadSlide(sw, h)
    img = ou._read_level_bounded(slide, 0, (sw, h))
    try:
        rgb = img.convert("RGB")
        try:
            px = rgb.load()
            # _FakeReadSlide paints opaque grey; an unpainted row keeps the
            # black Image.new gives the RGB canvas. Compared against the grey,
            # not a guessed canvas colour: a check for white, which the canvas
            # never is, could not fail.
            unpainted = [y for y in range(rgb.height) if px[0, y] != (128, 128, 128)]
            assert not unpainted, f"{len(unpainted)} unpainted rows, first at y={unpainted[0]}"
        finally:
            rgb.close()
    finally:
        img.close()


# ---------------------------------------------------------------------------
# check_tiff_segment_sizes: a crafted TileWidth must be refused before
# libopenslide sees it (its g_malloc aborts the whole process).
# ---------------------------------------------------------------------------


def _write_tiled_tiff(path, tile_w=None):
    import numpy as np
    import tifffile

    tifffile.imwrite(str(path), np.zeros((64, 64, 3), dtype="uint8"), tile=(16, 16))
    if tile_w is not None:
        import struct

        with tifffile.TiffFile(str(path)) as tf:
            tag = tf.pages[0].tags["TileWidth"]
            offset, dtype = tag.valueoffset, tag.dtype
        assert dtype == 4  # LONG, stored inline in the entry's value field
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(struct.pack("<I", tile_w))


def test_check_tiff_segment_sizes_accepts_normal_tiles(tmp_path):
    path = tmp_path / "ok.tif"
    _write_tiled_tiff(path)
    ou.check_tiff_segment_sizes(str(path))


def test_check_tiff_segment_sizes_rejects_oversized_tile(tmp_path):
    path = tmp_path / "evil.tif"
    _write_tiled_tiff(path, tile_w=0x56000100)
    with pytest.raises(ValueError, match="tile"):
        ou.check_tiff_segment_sizes(str(path))
    with pytest.raises(ValueError, match="tile"):
        ou.read_slide_properties(str(path))


def _write_lsm_tagged_tiff(path, tile_w):
    """A one-page tiled TIFF carrying the LSM private tag (34412) and a
    patched TileWidth. tifffile takes the tag for a Zeiss LSM file, whose
    reader looks at a second page and raises IndexError on this one; libtiff
    ignores the tag and reads the page, tile size and all."""
    import struct

    import numpy as np
    import tifffile

    with tifffile.TiffWriter(str(path)) as tw:
        tw.write(
            np.zeros((64, 64, 3), dtype="uint8"), tile=(16, 16), photometric="rgb",
            extratags=[(34412, "B", 64, b"\xff" * 64, True)],
        )
    # tifffile cannot open the result, so find TileWidth (322) by hand.
    with open(path, "r+b") as fh:
        data = fh.read()
        assert data[:4] == b"II*\x00"
        ifd = struct.unpack_from("<I", data, 4)[0]
        for i in range(struct.unpack_from("<H", data, ifd)[0]):
            entry = ifd + 2 + 12 * i
            code, dtype = struct.unpack_from("<HH", data, entry)
            if code == 322:
                fh.seek(entry + 8)
                fh.write(struct.pack("<H", tile_w) if dtype == 3 else struct.pack("<I", tile_w))
                return
    raise AssertionError("TileWidth tag not found")


def test_tiff_tifffile_cannot_walk_is_refused_before_openslide_sees_it(tmp_path, monkeypatch):
    """tifffile raising anything but TiffFileError must not read as "unparseable,
    let OpenSlide report it" — libopenslide would then abort the whole worker
    (SIGABRT from g_malloc) allocating the crafted ~92 GB tile."""
    import tifffile

    path = tmp_path / "lsm.tif"
    _write_lsm_tagged_tiff(path, tile_w=0x56000100)
    with pytest.raises(Exception) as raised:
        with tifffile.TiffFile(str(path), _multifile=False):
            pass
    assert not isinstance(raised.value, tifffile.TiffFileError)  # the bypass precondition

    def must_not_open(*args, **kwargs):
        pytest.fail("OpenSlide was handed a TIFF whose tile geometry was never checked")

    monkeypatch.setattr(openslide, "OpenSlide", must_not_open)
    with pytest.raises(ValueError, match="could not be checked"):
        ou.check_tiff_segment_sizes(str(path))
    for strict in (True, False):  # upload validation and serving a stored slide
        with pytest.raises(ValueError, match="could not be checked"):
            ou._open_openslide(str(path), strict=strict)
    with pytest.raises(ValueError, match="could not be checked"):
        ou.read_slide_properties(str(path))
    with pytest.raises(ValueError, match="could not be checked"):
        ou.make_thumbnail(str(path), str(tmp_path / "thumb.jpg"))


@pytest.mark.parametrize("make_error,refused", [
    (lambda: __import__("tifffile").TiffFileError("corrupted IFD structure"), False),
    (lambda: IndexError("index out of range"), True),
    (lambda: KeyError("TileWidth"), True),
    (lambda: OSError("I/O error"), True),
])
def test_only_a_broken_tiff_is_left_to_openslide(tmp_path, monkeypatch, make_error, refused):
    """A TIFF tifffile reports as structurally broken is damage libtiff refuses
    too, so OpenSlide still reports it the usual way; any other failure means
    the geometry went unchecked and the file is refused."""
    import tifffile

    path = tmp_path / "x.tif"
    _write_tiled_tiff(path)

    def raising(*args, **kwargs):
        raise make_error()

    monkeypatch.setattr(tifffile, "TiffFile", raising)
    if refused:
        with pytest.raises(ValueError, match="could not be checked"):
            ou.check_tiff_segment_sizes(str(path))
    else:
        ou.check_tiff_segment_sizes(str(path))


def test_genuinely_broken_tiff_still_reaches_openslide(tmp_path):
    import struct

    path = tmp_path / "broken.tif"
    # A first directory claiming 10 entries but holding only 20 bytes of them.
    path.write_bytes(b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 10) + b"\x00" * 20)
    ou.check_tiff_segment_sizes(str(path))
    with pytest.raises(openslide.OpenSlideError):
        ou._open_openslide(str(path), strict=True)


# ---------------------------------------------------------------------------
# Upload validation first reads the file in a child process, so an abort in
# libopenslide costs that child, not the worker.
# ---------------------------------------------------------------------------


def _rgb_tiff(path):
    import numpy as np
    import tifffile

    tifffile.imwrite(str(path), np.zeros((64, 64, 3), dtype="uint8"), tile=(16, 16), photometric="rgb")


def test_probe_contains_a_real_libopenslide_abort(tmp_path, monkeypatch):
    """With the tile-size check switched off in the child, the crafted TIFF
    makes libopenslide abort the process; the probe turns that into a
    refusal and this process never opens the file."""
    path = tmp_path / "lsm.tif"
    _write_lsm_tagged_tiff(path, tile_w=0x56000100)
    monkeypatch.setattr(ou, "_PROBE_CODE", (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import wsi_app.openslide_utils as ou;"
        "ou.check_tiff_segment_sizes = lambda *a, **k: None;"
        "ou._probe_child(sys.argv[2])"
    ))
    monkeypatch.setattr(ou, "open_wsi", lambda *a, **k: pytest.fail("opened in-process"))
    with pytest.raises(ValueError, match=r"crashed \(signal 6\)"):
        ou.read_slide_properties(str(path))


def test_probe_lets_a_readable_slide_through(tmp_path):
    path = tmp_path / "ok.tif"
    _rgb_tiff(path)
    ou._probe_in_child(str(path))
    assert ou.read_slide_properties(str(path))["width"] == 64


def test_probe_leaves_ordinary_errors_to_the_in_process_read(tmp_path):
    """A file the child merely fails to open is reported as an ordinary error
    (here OpenSlide's own "unsupported format"), not as a crash."""
    path = tmp_path / "x.svs"
    path.write_bytes(b"not a slide")
    ou._probe_in_child(str(path))
    with pytest.raises(openslide.OpenSlideError):
        ou.read_slide_properties(str(path))


def test_probe_refuses_a_hang(tmp_path, monkeypatch):
    path = tmp_path / "ok.tif"
    _rgb_tiff(path)
    monkeypatch.setattr(ou, "_PROBE_CODE", "import time; time.sleep(30)")
    monkeypatch.setattr(ou, "_PROBE_TIMEOUT_S", 0.5)
    with pytest.raises(ValueError, match="did not finish"):
        ou.read_slide_properties(str(path))


def test_probe_holds_only_a_bounded_tail_of_the_childs_stderr(tmp_path, monkeypatch):
    """A child that prints ~50 MB before aborting must not make this process
    hold that output: the peak of Python allocations across the call (the
    stderr reader thread included) stays far below it, and the crash is
    still reported with the end of what the child printed."""
    import tracemalloc

    path = tmp_path / "ok.tif"
    _rgb_tiff(path)
    monkeypatch.setattr(ou, "_PROBE_CODE", (
        "import os, sys; w = sys.stderr.buffer.write;"
        "[w(b'x' * 65535 + b'\\n') for _ in range(800)];"
        "w(b'reader said goodbye'); sys.stderr.flush(); os.abort()"
    ))
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already running")
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match=r"crashed \(signal 6\)") as excinfo:
            ou._probe_in_child(str(path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert str(excinfo.value).endswith("reader said goodbye")
    assert len(str(excinfo.value)) < ou._PROBE_STDERR_TAIL + 100


def _bad_tags_tiff(path, n_ifd=4, n_tag=500):
    """A TIFF of ``n_ifd`` directories holding ``n_tag`` tags tifffile cannot
    parse (data type 0) each."""
    import struct

    buf = bytearray(b"II*\x00" + struct.pack("<I", 8))
    off = 8
    for i in range(n_ifd):
        nxt = off + 2 + 12 * n_tag + 4
        buf += struct.pack("<H", n_tag)
        for t in range(n_tag):
            buf += struct.pack("<HHII", 40000 + t, 0, 1, 0)  # data type 0 is invalid
        buf += struct.pack("<I", nxt if i < n_ifd - 1 else 0)
        off = nxt
    path.write_bytes(bytes(buf))


def test_probe_child_does_not_log_per_malformed_tag(tmp_path):
    """tifffile logs a line for every tag it cannot parse; the probe child
    silences that, so a small file of bad tags cannot make it print
    megabytes."""
    import subprocess
    import sys

    path = tmp_path / "badtags.tif"
    _bad_tags_tiff(path)
    package_root = ou.os.path.dirname(ou.os.path.dirname(ou.os.path.abspath(ou.__file__)))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", ou._PROBE_CODE, package_root, str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
    )
    assert b"invalid data type" not in result.stderr
    assert len(result.stderr) < 16 * 1024


def test_probe_falls_back_to_in_process_when_no_child_can_start(tmp_path, monkeypatch):
    import subprocess

    def cannot_spawn(*args, **kwargs):
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "Popen", cannot_spawn)
    path = tmp_path / "ok.tif"
    _rgb_tiff(path)
    assert ou.read_slide_properties(str(path))["width"] == 64


def test_check_tiff_segment_sizes_ignores_non_tiff_and_missing(tmp_path):
    other = tmp_path / "x.svs"
    other.write_bytes(b"not a tiff at all")
    ou.check_tiff_segment_sizes(str(other))
    ou.check_tiff_segment_sizes(str(tmp_path / "absent.tif"))


class _TransparentSlide(_FakeReadSlide):
    """Left half transparent (as an OME sparse segment / OpenSlide empty
    region reads), right half opaque red."""

    def __init__(self, width, height, properties=None):
        super().__init__(width, height)
        self.properties = properties if properties is not None else {}

    def read_region(self, location, level, size):
        w, h = int(size[0]), int(size[1])
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.paste((255, 0, 0, 255), (w // 2, 0, w, h))
        return img


def test_read_level_bounded_composites_transparent_pixels_on_white():
    """A bare convert("RGB") renders transparent regions black in the
    thumbnail while the viewer's tiles show them white."""
    img = ou._read_level_bounded(_TransparentSlide(40, 20), 0, (40, 20))
    assert img.getpixel((5, 5)) == (255, 255, 255)
    assert img.getpixel((35, 5)) == (255, 0, 0)


def test_read_level_bounded_strips_composite_on_the_slide_background(monkeypatch):
    monkeypatch.setattr(ou, "_THUMB_READ_BUDGET_PX", 400)  # force the strip path
    slide = _TransparentSlide(60, 60, {"openslide.background-color": "00FF00"})
    img = ou._read_level_bounded(slide, 0, (60, 60))
    assert img.size == (20, 20)  # downsampled by the strip path
    assert img.getpixel((2, 2)) == (0, 255, 0)
    assert img.getpixel((17, 2)) == (255, 0, 0)


@pytest.mark.parametrize("raw", ["", "zzzzzz", "fff", None])
def test_slide_background_falls_back_to_white(raw):
    slide = _TransparentSlide(4, 4, {"openslide.background-color": raw})
    assert ou._slide_background_rgb(slide) == (255, 255, 255)


def test_associated_size_guard_works_without_the_4x_properties(monkeypatch):
    """libopenslide 3.x publishes no openslide.associated.<name>.width/height
    properties, so a guard reading them would see (0, 0) and let a 16000²
    macro be decoded. The size comes from the low-level call, which every
    version has."""
    slide = _StubSlideWithProps(["macro", "label"], {})  # no size properties
    slide._osr = object()
    sizes = {"macro": (16000, 16000), "label": (200, 100)}
    monkeypatch.setattr(
        openslide.lowlevel, "get_associated_image_dimensions",
        lambda osr, name: sizes[name],
    )
    touched = []
    real_getitem = _StubAssoc.__getitem__
    monkeypatch.setattr(_StubAssoc, "__getitem__", lambda self, k: touched.append(k) or real_getitem(self, k))
    with patch.object(openslide, "OpenSlide", return_value=slide):
        out = ou.read_associated_images("/fake/path.svs")
    assert set(out) == {"label"}
    assert touched == ["label"]


def test_associated_dims_fall_back_to_properties_when_lowlevel_fails():
    slide = _StubSlideWithProps(["macro"], {
        "openslide.associated.macro.width": "300", "openslide.associated.macro.height": "200",
    })  # no _osr: the low-level call raises AttributeError
    assert ou._associated_image_dims(slide, "macro") == (300, 200)
    assert ou._associated_image_dims(_StubSlide(["macro"]), "macro") == (0, 0)


def test_check_tiff_segment_sizes_refuses_more_directories_than_it_scans(tmp_path, monkeypatch):
    """Stopping the scan at the page cap would leave the unchecked tail free to
    carry the oversized tile libopenslide aborts on; such a file is refused."""
    import numpy as np
    import tifffile

    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_CHECKED", 5)
    path = tmp_path / "many.tif"
    with tifffile.TiffWriter(str(path)) as tw:
        for _ in range(8):
            tw.write(np.zeros((16, 16), dtype="uint8"), tile=(16, 16), contiguous=False)
    with pytest.raises(ValueError, match="directories"):
        ou.check_tiff_segment_sizes(str(path))
    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_CHECKED", 8)
    ou.check_tiff_segment_sizes(str(path))  # exactly at the cap is fine


def test_page_cap_refuses_on_upload_but_not_when_serving_a_stored_slide(tmp_path, monkeypatch):
    """Only upload validation (strict) refuses a file with that many directories;
    opening an already-stored slide must not refuse a file that is already
    in the library (an oversized tile is refused either way)."""
    import numpy as np
    import tifffile

    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_CHECKED", 5)
    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_STORED", 100)
    path = tmp_path / "many.tif"
    with tifffile.TiffWriter(str(path)) as tw:
        for _ in range(8):
            tw.write(np.zeros((16, 16), dtype="uint8"), tile=(16, 16), contiguous=False)

    with pytest.raises(ValueError, match="directories"):
        ou._open_openslide(str(path), strict=True)
    try:
        ou._open_openslide(str(path), strict=False)
    except ValueError as exc:  # the check must not be what stops a stored slide
        assert "directories" not in str(exc)
    except Exception:
        pass  # OpenSlide itself may decline a bare grayscale TIFF - not the check under test


def test_worker_does_not_log_per_malformed_tag(tmp_path, caplog):
    """The worker parses an upload with tifffile again after the probe child
    (the tile-geometry check, the OME sniff) and on every later open. Its
    ERROR per unparseable tag would write 178 MB to the journal for one crafted
    19 MB file, so tifffile's logger is silenced in the worker too."""
    import logging

    path = tmp_path / "badtags.tif"
    _bad_tags_tiff(path)
    with caplog.at_level(logging.DEBUG):
        ou.check_tiff_segment_sizes(str(path))
        ou.is_ome_tiff(str(path))
    assert not [r for r in caplog.records if r.name.startswith("tifffile")]


# ---------------------------------------------------------------------------
# Slides OpenSlide reads without a usable pyramid: refused at upload, and
# read in bounded blocks when one is already stored.
# ---------------------------------------------------------------------------
class _LevelsSlide:
    def __init__(self, *dims):
        self.level_dimensions = list(dims)


@pytest.mark.parametrize("dims", [
    # Aperio SVS: 4x steps, smallest level 16x down.
    [(46000, 32914), (11500, 8228), (2875, 2057)],
    # A large scan whose 4x pyramid stops at 16x: a 31 Mpx smallest level.
    [(100000, 80000), (25000, 20000), (6250, 5000)],
    # Generic / Philips TIFF, BIF: 2x steps.
    [(4096, 4096), (2048, 2048), (1024, 1024), (512, 512)],
    # A small image with no pyramid at all.
    [(4000, 3000)],
    # Exactly a 16x step.
    [(16384, 16384), (1024, 1024)],
    # 16x steps the writer rounded down: 16.0009x / 16.0003x by raw size.
    [(16385, 16385), (1024, 1024)],
    [(46001, 32914), (2875, 2057)],
])
def test_layout_check_accepts_real_pyramids(dims):
    ou.check_pyramid_layout(_LevelsSlide(*dims))


def test_layout_check_refuses_no_pyramid():
    with pytest.raises(ou.UnsupportedSlideError) as excinfo:
        ou.check_pyramid_layout(_LevelsSlide((40000, 40000)))
    assert excinfo.value.code == "slideLayout.no_pyramid"


def test_layout_check_refuses_a_pyramid_that_stops_too_early_under_its_own_reason():
    """A real 4x pyramid whose smallest level is still over the limit is not
    "no pyramid": the admin is told to add levels, not a pyramid."""
    with pytest.raises(ou.UnsupportedSlideError) as excinfo:
        ou.check_pyramid_layout(_LevelsSlide((130000, 128001), (32500, 32000), (8125, 8000)))
    assert excinfo.value.code == "slideLayout.lowest_level_too_large"


@pytest.mark.parametrize("dims", [
    [(40000, 40000), (20000, 20000), (625, 625)],
    # One pixel past what rounding a 16x level can explain.
    [(16401, 16384), (1024, 1024)],
])
def test_layout_check_refuses_a_level_gap(dims):
    with pytest.raises(ou.UnsupportedSlideError) as excinfo:
        ou.check_pyramid_layout(_LevelsSlide(*dims))
    assert excinfo.value.code == "slideLayout.level_gap"


def _flat_tiff(path, side):
    import numpy as np
    import tifffile

    tifffile.imwrite(
        str(path), np.full((side, side, 3), 200, np.uint8), tile=(64, 64),
        photometric="rgb", compression="zlib",
    )


def test_read_slide_properties_refuses_an_unpyramided_openslide_slide(tmp_path, monkeypatch):
    """A tiled TIFF with no pyramid opens fine in OpenSlide, and every
    low-zoom tile then read the whole base image (a 40000 x 40000 one: 6.4 GB
    of RGBA per tile). Upload validation refuses it, with the reason."""
    monkeypatch.setattr(ou, "_MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS", 256 * 256)
    path = tmp_path / "flat.tif"
    _flat_tiff(path, 320)
    with pytest.raises(ou.UnsupportedSlideError) as excinfo:
        ou.read_slide_properties(str(path))
    assert excinfo.value.reason == "no_pyramid"
    # Serving an already-stored one still opens it.
    ou.open_wsi(str(path)).close()


class _BigFlatSlide:
    """A single-level slide too large to read whole; records every read."""

    def __init__(self, w, h):
        self.level_dimensions = ((w, h),)
        self.level_downsamples = (1.0,)
        self.dimensions = (w, h)
        self.properties = {}
        self.reads: list = []

    def get_best_level_for_downsample(self, _downsample):
        return 0

    def read_region(self, location, level, size):
        self.reads.append((location, size))
        # Shade by position so a misplaced block would show.
        return Image.new("RGBA", size, (location[0] * 255 // self.dimensions[0], 90, 30, 255))


def test_bounded_generator_never_reads_more_than_its_budget(monkeypatch):
    from openslide.deepzoom import DeepZoomGenerator

    from wsi_app import dzi

    # Scaled down 64x per side from a 40000 x 30000 pyramid-less slide.
    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 640 * 640)
    slide = _BigFlatSlide(10000, 7500)
    bounded = dzi.BoundedDeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
    plain = DeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
    for level in range(bounded.level_count - 2):  # every level whose tile reads more than the budget
        slide.reads.clear()
        tile = bounded.get_tile(level, (0, 0))
        try:
            assert slide.reads and all(w * h <= dzi._TILE_READ_BUDGET_PX for _, (w, h) in slide.reads)
            # Exactly the size DeepZoomGenerator's own single read produces.
            assert tile.size == dzi._thumbnail_size(
                plain.get_tile_coordinates(level, (0, 0))[2], plain.get_tile_dimensions(level, (0, 0)),
            )
            assert tile.mode == "RGB"
        finally:
            tile.close()


def test_bounded_generator_matches_deepzoom_on_a_real_slide(tmp_path, monkeypatch):
    """Same tiles, size and content, as DeepZoomGenerator's single read, with
    the block path forced on a small real OpenSlide slide."""
    import numpy as np
    import tifffile
    from openslide.deepzoom import DeepZoomGenerator

    from wsi_app import dzi

    path = tmp_path / "gradient.tif"
    y, x = np.mgrid[0:1000, 0:1200]
    rgb = np.stack([x * 255 // 1199, y * 255 // 999, (x + y) * 255 // 2198], axis=-1).astype(np.uint8)
    tifffile.imwrite(str(path), rgb, tile=(64, 64), photometric="rgb", compression="zlib")
    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 100 * 100)
    slide = ou.open_wsi(str(path))
    try:
        bounded = dzi.BoundedDeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
        plain = DeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
        for level in range(bounded.level_count - 1):
            cols, rows = bounded.level_tiles[level]
            for address in {(0, 0), (cols - 1, rows - 1)}:
                ours, theirs = bounded.get_tile(level, address), plain.get_tile(level, address)
                try:
                    assert ours.size == theirs.size, (level, address)
                    diff = np.abs(np.asarray(ours, np.int16) - np.asarray(theirs, np.int16))
                    assert diff.mean() < 2.0, (level, address, diff.mean())
                finally:
                    ours.close()
                    theirs.close()
    finally:
        slide.close()


def test_bounded_generator_does_not_stretch_towards_the_right_and_bottom_edges(tmp_path, monkeypatch):
    """A region whose size isn't a multiple of the reduce factor leaves a
    partial cell in the canvas's last column / row. Resized as a whole
    canvas pixel it would stretch the tile by up to ~0.5 px towards the right
    and bottom edges, so the columns there would drift from DeepZoomGenerator's
    one-read tile on a steep gradient; mapped by its true extent it matches."""
    import numpy as np
    import tifffile
    from openslide.deepzoom import DeepZoomGenerator

    from wsi_app import dzi

    path = tmp_path / "ramp.tif"
    # Odd sizes, so no reduce factor divides them; a steep ramp on each axis.
    y, x = np.mgrid[0:1001, 0:1203]
    rgb = np.stack([x * 255 // 1202, y * 255 // 1000, np.full_like(x, 128)], axis=-1).astype(np.uint8)
    tifffile.imwrite(str(path), rgb, tile=(64, 64), photometric="rgb", compression="zlib")
    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 100 * 100)
    slide = ou.open_wsi(str(path))
    try:
        bounded = dzi.BoundedDeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
        plain = DeepZoomGenerator(slide, tile_size=254, overlap=1, limit_bounds=False)
        # Mean drift of the last column (red ramps along x) and the last row
        # (green ramps along y) per tile: must be 0 (an unbounded edge read
        # drifts by 1-3 levels of 255).
        drift = {}
        for level in range(bounded.level_count):
            cols, rows = bounded.level_tiles[level]
            address = (cols - 1, rows - 1)
            w, h = plain.get_tile_coordinates(level, address)[2]
            out_w, out_h = plain.get_tile_dimensions(level, address)
            # Only tiles the block path renders with a reduce factor > 1, and
            # large enough for a column to mean something (the 1-4 px top
            # tiles differ by filter support alone).
            if (w * h <= dzi._TILE_READ_BUDGET_PX or min(out_w, out_h) < 16
                    or min(w // (2 * out_w), h // (2 * out_h)) < 2):
                continue
            ours, theirs = bounded.get_tile(level, address), plain.get_tile(level, address)
            try:
                diff = np.abs(np.asarray(ours, np.int16) - np.asarray(theirs, np.int16))
                drift[level] = (diff[:, -1, 0].mean(), diff[-1, :, 1].mean())
            finally:
                ours.close()
                theirs.close()
        assert drift
        assert max(max(d) for d in drift.values()) < 0.5, drift
    finally:
        slide.close()


def test_bounded_renders_are_one_at_a_time_and_once_per_tile(monkeypatch):
    """A block-read tile on a flat slide can cost a minute of CPU and
    ~160 MB, and nginx lets concurrent misses through after 10 s. Renders
    run one at a time per process, requests queued behind a render of the
    same tile get its result, and ordinary tiles never wait for either."""
    import math
    import threading
    import time

    from wsi_app import dzi

    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 640 * 640)
    # Twelve requests at once: above the waiter cap, which has its own test.
    monkeypatch.setattr(dzi, "_BOUNDED_MAX_QUEUED", 100)
    state = {"active": 0, "peak": 0, "renders": []}
    guard = threading.Lock()
    real = dzi._read_region_bounded

    def slow_render(osr, location, level, size, out_box, background):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["renders"].append((id(osr), location, size))
        try:
            time.sleep(0.05)
            return real(osr, location, level, size, out_box, background)
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(dzi, "_read_region_bounded", slow_render)
    slides = [_BigFlatSlide(10000, 7500), _BigFlatSlide(10000, 7500)]
    gens = [dzi.BoundedDeepZoomGenerator(s, tile_size=254, overlap=1, limit_bounds=False) for s in slides]
    bounded_levels = [
        lv for lv in range(gens[0].level_count)
        if math.prod(gens[0].get_tile_coordinates(lv, (0, 0))[2]) > dzi._TILE_READ_BUDGET_PX
    ]
    assert len(bounded_levels) >= 2
    # Six viewers of each slide, all asking for the same two low-zoom tiles.
    jobs = [(g, lv) for g in gens for lv in bounded_levels[-2:] for _ in range(3)]
    sizes, errors = [], []

    def fetch(g, lv):
        try:
            tile = g.get_tile(lv, (0, 0))
            sizes.append(tile.size)
            tile.close()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=fetch, args=job) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(sizes) == len(jobs)
    assert state["peak"] == 1, "bounded renders must not overlap"
    # Once per distinct tile, however many asked for it at once.
    assert len(state["renders"]) == len(set(state["renders"])) == 4

    # An ordinary tile does not queue behind a bounded render in progress.
    started, release = threading.Event(), threading.Event()

    def blocked_render(*args):
        started.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(dzi, "_read_region_bounded", blocked_render)
    holder = threading.Thread(target=fetch, args=(gens[0], bounded_levels[0]))
    holder.start()
    try:
        assert started.wait(10)
        top = gens[0].level_count - 1
        tile = gens[0].get_tile(top, (0, 0))
        tile.close()
        assert not release.is_set()
    finally:
        release.set()
        holder.join(30)
    assert not errors


def test_bounded_tile_requests_past_the_cap_are_refused_not_queued(monkeypatch):
    """Renders are serial and slow, so a worker lets only _BOUNDED_MAX_QUEUED
    block-read requests wait at once; the next is refused straight away
    (BoundedRenderBusy, a 503 at the endpoint) rather than holding one more
    threadpool thread for minutes. The count comes back down as they finish."""
    import math
    import threading

    from wsi_app import dzi

    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 640 * 640)
    monkeypatch.setattr(dzi, "_BOUNDED_MAX_QUEUED", 2)
    started, release = threading.Event(), threading.Event()
    real = dzi._read_region_bounded

    def blocked_render(*args):
        started.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(dzi, "_read_region_bounded", blocked_render)
    g = dzi.BoundedDeepZoomGenerator(_BigFlatSlide(10000, 7500), tile_size=254, overlap=1, limit_bounds=False)
    lv = next(
        lv for lv in range(g.level_count)
        if math.prod(g.get_tile_coordinates(lv, (0, 0))[2]) > dzi._TILE_READ_BUDGET_PX
    )
    results = []

    def fetch():
        try:
            g.get_tile(lv, (0, 0)).close()
            results.append("ok")
        except Exception as exc:  # surfaced below
            results.append(exc)

    holders = [threading.Thread(target=fetch) for _ in range(2)]
    holders[0].start()
    try:
        assert started.wait(10)
        holders[1].start()
        # Wait until the second request is counted too (it queues on the lock).
        for _ in range(500):
            with dzi._bounded_queued_lock:
                if dzi._bounded_queued == 2:
                    break
            threading.Event().wait(0.01)
        with pytest.raises(dzi.BoundedRenderBusy):
            g.get_tile(lv, (0, 0))
    finally:
        release.set()
        for t in holders:
            t.join(30)
    assert results == ["ok", "ok"]
    assert dzi._bounded_queued == 0
    # With the queue drained, the same tile is served again (from the memo).
    g.get_tile(lv, (0, 0)).close()


def _flat_tiff_bytes(tmp_path, name):
    path = tmp_path / name
    _flat_tiff(path, 320)
    return path.read_bytes()


def test_upload_and_import_refuse_an_unpyramided_slide_with_its_reason(
    client, admin_headers, db_session, tmp_path, monkeypatch,
):
    import io
    import os
    import uuid

    from wsi_app import slide_transfer as st
    from wsi_app.admin_helpers import mint_public_id
    from wsi_app.config import settings
    from wsi_app.models import Slide

    for sub in ("wsi", "thumbs"):
        os.makedirs(os.path.join(settings.data_dir, sub), exist_ok=True)
    monkeypatch.setattr(ou, "_MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS", 256 * 256)
    name = f"flat-{uuid.uuid4().hex[:8]}.tif"
    before = db_session.query(Slide).count()
    r = client.post(
        "/api/admin/slides/upload", headers=admin_headers,
        files={"file": (name, io.BytesIO(_flat_tiff_bytes(tmp_path, "up.tif")), "application/octet-stream")},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "adminDash.apiErr.slideLayout.no_pyramid"}
    db_session.expire_all()
    assert db_session.query(Slide).count() == before
    assert not [f for f in os.listdir(os.path.join(settings.data_dir, "wsi")) if name in f]

    # The import reports the same reason (adminDash.apiErr.<code> in the UI).
    pid = mint_public_id()
    staged = tmp_path / "stage" / "wsi" / pid
    staged.mkdir(parents=True)
    (staged / "flat.tif").write_bytes(_flat_tiff_bytes(tmp_path, "imp.tif"))
    sd = {"public_id": pid, "wsi_original_basename": f"flat-{uuid.uuid4().hex[:8]}.tif", "title": "x"}
    with pytest.raises(st.SlideTransferError) as excinfo:
        st._import_one_slide(db_session, str(tmp_path / "stage"), sd, {}, "copy")
    assert excinfo.value.code == "slideLayout.no_pyramid"


# ---------------------------------------------------------------------------
# The OME-TIFF route gets the same directory cap as the OpenSlide one
# ---------------------------------------------------------------------------
def _ifd_chain_tiff(path, n_ifd, *, big=False, order="<"):
    """A TIFF of ``n_ifd`` directories of one tag each (NewSubfileType)."""
    import struct

    if big:
        buf = bytearray((b"II" if order == "<" else b"MM") + struct.pack(order + "HHHQ", 43, 8, 0, 16))
        entry, count_fmt, next_fmt, hdr = 20, "Q", "Q", 16
        pack_entry = lambda: struct.pack(order + "HHQQ", 254, 4, 1, 0)  # noqa: E731
    else:
        buf = bytearray((b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8))
        entry, count_fmt, next_fmt, hdr = 12, "H", "I", 8
        pack_entry = lambda: struct.pack(order + "HHII", 254, 4, 1, 0)  # noqa: E731
    size = struct.calcsize(order + count_fmt) + entry + struct.calcsize(order + next_fmt)
    for i in range(n_ifd):
        here = hdr + i * size
        nxt = here + size if i < n_ifd - 1 else 0
        buf += struct.pack(order + count_fmt, 1) + pack_entry() + struct.pack(order + next_fmt, nxt)
    path.write_bytes(bytes(buf))


@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("order", ["<", ">"])
def test_directory_count_is_exact_around_the_limit(tmp_path, big, order):
    p = tmp_path / "chain.tif"
    _ifd_chain_tiff(p, 7, big=big, order=order)
    assert ou.tiff_directory_count_exceeds(str(p), 6) is True
    assert ou.tiff_directory_count_exceeds(str(p), 7) is False
    assert ou.tiff_directory_count_exceeds(str(p), 100) is False


def test_directory_count_treats_a_cyclic_chain_as_too_long(tmp_path):
    import struct

    p = tmp_path / "loop.tif"
    # One directory whose "next" points back at itself.
    p.write_bytes(b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 0) + struct.pack("<I", 8))
    assert ou.tiff_directory_count_exceeds(str(p), 10**6) is True


def test_directory_count_leaves_unreadable_files_to_the_readers(tmp_path):
    (tmp_path / "x.tif").write_bytes(b"not a tiff at all")
    assert ou.tiff_directory_count_exceeds(str(tmp_path / "x.tif"), 5) is False
    assert ou.tiff_directory_count_exceeds(str(tmp_path / "absent.tif"), 5) is False
    (tmp_path / "trunc.tif").write_bytes(b"II*\x00\x08\x00\x00\x00\x05")  # entry count, then nothing
    assert ou.tiff_directory_count_exceeds(str(tmp_path / "trunc.tif"), 5) is False


def _ome_with_long_chain(path, extra_ifds):
    """A small valid OME-TIFF with ``extra_ifds`` one-tag directories chained
    after its last page - what makes the OME reader parse a long chain."""
    import struct

    import numpy as np
    import tifffile

    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(np.zeros((8, 8, 3), dtype="uint8"), photometric="rgb")
    data = bytearray(path.read_bytes())
    assert data[:2] == b"II" and struct.unpack("<H", data[2:4])[0] == 42
    off = struct.unpack("<I", data[4:8])[0]
    while True:
        n = struct.unpack("<H", data[off:off + 2])[0]
        nxt_pos = off + 2 + 12 * n
        (nxt,) = struct.unpack("<I", data[nxt_pos:nxt_pos + 4])
        if nxt == 0:
            break
        off = nxt
    base = len(data)
    struct.pack_into("<I", data, nxt_pos, base)
    for i in range(extra_ifds):
        nxt = base + (i + 1) * 18 if i < extra_ifds - 1 else 0
        data += struct.pack("<H", 1) + struct.pack("<HHII", 254, 4, 1, 0) + struct.pack("<I", nxt)
    path.write_bytes(bytes(data))


def test_ome_tiff_with_an_endless_directory_chain_is_refused_before_it_is_parsed(tmp_path, monkeypatch):
    from wsi_app import ome_tiff

    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_CHECKED", 20)
    monkeypatch.setattr(ou, "_MAX_TIFF_PAGES_STORED", 60)
    path = tmp_path / "many.ome.tif"
    _ome_with_long_chain(path, 100)
    assert ome_tiff.is_ome_tiff(str(path))

    parsed = []
    monkeypatch.setattr(ou, "OmeTiffSlide", lambda p: parsed.append(p))
    # Upload validation (strict) and serving a stored slide each refuse it at
    # their own bound - and neither gets as far as the OME reader.
    for strict, cap in ((True, 20), (False, 60)):
        with pytest.raises(ValueError, match=f"more than {cap} directories"):
            ou.open_wsi(str(path), strict_ome=strict)
    assert parsed == []


def test_ome_tiff_within_the_directory_cap_still_opens(tmp_path):
    path = tmp_path / "ok.ome.tif"
    _ome_with_long_chain(path, 0)
    slide = ou.open_wsi(str(path), strict_ome=True)
    try:
        assert slide.dimensions == (8, 8)
    finally:
        slide.close()


def test_a_crafted_offset_or_entry_count_is_unreadable_not_an_exception(tmp_path):
    """The walker's contract is "anything it cannot read is not exceeding":
    offsets and entry counts beyond what seek() can take must not raise."""
    import struct

    # BigTIFF, first directory at an offset past 2**63.
    p = tmp_path / "huge-offset.tif"
    p.write_bytes(b"II" + struct.pack("<HHHQ", 43, 8, 0, 2**63 + 5))
    assert ou.tiff_directory_count_exceeds(str(p), 5) is False

    # BigTIFF, one directory whose entry count is 2**62.
    q = tmp_path / "huge-count.tif"
    q.write_bytes(b"II" + struct.pack("<HHHQ", 43, 8, 0, 16) + struct.pack("<Q", 2**62))
    assert ou.tiff_directory_count_exceeds(str(q), 5) is False
