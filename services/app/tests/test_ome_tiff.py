"""OME-TIFF reader: structure, pixels, metadata, refusals and the upload path.

Fixtures are written with tifffile at test time (small images, a few pyramid
levels), so the suite needs no sample-data download. Lossless compression is
used wherever pixels are compared exactly against the source array.
"""
from __future__ import annotations

import io
import os
import random
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import openslide
import pytest
import tifffile
from openslide.deepzoom import DeepZoomGenerator

from wsi_app import ome_tiff
from wsi_app import openslide_utils
from wsi_app.ome_tiff import OmeTiffSlide, UnsupportedOmeTiffError, is_ome_tiff


# ---------------------------------------------------------------------------
# Fixture writers
# ---------------------------------------------------------------------------


def _rgb(h: int, w: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    return np.stack([xx % 251, yy % 241, (xx * 3 + yy) % 239], -1).astype(np.uint8)


def _down(a: np.ndarray, planes_first: bool) -> np.ndarray:
    return a[..., ::2, ::2] if planes_first else a[::2, ::2]


def _write_pyramid(
    path, base, *, axes, levels=2, tile=(256, 256), compression="zlib",
    photometric=None, planarconfig=None, metadata=None, description=None,
    extra_series=(), ome=True, colormap=None,
):
    """Write ``base`` plus ``levels`` 2x-reduced SubIFD levels."""
    planes_first = axes[0] in "CZS" and len(axes) == 3 and axes[-1] != "S"
    pyramid = [base]
    for _ in range(levels):
        pyramid.append(_down(pyramid[-1], planes_first))
    common = dict(compression=compression, photometric=photometric, planarconfig=planarconfig)
    if tile is not None:
        common["tile"] = tile
    if colormap is not None:
        common["colormap"] = colormap
    with tifffile.TiffWriter(path, bigtiff=True, ome=ome) as tw:
        first_meta = {"axes": axes, **(metadata or {})} if description is None else None
        tw.write(
            pyramid[0], subifds=levels or None, metadata=first_meta,
            description=description, **common,
        )
        for lv in pyramid[1:]:
            tw.write(lv, subfiletype=1, metadata=None, **common)
        for name, arr in extra_series:
            tw.write(arr, photometric="rgb", metadata={"axes": "YXS", "Name": name})
    return pyramid


def _ome_xml(width, height, *, size_c=3, spp=3, pixel_type="uint8",
             physical=("250", "nm"), objective="40.0",
             acquired="2021-05-06T16:08:09+09:00", tiffdata='<TiffData IFD="0" PlaneCount="1"/>',
             root_extra="", doctype=""):
    channels = "".join(
        f'<Channel ID="Channel:0:{i}" SamplesPerPixel="{spp}"/>' for i in range(max(1, size_c // spp))
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>{doctype}'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" '
        'UUID="urn:uuid:00000000-0000-0000-0000-000000000001">'
        f"{root_extra}"
        '<Instrument ID="Instrument:0"><Microscope Manufacturer="Acme" Model="Scope 1"/>'
        f'<Objective ID="Objective:0:0" NominalMagnification="{objective}"/></Instrument>'
        '<Image ID="Image:0" Name="slide">'
        f"<AcquisitionDate>{acquired}</AcquisitionDate>"
        '<InstrumentRef ID="Instrument:0"/><ObjectiveSettings ID="Objective:0:0"/>'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="{pixel_type}" '
        f'SizeX="{width}" SizeY="{height}" SizeC="{size_c}" SizeZ="1" SizeT="1" '
        f'PhysicalSizeX="{physical[0]}" PhysicalSizeXUnit="{physical[1]}" '
        f'PhysicalSizeY="{physical[0]}" PhysicalSizeYUnit="{physical[1]}" Interleaved="true">'
        f"{channels}{tiffdata}</Pixels></Image></OME>"
    )


@pytest.fixture
def rgb_pyramid(tmp_path):
    base = _rgb(900, 1200)
    path = tmp_path / "rgb.ome.tif"
    pyramid = _write_pyramid(path, base, axes="YXS", photometric="rgb",
                             metadata={"PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5})
    return str(path), pyramid


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_sniff_is_content_based(tmp_path):
    base = _rgb(300, 400)
    no_suffix = tmp_path / "plain-name.tif"
    _write_pyramid(no_suffix, base, axes="YXS", photometric="rgb", levels=1)
    assert is_ome_tiff(str(no_suffix)) is True

    plain = tmp_path / "plain.tif"
    tifffile.imwrite(plain, base, photometric="rgb", tile=(256, 256))
    assert is_ome_tiff(str(plain)) is False

    # Other TIFF-based vendor suffixes are never sniffed (they go straight to
    # OpenSlide), even when the bytes happen to be OME.
    svs = tmp_path / "ome-bytes.svs"
    svs.write_bytes(no_suffix.read_bytes())
    assert is_ome_tiff(str(svs)) is False

    assert is_ome_tiff(str(tmp_path / "missing.ome.tif")) is False
    garbage = tmp_path / "garbage.ome.tif"
    garbage.write_bytes(b"not a tiff at all")
    assert is_ome_tiff(str(garbage)) is False


# ---------------------------------------------------------------------------
# Structure, metadata, pixels
# ---------------------------------------------------------------------------


def test_subifd_pyramid_levels_and_openslide_properties(rgb_pyramid):
    path, pyramid = rgb_pyramid
    with OmeTiffSlide(path) as s:
        assert s.level_count == 3
        assert s.level_dimensions == ((1200, 900), (600, 450), (300, 225))
        assert s.level_downsamples == (1.0, 2.0, 4.0)
        assert s.properties[openslide.PROPERTY_NAME_VENDOR] == "ome-tiff"
        assert float(s.properties[openslide.PROPERTY_NAME_MPP_X]) == pytest.approx(0.5)
        assert s.properties["openslide.level[1].tile-width"] == "256"
        assert s.properties["ome.color-mode"] == "rgb"
        assert s.get_best_level_for_downsample(0.5) == 0
        assert s.get_best_level_for_downsample(3.0) == 1
        assert s.get_best_level_for_downsample(100) == 2
        assert OmeTiffSlide.detect_format(path) == "ome-tiff"


def test_ome_xml_calibration_objective_and_date(tmp_path):
    base = _rgb(600, 800)
    path = tmp_path / "custom.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", levels=1, ome=False,
                   description=_ome_xml(800, 600))
    props = openslide_utils.read_slide_properties(str(path))
    assert props["vendor"] == "ome-tiff"
    assert (props["width"], props["height"]) == (800, 600)
    assert props["mpp_x"] == pytest.approx(0.25)  # 250 nm
    assert props["mpp_y"] == pytest.approx(0.25)
    assert props["objective_power"] == pytest.approx(40.0)
    # +09:00 local time normalised to UTC-naive.
    assert props["acquired_at"] == "2021-05-06T07:08:09"
    with OmeTiffSlide(str(path)) as s:
        assert s.properties["ome.microscope-manufacturer"] == "Acme"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2021-05-06T07:08:09Z", "2021-05-06T07:08:09"),
        ("2021-05-06T07:08:09.1234567", "2021-05-06T07:08:09"),
        # Python 3.10's fromisoformat takes exactly 3 or 6 fractional digits.
        ("2021-05-06T07:08:09.12Z", "2021-05-06T07:08:09"),
        ("2021-05-06T07:08:09.123456+00:00", "2021-05-06T07:08:09"),
        ("2021-05-06T16:08:09+09:00", "2021-05-06T07:08:09"),
        ("not a date", ""),
    ],
)
def test_acquired_at_from_ome_dates(raw, expected):
    assert openslide_utils._extract_acquired_at({"ome.acquisition-date": raw}) == expected


@pytest.mark.parametrize("value, unit, expected", [
    ("0.25", None, 0.25), ("0.25", "µm", 0.25), ("0.25", "μm", 0.25),
    ("250", "nm", 0.25), ("0.00025", "mm", 0.25), ("2500", "Å", 0.25),
    ("1", "parsec", 0.0), ("-1", "µm", 0.0), ("abc", "µm", 0.0), ("0", "µm", 0.0),
])
def test_length_unit_conversion(value, unit, expected):
    assert ome_tiff._length_um(value, unit) == pytest.approx(expected)


def _region(slide, loc, level, size):
    return np.asarray(slide.read_region(loc, level, size))


def test_read_region_matches_source_interleaved(rgb_pyramid):
    path, pyramid = rgb_pyramid
    with OmeTiffSlide(path) as s:
        # Crosses tile boundaries at level 0.
        reg = _region(s, (200, 230), 0, (400, 300))
        assert (reg[..., 3] == 255).all()
        np.testing.assert_array_equal(reg[..., :3], pyramid[0][230:530, 200:600])
        # Level 1: location is in level-0 pixels.
        reg1 = _region(s, (400, 100), 1, (250, 200))
        np.testing.assert_array_equal(reg1[..., :3], pyramid[1][50:250, 200:450])


def test_read_region_outside_image_is_transparent(rgb_pyramid):
    path, pyramid = rgb_pyramid
    with OmeTiffSlide(path) as s:
        reg = _region(s, (1190, 890), 0, (30, 30))
        assert (reg[:10, :10, 3] == 255).all()
        np.testing.assert_array_equal(reg[:10, :10, :3], pyramid[0][890:900, 1190:1200])
        assert (reg[10:, :, 3] == 0).all() and (reg[:, 10:, 3] == 0).all()
        assert (np.asarray(s.read_region((-50, -50), 0, (20, 20)))[..., 3] == 0).all()
        assert s.read_region((0, 0), 0, (0, 5)).size == (0, 5)
        with pytest.raises(openslide.OpenSlideError):
            s.read_region((0, 0), 0, (-1, 5))


def test_planar_separate_samples(tmp_path):
    base = _rgb(700, 900)
    planar = np.moveaxis(base, -1, 0).copy()
    path = tmp_path / "planar.ome.tif"
    _write_pyramid(path, planar, axes="SYX", photometric="rgb", planarconfig="separate")
    with OmeTiffSlide(str(path)) as s:
        reg = _region(s, (100, 250), 0, (500, 300))
        np.testing.assert_array_equal(reg[..., :3], base[250:550, 100:600])


def _patch_photometric(path, value: int) -> None:
    """Rewrite PhotometricInterpretation on every page and SubIFD in place."""
    with tifffile.TiffFile(path, mode="r+b") as tf:
        for page in tf.pages:
            page.tags["PhotometricInterpretation"].overwrite(value)
            for level, offset in enumerate(page.subifds or (), start=1):
                tf.filehandle.seek(offset)
                sub = tifffile.TiffPage(tf, index=(page.index, level))
                sub.tags["PhotometricInterpretation"].overwrite(value)


def test_bioformats_planar_jpeg_layout(tmp_path):
    """Bio-Formats (and QuPath through it) writes JPEG RGB as three separate
    single-component JPEG planes (PlanarConfiguration=2) but tags the page
    Photometric=YCbCr. The planes are R, G, B; tifffile refuses to decode
    the combination, so the reader has to decode the planes itself."""
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (600, 800, 3), dtype=np.uint8)
    label = np.full((150, 200, 3), 200, np.uint8)
    path = tmp_path / "bioformats.ome.tif"
    with tifffile.TiffWriter(path, ome=True) as tw:
        common = dict(compression="jpeg", photometric="rgb", planarconfig="separate")
        tw.write(np.moveaxis(base, -1, 0).copy(), subifds=1, tile=(256, 256), metadata={"axes": "SYX"}, **common)
        tw.write(np.moveaxis(base[::2, ::2], -1, 0).copy(), subfiletype=1, tile=(256, 256), **common)
        tw.write(np.moveaxis(label, -1, 0).copy(), metadata={"axes": "SYX", "Name": "label image"}, **common)
    _patch_photometric(path, 6)
    with tifffile.TiffFile(path) as tf:
        kf = tf.series[0].keyframe
        assert (int(kf.photometric), int(kf.planarconfig)) == (6, 2)
    with OmeTiffSlide(str(path)) as s:
        assert s.properties["ome.color-mode"] == "rgb"
        assert s.level_dimensions == ((800, 600), (400, 300))
        reg = _region(s, (0, 0), 0, (800, 600))[..., :3].astype(int)
        # JPEG is lossy; decoding the planes as YCbCr would be off by far more.
        assert np.abs(reg - base.astype(int)).mean() < 8
        assert list(s.associated_images) == ["label"]
    out = openslide_utils.read_associated_images(str(path))
    assert set(out) == {"label"}


def test_three_uint8_channel_planes_render_as_rgb(tmp_path):
    base = _rgb(700, 900)
    planes = np.moveaxis(base, -1, 0).copy()
    path = tmp_path / "c3.ome.tif"
    _write_pyramid(path, planes, axes="CYX", photometric="minisblack")
    with OmeTiffSlide(str(path)) as s:
        assert s.properties["ome.color-mode"] == "rgb"
        reg = _region(s, (0, 0), 0, (900, 700))
        np.testing.assert_array_equal(reg[..., :3], base)


def test_stripped_pyramid(tmp_path):
    base = _rgb(700, 900)
    path = tmp_path / "strips.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", tile=None)
    with tifffile.TiffFile(path) as tf:
        assert not tf.series[0].keyframe.is_tiled
    with OmeTiffSlide(str(path)) as s:
        reg = _region(s, (123, 456), 0, (321, 200))
        np.testing.assert_array_equal(reg[..., :3], base[456:656, 123:444])


