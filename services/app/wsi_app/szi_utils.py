from __future__ import annotations

import io
import math
import os
import re
import zipfile
from typing import Any, Dict, Tuple
from xml.etree import ElementTree as ET

from PIL import Image, UnidentifiedImageError

# Upper bounds enforced on the DZI XML attributes parsed out of an SZI archive.
# These are sanity caps, not capability caps: the values come from a file the
# admin uploaded, so an attacker who can replace the archive can also write
# anything into the .dzi descriptor. Without bounds, a malicious or accidental
# Width="10000000000" forces PIL.Image.new in make_szi_thumbnail to attempt a
# huge allocation. The caps are deliberately generous — well above any real
# microscope scan — so they never trip on legitimate slides.
_MAX_DZI_DIMENSION = 2_000_000          # px; ~2M on the long edge
_MAX_DZI_TILE_SIZE = 4096               # px; DZI spec defaults to 254/256
_MAX_DZI_OVERLAP = 1024                 # px; spec values are 0-2

# The `.dzi` descriptor and every tile inside an SZI are ZIP_STORED
# (uncompressed), so ``zipfile.ZipInfo.file_size`` is the exact number of
# bytes ``zf.read`` will pull into RAM. Both members come from an
# admin-uploaded archive; without a ceiling a crafted (or accidental) SZI
# could stage a multi-GB stored member and OOM the worker on a single read.
# A real DZI descriptor is a few hundred bytes of XML — even a verbosely
# commented one stays well under a KiB — so 1 MiB is ~1000x any legitimate
# descriptor yet a trivial allocation. A legitimate tile is a JPEG/PNG at
# most (_MAX_DZI_TILE_SIZE + 2*overlap) px per side (a few MB at worst), so
# 32 MiB never trips on a real slide while still bounding the malicious case;
# the thumbnail builder reads tiles one at a time and frees each in turn.
_MAX_DZI_DESCRIPTOR_BYTES = 1 << 20     # 1 MiB
_MAX_DZI_TILE_BYTES = 32 << 20          # 32 MiB
# Decoded tiles may exceed TileSize + 2*Overlap by at most this many px per
# side (the byte cap above says nothing about decoded dimensions).
_SZI_TILE_DIM_SLACK = 8

# Pillow decoders a DZI tile may be handed to. DZI tiles are browser images
# (the viewer draws them directly), so these cover every legitimate archive.
_SZI_TILE_FORMATS = ("JPEG", "PNG", "WEBP")

# Ceiling on the number of members an SZI may contain. The per-member byte caps
# above bound what a single ``zf.read`` allocates, but they say nothing about
# how many members there are — and ``zipfile.ZipFile(path)`` eagerly parses the
# whole central directory into one ZipInfo per entry before any application
# code runs. A central-directory record is ~46 bytes plus the filename, so an
# archive well inside the upload limit can carry tens of millions of entries
# and OOM the worker during construction. The count therefore has to be screened
# *before* ZipFile opens the file.
#
# Sizing: tiles dominate the entry count. The largest plausible real scan
# (~250k x 250k px at 254 px tiles) yields ~970k level-0 tiles and ~1.3M once
# the rest of the pyramid is counted, so 2M leaves generous headroom over any
# legitimate slide. A ZipInfo plus its NameToInfo entry measures ~570 bytes for
# a typical tile path, so the ceiling keeps the worst-case table near 1 GB
# rather than the tens of GB an unscreened archive could reach.
_MAX_SZI_ENTRIES = 2_000_000

