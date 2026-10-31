from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageFile

from wsi_app.openslide_utils import make_thumbnail, read_slide_properties
from wsi_app.szi_utils import (
    make_szi_thumbnail,
    read_szi_properties,
)


def _dzi_xml(width: int, height: int, tile_size: int = 256, overlap: int = 0, fmt: str = "jpeg") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" '
        f'TileSize="{tile_size}" Overlap="{overlap}" Format="{fmt}">'
        f'<Size Width="{width}" Height="{height}"/>'
        f'</Image>'
    ).encode("utf-8")


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _build_szi(
    path: Path,
    width: int = 1024,
    height: int = 768,
    tile_size: int = 256,
    overlap: int = 0,
    *,
    stem: str = "image",
    compression: int = zipfile.ZIP_STORED,
) -> None:
    """Write a minimal valid SZI archive to *path* with a complete pyramid."""
    import math as _math

    max_dim = max(width, height)
    max_level = max(0, _math.ceil(_math.log2(max_dim)) if max_dim > 1 else 0)

    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        zf.writestr(f"{stem}.dzi", _dzi_xml(width, height, tile_size, overlap))
        for lvl in range(max_level + 1):
            scale = 2 ** (max_level - lvl)
            lvl_w = max(1, _math.ceil(width / scale))
            lvl_h = max(1, _math.ceil(height / scale))
            cols = _math.ceil(lvl_w / tile_size)
            rows = _math.ceil(lvl_h / tile_size)
            for col in range(cols):
                for row in range(rows):
                    x0 = col * tile_size
                    y0 = row * tile_size
                    tw = min(tile_size, lvl_w - x0)
                    th = min(tile_size, lvl_h - y0)
                    if tw <= 0 or th <= 0:
                        continue
                    # Distinct colour per tile so paste order is observable.
                    color = (
                        (col * 37) % 256,
                        (row * 53) % 256,
                        (lvl * 29) % 256,
                    )
                    tile = Image.new("RGB", (tw, th), color)
                    zf.writestr(
                        f"{stem}_files/{lvl}/{col}_{row}.jpeg",
                        _encode_jpeg(tile),
                    )


def test_read_szi_properties_returns_size_and_zero_mpp(tmp_path: Path) -> None:
    p = tmp_path / "tiny.szi"
    _build_szi(p, width=512, height=384)
    props = read_szi_properties(str(p))
    assert props == {
        "width": 512,
        "height": 384,
        "mpp_x": 0.0,
        "mpp_y": 0.0,
        "objective_power": 0.0,
        "vendor": "szi",
        "acquired_at": "",
    }


def test_read_szi_properties_rejects_compressed_archive(tmp_path: Path) -> None:
    p = tmp_path / "bad.szi"
    _build_szi(p, width=256, height=256, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="ZIP_STORED"):
        read_szi_properties(str(p))


def test_read_szi_properties_rejects_missing_dzi(tmp_path: Path) -> None:
    p = tmp_path / "nodzi.szi"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("image_files/0/0_0.jpeg", b"\xff\xd8\xff\xd9")
    with pytest.raises(ValueError, match="missing root .dzi"):
        read_szi_properties(str(p))


def test_make_szi_thumbnail_writes_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "tiny.szi"
    out = tmp_path / "thumbs" / "tiny.jpg"
    _build_szi(p, width=1024, height=768)
    make_szi_thumbnail(str(p), str(out), max_size=(256, 256))
    assert out.exists() and out.stat().st_size > 0
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert max(im.size) <= 256


def test_openslide_dispatch_routes_szi(tmp_path: Path) -> None:
    """openslide_utils must not invoke OpenSlide for .szi files."""
    p = tmp_path / "dispatch.szi"
    _build_szi(p, width=384, height=384)
    props = read_slide_properties(str(p))
    assert props["vendor"] == "szi"
    assert props["width"] == 384
    out = tmp_path / "thumbs" / "dispatch.jpg"
    make_thumbnail(str(p), str(out), max_size=(128, 128))
    assert out.exists()