def test_gray16_uses_one_deterministic_window(tmp_path):
    yy, xx = np.mgrid[0:800, 0:1000]
    base = (1000 + (xx + yy) % 1000).astype(np.uint16)
    path = tmp_path / "gray16.ome.tif"
    _write_pyramid(path, base, axes="YX", photometric="minisblack", compression="lzw")
    with OmeTiffSlide(str(path)) as a, OmeTiffSlide(str(path)) as b:
        assert a.properties["ome.color-mode"] == "gray"
        assert a.properties["ome.window-low"] == b.properties["ome.window-low"]
        assert a.properties["ome.window-high"] == b.properties["ome.window-high"]
        lo = float(a.properties["ome.window-low"])
        hi = float(a.properties["ome.window-high"])
        assert 1000 <= lo < hi <= 1999
        reg = _region(a, (0, 0), 0, (1000, 800))
        # Gray is replicated into R, G and B, and the window spans the range.
        assert (reg[..., 0] == reg[..., 1]).all() and (reg[..., 1] == reg[..., 2]).all()
        assert reg[..., 0].min() == 0 and reg[..., 0].max() == 255
        # Same source value -> same output value on every level (no seams):
        # level-1 pixel (10, 10) was sampled from level-0 pixel (20, 20).
        level1 = _region(a, (20, 20), 1, (1, 1))[0, 0, 0]
        level0 = _region(a, (20, 20), 0, (1, 1))[0, 0, 0]
        assert level0 == level1