_EOCD_SIG = b"PK\x05\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_CD_RECORD_SIG = b"PK\x01\x02"
# Fixed-size prefix of one central-directory record, before the variable-length
# filename / extra / comment fields.
_CD_RECORD_FIXED = 46
_EOCD_SIZE = 22
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_SIZE = 56
# How far back from EOF to hunt for the EOCD signature. A spec-legal record is
# 22 bytes plus a comment of at most 0xFFFF, but this MUST cover the widest
# window any supported stdlib uses rather than the spec's: older releases
# (3.11 included) search ``1 << 16`` plus the fixed size — one byte more
# generous — and none of them check that the comment length accounts for the
# trailing bytes. An archive with exactly 65536 bytes of junk appended puts the
# real EOCD one byte outside the spec-legal window — such a stdlib still finds
# it and parses every entry, while a screen that stopped at 65557 would find
# nothing, report "unknown" and wave the archive through unscreened. Newer
# patch releases (3.12.x, 3.14.x) narrowed their window to the spec's; the
# wider one stays safe there because ``rfind`` over a superset of the tail
# still lands on the same last signature whenever the narrower search finds
# one, and otherwise the stdlib refuses the file outright.
_EOCD_MAX_SEARCH = (1 << 16) + _EOCD_SIZE

# stdlib xml.etree.ElementTree disables external entity resolution since
# Python 3.7.1, but does NOT cap internal entity expansion ("billion laughs"):
# a malicious .dzi can declare nested entities that amplify a few KB of
# input into gigabytes once expat expands them. The DZI format does not use
# DOCTYPE/ENTITY declarations at all, so the safest mitigation is to refuse
# any input containing them — strictly cheaper than introducing a new
# runtime dependency (defusedxml) for one parse site.
#
# The scan MUST cover the whole buffer, not just a fixed-size head: XML allows
# arbitrary comments / whitespace / processing-instructions in the prolog
# before the DOCTYPE, so a multi-KB comment header would otherwise push the
# DOCTYPE past a truncated check and straight into expat's entity expansion.
# A case-insensitive regex avoids allocating a full lowercased copy.
_FORBIDDEN_XML_RE = re.compile(rb"<!(?:doctype|entity)", re.IGNORECASE)


# The screen above is a byte pattern, so it only means something when the bytes
# are ASCII-compatible text. In UTF-16 / UTF-32 every character carries NULs
# ("<\0!\0D\0O\0C..."), which no ASCII pattern matches, and expat decodes it
# all the same; pyexpat also takes any single-byte Python codec named in the
# XML declaration. A .dzi descriptor is plain UTF-8/ASCII in practice, so
# anything else is refused rather than decoded and re-screened.
_XML_DECL_ENCODING_RE = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?\sencoding\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_ASCII_COMPATIBLE_ENCODINGS = frozenset({
    "utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1",
})


def _safe_parse(xml_bytes: bytes) -> ET.Element:
    if b"\x00" in xml_bytes or xml_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError("SZI .dzi XML must be UTF-8 text")
    m = _XML_DECL_ENCODING_RE.match(xml_bytes)
    if m and m.group(1).decode("ascii", "replace").strip().lower() not in _ASCII_COMPATIBLE_ENCODINGS:
        raise ValueError("SZI .dzi XML must be UTF-8 text")
    if _FORBIDDEN_XML_RE.search(xml_bytes):
        raise ValueError(
            "SZI .dzi XML must not contain DOCTYPE or ENTITY declarations"
        )
    return ET.fromstring(xml_bytes)


def _bounded_int(raw: str, *, name: str, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"SZI .dzi {name!r} is not an integer: {raw!r}")
    if v < lo or v > hi:
        raise ValueError(f"SZI .dzi {name!r}={v} is outside [{lo}, {hi}]")
    return v

# SZI is a single ZIP archive holding a Deep Zoom Image (DZI) pyramid:
#   <stem>.dzi                                 — XML descriptor at the root
#   <stem>_files/<level>/<col>_<row>.<format>  — pre-encoded tiles
# The frontend (szi-tile-source) reads tiles directly from the archive over
# HTTP Range, so the entries MUST be ZIP_STORED (uncompressed). Validate that
# at upload time so a malformed SZI fails fast with a 422 instead of becoming
# a "looks fine, won't display" zombie.

_DZI_ROOT_RE = re.compile(r"^[^/]+\.dzi\Z", re.IGNORECASE)