def test_make_szi_thumbnail_sparse_pyramid_canvas_bounded(tmp_path: Path, monkeypatch) -> None:
    """A sparse pyramid -- only a sub-256px level and an over-16MP base level,
    nothing in between -- must not make the level chooser fall back to the
    full-resolution base and allocate a canvas of the base dimensions. Assert
    the canvas the code actually allocates stays within the 16 MP budget."""
    from wsi_app import szi_utils

    allocations: list[int] = []
    real_new = szi_utils.Image.new

    def _recording_new(mode, size, *args, **kwargs):
        allocations.append(size[0] * size[1])
        return real_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(szi_utils.Image, "new", _recording_new)

    # Width/height = 4097 -> base level (13) is 4097x4097 ~ 16.78 MP (over the
    # 16 MP budget); level 0 is 1x1 (under the 256px floor). Nothing in
    # between qualifies, so a naive chooser would fall back to level 13.
    p = tmp_path / "sparse.szi"
    out = tmp_path / "thumbs" / "sparse.jpg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("image.dzi", _dzi_xml(4097, 4097))
        zf.writestr("image_files/0/0_0.jpeg", _encode_jpeg(Image.new("RGB", (1, 1))))
        zf.writestr("image_files/13/0_0.jpeg", _encode_jpeg(Image.new("RGB", (256, 256))))

    make_szi_thumbnail(str(p), str(out), max_size=(256, 256))

    assert out.exists()
    assert allocations, "canvas was never allocated"
    assert max(allocations) <= 16 * 1024 * 1024, (
        f"canvas allocation {max(allocations)} px exceeds the 16 MP budget -- "
        "sparse-pyramid fallback regressed to the full-resolution base"
    )


def test_safe_parse_rejects_doctype_after_large_comment_prolog():
    """billion-laughs guard must scan the whole buffer: a DOCTYPE hidden behind
    a >4KB comment prolog is still rejected, not just one in the head."""
    from wsi_app.szi_utils import _safe_parse

    payload = (
        b'<?xml version="1.0"?>'
        + b'<!-- ' + (b'x' * 5000) + b' -->'
        + b'<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        + b'<Image><Size Width="1" Height="1"/></Image>'
    )
    with pytest.raises(ValueError):
        _safe_parse(payload)


def test_safe_parse_rejects_entity_declaration():
    from wsi_app.szi_utils import _safe_parse

    payload = b'<!DOCTYPE x [<!ENTITY e "boom">]><Image/>'
    with pytest.raises(ValueError):
        _safe_parse(payload)


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_safe_parse_refuses_a_descriptor_in_a_wide_encoding(codec):
    """The DOCTYPE screen is a byte pattern; the same declaration in UTF-16 /
    UTF-32 carries NULs between its letters, matches nothing, and expat decodes
    it all the same."""
    from wsi_app.szi_utils import _safe_parse

    text = '<?xml version="1.0" encoding="%s"?><!DOCTYPE d [<!ENTITY a "aaaa">]><Image/>' % codec.split("-")[0].upper()
    with pytest.raises(ValueError):
        _safe_parse(text.encode(codec))
    with pytest.raises(ValueError):
        _safe_parse("<Image/>".encode(codec))


def test_safe_parse_refuses_a_declared_encoding_the_screen_cannot_read():
    from wsi_app.szi_utils import _safe_parse

    for enc in ("UTF-16", "cp037", "EBCDIC-US", "koi8-r"):
        with pytest.raises(ValueError):
            _safe_parse(('<?xml version="1.0" encoding="%s"?><Image/>' % enc).encode("ascii"))


@pytest.mark.parametrize("head", [
    b"", b"\xef\xbb\xbf", b'<?xml version="1.0"?>', b'<?xml version="1.0" encoding="UTF-8"?>',
    b"<?xml version='1.0' encoding='us-ascii'?>", b'<?xml version="1.0" encoding="ISO-8859-1"?>',
])
def test_safe_parse_still_reads_ordinary_descriptors(head):
    from wsi_app.szi_utils import _safe_parse

    assert _safe_parse(head + b'<Image Format="jpeg"/>').tag == "Image"


def test_safe_parse_accepts_plain_dzi():
    from wsi_app.szi_utils import _safe_parse

    el = _safe_parse(_dzi_xml(100, 80))
    assert el.tag.endswith("Image")