def test_rgb16_scaled_to_bit_depth(tmp_path):
    base = (_rgb(600, 800).astype(np.uint16) * 16)  # 12-bit data, max < 4096
    base[0, 0] = (4000, 0, 16)
    path = tmp_path / "rgb16.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", levels=1)
    with OmeTiffSlide(str(path)) as s:
        assert float(s.properties["ome.window-low"]) == 0.0
        assert float(s.properties["ome.window-high"]) == 4095.0
        px = _region(s, (0, 0), 0, (1, 1))[0, 0, :3]
        assert list(px) == [round(4000 * 255 / 4095), 0, 1]


def test_miniswhite_is_inverted(tmp_path):
    base = np.zeros((600, 800), np.uint8)
    base[:, 400:] = 200
    path = tmp_path / "white.ome.tif"
    _write_pyramid(path, base, axes="YX", photometric="miniswhite", levels=1)
    with OmeTiffSlide(str(path)) as s:
        reg = _region(s, (0, 0), 0, (800, 600))
        assert (reg[:, :400, 0] == 255).all() and (reg[:, 400:, 0] == 55).all()


def test_z_stack_shows_first_plane(tmp_path):
    stack = np.stack([np.full((512, 512, 3), 10, np.uint8), np.full((512, 512, 3), 200, np.uint8)])
    path = tmp_path / "z.ome.tif"
    tifffile.imwrite(path, stack, photometric="rgb", tile=(256, 256), metadata={"axes": "ZYXS"}, ome=True)
    with OmeTiffSlide(str(path)) as s:
        assert s.level_count == 1
        assert (_region(s, (0, 0), 0, (512, 512))[..., :3] == 10).all()


def test_label_and_macro_series_become_associated_images(tmp_path, caplog):
    base = _rgb(900, 1200)
    path = tmp_path / "assoc.ome.tif"
    label = np.full((300, 400, 3), 200, np.uint8)
    macro = np.full((500, 1500, 3), 90, np.uint8)
    _write_pyramid(path, base, axes="YXS", photometric="rgb",
                   extra_series=[("label", label), ("macro image", macro)])
    with OmeTiffSlide(str(path)) as s:
        assert set(s.associated_images) == {"label", "macro"}
        assert s.associated_images["label"].size == (400, 300)
        assert s.associated_images["macro"].getpixel((0, 0)) == (90, 90, 90)
    caplog.set_level("INFO")
    out = openslide_utils.read_associated_images(str(path))
    assert set(out) == {"label", "macro"}
    assert all(v.startswith(b"\xff\xd8") for v in out.values())
    # The Aperio-convention PIL IFD walk is skipped for OME-TIFF: it would
    # log a WARNING (mirrored into the admin error log) for nothing.
    assert not [r for r in caplog.records if r.levelname == "WARNING" and r.name.startswith("wsi_app")]