def _locate_central_directory(fh, filesize: int) -> list[Tuple[int, int]]:
    """Where ``ZipFile`` may read the central directory from, and how much.

    Returns every ``(start_dir, size_cd)`` — the two values
    ``zipfile.ZipFile._RealGetContents`` derives — that a supported stdlib
    could arrive at, or ``[]`` when the End Of Central Directory record can't
    be located. More than one when a Zip64 locator is present: patched
    interpreters (CVE-2025-8291) read the Zip64 record at the locator's
    offset, older ones only the slot right before the locator, so a decoy at
    one of them must not hide the directory the other would parse. The
    caller screens the largest.

    This mirrors ``zipfile._EndRecData`` deliberately, including the parts that
    look lax: the stdlib takes the *last* EOCD signature in the tail without
    checking that its comment length accounts for the trailing bytes. Applying a
    stricter consistency test here would be a bypass, not a hardening — an
    archive ending in a bogus EOCD would send this screen to an earlier, honest
    record while ``ZipFile`` went on to parse the attacker's.

    ``start_dir`` is derived as "the central directory ends where the record
    that describes it begins", which is exactly what the stdlib's ``concat``
    arithmetic (and, for Zip64, its ``diroffset + dirsize == reloff`` check)
    works out to for any archive it accepts.
    """
    if filesize < _EOCD_SIZE:
        return []
    # Fast path: no archive comment, so the EOCD is the last 22 bytes.
    fh.seek(filesize - _EOCD_SIZE)
    rec = fh.read(_EOCD_SIZE)
    if len(rec) == _EOCD_SIZE and rec.startswith(_EOCD_SIG) and rec[-2:] == b"\x00\x00":
        eocd_at = filesize - _EOCD_SIZE
    else:
        search_from = max(filesize - _EOCD_MAX_SEARCH, 0)
        fh.seek(search_from)
        buf = fh.read()
        idx = buf.rfind(_EOCD_SIG)
        if idx < 0:
            return []
        rec = buf[idx:idx + _EOCD_SIZE]
        if len(rec) != _EOCD_SIZE:
            return []
        eocd_at = search_from + idx

    size_cd = int.from_bytes(rec[12:16], "little")
    layouts = [(eocd_at - size_cd, size_cd)]

    # The Zip64 record, when a locator precedes the EOCD, overrides the 32-bit
    # fields. The stdlib consults it unconditionally — NOT only when the 16-bit
    # entry count saturates at 0xFFFF — so this screen must too. The 32-bit
    # layout stays a candidate as well: screening one more interpretation can
    # only make the screen stricter.
    for z64_at, z64_size in _read_zip64_eocd(fh, eocd_at, filesize):
        layouts.append((z64_at - z64_size, z64_size))

    return [(start, size) for start, size in layouts if 0 <= start <= filesize]


def _read_zip64_eocd(fh, eocd_at: int, filesize: int) -> list[Tuple[int, int]]:
    """Every Zip64 EOCD record a stdlib might use, as ``(record_offset,
    size_cd)``: the one at the locator's offset (patched interpreters try it
    first) and the slot immediately before the locator (the only one older
    interpreters read, and the patched ones' fallback when data was prepended
    to the file)."""
    loc_at = eocd_at - _ZIP64_LOCATOR_SIZE
    if loc_at < 0:
        return []
    fh.seek(loc_at)
    loc = fh.read(_ZIP64_LOCATOR_SIZE)
    if len(loc) != _ZIP64_LOCATOR_SIZE or not loc.startswith(_ZIP64_LOCATOR_SIG):
        return []
    reloff = int.from_bytes(loc[8:16], "little")
    found = []
    for cand in dict.fromkeys((reloff, loc_at - _ZIP64_EOCD_SIZE)):
        if cand < 0 or cand + _ZIP64_EOCD_SIZE > filesize:
            continue
        fh.seek(cand)
        rec = fh.read(_ZIP64_EOCD_SIZE)
        if len(rec) == _ZIP64_EOCD_SIZE and rec.startswith(_ZIP64_EOCD_SIG):
            found.append((cand, int.from_bytes(rec[40:48], "little")))
    return found