def test_parse_dzi_rejects_tiny_tile_size():
    """TileSize below the lower bound (a DoS lever for the thumbnail builder)
    is rejected at parse time; the spec default 256 is accepted."""
    from wsi_app.szi_utils import _parse_dzi_xml

    with pytest.raises(ValueError):
        _parse_dzi_xml(_dzi_xml(1000, 800, tile_size=1))
    meta = _parse_dzi_xml(_dzi_xml(1000, 800, tile_size=256))
    assert meta["tile_size"] == 256


def test_make_szi_thumbnail_ignores_spurious_high_level(tmp_path: Path, monkeypatch) -> None:
    """max_level must be derived from the validated Width/Height, not from
    max(present.keys()). A crafted SZI with a spurious higher-numbered
    ``_files/<level>/`` dir must not inflate max_level, which would shift every
    ``scale = 2 ** (max_level - lvl)`` so the builder samples the wrong level
    and clips the thumbnail. Assert the canvas the builder allocates is
    identical with and without the spurious dir."""
    from wsi_app import szi_utils

    sizes: list[tuple[int, int]] = []
    real_new = szi_utils.Image.new

    def _recording_new(mode, size, *args, **kwargs):
        sizes.append((size[0], size[1]))
        return real_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(szi_utils.Image, "new", _recording_new)

    width, height = 1024, 768  # real top level for max_dim=1024 is ceil(log2)=10

    clean = tmp_path / "clean.szi"
    _build_szi(clean, width=width, height=height)
    make_szi_thumbnail(str(clean), str(tmp_path / "clean.jpg"), max_size=(256, 256))
    clean_canvas = sizes[-1]

    crafted = tmp_path / "crafted.szi"
    _build_szi(crafted, width=width, height=height)
    # A spurious level dir far above the real top (level 15 >> 10). Without the
    # guard it would become max(present.keys()) and even be *chosen* as the source level.
    with zipfile.ZipFile(crafted, "a", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("image_files/15/0_0.jpeg", _encode_jpeg(Image.new("RGB", (8, 8))))
    make_szi_thumbnail(str(crafted), str(tmp_path / "crafted.jpg"), max_size=(256, 256))
    crafted_canvas = sizes[-1]

    assert crafted_canvas == clean_canvas, (
        f"spurious level shifted the canvas: clean={clean_canvas} "
        f"crafted={crafted_canvas} -- max_level trusted the archive dir names"
    )


def test_existing_levels_keeps_only_canonical_levels_in_range(tmp_path: Path) -> None:
    """``int()`` accepts far more than a level name: signs, leading zeros,
    digit separators, whitespace, non-ASCII digits. Only the canonical name of
    a level in [0, max_level] is a level; everything else is skipped."""
    from wsi_app.szi_utils import _existing_levels

    p = tmp_path / "names.szi"
    heads = ("0", "3", "8", "9", "-1", "-1000000000000", "+2", "05", "1_0", " 4", "４", "x")
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as zf:
        for head in heads:
            zf.writestr(f"image_files/{head}/0_0.jpeg", b"x")
    with zipfile.ZipFile(p) as zf:
        assert _existing_levels(zf, "image_files", 8) == {0: "0", 3: "3", 8: "8"}


def test_make_szi_thumbnail_never_raises_two_to_a_negative_level(tmp_path: Path) -> None:
    """A level dir named ``-1000000000000`` would pass the ``lvl <= max_level``
    bound, and ``2 ** (max_level - lvl)`` would then ask for a ~125 GB integer
    from a few-KB archive.

    The builder runs under a line tracer that aborts the moment a negative
    ``lvl`` is bound in its frame — i.e. before that power can be evaluated —
    so a regression fails here instead of attempting the allocation."""
    import sys

    from wsi_app import szi_utils

    p = tmp_path / "negative.szi"
    _build_szi(p, width=256, height=256)
    with zipfile.ZipFile(p, "a", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("image_files/-1000000000000/0_0.jpeg", _encode_jpeg(Image.new("RGB", (8, 8))))

    target = szi_utils.make_szi_thumbnail.__code__

    def local(frame, event, arg):
        lvl = frame.f_locals.get("lvl")
        if isinstance(lvl, int) and lvl < 0:
            raise AssertionError(f"negative level {lvl} reached the thumbnail builder")
        return local

    def tracer(frame, event, arg):
        return local if frame.f_code is target else None

    out = tmp_path / "negative.jpg"
    prev = sys.gettrace()
    sys.settrace(tracer)
    try:
        make_szi_thumbnail(str(p), str(out), max_size=(128, 128))
    finally:
        sys.settrace(prev)
    assert out.exists() and out.stat().st_size > 0
    # Properties stay readable: the junk level is skipped, not fatal.
    assert read_szi_properties(str(p))["width"] == 256


def test_read_szi_properties_rejects_oversized_descriptor(tmp_path: Path) -> None:
    """A stored .dzi member larger than the descriptor cap is refused before
    it is read whole into RAM (a real descriptor is well under a KiB)."""
    from wsi_app import szi_utils

    p = tmp_path / "bigdzi.szi"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as zf:
        # Valid-looking DZI XML padded past the cap with a comment.
        bloat = (
            b'<?xml version="1.0"?><!-- '
            + b"x" * (szi_utils._MAX_DZI_DESCRIPTOR_BYTES + 16)
            + b' --><Image TileSize="256"><Size Width="4" Height="4"/></Image>'
        )
        zf.writestr("image.dzi", bloat)
        zf.writestr("image_files/0/0_0.jpeg", b"\xff\xd8\xff\xd9")
    with pytest.raises(ValueError, match="descriptor"):
        read_szi_properties(str(p))


def test_szi_upload_via_endpoint_stages_and_lands(client, admin_headers, tmp_path: Path) -> None:
    """A real .szi uploaded through the admin endpoint validates on the staged
    ``.upload-<token>.szi`` file (extension preserved so SZI dispatch still
    routes to szi_utils, not OpenSlide) and lands at the final
    ``{id}_{name}.szi`` path with no staging remnant left behind."""
    from wsi_app.config import settings

    # ensure_dirs() runs in the app lifespan, which a plain TestClient doesn't
    # trigger; create the upload dirs explicitly like the other upload tests.
    wsi_dir = Path(settings.data_dir) / "wsi"
    wsi_dir.mkdir(parents=True, exist_ok=True)
    (Path(settings.data_dir) / "thumbs").mkdir(parents=True, exist_ok=True)

    src = tmp_path / "endpoint.szi"
    _build_szi(src, width=512, height=384)
    with open(src, "rb") as fh:
        r = client.post(
            "/api/admin/slides/upload",
            headers=admin_headers,
            files={"file": ("endpoint.szi", fh, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    slide_id = r.json()["slide_id"]

    assert (wsi_dir / f"{slide_id}_endpoint.szi").exists()
    remnants = [
        q.name for q in wsi_dir.glob(f"{slide_id}_*") if ".upload-" in q.name
    ]
    assert remnants == [], remnants


# ---------------------------------------------------------------------------
# Member-count ceiling
# ---------------------------------------------------------------------------
# ``zipfile.ZipFile(path)`` materialises one ZipInfo per central-directory
# entry before any of the app's code runs, so the per-member byte caps cannot bound
# the memory an archive with tens of millions of entries costs. The count is
# screened by walking the central directory before the file is opened.

def test_entry_count_matches_real_archives(tmp_path: Path) -> None:
    from wsi_app.szi_utils import _central_directory_entry_count

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        for i in range(37):
            zf.writestr(f"f{i}", b"x")
    assert _central_directory_entry_count(str(plain)) == 37

    # An archive comment pushes the EOCD away from the file end.
    commented = tmp_path / "commented.zip"
    with zipfile.ZipFile(commented, "w") as zf:
        zf.writestr("only", b"x")
        zf.comment = b"C" * 5000
    assert _central_directory_entry_count(str(commented)) == 1

    # Past 0xFFFF entries the 32-bit EOCD saturates and the real figures live
    # in the Zip64 record instead.
    zip64 = tmp_path / "many.zip"
    with zipfile.ZipFile(zip64, "w", zipfile.ZIP_STORED) as zf:
        for i in range(70000):
            zf.writestr(str(i), b"")
    assert _central_directory_entry_count(str(zip64)) == 70000


def test_entry_count_returns_minus_one_for_non_zip(tmp_path: Path) -> None:
    """Unwalkable central directory means "unknown", not "reject": ZipFile
    still gets to raise its own BadZipFile for a genuinely malformed file."""
    from wsi_app.szi_utils import _central_directory_entry_count

    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a zip at all")
    assert _central_directory_entry_count(str(junk)) == -1


def test_entry_count_ignores_a_lying_eocd_total(tmp_path: Path) -> None:
    """The EOCD's "total entries" field is not what bounds ZipFile.

    ``_RealGetContents`` walks ``size_cd`` bytes of records and never reads the
    count, so an archive may claim 5 members and still cost thousands of
    ZipInfo allocations. Screening on the claim alone would be a straight
    bypass.
    """
    import struct

    from wsi_app.szi_utils import _central_directory_entry_count

    p = tmp_path / "liar.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        for i in range(400):
            zf.writestr(f"s_files/0/{i}_0.jpeg", b"x")

    data = bytearray(p.read_bytes())
    idx = data.rfind(b"PK\x05\x06")
    struct.pack_into("<H", data, idx + 8, 5)    # entries on this disk
    struct.pack_into("<H", data, idx + 10, 5)   # entries in total
    p.write_bytes(bytes(data))

    # What ZipFile really builds is what the screen must see.
    assert len(zipfile.ZipFile(p).infolist()) == 400
    assert _central_directory_entry_count(str(p)) == 400


def test_entry_count_follows_the_eocd_zipfile_picks(tmp_path: Path) -> None:
    """A bogus EOCD appended after the real one must not be skipped.

    The stdlib takes the *last* EOCD signature in the tail without validating
    its comment length, so a screen that insisted on a consistent record would
    read the honest one while ZipFile parsed the attacker's.
    """
    from wsi_app.szi_utils import _central_directory_entry_count

    p = tmp_path / "trailer.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        for i in range(20):
            zf.writestr(f"f{i}", b"x")
    # Append a second EOCD claiming a comment that is not there.
    p.write_bytes(p.read_bytes() + b"PK\x05\x06" + b"\x00" * 16 + b"\xff\xff")

    # ZipFile reads the appended record; whatever it makes of the archive,
    # the screen must agree rather than reporting the original 20.
    assert _central_directory_entry_count(str(p)) != 20


def test_entry_count_searches_as_far_back_as_zipfile_does(tmp_path: Path) -> None:
    """The EOCD hunt must use the stdlib's window, not the spec-legal one.

    A comment is capped at 0xFFFF, so 22 + 0xFFFF looks like the right search
    depth — but the stdlib scans one byte further and never validates the
    comment length. Appending exactly 65536 bytes of junk parks the real EOCD
    in that extra byte: ZipFile still finds it and materialises every entry,
    while a screen stopping at the spec bound sees nothing, answers "unknown"
    and lets the archive through unscreened.

    Newer patch releases narrowed the stdlib's window to the spec's and refuse
    this file instead; the screen keeps the wider window either way, so it is
    only ever stricter than the interpreter it guards.
    """
    from wsi_app.szi_utils import _central_directory_entry_count

    p = tmp_path / "trailing.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        for i in range(300):
            zf.writestr(f"s_files/0/{i}_0.jpeg", b"x")
    p.write_bytes(p.read_bytes() + b"\x00" * 65536)

    try:
        stdlib_count = len(zipfile.ZipFile(p).infolist())
    except zipfile.BadZipFile:
        stdlib_count = None  # narrowed window: the stdlib won't open it at all
    assert stdlib_count in (300, None)
    assert _central_directory_entry_count(str(p)) == 300


@pytest.mark.parametrize("entries,comment,zip64", [
    (5, b"", False),
    (5, b"C" * 300, False),
    (3, b"PK\x05\x06 planted in the comment", False),
    (70000, b"", True),
])
def test_entry_count_agrees_with_zipfile(
    tmp_path: Path, entries: int, comment: bytes, zip64: bool
) -> None:
    """Cross-check the hand-rolled walk against the stdlib it has to mirror.

    Any divergence here is a bypass, so this pins the agreement on whichever
    Python the suite runs against rather than trusting it to hold forever.
    """
    from wsi_app.szi_utils import _central_directory_entry_count

    p = tmp_path / f"x{entries}{len(comment)}{zip64}.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        for i in range(entries):
            zf.writestr(f"s_files/0/{i}_0.jpeg", b"x")
        zf.comment = comment
    try:
        real = len(zipfile.ZipFile(p).infolist())
    except zipfile.BadZipFile:
        # ZipFile refuses the archive outright, so no ZipInfo table is ever
        # built and "unknown" is the honest answer for the screen.
        assert _central_directory_entry_count(str(p)) == -1
        return
    assert _central_directory_entry_count(str(p)) == real


def test_read_szi_properties_rejects_too_many_members(tmp_path: Path, monkeypatch) -> None:
    import wsi_app.szi_utils as su

    p = tmp_path / "many.szi"
    _build_szi(p, width=512, height=384)
    # Lower the ceiling rather than building a 2M-entry archive.
    monkeypatch.setattr(su, "_MAX_SZI_ENTRIES", 3)
    with pytest.raises(ValueError, match="members"):
        read_szi_properties(str(p))


def test_make_szi_thumbnail_rejects_too_many_members(tmp_path: Path, monkeypatch) -> None:
    import wsi_app.szi_utils as su

    p = tmp_path / "many.szi"
    _build_szi(p, width=512, height=384)
    monkeypatch.setattr(su, "_MAX_SZI_ENTRIES", 3)
    with pytest.raises(ValueError, match="members"):
        make_szi_thumbnail(str(p), str(tmp_path / "out.jpg"))


def test_szi_under_the_member_ceiling_still_opens(tmp_path: Path) -> None:
    """Sanity check that the screen does not reject legitimate archives."""
    p = tmp_path / "ok.szi"
    _build_szi(p, width=512, height=384)
    assert read_szi_properties(str(p))["width"] == 512


def test_entry_count_screens_both_zip64_record_locations(tmp_path: Path) -> None:
    """A decoy Zip64 record at the locator's offset (empty directory) and the
    real one in the slot right before the locator. Patched interpreters
    (CVE-2025-8291) read the decoy first; older ones read only the slot and
    materialise every entry. The screen must count what the latter would."""
    import struct

    from wsi_app.szi_utils import _central_directory_entry_count

    def z64(dirsize: int, diroffset: int, n: int) -> bytes:
        return struct.pack(
            "<4sQ2H2L2Q2Q", b"PK\x06\x06", 44, 45, 45, 0, 0, n, n, dirsize, diroffset
        )

    entries = 50
    buf = bytearray(z64(0, 0, 0))  # the decoy, at offset 0
    cd_start = len(buf)
    for i in range(entries):
        name = f"f{i}".encode()
        buf += struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0,
            len(name), 0, 0, 0, 0, 0, 0,
        ) + name
    cd_size = len(buf) - cd_start
    buf += z64(cd_size, cd_start, entries)
    buf += struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)  # reloff -> the decoy
    buf += struct.pack(
        "<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0
    )
    p = tmp_path / "decoy.szi"
    p.write_bytes(bytes(buf))

    assert _central_directory_entry_count(str(p)) == entries


def test_make_szi_thumbnail_skips_tile_larger_than_declared_geometry(
    tmp_path: Path, monkeypatch
) -> None:
    """A small PNG that declares a canvas far beyond TileSize+2*Overlap must be
    skipped before load() - the compressed-size cap alone lets it expand to
    hundreds of MB."""
    p = tmp_path / "bomb.szi"
    buf = io.BytesIO()
    Image.new("L", (6000, 6000)).save(buf, "PNG", compress_level=9)
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("s.dzi", _dzi_xml(1024, 1024, 256, 0, "png"))
        # max_level is 10, so level 8 is 256x256: a single 0_0 tile.
        zf.writestr("s_files/8/0_0.png", buf.getvalue())
    loaded: list[tuple[int, int]] = []
    real_load = ImageFile.ImageFile.load

    def spy_load(self, *a, **kw):
        loaded.append(self.size)
        return real_load(self, *a, **kw)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)
    out = tmp_path / "bomb.jpg"
    make_szi_thumbnail(str(p), str(out), max_size=(128, 128))
    assert (6000, 6000) not in loaded
    assert out.exists()