def test_deepzoom_generates_every_level(rgb_pyramid):
    path, _ = rgb_pyramid
    with OmeTiffSlide(path) as s:
        dz = DeepZoomGenerator(s, tile_size=256, overlap=1, limit_bounds=False)
        for level in range(dz.level_count):
            cols, rows = dz.level_tiles[level]
            for address in {(0, 0), (cols - 1, rows - 1)}:
                tile = dz.get_tile(level, address)
                # DeepZoomGenerator shrinks with an aspect-preserving
                # thumbnail(), which can land 1 px short of the nominal tile
                # size on small levels — OpenSlide-backed slides do the same.
                expected = dz.get_tile_dimensions(level, address)
                assert abs(tile.size[0] - expected[0]) <= 1 and abs(tile.size[1] - expected[1]) <= 1
                assert tile.mode == "RGB"
        assert '<Size Width="1200" Height="900" />' in dz.get_dzi("jpeg")


def test_concurrent_reads_match_sequential(rgb_pyramid):
    path, _ = rgb_pyramid
    rnd = random.Random(7)
    jobs = [((rnd.randrange(1200), rnd.randrange(900)), rnd.randrange(3), (rnd.randrange(1, 300), rnd.randrange(1, 300)))
            for _ in range(120)]
    with OmeTiffSlide(path) as s:
        expected = [_region(s, *j).tobytes() for j in jobs]
    ome_tiff._segment_cache.drop_owner(-1)
    with OmeTiffSlide(path) as s, ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda j: _region(s, *j).tobytes(), jobs))
    assert got == expected


def test_close_releases_cached_segments(rgb_pyramid):
    path, _ = rgb_pyramid
    s = OmeTiffSlide(path)
    s.read_region((0, 0), 0, (600, 600))
    assert any(k[0] == s._uid for k in list(ome_tiff._segment_cache._data))
    s.close()
    assert not any(k[0] == s._uid for k in list(ome_tiff._segment_cache._data))
    with pytest.raises(ValueError):
        s.read_region((0, 0), 0, (10, 10))
    s.close()  # idempotent


def test_segment_cache_is_byte_bounded():
    cache = ome_tiff._SegmentCache(1000)
    for i in range(5):
        cache.put((1, 0, i), np.zeros(400, np.uint8))
    assert cache.stats()["bytes"] <= 1000
    assert cache.get((1, 0, 0)) is ome_tiff._MISS
    assert cache.get((1, 0, 4)) is not ome_tiff._MISS
    cache.put((2, 0, 0), None)
    assert cache.get((2, 0, 0)) is None
    cache.drop_owner(1)
    assert cache.stats()["entries"] == 1
    too_big = ome_tiff._SegmentCache(10)
    too_big.put((1, 0, 0), np.zeros(100, np.uint8))
    assert too_big.stats()["entries"] == 0


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _reason(path) -> str:
    with pytest.raises(UnsupportedOmeTiffError) as excinfo:
        OmeTiffSlide(str(path))
    return excinfo.value.reason


def test_multichannel_fluorescence_is_refused(tmp_path):
    four = np.zeros((4, 512, 512), np.uint16)
    p4 = tmp_path / "fluo4.ome.tif"
    _write_pyramid(p4, four, axes="CYX", photometric="minisblack", levels=1)
    assert _reason(p4) == "multichannel"

    two = np.zeros((2, 512, 512), np.uint8)
    p2 = tmp_path / "fluo2.ome.tif"
    _write_pyramid(p2, two, axes="CYX", photometric="minisblack", levels=1)
    assert _reason(p2) == "multichannel"

    # Three 16-bit channels are fluorescence, not brightfield RGB planes.
    three16 = np.zeros((3, 512, 512), np.uint16)
    p3 = tmp_path / "fluo3.ome.tif"
    _write_pyramid(p3, three16, axes="CYX", photometric="minisblack", levels=1)
    assert _reason(p3) == "multichannel"


def test_palette_is_refused(tmp_path):
    path = tmp_path / "palette.ome.tif"
    _write_pyramid(path, np.zeros((512, 512), np.uint8), axes="YX", photometric="palette",
                   levels=1, colormap=np.zeros((3, 256), np.uint16))
    assert _reason(path) == "pixel_type"


def test_unpyramided_image_over_the_cap_is_refused(tmp_path, monkeypatch):
    small = tmp_path / "small.ome.tif"
    tifffile.imwrite(small, _rgb(400, 500), photometric="rgb", metadata={"axes": "YXS"}, ome=True)
    with OmeTiffSlide(str(small)) as s:
        assert s.level_count == 1

    monkeypatch.setattr(ome_tiff, "_MAX_LOWEST_LEVEL_PIXELS", 100_000)
    assert _reason(small) == "no_pyramid"
    # A pyramid whose smallest level is still over the cap is refused too,
    # under its own reason: it has a pyramid, just not deep enough.
    shallow = tmp_path / "shallow.ome.tif"
    _write_pyramid(shallow, _rgb(900, 1200), axes="YXS", photometric="rgb", levels=1)
    assert _reason(shallow) == "lowest_level_too_large"


def _two_level(path, base, child):
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(base, subifds=1, tile=(256, 256), photometric="rgb", metadata={"axes": "YXS"})
        tw.write(child, subfiletype=1, tile=(64, 64), photometric="rgb")


def test_levels_too_far_apart_are_refused(tmp_path):
    base = _rgb(2048, 2048)
    path = tmp_path / "gap.ome.tif"
    _two_level(path, base, base[::32, ::32].copy())
    assert _reason(path) == "level_gap"


def test_an_exact_16x_step_rounded_down_is_accepted(tmp_path):
    """16x down from 2063 px is 128.9 px, written as 128: 16.1x by raw size,
    but a 16x level all the same."""
    base = _rgb(2063, 2063)
    path = tmp_path / "floor16.ome.tif"
    _two_level(path, base, base[:2048:16, :2048:16].copy())
    with OmeTiffSlide(str(path)) as s:
        assert s.level_dimensions == ((2063, 2063), (128, 128))
    # One pixel more than any rounding of a 16x level explains.
    wider = tmp_path / "gap17.ome.tif"
    base = _rgb(2065, 2065)
    _two_level(wider, base, base[:2048:16, :2048:16].copy())
    assert _reason(wider) == "level_gap"