def _central_directory_entry_count(path: str) -> int:
    """Number of members ``ZipFile`` would materialise for this archive.

    Counts the central-directory records rather than trusting the EOCD's
    "total entries" field, because that field is not what bounds the stdlib:
    ``_RealGetContents`` reads ``size_cd`` bytes and walks records until they
    are consumed, never once consulting the count. An archive can therefore
    declare 5 entries and still drive millions of ZipInfo allocations.

    Deliberately hand-rolled rather than via ``zipfile``: the whole point is to
    learn the member count *before* handing the file to ``ZipFile``, whose
    constructor is where the unbounded allocation happens. The walk itself
    allocates nothing per record and stops as soon as the ceiling is passed, so
    screening a hostile archive costs a bounded sequential read.

    Returns -1 when the archive can't be walked, leaving the verdict to
    ``ZipFile`` (which raises BadZipFile / OSError for a genuinely broken file).
    """
    try:
        with open(path, "rb", buffering=1 << 20) as fh:
            fh.seek(0, os.SEEK_END)
            filesize = fh.tell()
            # The largest count over every layout a stdlib could parse (see
            # _locate_central_directory); -1 only when none can be walked.
            best = -1
            for start_dir, size_cd in _locate_central_directory(fh, filesize):
                best = max(best, _walk_central_directory(fh, filesize, start_dir, size_cd))
                if best > _MAX_SZI_ENTRIES:
                    break
            return best
    except OSError:
        return -1


def _walk_central_directory(fh, filesize: int, start_dir: int, size_cd: int) -> int:
    """Count the central-directory records in one layout, stopping past the
    ceiling; -1 when they can't be walked."""
    # ZipFile reads exactly size_cd bytes starting at start_dir; bytes the
    # EOCD claims beyond EOF simply are not there (it raises "Truncated
    # central directory"), so the walk shares that budget.
    budget = min(size_cd, filesize - start_dir)
    fh.seek(start_dir)
    count = 0
    consumed = 0
    while consumed < budget:
        hdr = fh.read(_CD_RECORD_FIXED)
        if len(hdr) != _CD_RECORD_FIXED or not hdr.startswith(_CD_RECORD_SIG):
            return -1
        name_len = int.from_bytes(hdr[28:30], "little")
        extra_len = int.from_bytes(hdr[30:32], "little")
        comment_len = int.from_bytes(hdr[32:34], "little")
        consumed += _CD_RECORD_FIXED + name_len + extra_len + comment_len
        count += 1
        if count > _MAX_SZI_ENTRIES:
            # Over the ceiling whatever the remaining records say.
            return count
        skip = name_len + extra_len + comment_len
        if len(fh.read(skip)) != skip:
            return -1
    return count


def _open_szi(path: str) -> zipfile.ZipFile:
    """Open an SZI archive, refusing one that carries too many members.

    See ``_MAX_SZI_ENTRIES``: the screen has to happen before ``ZipFile`` runs,
    because the constructor is where the unbounded allocation would occur.
    """
    count = _central_directory_entry_count(path)
    if count > _MAX_SZI_ENTRIES:
        raise ValueError(
            f"SZI archive contains more than {_MAX_SZI_ENTRIES} members"
        )
    return zipfile.ZipFile(path)


def _find_dzi_member(zf: zipfile.ZipFile) -> str:
    for name in zf.namelist():
        if _DZI_ROOT_RE.match(name):
            return name
    raise ValueError("SZI archive missing root .dzi descriptor")


def _ensure_stored(zf: zipfile.ZipFile) -> None:
    for info in zf.infolist():
        if info.is_dir():
            continue
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError(
                "SZI archive must be ZIP_STORED (uncompressed); "
                f"entry {info.filename!r} uses compress_type={info.compress_type}"
            )


def _read_member_capped(
    zf: zipfile.ZipFile, name: str, cap: int, *, what: str
) -> bytes:
    """Read a ZIP member into RAM only after its declared size clears ``cap``.

    SZI members are ZIP_STORED, so ``file_size`` is the exact allocation
    ``zf.read`` will make. Checking it first turns a crafted multi-GB member
    into a fast ValueError instead of an OOM. Raises KeyError (from
    ``getinfo``) when the member is absent, matching ``zf.read``'s contract so
    callers can keep their existing missing-member handling.
    """
    size = zf.getinfo(name).file_size
    if size > cap:
        raise ValueError(
            f"SZI {what} {name!r} is {size} bytes, exceeding the {cap}-byte cap"
        )
    return zf.read(name)


def _parse_dzi_xml(xml_bytes: bytes) -> Dict[str, Any]:
    # DZI XML may or may not declare the
    # http://schemas.microsoft.com/deepzoom/2008 namespace, so match on the
    # local tag name rather than a fixed qualified name.
    root = _safe_parse(xml_bytes)
    image_attrs = root.attrib if root.tag.split("}")[-1] == "Image" else {}
    if not image_attrs:
        for el in root.iter():
            if el.tag.split("}")[-1] == "Image":
                image_attrs = el.attrib
                break
    size_attrs: Dict[str, str] = {}
    for el in root.iter():
        if el.tag.split("}")[-1] == "Size":
            size_attrs = el.attrib
            break
    if not size_attrs:
        raise ValueError("SZI .dzi descriptor missing <Size>")
    width = _bounded_int(size_attrs["Width"], name="Width", lo=1, hi=_MAX_DZI_DIMENSION)
    height = _bounded_int(size_attrs["Height"], name="Height", lo=1, hi=_MAX_DZI_DIMENSION)
    # lo=64: a TileSize of 1 would make the thumbnail builder iterate
    # cols*rows ~= level_area tiles (up to ~16M zf.read calls = minutes of CPU)
    # for a malicious descriptor. 64 is far below any real DZI tile size (spec
    # default 254/256), so it never trips on a legitimate slide.
    tile_size = _bounded_int(
        image_attrs.get("TileSize", "256"), name="TileSize", lo=64, hi=_MAX_DZI_TILE_SIZE,
    )
    overlap = _bounded_int(
        image_attrs.get("Overlap", "0"), name="Overlap", lo=0, hi=_MAX_DZI_OVERLAP,
    )
    fmt = (image_attrs.get("Format") or "jpeg").lower()
    return {
        "width": width,
        "height": height,
        "tile_size": tile_size,
        "overlap": overlap,
        "format": fmt,
    }


def read_szi_properties(path: str) -> Dict[str, Any]:
    with _open_szi(path) as zf:
        _ensure_stored(zf)
        dzi_name = _find_dzi_member(zf)
        meta = _parse_dzi_xml(
            _read_member_capped(
                zf, dzi_name, _MAX_DZI_DESCRIPTOR_BYTES, what="descriptor"
            )
        )
    # MPP is not part of the DZI spec. Admins can fill it in via PATCH later;
    # the viewer hides the scale bar / zoom indicator while it is 0.
    return {
        "width": meta["width"],
        "height": meta["height"],
        "mpp_x": 0.0,
        "mpp_y": 0.0,
        "objective_power": 0.0,
        "vendor": "szi",
        "acquired_at": "",
    }


def _stem_from_dzi_name(dzi_name: str) -> str:
    return dzi_name[: -len(".dzi")]