def test_oversized_segments_are_refused(tmp_path, monkeypatch):
    path = tmp_path / "bigtiles.ome.tif"
    _write_pyramid(path, _rgb(600, 800), axes="YXS", photometric="rgb", levels=1)
    monkeypatch.setattr(ome_tiff, "_MAX_SEGMENT_PIXELS", 10_000)
    assert _reason(path) == "segment_size"


def test_multifile_datasets_are_refused(tmp_path):
    base = _rgb(512, 512)
    other = (
        '<TiffData IFD="0" PlaneCount="1">'
        '<UUID FileName="elsewhere.ome.tif">urn:uuid:00000000-0000-0000-0000-00000000beef</UUID>'
        "</TiffData>"
    )
    path = tmp_path / "multi.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", levels=1, ome=False,
                   description=_ome_xml(512, 512, tiffdata=other))
    assert _reason(path) == "multifile"

    binary_only = tmp_path / "binonly.ome.tif"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" UUID="urn:uuid:1">'
        '<BinaryOnly MetadataFile="set.companion.ome" UUID="urn:uuid:2"/></OME>'
    )
    _write_pyramid(binary_only, base, axes="YXS", photometric="rgb", levels=1, ome=False, description=xml)
    assert _reason(binary_only) == "multifile"


def test_doctype_in_ome_xml_is_refused(tmp_path):
    path = tmp_path / "doctype.ome.tif"
    xml = _ome_xml(512, 512, doctype='<!DOCTYPE OME [<!ENTITY boom "x">]>')
    _write_pyramid(path, _rgb(512, 512), axes="YXS", photometric="rgb", levels=1, ome=False, description=xml)
    assert _reason(path) == "structure"


def test_truncated_file_is_refused(tmp_path):
    path = tmp_path / "trunc.ome.tif"
    _write_pyramid(path, _rgb(900, 1200), axes="YXS", photometric="rgb")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    assert _reason(path) == "structure"


def test_undecodable_segments_are_reported_as_codec(tmp_path):
    path = tmp_path / "corrupt.ome.tif"
    _write_pyramid(path, _rgb(900, 1200), axes="YXS", photometric="rgb")
    with tifffile.TiffFile(path) as tf:
        page = tf.series[0].levels[-1].pages[0]
        offset = int(page.dataoffsets[0])
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(b"\xff" * 16)
    assert _reason(path) == "codec"


def _replace_segment(path, page_of, index: int, data: bytes) -> None:
    """Point segment ``index`` of the page ``page_of(tf)`` at ``data``,
    appended to the file."""
    with tifffile.TiffFile(path, mode="r+b") as tf:
        page = page_of(tf)
        offsets, counts = list(page.dataoffsets), list(page.databytecounts)
        fh = tf.filehandle
        fh.seek(0, 2)
        offsets[index], counts[index] = fh.tell(), len(data)
        fh.write(data)
        page.tags["TileOffsets" if page.is_tiled else "StripOffsets"].overwrite(offsets)
        page.tags["TileByteCounts" if page.is_tiled else "StripByteCounts"].overwrite(counts)


def _jpeg_claiming(dim: int) -> bytes:
    import imagecodecs
    import struct

    enc = bytearray(imagecodecs.jpeg8_encode(np.full((256, 256, 3), 128, np.uint8), level=50))
    sof = enc.find(b"\xff\xc0")
    struct.pack_into(">HH", enc, sof + 5, dim, dim)
    return bytes(enc)


def test_codec_header_claiming_more_than_the_segment_is_refused(tmp_path):
    base = np.full((512, 512, 3), 128, np.uint8)
    probed = tmp_path / "bomb-probed.ome.tif"
    _write_pyramid(probed, base, axes="YXS", photometric="rgb", compression="jpeg", levels=1)
    # The smallest level's first tile is what the open-time probe decodes.
    _replace_segment(probed, lambda tf: tf.series[0].levels[-1].pages[0], 0, _jpeg_claiming(20000))
    assert _reason(probed) == "codec"

    hidden = tmp_path / "bomb-hidden.ome.tif"
    _write_pyramid(hidden, base, axes="YXS", photometric="rgb", compression="jpeg", levels=1)
    _replace_segment(hidden, lambda tf: tf.series[0].levels[0].pages[0], 3, _jpeg_claiming(20000))
    with OmeTiffSlide(str(hidden)) as s:
        # Refused before any decoder allocates for 20000 x 20000, and not as
        # a ValueError (which the tile endpoint would turn into a silent 404).
        with pytest.raises(openslide.OpenSlideError):
            s.read_region((300, 300), 0, (10, 10))
        assert _region(s, (0, 0), 0, (10, 10))[0, 0, 3] == 255


@pytest.mark.parametrize("encode, compression", [
    ("jpeg8", 7), ("jpeg2k", 34712), ("jpeg2k_jp2", 34712), ("webp", 50001), ("webp_lossless", 50001), ("png", 34933),
])
def test_codec_header_parsers(encode, compression):
    import imagecodecs

    tile = np.full((200, 250, 3), 90, np.uint8)
    if encode == "jpeg2k_jp2":
        data = imagecodecs.jpeg2k_encode(tile, codecformat="JP2")
    elif encode == "webp_lossless":
        data = imagecodecs.webp_encode(tile, lossless=True)
    else:
        data = getattr(imagecodecs, f"{encode}_encode")(tile)
    ome_tiff._check_segment_header(bytes(data), compression, 256, 256)  # smaller than the tile: fine
    with pytest.raises(ValueError):
        ome_tiff._check_segment_header(bytes(data), compression, 128, 256)
    with pytest.raises(ValueError):
        ome_tiff._check_segment_header(b"\x00" * 64, compression, 256, 256)
    ome_tiff._check_segment_header(b"anything", 8, 256, 256)  # byte codecs are bounded by tifffile