# A ``<stem>_files/<level>/`` directory name, in canonical decimal only (the
# same rule as dzi._CANONICAL_UINT for tile URLs). ``int()`` alone also takes
# "-1000000000000", "+8", "08", "0_8", " 8" and non-ASCII digits. A negative
# level slips under the ``lvl <= max_level`` bound and turns the thumbnail
# builder's ``2 ** (max_level - lvl)`` into a Python big int sized by the
# number written in the name: a 2 KB archive naming level -10**12 asks for a
# ~125 GB integer, which neither the Width/Height caps nor the member caps
# can bound. The other spellings name directories nobody reads — the viewer
# (szi-tile-source) and the thumbnail builder both address a tile as
# ``<level>/<col>_<row>`` with a plain integer — so they are junk, skipped
# like any other non-level entry rather than refused.
_CANONICAL_LEVEL_RE = re.compile(r"0|[1-9][0-9]{0,8}")


def _level_number(head: str, max_level: int) -> int | None:
    """The pyramid level a ``_files/<head>/`` directory holds, or None when
    ``head`` is not the canonical name of a level in ``[0, max_level]``.

    ``max_level`` must come from the validated Width/Height, never from the
    directory names themselves (see make_szi_thumbnail)."""
    if not _CANONICAL_LEVEL_RE.fullmatch(head):
        return None
    lvl = int(head)
    return lvl if lvl <= max_level else None


def _existing_levels(
    zf: zipfile.ZipFile, files_prefix: str, max_level: int
) -> Dict[int, str]:
    """Levels the archive has tiles for, mapped to their directory name.

    Only canonical names of levels in ``[0, max_level]`` are returned (see
    ``_CANONICAL_LEVEL_RE``), so every caller can use the key in a power of
    two or a member name without range-checking it again."""
    levels: Dict[int, str] = {}
    prefix = files_prefix.rstrip("/") + "/"
    for name in zf.namelist():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if not rest or "/" not in rest:
            continue
        head = rest.split("/", 1)[0]
        lvl = _level_number(head, max_level)
        if lvl is None:
            continue
        levels.setdefault(lvl, head)
    return levels