@pytest.mark.parametrize("compression", [6, 50002, 34934])
def test_unchecked_image_codecs_are_refused(tmp_path, compression):
    path = tmp_path / f"c{compression}.ome.tif"
    _write_pyramid(path, _rgb(600, 800), axes="YXS", photometric="rgb", levels=1)
    with tifffile.TiffFile(path, mode="r+b") as tf:
        tf.pages[0].tags["Compression"].overwrite(compression)
    assert _reason(path) == "codec"


def test_refused_codec_on_a_reduced_level_is_refused_before_decoding(tmp_path, monkeypatch):
    # Checking only the base keyframe's codec would let a JPEG XL
    # sub-level reach its decoder (the open-time probe), which allocates
    # whatever size its header claims. With the probe out of the way, the
    # structure pass alone must refuse it.
    monkeypatch.setattr(OmeTiffSlide, "_probe_decode", lambda self: None)
    path = tmp_path / "jxl-sublevel.ome.tif"
    _write_pyramid(path, _rgb(1024, 1024), axes="YXS", photometric="rgb", levels=2)
    with tifffile.TiffFile(path, mode="r+b") as tf:
        tf.series[0].levels[1].pages[0].tags["Compression"].overwrite(50002)
    assert _reason(path) == "codec"


def test_wide_samples_are_refused(tmp_path):
    many = tmp_path / "spp8.ome.tif"
    tifffile.imwrite(many, np.zeros((512, 512, 8), np.uint8), photometric="minisblack", planarconfig="contig",
                     tile=(256, 256), metadata={"axes": "YXS"}, ome=True)
    assert _reason(many) in ("multichannel", "pixel_type")
    f64 = tmp_path / "f64.ome.tif"
    tifffile.imwrite(f64, np.zeros((512, 512), np.float64), tile=(256, 256), metadata={"axes": "YX"}, ome=True)
    assert _reason(f64) == "pixel_type"


def test_edge_tiles_stored_at_their_cropped_size(tmp_path):
    import imagecodecs

    base = _rgb(500, 600)
    path = tmp_path / "edge.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", compression="zlib", levels=0)
    # Re-store the right-hand edge tile (x 512..599) as an 88 px wide JPEG,
    # the way some JPEG / PNG writers store edge tiles.
    crop = np.ascontiguousarray(base[0:256, 512:600])
    with tifffile.TiffFile(path) as tf:
        across = -(-600 // 256)
    _replace_segment(path, lambda tf: tf.pages[0], across - 1, imagecodecs.png_encode(crop))
    with tifffile.TiffFile(path, mode="r+b") as tf:
        tf.pages[0].tags["Compression"].overwrite(34933)
    # The other tiles are zlib; re-encode them as PNG too so one codec fits.
    for i in range(across * 2):
        if i == across - 1:
            continue
        ty, tx = divmod(i, across)
        tile = np.zeros((256, 256, 3), np.uint8)
        part = base[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256]
        tile[:part.shape[0], :part.shape[1]] = part
        _replace_segment(path, lambda tf: tf.pages[0], i, imagecodecs.png_encode(tile))
    with OmeTiffSlide(str(path)) as s:
        reg = _region(s, (0, 0), 0, (600, 500))
        assert np.array_equal(reg[:256, :, :3], base[:256])


def test_main_image_is_the_first_in_the_file(tmp_path):
    first = np.full((768, 1024, 3), 50, np.uint8)
    second = np.full((1536, 2048, 3), 200, np.uint8)
    # A same-sized third scene must not lend the first its metadata.
    third = np.full((768, 1024, 3), 90, np.uint8)
    path = tmp_path / "scenes.ome.tif"
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tw:
        tw.write(first, photometric="rgb", tile=(256, 256), metadata={"axes": "YXS", "Name": "Scene #1"})
        tw.write(second, photometric="rgb", tile=(256, 256), metadata={"axes": "YXS", "Name": "Scene #2"})
        tw.write(third, photometric="rgb", tile=(256, 256), metadata={"axes": "YXS", "Name": "Scene #3"})
    with OmeTiffSlide(str(path)) as s:
        # What OpenSlide shows for this file.
        assert s.dimensions == (1024, 768)
        assert s.properties["ome.image-name"] == "Scene #1"


@pytest.mark.parametrize("name, kind", [
    ("label", "label"), ("Label Image", "label"), ("slide label 1", "label"), ("barcode", "label"),
    ("macro image", "macro"), ("Overview", "macro"), ("thumbnail image", "thumbnail"),
    ("CD68 macrophages", None), ("unlabeled", None), ("Baseline", None), ("label and macro", None), ("", None),
])
def test_associated_names_match_whole_words(name, kind):
    assert ome_tiff._is_associated_name(name) == kind


def test_a_series_named_like_a_macro_is_still_the_main_image(tmp_path):
    base = _rgb(900, 1200)
    path = tmp_path / "named.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", metadata={"Name": "CD68 macrophages"},
                   extra_series=[("Image 2", np.full((600, 800, 3), 7, np.uint8))])
    with OmeTiffSlide(str(path)) as s:
        assert s.dimensions == (1200, 900) and dict(s.associated_images) == {}


def test_unreconcilable_ome_xml_falls_back_to_openslide_on_upload(tmp_path):
    # SizeC=1 declared over an interleaved RGB page: tifffile can't map it
    # and returns a generic series. OpenSlide can read such a file.
    path = tmp_path / "quirk.ome.tif"
    tifffile.imwrite(path, _rgb(512, 768), photometric="rgb", tile=(256, 256), metadata=None,
                     description=_ome_xml(768, 512, size_c=1, spp=1))
    with pytest.raises(UnsupportedOmeTiffError) as excinfo:
        OmeTiffSlide(str(path))
    assert excinfo.value.fallback_ok
    props = openslide_utils.read_slide_properties(str(path))
    assert (props["width"], props["height"]) == (768, 512)