def make_szi_thumbnail(
    path: str,
    out_path: str,
    max_size: Tuple[int, int] = (512, 512),
) -> None:
    with _open_szi(path) as zf:
        dzi_name = _find_dzi_member(zf)
        meta = _parse_dzi_xml(
            _read_member_capped(
                zf, dzi_name, _MAX_DZI_DESCRIPTOR_BYTES, what="descriptor"
            )
        )
        width = meta["width"]
        height = meta["height"]
        tile_size = meta["tile_size"]
        overlap = meta["overlap"]
        fmt = meta["format"]
        stem = _stem_from_dzi_name(dzi_name)
        files_prefix = f"{stem}_files"

        # DZI levels are numbered from 0 (1x1) up to ceil(log2(max(W, H))).
        # Derive max_level from the *validated* Width/Height rather than from
        # max(present.keys()): the level directory names come straight from the
        # archive, so a crafted SZI can add a spurious higher-numbered
        # ``_files/<level>/`` dir. Trusting it would inflate max_level and shift
        # every ``scale = 2 ** (max_level - lvl)``, making the builder sample
        # the wrong level and clip the thumbnail to a fraction of the slide.
        # _existing_levels discards any level dir outside [0, max_level] (and
        # any non-canonical spelling) before a single power is computed.
        max_dim = max(width, height)
        max_level = math.ceil(math.log2(max_dim)) if max_dim > 1 else 0
        present = _existing_levels(zf, files_prefix, max_level)
        if not present:
            raise ValueError(
                "SZI archive has no tile level within the pyramid bounds"
            )

        # Pick the smallest level whose long edge is >= 256 px and whose
        # total area stays under ~16 MP, to keep memory bounded for huge
        # pyramids without blurring the thumbnail.
        chosen_level = None
        best_under_budget = None
        for lvl in sorted(present.keys()):
            scale = 2 ** (max_level - lvl)
            lvl_w = max(1, math.ceil(width / scale))
            lvl_h = max(1, math.ceil(height / scale))
            if lvl_w * lvl_h <= 16 * 1024 * 1024:
                # Largest in-budget level (keys are ascending), kept as a
                # fallback for sparse pyramids that never hit the sweet spot.
                best_under_budget = lvl
            if max(lvl_w, lvl_h) < 256:
                continue
            if lvl_w * lvl_h > 16 * 1024 * 1024:
                continue
            chosen_level = lvl
            break
        if chosen_level is None:
            # No level satisfied both ">=256px long edge" and "<=16MP area"
            # (e.g. a sparse pyramid that jumps from a sub-256px level straight
            # to one over the area budget). Prefer the largest in-budget level.
            if best_under_budget is not None:
                chosen_level = best_under_budget
            else:
                # NO level fits the 16MP budget at all — e.g. a pyramid that
                # ships ONLY its full-resolution base. Falling back to
                # min(present.keys()) would pick the base here, so Image.new() below
                # would allocate a canvas up to _MAX_DZI_DIMENSION per side
                # (~4e12 px) and OOM the worker. Refuse instead; the caller
                # turns this into a clean 422.
                raise ValueError(
                    "SZI pyramid has no level within the thumbnail size budget"
                )

        scale = 2 ** (max_level - chosen_level)
        lvl_w = max(1, math.ceil(width / scale))
        lvl_h = max(1, math.ceil(height / scale))
        canvas = Image.new("RGB", (lvl_w, lvl_h), (255, 255, 255))

        cols = math.ceil(lvl_w / tile_size)
        rows = math.ceil(lvl_h / tile_size)
        for col in range(cols):
            for row in range(rows):
                tile_name = f"{files_prefix}/{chosen_level}/{col}_{row}.{fmt}"
                try:
                    raw = _read_member_capped(
                        zf, tile_name, _MAX_DZI_TILE_BYTES, what="tile"
                    )
                except KeyError:
                    continue
                except ValueError:
                    # Oversized/crafted tile member: skip it (leaving that
                    # region of the canvas white) instead of aborting the whole
                    # thumbnail, mirroring the missing-tile handling above.
                    continue
                # Pin the decoders: the tile bytes come from an uploaded
                # archive, and without formats= Pillow sniffs every plugin it
                # ships (PSD, McIdas, JPEG 2000, ...) — a crafted "tile" would
                # reach whichever decoder its magic bytes name.
                try:
                    tile_img = Image.open(io.BytesIO(raw), formats=_SZI_TILE_FORMATS)
                except UnidentifiedImageError:
                    # Not a browser image format: skip it like a missing tile.
                    continue
                with tile_img:
                    # _MAX_DZI_TILE_BYTES only bounds the COMPRESSED size: a
                    # ~170 KB PNG can declare a huge canvas and expand to ~1 GB
                    # on load(). Header dimensions are known before decoding, so
                    # refuse anything beyond the SZI's declared tile geometry
                    # (plus a few px of slack) and skip it like a bad tile.
                    max_px = tile_size + 2 * overlap + _SZI_TILE_DIM_SLACK
                    if tile_img.width > max_px or tile_img.height > max_px:
                        continue
                    tile_img.load()
                    rgb = tile_img.convert("RGB")
                # DZI tiles include `overlap` extra pixels on every interior
                # edge. Crop them off before pasting so neighbours don't
                # overwrite each other with duplicated rows/columns.
                left = overlap if col > 0 else 0
                top = overlap if row > 0 else 0
                if left or top:
                    rgb = rgb.crop((left, top, rgb.width, rgb.height))
                canvas.paste(rgb, (col * tile_size, row * tile_size))

        canvas.thumbnail(max_size, Image.LANCZOS)
        # Imported lazily to keep this module's import graph one-directional:
        # openslide_utils imports szi_utils (never the reverse at module
        # scope), and it pulls in the OpenSlide native library, which the SZI
        # path otherwise does not need. By the time this runs the caller
        # (openslide_utils.make_thumbnail) has already imported it, so the
        # lookup is a sys.modules hit.
        from .openslide_utils import save_thumbnail_atomic
        save_thumbnail_atomic(canvas, out_path)