def test_multifile_without_root_uuid_is_reported_as_multifile(tmp_path):
    ns = "http://www.openmicroscopy.org/Schemas/OME/2016-06"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><OME xmlns="{ns}">'
           '<Image ID="Image:0" Name="x"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint8" '
           'SizeX="512" SizeY="512" SizeC="3" SizeZ="2" SizeT="1"><Channel ID="Channel:0:0" SamplesPerPixel="3"/>'
           '<TiffData IFD="0" FirstZ="0" PlaneCount="1"/>'
           '<TiffData IFD="0" FirstZ="1" PlaneCount="1"><UUID FileName="other.ome.tif">urn:uuid:bbbb</UUID></TiffData>'
           '</Pixels></Image></OME>')
    path = tmp_path / "mf.ome.tif"
    tifffile.imwrite(path, np.zeros((512, 512, 3), np.uint8), photometric="rgb", tile=(256, 256),
                     description=xml, metadata=None)
    assert _reason(path) == "multifile"


def test_corrupt_16bit_segment_is_a_codec_problem(tmp_path):
    path = tmp_path / "corrupt16.ome.tif"
    _write_pyramid(path, (_rgb(900, 1200)[..., 0].astype(np.uint16) * 16), axes="YX", levels=1)
    with tifffile.TiffFile(path) as tf:
        offset = int(tf.series[0].levels[-1].pages[0].dataoffsets[0])
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(b"\xff" * 16)
    assert _reason(path) == "codec"


def test_every_level_is_probed(tmp_path):
    path = tmp_path / "levels.ome.tif"
    _write_pyramid(path, _rgb(1024, 1024), axes="YXS", photometric="rgb", levels=2)
    with tifffile.TiffFile(path, mode="r+b") as tf:
        # A JPEG 2000 code on zlib data, on the base level only.
        tf.series[0].levels[0].pages[0].tags["Compression"].overwrite(34712)
    assert _reason(path) == "codec"


@pytest.mark.parametrize("how, expected_high", [
    ("guess", 4095.0),          # max 1900 at the smallest level: 11 bits snaps up to 12
    ("significant", 16383.0),   # OME SignificantBits="14"
    ("maxsample", 8191.0),      # MaxSampleValue 8000
])
def test_rgb16_bit_depth_sources(tmp_path, how, expected_high):
    base = np.full((600, 800, 3), 1500, np.uint16)
    base[:2, :2] = 1900
    path = tmp_path / f"rgb16-{how}.ome.tif"
    kwargs = {}
    if how == "significant":
        kwargs["description"] = _ome_xml(800, 600, pixel_type="uint16").replace(
            'Type="uint16"', 'Type="uint16" SignificantBits="14"')
        kwargs["ome"] = False
    if how == "maxsample":
        kwargs["metadata"] = {}
    _write_pyramid(path, base, axes="YXS", photometric="rgb", levels=1, **kwargs)
    if how == "maxsample":
        with tifffile.TiffFile(path, mode="r+b") as tf:
            assert 281 not in tf.pages[0].tags
        # tifffile can't add a tag in place; write it through extratags.
        pyr = [base, base[::2, ::2].copy()]
        with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tw:
            tw.write(pyr[0], subifds=1, tile=(256, 256), photometric="rgb", compression="zlib",
                     metadata={"axes": "YXS"}, extratags=[(281, "H", 3, (8000, 8000, 8000), True)])
            tw.write(pyr[1], subfiletype=1, tile=(256, 256), photometric="rgb", compression="zlib",
                     extratags=[(281, "H", 3, (8000, 8000, 8000), True)])
    with OmeTiffSlide(str(path)) as s:
        assert float(s.properties["ome.window-high"]) == expected_high


def test_alpha_is_kept_so_deep_zoom_composites_onto_white(tmp_path):
    base = np.zeros((512, 1024, 4), np.uint8)
    base[..., :3] = 60
    base[:, :512, 3] = 255  # right half fully transparent
    path = tmp_path / "rgba.ome.tif"
    _write_pyramid(path, base, axes="YXS", photometric="rgb", levels=1, metadata={})
    with tifffile.TiffFile(path) as tf:
        assert tuple(tf.pages[0].extrasamples) in ((1,), (2,))
    with OmeTiffSlide(str(path)) as s:
        reg = _region(s, (0, 0), 0, (1024, 512))
        assert tuple(reg[10, 10]) == (60, 60, 60, 255)
        assert reg[10, 700, 3] == 0
        dz = DeepZoomGenerator(s, tile_size=254, overlap=1, limit_bounds=False)
        tile = dz.get_tile(dz.level_count - 1, (3, 0)).convert("RGB")
        assert tile.getpixel((10, 10)) == (255, 255, 255)


def test_segment_cache_charges_views_and_overhead(rgb_pyramid):
    path, _ = rgb_pyramid
    with OmeTiffSlide(path) as s:
        _region(s, (0, 0), 0, (512, 512))
        for key, value in list(ome_tiff._segment_cache._data.items()):
            if key[0] == s._uid and value is not None:
                # Never a view pinning a bigger buffer than it is charged for.
                assert value.base is None or value.base.nbytes == value.nbytes


# ---------------------------------------------------------------------------
# open_wsi dispatch
# ---------------------------------------------------------------------------


def test_open_wsi_dispatch(rgb_pyramid, tmp_path, monkeypatch):
    path, _ = rgb_pyramid
    slide = openslide_utils.open_wsi(path)
    try:
        assert isinstance(slide, OmeTiffSlide)
    finally:
        slide.close()

    # A non-OME pyramidal TIFF still goes to OpenSlide.
    plain = tmp_path / "plain.tif"
    with tifffile.TiffWriter(plain) as tw:
        for a in (_rgb(512, 512), _rgb(256, 256)):
            tw.write(a, tile=(256, 256), photometric="rgb")
    slide = openslide_utils.open_wsi(str(plain))
    try:
        assert isinstance(slide, openslide.OpenSlide)
    finally:
        slide.close()


def test_open_wsi_falls_back_unless_strict(rgb_pyramid, monkeypatch):
    path, _ = rgb_pyramid
    sentinel = object()

    def refuse(_path):
        raise UnsupportedOmeTiffError("multichannel", "test")

    monkeypatch.setattr(openslide_utils, "OmeTiffSlide", refuse)
    monkeypatch.setattr(openslide_utils.openslide, "OpenSlide", lambda _p: sentinel)
    assert openslide_utils.open_wsi(path) is sentinel
    with pytest.raises(UnsupportedOmeTiffError):
        openslide_utils.open_wsi(path, strict_ome=True)
    with pytest.raises(UnsupportedOmeTiffError):
        openslide_utils.read_slide_properties(path)


def test_thumbnail_from_ome(rgb_pyramid, tmp_path):
    path, _ = rgb_pyramid
    out = tmp_path / "thumb.jpg"
    openslide_utils.make_thumbnail(path, str(out))
    from PIL import Image
    with Image.open(out) as im:
        assert max(im.size) <= 512 and im.size[0] > im.size[1]


# ---------------------------------------------------------------------------
# HTTP: upload, tiles, replace, import, stats
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dirs():
    """wsi/ and thumbs/ exist in production (created at app startup); the
    TestClient never runs the lifespan, so create them for the upload tests."""
    from wsi_app.config import settings

    for sub in ("wsi", "thumbs"):
        os.makedirs(os.path.join(settings.data_dir, sub), exist_ok=True)


def _ome_bytes(tmp_path, *, fluorescence=False) -> bytes:
    path = tmp_path / f"payload-{uuid.uuid4().hex[:6]}.ome.tif"
    if fluorescence:
        _write_pyramid(path, np.zeros((4, 512, 512), np.uint16), axes="CYX", photometric="minisblack", levels=1)
    else:
        _write_pyramid(path, _rgb(900, 1200), axes="YXS", photometric="rgb", compression="jpeg",
                       metadata={"PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5})
    return path.read_bytes()


def _upload(client, headers, name, data):
    return client.post(
        "/api/admin/slides/upload",
        headers=headers,
        files={"file": (name, io.BytesIO(data), "application/octet-stream")},
    )


def test_upload_and_serve_ome_tiff(client, admin_headers, db_session, tmp_path, data_dirs):
    from wsi_app.config import settings
    from wsi_app.models import Slide

    name = f"slide-{uuid.uuid4().hex[:8]}.ome.tif"
    resp = _upload(client, admin_headers, name, _ome_bytes(tmp_path))
    assert resp.status_code == 200, resp.text
    slide_id = resp.json()["slide_id"]
    db_session.expire_all()
    s = db_session.get(Slide, slide_id)
    assert s.vendor == "ome-tiff"
    assert (s.width, s.height) == (1200, 900)
    assert s.mpp_x_file == pytest.approx(0.5)
    assert s.wsi_filename.endswith(".ome.tif")
    assert os.path.isfile(os.path.join(settings.data_dir, "thumbs", s.thumb_filename))

    dzi = client.get(f"/dzi/{s.public_id}.dzi", headers=admin_headers)
    assert dzi.status_code == 200 and 'Width="1200"' in dzi.text and 'Height="900"' in dzi.text
    for level in (0, 8, 11):
        tile = client.get(f"/dzi/{s.public_id}_files/{level}/0_0.jpeg", headers=admin_headers)
        assert tile.status_code == 200, (level, tile.text)
        assert tile.headers["content-type"] == "image/jpeg"
        assert tile.content.startswith(b"\xff\xd8")

    stats = client.get("/api/admin/stats", headers=admin_headers).json()
    assert any(row["label"] == "OME-TIFF" for row in stats["slides"]["by_extension"])


def test_upload_refuses_fluorescence_with_reason(client, admin_headers, db_session, tmp_path, data_dirs):
    from wsi_app.config import settings
    from wsi_app.models import Slide

    name = f"fluo-{uuid.uuid4().hex[:8]}.ome.tif"
    before = db_session.query(Slide).count()
    resp = _upload(client, admin_headers, name, _ome_bytes(tmp_path, fluorescence=True))
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"code": "adminDash.apiErr.omeTiff.multichannel"}
    db_session.expire_all()
    assert db_session.query(Slide).count() == before
    assert not [f for f in os.listdir(os.path.join(settings.data_dir, "wsi")) if name in f]


def test_replace_refuses_unsupported_ome(client, admin_headers, db_session, tmp_path, data_dirs):
    from wsi_app.models import Slide

    resp = _upload(client, admin_headers, f"keep-{uuid.uuid4().hex[:8]}.ome.tif", _ome_bytes(tmp_path))
    assert resp.status_code == 200, resp.text
    slide_id = resp.json()["slide_id"]
    db_session.expire_all()
    original = db_session.get(Slide, slide_id).wsi_filename

    r = client.post(
        f"/api/admin/slides/{slide_id}/wsi",
        headers=admin_headers,
        files={"file": (f"fluo-{uuid.uuid4().hex[:8]}.ome.tif", io.BytesIO(_ome_bytes(tmp_path, fluorescence=True)),
                        "application/octet-stream")},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "adminDash.apiErr.omeTiff.multichannel"
    db_session.expire_all()
    assert db_session.get(Slide, slide_id).wsi_filename == original


def test_import_reports_ome_reason_code(db_session, tmp_path, data_dirs):
    from wsi_app import slide_transfer as st
    from wsi_app.admin_helpers import mint_public_id
    from wsi_app.models import Slide

    pid = mint_public_id()
    stage = tmp_path / "stage"
    wsi_dir = stage / "wsi" / pid
    wsi_dir.mkdir(parents=True)
    staged = wsi_dir / "fluo.ome.tif"
    staged.write_bytes(_ome_bytes(tmp_path, fluorescence=True))
    sd = {"public_id": pid, "wsi_original_basename": f"fluo-{uuid.uuid4().hex[:8]}.ome.tif", "title": "x"}
    with pytest.raises(st.SlideTransferError) as excinfo:
        st._import_one_slide(db_session, str(stage), sd, {}, "copy")
    assert excinfo.value.code == "omeTiff.multichannel"
    # The staged file is restored for a retry and no placeholder row remains.
    assert staged.exists()
    db_session.expire_all()
    assert db_session.query(Slide).filter_by(public_id=pid).count() == 0
