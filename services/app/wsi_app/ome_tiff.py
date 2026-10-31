"""OME-TIFF slide reader exposing openslide-python's ``AbstractSlide`` API.

OpenSlide has no OME-TIFF driver. It opens an OME-TIFF through its generic
tiled-TIFF fallback, which only walks the main IFD chain — but every modern
pyramidal OME-TIFF writer (Bio-Formats 6+, QuPath, raw2ometiff, libvips)
stores the reduced resolutions in SubIFDs. OpenSlide therefore sees a single
full-resolution level with no MPP, and every low-zoom Deep Zoom tile has to
downsample the whole base image: seconds per tile on a real slide, and no
scale bar.

This module reads the pyramid with tifffile (TIFF structure + the OME-XML
plane mapping) and imagecodecs (the segment codecs tifffile delegates to),
and presents it through the same ``AbstractSlide`` interface OpenSlide
implements. ``DeepZoomGenerator``, the thumbnailer and the associated-image
extractor therefore consume an ``OmeTiffSlide`` unchanged; callers get one via
``openslide_utils.open_wsi``.

Scope (anything else is refused with :class:`UnsupportedOmeTiffError`, which
names the reason so the upload endpoint can say why):

* Brightfield RGB — interleaved or planar samples in one page, or three
  separate 8-bit channel planes (C=3) — and single-channel grayscale.
  Non-8-bit data is mapped to 8 bits with ONE window computed from the
  smallest pyramid level at open time, so every tile uses the same mapping
  (per-tile auto-contrast would leave visible seams).
* Pyramids in SubIFDs. A file without a pyramid is accepted only while its
  whole image stays under ``WSI_OME_MAX_LOWEST_LEVEL_PIXELS``: the lowest
  Deep Zoom levels read the smallest stored level in full, so that bound is
  what keeps a single tile request from pulling gigapixels into memory.
* Z/T stacks show the first plane (Z=0, T=0).
* The main image is the first OME Image in the file (by IFD) that is not a
  label / macro / thumbnail — the image OpenSlide shows for such a file, so
  either reader opens the same image.
* Segment codecs: the byte codecs (none, LZW, Deflate, zstd, ...) — tifffile
  bounds their output by the segment size — and the image codecs whose
  header this module checks against the segment size before decoding (JPEG,
  JPEG 2000, WebP, PNG), so a crafted header can't make a decoder allocate
  gigabytes. Other image codecs (JPEG XL, JPEG XR, old-style JPEG, ...) are
  refused.
* Single-file OME-TIFF only. Multi-file datasets (companion / ``BinaryOnly``
  files, TiffData UUIDs naming other files) are refused: the store keeps one
  file per slide, and following a FileName reference would read other files
  on the server.

Multi-channel fluorescence (C=2 or C>=4, or three 16-bit channels) is out of
scope and refused with reason ``multichannel``.
"""
from __future__ import annotations

import itertools
import logging
import math
import os
import re
import struct
import threading
import types
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Iterator, Optional

import numpy as np
import openslide
import tifffile
from PIL import Image

from .config import env_int
from .szi_utils import _FORBIDDEN_XML_RE

_logger = logging.getLogger("wsi_app.ome_tiff")

# tifffile logs an ERROR for every tag it cannot parse and goes on without it,
# so a crafted file packs millions of those lines into a few MB: one 19 MB
# TIFF would write 178 MB to the journal and spend ~20 s of a worker thread on
# logging each time it was opened. Every reader here (this module and
# openslide_utils.check_tiff_segment_sizes) acts on what tifffile raises,
# never on those lines, so they are dropped; the upload probe child does the
# same (openslide_utils._PROBE_CODE).
logging.getLogger("tifffile").setLevel(logging.CRITICAL)

VENDOR = "ome-tiff"

# Extensions sniffed for OME-XML. ``.ome.tif`` / ``.ome.tiff`` reach here as
# ``.tif`` / ``.tiff`` (the extension check only looks at the final suffix);
# ``.btf`` / ``.tf2`` / ``.tf8`` are the BigTIFF spellings Bio-Formats uses.
# Other TIFF-based vendor formats (.svs, .ndpi, .scn, .bif) keep going
# straight to OpenSlide without the extra open.
OME_TIFF_EXTS = (".tif", ".tiff", ".btf", ".tf2", ".tf8")

# The smallest stored level is read in full by the lowest Deep Zoom levels
# (and by the thumbnailer), so it bounds the memory one tile request can
# need. A pyramidal WSI's smallest level is typically well under 4 Mpx; a
# file without a pyramid is its own smallest level, so this is also the size
# limit for unpyramided images. 16 Mpx (4096 x 4096) keeps the transient
# RGBA + composite buffers of one such read around 160 MB.
_MAX_LOWEST_LEVEL_PIXELS = env_int("WSI_OME_MAX_LOWEST_LEVEL_PIXELS", 16_777_216)

# Decoded-segment cache shared by every open OME-TIFF in the process (the
# same shape as OpenSlide 4's process-wide tile cache). A 256 px Deep Zoom
# tile with a 1 px overlap straddles up to 9 stored tiles, and neighbouring
# Deep Zoom tiles share most of them, so a small cache avoids decoding the
# same stored tile over and over while a viewer pans. 0 disables it.
_SEGMENT_CACHE_BYTES = env_int("WSI_OME_TILE_CACHE_BYTES", 32 * 1024 * 1024)

# Largest downsample between two consecutive stored levels. Deep Zoom reads
# each tile from the next-finer stored level, so the region it reads is up
# to (tile size x this ratio) pixels on a side: 16 keeps that near 4 k x 4 k.
# Real writers use 2 (Bio-Formats, libvips) or 4 (QuPath).
_MAX_LEVEL_RATIO = 16.0


def level_gap_too_wide(parent: tuple[int, int], child: tuple[int, int]) -> bool:
    """True when the step from level ``parent`` to the next smaller ``child``
    (``(width, height)``) exceeds _MAX_LEVEL_RATIO on either side.

    Writers round a level's size to whole pixels, mostly down, so an exact
    16x step can come out a hair over 16x: 16385 -> 1024 (16.0009x), or an
    SVS 46001 -> 2875 written 16x down. The child is allowed one pixel of
    that rounding per side: refused only when no rounding of a 16x level
    explains the sizes. That adds at most 16 px to a side of what one tile
    reads, nothing a read budget notices.
    """
    return any(
        p > _MAX_LEVEL_RATIO * (max(c, 1) + 1) for p, c in zip(parent, child)
    )


def level_gap_message(parent: tuple[int, int], child: tuple[int, int]) -> str:
    ratio = max(p / max(c, 1) for p, c in zip(parent, child))
    # Two decimals: a refused 16.04x must not read "16.0x (max 16x)".
    return f"consecutive levels are {ratio:.2f}x apart (max {_MAX_LEVEL_RATIO:g}x)"

# Largest single stored segment (tile or strip) in pixels. One segment is
# decoded whole even when a request needs a sliver of it, so a file that
# stores its base level as one giant strip would otherwise make every tile
# request decode the entire image.
_MAX_SEGMENT_PIXELS = max(4096 * 4096, _MAX_LOWEST_LEVEL_PIXELS)

# Largest decoded segment in bytes, all samples included, so a wide sample
# type or many samples per pixel can't multiply the pixel bound above. A
# 4096 x 4096 16-bit RGBA tile is exactly this.
_MAX_SEGMENT_BYTES = 128 * 1024 * 1024

# A real slide's OME-XML is a few KB to a few hundred KB. The cap only stops
# a pathological description from being parsed at all.
_MAX_OME_XML_BYTES = 16 * 1024 * 1024

# Largest associated (label / macro) image decoded at all, in pixels; this
# reader reads one whole from its smallest level, and openslide_utils applies
# the same ceiling to the macro / label pages of every other format. Real
# macro / label images sit far below it.
_ASSOCIATED_MAX_PIXELS = 32_000_000

# Samples used to derive the intensity window of non-8-bit data. The smallest
# level is strided down to about this many pixels; percentiles don't need
# more, and it keeps the open-time pass cheap on a large unpyramided image.
_WINDOW_SAMPLE_PIXELS = 1_000_000

# TIFF Photometric values handled here.
_PHOTOMETRIC_MINISWHITE = 0
_PHOTOMETRIC_MINISBLACK = 1
_PHOTOMETRIC_RGB = 2
_PHOTOMETRIC_YCBCR = 6

# TIFF ExtraSamples values that mark the extra sample as alpha.
_EXTRASAMPLE_ASSOCALPHA = 1
_EXTRASAMPLE_UNASSALPHA = 2

# Image codecs allocate their output at the size their own header claims,
# not the segment size tifffile expects, so each one served here has its
# header checked first (_check_segment_header). JPEG: tifffile routes these
# through its JPEG decoder, which is also what converts YCbCr to RGB.
_JPEG_COMPRESSIONS = frozenset({7, 33007, 34892})
_JPEG2000_COMPRESSIONS = frozenset({33003, 33004, 33005, 34712})
_WEBP_COMPRESSIONS = frozenset({50001})
_PNG_COMPRESSIONS = frozenset({34933})
_HEADER_CHECKED_COMPRESSIONS = (
    _JPEG_COMPRESSIONS | _JPEG2000_COMPRESSIONS | _WEBP_COMPRESSIONS | _PNG_COMPRESSIONS
)
# Every other image codec is refused: old-style JPEG (needs a JPEG header
# synthesised from tags), JPEG XR, JPEG XL, Jetraw and EER.
_REFUSED_COMPRESSIONS = (
    frozenset(getattr(tifffile.TIFF, "IMAGE_COMPRESSIONS", ()))
    | {6, 22610, 34934, 48124, 50002, 52546, 65000, 65001, 65002}
) - _HEADER_CHECKED_COMPRESSIONS

# What a malformed file makes tifffile / the codecs raise. Anything else
# escaping the open is a bug (for example a tifffile API change) and is
# logged as such rather than passed off as a damaged upload.
_DATA_ERRORS = (
    tifffile.TiffFileError, ValueError, KeyError, IndexError, struct.error,
    OSError, EOFError, NotImplementedError, OverflowError, RuntimeError,
)

# OME length units -> micrometres. Absent unit means micrometres (the OME
# schema default). Both the micro sign (U+00B5) and the Greek mu (U+03BC)
# appear in the wild.
_UNIT_TO_UM = {
    "m": 1e6,
    "dm": 1e5,
    "cm": 1e4,
    "mm": 1e3,
    "µm": 1.0,
    "μm": 1.0,
    "um": 1.0,
    "nm": 1e-3,
    "pm": 1e-6,
    "Å": 1e-4,
    "in": 25400.0,
    "thou": 25.4,
}


class UnsupportedSlideError(ValueError):
    """A slide refused at upload for its layout, not because it is damaged.

    ``code`` (``<CODE_PREFIX>.<reason>``) is the stable key the upload,
    replace and import paths hand the frontend as ``adminDash.apiErr.<code>``;
    the message carries the specifics for the server log. Raised as is
    (``slideLayout.*``) by openslide_utils for an OpenSlide-read slide whose
    pyramid Deep Zoom can't serve within bounds — the same ``no_pyramid`` /
    ``lowest_level_too_large`` / ``level_gap`` limits this reader applies to an OME-TIFF (see
    ``_MAX_LEVEL_RATIO``) — and as :class:`UnsupportedOmeTiffError` by this
    reader.
    """

    CODE_PREFIX = "slideLayout"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    @property
    def code(self) -> str:
        return f"{self.CODE_PREFIX}.{self.reason}"


class UnsupportedOmeTiffError(UnsupportedSlideError):
    """An OME-TIFF this reader recognises but deliberately does not serve.

    ``reason`` is a stable machine-readable code the upload endpoints pass to
    the frontend (``adminDash.apiErr.omeTiff.<reason>``); the message
    carries the specifics for the server log.
    """

    CODE_PREFIX = "omeTiff"
    REASONS = (
        "multifile",
        "multichannel",
        "pixel_type",
        "no_pyramid",
        "lowest_level_too_large",
        "level_gap",
        "segment_size",
        "codec",
        "structure",
    )

    def __init__(self, reason: str, message: str, *, fallback_ok: bool = False) -> None:
        super().__init__(reason if reason in self.REASONS else "structure", message)
        # The file is a TIFF whose OME metadata tifffile could not reconcile
        # with its pages; OpenSlide's generic-TIFF reader (which ignores the
        # OME-XML) may still read it.
        self.fallback_ok = fallback_ok


def is_ome_tiff(path: str) -> bool:
    """True when ``path`` is a TIFF whose first IFD carries OME-XML.

    Content-based on purpose: an upload is staged as
    ``<id>_<name>.upload-<token>.tif`` and stored as ``<id>_<name>``, so a
    ``.ome.tif`` double suffix cannot be relied on. Any error (missing file,
    not a TIFF) is simply "not OME" so the caller falls through to OpenSlide
    and reports that failure the usual way.
    """
    if not path.lower().endswith(OME_TIFF_EXTS):
        return False
    try:
        with tifffile.TiffFile(path, _multifile=False) as tf:
            return bool(tf.is_ome)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Process-wide decoded-segment cache
# ---------------------------------------------------------------------------

_MISS = object()


class _SegmentCache:
    """Byte-bounded LRU of decoded segments, keyed by (slide uid, level, index).

    ``None`` values record an empty (sparse) segment so it is not re-read on
    every request. Every entry is also charged the dict / key overhead, so an
    all-empty file can't grow the dict past the byte bound either.
    """

    _ENTRY_OVERHEAD = 256

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        self._data: "OrderedDict[tuple, Optional[np.ndarray]]" = OrderedDict()
        self._bytes = 0

    def _cost(self, value: Optional[np.ndarray]) -> int:
        return self._ENTRY_OVERHEAD + (0 if value is None else int(value.nbytes))

    def get(self, key: tuple) -> Any:
        with self._lock:
            value = self._data.get(key, _MISS)
            if value is not _MISS:
                self._data.move_to_end(key)
            return value

    def put(self, key: tuple, value: Optional[np.ndarray]) -> None:
        cost = self._cost(value)
        if self.max_bytes <= 0 or cost > self.max_bytes:
            return
        with self._lock:
            if key in self._data:
                return
            self._data[key] = value
            self._bytes += cost
            while self._bytes > self.max_bytes and self._data:
                _, old = self._data.popitem(last=False)
                self._bytes -= self._cost(old)

    def drop_owner(self, uid: int) -> None:
        with self._lock:
            for key in [k for k in self._data if k[0] == uid]:
                self._bytes -= self._cost(self._data.pop(key))

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "bytes": self._bytes, "max_bytes": self.max_bytes}


_segment_cache = _SegmentCache(_SEGMENT_CACHE_BYTES)
_uid_counter = itertools.count(1)


def segment_cache_stats() -> dict:
    return _segment_cache.stats()


# ---------------------------------------------------------------------------
# OME-XML metadata
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el, name: str) -> Iterator[Any]:
    return (c for c in el if _local(c.tag) == name)


def _first_child(el, name: str):
    return next(_children(el, name), None)


def _length_um(value: Optional[str], unit: Optional[str]) -> float:
    """Convert an OME length attribute to micrometres; 0.0 when unusable."""
    if not value:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    factor = _UNIT_TO_UM.get((unit or "µm").strip())
    if factor is None:
        return 0.0
    um = v * factor
    # A pixel size outside (0, 10 mm] is not a microscope calibration.
    if not math.isfinite(um) or um <= 0 or um > 1e4:
        return 0.0
    return um


def _parse_ome_xml(omexml: str):
    """Parse the OME-XML with the same DOCTYPE / ENTITY refusal as SZI."""
    import xml.etree.ElementTree as ET

    raw = omexml.encode("utf-8", errors="replace")
    if len(raw) > _MAX_OME_XML_BYTES:
        raise UnsupportedOmeTiffError("structure", f"OME-XML is {len(raw)} bytes")
    if _FORBIDDEN_XML_RE.search(raw):
        raise UnsupportedOmeTiffError("structure", "OME-XML must not contain DOCTYPE or ENTITY declarations")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UnsupportedOmeTiffError("structure", f"OME-XML does not parse: {exc}") from exc


def _looks_multifile(root) -> bool:
    """True when the OME-XML points at pixel data in another file."""
    if any(_local(el.tag) == "BinaryOnly" for el in root):
        return True
    root_uuid = root.attrib.get("UUID")
    uuids, names = set(), set()
    for el in root.iter():
        if _local(el.tag) != "UUID":
            continue
        text = (el.text or "").strip()
        if root_uuid and text and text != root_uuid:
            return True
        if text:
            uuids.add(text)
        if el.attrib.get("FileName"):
            names.add(el.attrib["FileName"])
    # Without a root UUID to compare against, TiffData naming a file (or
    # more than one file UUID) still means the planes are spread over files.
    # Only consulted once the pixel data turned out incomplete.
    return not root_uuid and (len(uuids) > 1 or bool(names))


def _image_metadata(root, width: int, height: int, name: Optional[str] = None) -> dict[str, str]:
    """Pull calibration / provenance for the Image whose Pixels match (w, h).

    The main series is identified by its base dimensions (and its name, when
    two Images share a size) rather than its position: tifffile skips Image
    elements without pixel data, so series indices and Image indices need
    not line up, whereas the label and macro images never share the
    pyramid's base size.
    """
    images = list(_children(root, "Image"))
    sized = []
    for img in images:
        px = _first_child(img, "Pixels")
        if px is None:
            continue
        if px.attrib.get("SizeX") == str(width) and px.attrib.get("SizeY") == str(height):
            sized.append(img)
    chosen = next((img for img in sized if name and img.attrib.get("Name") == name), None)
    if chosen is None and sized:
        chosen = sized[0]
    if chosen is None and images:
        chosen = images[0]
    out: dict[str, str] = {}
    if chosen is None:
        return out

    name = chosen.attrib.get("Name")
    if name:
        out["ome.image-name"] = name
    acq = _first_child(chosen, "AcquisitionDate")
    if acq is not None and (acq.text or "").strip():
        out["ome.acquisition-date"] = acq.text.strip()

    px = _first_child(chosen, "Pixels")
    if px is not None:
        a = px.attrib
        for key, attr in (
            ("ome.pixel-type", "Type"), ("ome.size-c", "SizeC"), ("ome.size-z", "SizeZ"),
            ("ome.size-t", "SizeT"), ("ome.significant-bits", "SignificantBits"),
        ):
            if a.get(attr):
                out[key] = a[attr]
        mpp_x = _length_um(a.get("PhysicalSizeX"), a.get("PhysicalSizeXUnit"))
        mpp_y = _length_um(a.get("PhysicalSizeY"), a.get("PhysicalSizeYUnit"))
        if mpp_x > 0:
            out[openslide.PROPERTY_NAME_MPP_X] = repr(mpp_x)
        if mpp_y > 0:
            out[openslide.PROPERTY_NAME_MPP_Y] = repr(mpp_y)

    # Objective: the Image's ObjectiveSettings names an Objective in an
    # Instrument; fall back to the only Objective when there is exactly one.
    objectives = {o.attrib.get("ID"): o for o in root.iter() if _local(o.tag) == "Objective"}
    objective = None
    settings_el = _first_child(chosen, "ObjectiveSettings")
    if settings_el is not None:
        objective = objectives.get(settings_el.attrib.get("ID"))
    if objective is None and len(objectives) == 1:
        objective = next(iter(objectives.values()))
    if objective is not None:
        try:
            mag = float(objective.attrib.get("NominalMagnification", "0") or "0")
        except (TypeError, ValueError):
            mag = 0.0
        if math.isfinite(mag) and 0 < mag <= 1000:
            out[openslide.PROPERTY_NAME_OBJECTIVE_POWER] = f"{mag:g}"

    for instrument in _children(root, "Instrument"):
        scope = _first_child(instrument, "Microscope")
        if scope is not None:
            if scope.attrib.get("Manufacturer"):
                out["ome.microscope-manufacturer"] = scope.attrib["Manufacturer"]
            if scope.attrib.get("Model"):
                out["ome.microscope-model"] = scope.attrib["Model"]
        break
    return out


# ---------------------------------------------------------------------------
# Level / segment structure
# ---------------------------------------------------------------------------


class _Source:
    """One stored page feeding a level: where its segments are and how to
    decode them. ``seg_offset`` selects a sample plane of a planar-separate
    page (segment indices are sample-major there)."""

    __slots__ = ("offsets", "bytecounts", "decode", "jpegtables", "seg_offset", "max_bytes", "compression")

    def __init__(self, page, seg_offset: int, max_bytes: int, decode=None) -> None:
        keyframe = page.keyframe
        self.compression = int(keyframe.compression)
        self.offsets = np.asarray(page.dataoffsets, dtype=np.uint64)
        self.bytecounts = np.asarray(page.databytecounts, dtype=np.uint64)
        # Building the decode closure touches only tags already read, and it
        # holds no file handle, so it stays valid after the TiffFile closes.
        self.decode = decode or keyframe.decode
        self.jpegtables = keyframe.jpegtables
        self.seg_offset = seg_offset
        self.max_bytes = max_bytes


def _plane_jpeg_decode(data, index, /, *, jpegtables=None, **_kwargs):
    """Decode one sample plane of a planar-separate JPEG page.

    Bio-Formats (and so QuPath's OME-TIFF export, which writes through it)
    stores JPEG-compressed RGB with PlanarConfiguration=2 — every plane of a
    tile is its own single-component JPEG — yet tags the page
    Photometric=YCbCr. The planes hold R, G and B, not Y/Cb/Cr, so each is
    decoded as a plain grayscale JPEG. tifffile refuses the combination (it
    reads it as chroma-subsampled YCbCr), hence this stand-in with the same
    signature and return shape as tifffile's decode closure.
    """
    if data is None:
        return None, None, None
    import imagecodecs

    a = np.asarray(imagecodecs.jpeg_decode(data, tables=jpegtables))
    if a.ndim == 3:
        a = a[..., 0]
    h, w = a.shape[:2]
    return a.reshape(1, h, w, 1), None, (1, h, w, 1)


# -- codec headers -----------------------------------------------------------
#
# Each parser returns (height, width, components, bits per sample) as the
# codec header claims them, or None when the header can't be read.

_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})


def _jpeg_header(data: bytes) -> Optional[tuple[int, int, int, int]]:
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # TEM / RSTn carry no length
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS before any frame header
            return None
        length = (data[i + 2] << 8) | data[i + 3]
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if i + 10 > n:
                return None
            precision = data[i + 4]
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return height, width, data[i + 9], precision
        i += 2 + length
    return None


def _jpeg2000_header(data: bytes) -> Optional[tuple[int, int, int, int]]:
    cs = None
    if data[:4] == b"\xff\x4f\xff\x51":
        cs = data
    elif data[4:8] == b"jP  ":
        # JP2 file format: walk the boxes to the contiguous codestream.
        pos, n = 0, len(data)
        while pos + 8 <= n:
            length = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            head = 8
            if length == 1:
                if pos + 16 > n:
                    return None
                length = int.from_bytes(data[pos + 8:pos + 16], "big")
                head = 16
            elif length == 0:
                length = n - pos
            if length < head:
                return None
            if kind == b"jp2c":
                cs = data[pos + head:pos + length]
                break
            pos += length
    if cs is None or len(cs) < 42 or cs[:4] != b"\xff\x4f\xff\x51":
        return None
    xsiz, ysiz, xosiz, yosiz = struct.unpack(">IIII", cs[8:24])
    csiz = int.from_bytes(cs[40:42], "big")
    if len(cs) < 42 + 3 * csiz or xsiz <= xosiz or ysiz <= yosiz:
        return None
    bits = max(((cs[42 + 3 * k] & 0x7F) + 1 for k in range(csiz)), default=0)
    return ysiz - yosiz, xsiz - xosiz, csiz, bits


def _webp_header(data: bytes) -> Optional[tuple[int, int, int, int]]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
    elif chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    else:
        return None
    return height, width, 4, 8


_PNG_CHANNELS = {0: 1, 2: 3, 3: 4, 4: 2, 6: 4}


def _png_header(data: bytes) -> Optional[tuple[int, int, int, int]]:
    if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    channels = _PNG_CHANNELS.get(data[25])
    if channels is None:
        return None
    return height, width, channels, data[24]


def _check_segment_header(data: bytes, compression: int, max_h: int, max_w: int) -> None:
    """Refuse an image-codec segment whose own header claims more than the
    segment it sits in, before any decoder allocates for it.

    A segment may be smaller than the tile (edge tiles some writers store at
    their cropped size; the last strip), never larger. Byte codecs need no
    check: tifffile hands them an output buffer of the segment's size.
    """
    if compression in _JPEG_COMPRESSIONS:
        info = _jpeg_header(data)
    elif compression in _JPEG2000_COMPRESSIONS:
        info = _jpeg2000_header(data)
    elif compression in _WEBP_COMPRESSIONS:
        info = _webp_header(data)
    elif compression in _PNG_COMPRESSIONS:
        info = _png_header(data)
    else:
        return
    if info is None:
        raise ValueError(f"unreadable header in a compression-{compression} segment")
    height, width, components, bits = info
    sample_bytes = 1 if bits <= 8 else 2 if bits <= 16 else 4
    if (
        not (0 < height <= max_h and 0 < width <= max_w)
        or not (0 < components <= 4)
        or height * width * components * sample_bytes > _MAX_SEGMENT_BYTES
    ):
        raise ValueError(
            f"segment header claims {width}x{height}x{components} at {bits} bits "
            f"in a {max_w}x{max_h} segment"
        )


class _Level:
    __slots__ = ("width", "height", "seg_w", "seg_h", "segs_across", "segs_down", "sources")

    def __init__(self, width, height, seg_w, seg_h, sources) -> None:
        self.width = width
        self.height = height
        self.seg_w = seg_w
        self.seg_h = seg_h
        self.segs_across = -(-width // seg_w)
        self.segs_down = -(-height // seg_h)
        self.sources = sources


def _file_series(tf) -> list:
    """The file's series with unsqueezed shapes, on either tifffile API.

    tifffile 2026.5 turned ``TiffFile.series`` into a callable sequence that
    squeezes by default and dropped ``TiffPageSeries.get_axes`` /
    ``get_shape``; older releases return a plain list whose series answer
    ``get_axes(False)``. The positions match in both, so an index taken from
    this list can be looked up again later."""
    series = tf.series
    if callable(series):
        return list(series(squeeze=False))
    return list(series)


def _page_axes(series) -> list[tuple[str, int]]:
    """(axis, size) of the dimensions tifffile spreads across pages, in the
    C-order its ``pages`` list follows (every axis except Y, X and S).
    ``series`` must come from _file_series."""
    get_axes = getattr(series, "get_axes", None)
    if get_axes is not None:
        axes, shape = get_axes(False), series.get_shape(False)
    else:
        axes, shape = series.axes, series.shape
    return [(ax, int(n)) for ax, n in zip(axes, shape) if ax not in "YXS"]


def _page_index(page_axes: list[tuple[str, int]], **select: int) -> int:
    idx = 0
    for ax, n in page_axes:
        idx = idx * n + select.get(ax, 0)
    return idx


_ASSOCIATED_WORDS = {
    "label": "label", "barcode": "label",
    "macro": "macro", "overview": "macro",
    "thumbnail": "thumbnail",
}
_ASSOCIATED_FILLER = frozenset({"image", "slide", "img"})


def _is_associated_name(name: str) -> Optional[str]:
    """'label' / 'macro' / 'thumbnail' when an Image's name says it is one.

    Whole words only, and nothing else but filler ("label image", "Macro",
    "slide label 1"): "CD68 macrophages" or "unlabeled" name real images.
    """
    words = re.findall(r"[a-z]+", (name or "").lower())
    kinds = {_ASSOCIATED_WORDS[w] for w in words if w in _ASSOCIATED_WORDS}
    if len(kinds) != 1 or any(w not in _ASSOCIATED_WORDS and w not in _ASSOCIATED_FILLER for w in words):
        return None
    return kinds.pop()


def _series_first_ifd(series) -> int:
    """IFD index of a series' first page: its position in the file."""
    try:
        index = series.keyframe.index
        return int(index if isinstance(index, int) else index[0])
    except Exception:
        return 1 << 30


def _series_base_pixels(series) -> int:
    try:
        kf = series.keyframe
        return int(kf.imagewidth) * int(kf.imagelength)
    except Exception:
        return 0


class _Layout:
    """How one OME series maps onto the RGB / gray output — shared by the
    main image and the label / macro images."""

    __slots__ = ("dtype", "color", "layout", "channel_pages", "plane_decode", "invert",
                 "spp", "photometric", "planar", "alpha", "premultiplied")

    @property
    def channels(self) -> int:
        """Samples a decoded segment keeps: colour (3 or 1) plus alpha."""
        return (3 if self.color == "rgb" else 1) + (1 if self.alpha else 0)


def _series_layout(series) -> _Layout:
    """Classify a series' pixel layout, or refuse it with a reason."""
    kf0 = series.keyframe
    dtype = kf0.dtype
    if dtype is None or np.dtype(dtype).kind not in "uifb":
        raise UnsupportedOmeTiffError("pixel_type", f"unsupported sample type {dtype!r}")
    if np.dtype(dtype).itemsize > 4:
        raise UnsupportedOmeTiffError("pixel_type", f"{np.dtype(dtype)} samples are not supported")
    compression = int(kf0.compression)
    if compression in _REFUSED_COMPRESSIONS:
        raise UnsupportedOmeTiffError("codec", f"compression {compression} is not supported")
    lay = _Layout()
    lay.dtype = np.dtype(dtype)
    lay.spp = spp = int(kf0.samplesperpixel)
    lay.photometric = photometric = int(kf0.photometric)
    lay.planar = planar = int(kf0.planarconfig)
    lay.plane_decode = None
    extra = tuple(int(e) for e in (getattr(kf0, "extrasamples", None) or ()))
    lay.alpha = bool(extra) and extra[0] in (_EXTRASAMPLE_ASSOCALPHA, _EXTRASAMPLE_UNASSALPHA)
    lay.premultiplied = bool(extra) and extra[0] == _EXTRASAMPLE_ASSOCALPHA
    page_axes = _page_axes(series)
    n_c = dict(page_axes).get("C", 1)

    if spp > 4:
        raise UnsupportedOmeTiffError("multichannel", f"{spp} samples per pixel")
    if spp not in (2, 4):
        # Alpha is the 4th sample of RGBA or the 2nd of gray + alpha; an
        # extra sample that isn't alpha carries no picture and is dropped.
        lay.alpha = lay.premultiplied = False

    if spp >= 3:
        if photometric not in (_PHOTOMETRIC_RGB, _PHOTOMETRIC_YCBCR):
            if photometric == _PHOTOMETRIC_MINISBLACK:
                raise UnsupportedOmeTiffError("multichannel", f"{spp} samples per pixel without RGB photometric")
            raise UnsupportedOmeTiffError("pixel_type", f"photometric {photometric} is not supported")
        if photometric == _PHOTOMETRIC_YCBCR and int(kf0.compression) not in _JPEG_COMPRESSIONS:
            # Only the JPEG decoders convert YCbCr to RGB; any other codec
            # would hand back raw YCbCr samples painted as RGB.
            raise UnsupportedOmeTiffError("pixel_type", "YCbCr samples without JPEG compression")
        if n_c != 1:
            raise UnsupportedOmeTiffError("multichannel", f"RGB image with {n_c} channels")
        # Bio-Formats' planar JPEG layout: see _plane_jpeg_decode.
        if photometric == _PHOTOMETRIC_YCBCR and planar == 2:
            lay.plane_decode = _plane_jpeg_decode
        lay.color = "rgb"
        lay.layout = "contig" if planar == 1 else "planar"
        lay.channel_pages = [_page_index(page_axes)]
        if lay.plane_decode is not None and lay.alpha:
            raise UnsupportedOmeTiffError("pixel_type", "planar JPEG with an alpha plane")
    elif spp in (1, 2):
        if photometric not in (_PHOTOMETRIC_MINISBLACK, _PHOTOMETRIC_MINISWHITE):
            raise UnsupportedOmeTiffError("pixel_type", f"photometric {photometric} is not supported")
        if n_c == 1:
            lay.color = "gray"
            lay.layout = "contig" if planar == 1 else "planar"
            lay.channel_pages = [_page_index(page_axes)]
        elif n_c == 3 and spp == 1 and lay.dtype == np.uint8:
            # Three 8-bit channel planes: the brightfield RGB layout some
            # converters write instead of interleaved samples.
            lay.color = "rgb"
            lay.layout = "pages"
            lay.channel_pages = [_page_index(page_axes, C=c) for c in range(3)]
        else:
            raise UnsupportedOmeTiffError(
                "multichannel", f"{n_c} channels of {lay.dtype} are not an RGB or grayscale image"
            )
    else:
        raise UnsupportedOmeTiffError("pixel_type", f"{spp} samples per pixel")
    lay.invert = photometric == _PHOTOMETRIC_MINISWHITE
    return lay


def _build_level(level_series, lay: _Layout, file_size: int) -> _Level:
    """Validate one stored level of a series and index its segments."""
    pages = level_series.pages
    try:
        chosen = [pages[i] for i in lay.channel_pages]
    except IndexError as exc:
        raise UnsupportedOmeTiffError("structure", "a pyramid level is missing planes") from exc
    if any(p is None for p in chosen):
        # Only the planes actually shown must be present; a missing later
        # Z/T plane doesn't matter.
        raise UnsupportedOmeTiffError("structure", "a pyramid level is missing the planes shown")
    kf = chosen[0].keyframe
    if (
        np.dtype(kf.dtype) != lay.dtype
        or int(kf.samplesperpixel) != lay.spp
        or int(kf.photometric) != lay.photometric
        or int(kf.planarconfig) != lay.planar
    ):
        raise UnsupportedOmeTiffError("structure", "pyramid levels disagree on the pixel format")
    if int(kf.imagedepth) != 1 or (kf.is_tiled and int(kf.tiledepth) != 1):
        raise UnsupportedOmeTiffError("structure", "volumetric pages are not supported")
    # _series_layout judged the codec on the series' first page only; every
    # level (and channel page) names its own, and a refused one here would
    # still reach its decoder — which allocates what its header claims.
    for page in chosen:
        compression = int(page.keyframe.compression)
        if compression in _REFUSED_COMPRESSIONS:
            raise UnsupportedOmeTiffError("codec", f"compression {compression} is not supported")
        if lay.photometric == _PHOTOMETRIC_YCBCR and compression not in _JPEG_COMPRESSIONS:
            raise UnsupportedOmeTiffError("pixel_type", "YCbCr samples without JPEG compression")
    width, height = int(kf.imagewidth), int(kf.imagelength)
    if kf.is_tiled:
        seg_w, seg_h = int(kf.tilewidth), int(kf.tilelength)
    else:
        seg_w, seg_h = width, max(1, min(int(kf.rowsperstrip or height), height))
    if width <= 0 or height <= 0 or seg_w <= 0 or seg_h <= 0:
        raise UnsupportedOmeTiffError("structure", "empty level or segment")
    if seg_w * seg_h > _MAX_SEGMENT_PIXELS:
        raise UnsupportedOmeTiffError(
            "segment_size", f"{seg_w}x{seg_h} segments exceed {_MAX_SEGMENT_PIXELS} pixels"
        )
    samples_in_segment = lay.spp if lay.planar == 1 else 1
    raw_bytes = seg_w * seg_h * samples_in_segment * lay.dtype.itemsize
    if lay.layout == "planar":
        n_planes = lay.channels
    elif lay.layout == "pages":
        n_planes = len(chosen)
    else:
        n_planes = 1
    if raw_bytes * n_planes > _MAX_SEGMENT_BYTES:
        raise UnsupportedOmeTiffError(
            "segment_size", f"a decoded segment would take {raw_bytes * n_planes} bytes (max {_MAX_SEGMENT_BYTES})"
        )
    max_bytes = raw_bytes * 2 + 65536
    segs = -(-width // seg_w) * -(-height // seg_h)
    if lay.layout == "planar":
        sources = [_Source(chosen[0], s * segs, max_bytes, lay.plane_decode) for s in range(n_planes)]
    else:
        sources = [_Source(p, 0, max_bytes) for p in chosen]
    for src in sources:
        if len(src.offsets) < src.seg_offset + segs:
            raise UnsupportedOmeTiffError("structure", "segment table is shorter than the image")
        span = slice(src.seg_offset, src.seg_offset + segs)
        offs, cnts = src.offsets[span], src.bytecounts[span]
        if len(cnts) and int(cnts.max()) > max_bytes:
            raise UnsupportedOmeTiffError("structure", "a segment is larger than its decoded size allows")
        present = cnts > 0
        if present.any() and int((offs[present] + cnts[present]).max()) > file_size:
            raise UnsupportedOmeTiffError("structure", "segments point past the end of the file (truncated upload?)")
    return _Level(width, height, seg_w, seg_h, sources)


def _read_segment_bytes(fd: int, src: _Source, index: int) -> Optional[bytes]:
    off = int(src.offsets[index])
    cnt = int(src.bytecounts[index])
    if off == 0 or cnt == 0:
        return None
    if cnt > src.max_bytes:
        raise OSError(f"segment {index} is {cnt} bytes (max {src.max_bytes})")
    data = os.pread(fd, cnt, off)
    if len(data) != cnt:
        raise OSError(f"segment {index} is truncated ({len(data)} of {cnt} bytes)")
    return data


def _decode_segment(fd: int, lv: _Level, channels: int, idx: int) -> Optional[np.ndarray]:
    """Decode segment ``idx`` of a level to (h, w, channels) in the file's
    dtype — colour samples, then alpha when the layout has it — cropped to
    the image. None when the segment is absent.

    The result can be smaller than the segment: some writers store edge
    tiles at their cropped size, and tifffile then returns just that.
    """
    parts = []
    for src in lv.sources:
        index = src.seg_offset + idx
        data = _read_segment_bytes(fd, src, index)
        if data is None:
            return None
        _check_segment_header(data, src.compression, lv.seg_h, lv.seg_w)
        arr, _, _ = src.decode(data, index, jpegtables=src.jpegtables)
        if arr is None:
            return None
        a = np.asarray(arr)
        parts.append(a.reshape(a.shape[-3:]))  # (depth=1, h, w, samples) -> (h, w, samples)
    if len(parts) == 1:
        a = parts[0][..., :channels]
    else:
        h = min(p.shape[0] for p in parts)
        w = min(p.shape[1] for p in parts)
        a = np.concatenate([p[:h, :w, :1] for p in parts], axis=-1)
    if a.shape[-1] != channels:
        raise ValueError(f"segment has {a.shape[-1]} samples, expected {channels}")
    sx, sy = idx % lv.segs_across, idx // lv.segs_across
    valid_w = min(lv.seg_w, lv.width - sx * lv.seg_w)
    valid_h = min(lv.seg_h, lv.height - sy * lv.seg_h)
    return a[:valid_h, :valid_w]


class OmeTiffSlide(openslide.AbstractSlide):
    """A pyramidal OME-TIFF behind openslide-python's slide interface.

    Thread-safe for concurrent ``read_region`` calls (the Deep Zoom endpoints
    share one handle across the worker's threads): segments are read with
    ``os.pread`` on a private descriptor, so no file position is shared, and
    the decode closures are pure.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._uid = next(_uid_counter)
        self._closed = False
        self._props: dict[str, str] = {openslide.PROPERTY_NAME_VENDOR: VENDOR}
        self._associated: dict[str, int] = {}
        self._window: Optional[tuple[float, float]] = None
        # Open once and hand the same file object to tifffile (which leaves a
        # caller-supplied handle open), so the structure parsed below and the
        # bytes pread later come from the same inode even if the path is
        # replaced meanwhile.
        fh = open(path, "rb")
        self._fh = fh
        self._fd = fh.fileno()
        try:
            self._file_size = os.fstat(self._fd).st_size
            with tifffile.TiffFile(fh, _multifile=False) as tf:
                self._load_structure(tf)
            # Every level's codec first, so a corrupt or undecodable segment
            # is reported as such rather than surfacing from the window pass.
            self._probe_decode()
            self._window = self._compute_window()
            self._finish_properties()
        except (UnsupportedOmeTiffError, MemoryError):
            fh.close()
            raise
        except Exception as exc:
            fh.close()
            if not isinstance(exc, _DATA_ERRORS):
                _logger.warning("OME-TIFF reader failed unexpectedly on %s", path, exc_info=True)
            raise UnsupportedOmeTiffError("structure", f"unreadable OME-TIFF: {exc!r}") from exc

    # -- structure ---------------------------------------------------------

    def _load_structure(self, tf) -> None:
        if not tf.is_ome:
            raise UnsupportedOmeTiffError("structure", "no OME-XML in the first IFD")
        omexml = tf.ome_metadata or ""
        # Parse (and refuse DOCTYPE / ENTITY) before tifffile's own series
        # parser sees the XML.
        root = _parse_ome_xml(omexml)

        all_series = _file_series(tf)
        ome_series = [s for s in all_series if getattr(s, "kind", "") == "ome"]
        if not ome_series:
            if _looks_multifile(root):
                raise UnsupportedOmeTiffError("multifile", "pixel data is stored in other files")
            raise UnsupportedOmeTiffError(
                "structure", "no OME image series with pixel data (OME-XML does not match the pages)",
                fallback_ok=True,
            )

        # The main image is the first in the file that isn't a label / macro
        # / thumbnail — what OpenSlide shows for such a file, so a slide keeps
        # its image, size and tour coordinates whichever reader opens it.
        # A name alone never demotes a series too big to be a label / macro.
        candidates = [
            s for s in ome_series
            if _is_associated_name(s.name or "") is None or _series_base_pixels(s) > _ASSOCIATED_MAX_PIXELS
        ] or ome_series
        main = min(candidates, key=_series_first_ifd)
        if getattr(main, "is_multifile", False):
            raise UnsupportedOmeTiffError("multifile", "the main image references pages outside this file")

        for s in ome_series:
            if s is main:
                continue
            kind = _is_associated_name(s.name or "")
            if kind in ("label", "macro") and kind not in self._associated:
                self._associated[kind] = all_series.index(s)

        layout = _series_layout(main)
        self._dtype = layout.dtype
        self._color = layout.color
        self._invert = layout.invert
        self._alpha = layout.alpha
        self._premultiplied = layout.premultiplied
        self._channels = layout.channels
        self._bits = _integer_bits(main.keyframe, layout.dtype)
        try:
            self._levels: list[_Level] = [
                _build_level(level_series, layout, self._file_size) for level_series in main.levels
            ]
        except UnsupportedOmeTiffError as exc:
            # Planes tifffile left out because they live in another file.
            if exc.reason == "structure" and _looks_multifile(root):
                raise UnsupportedOmeTiffError("multifile", f"pixel data is stored in other files ({exc})") from exc
            raise

        # Levels must shrink monotonically; gaps between them bound how much a
        # single Deep Zoom tile reads (see _MAX_LEVEL_RATIO).
        for prev, cur in zip(self._levels, self._levels[1:]):
            if cur.width >= prev.width or cur.height >= prev.height:
                raise UnsupportedOmeTiffError("structure", "pyramid levels do not shrink")
            parent, child = (prev.width, prev.height), (cur.width, cur.height)
            if level_gap_too_wide(parent, child):
                raise UnsupportedOmeTiffError("level_gap", level_gap_message(parent, child))
        lowest = self._levels[-1]
        if lowest.width * lowest.height > _MAX_LOWEST_LEVEL_PIXELS:
            # Two reasons, so the admin isn't told a file with a pyramid has
            # none: what to fix differs (add a pyramid vs. add more levels).
            raise UnsupportedOmeTiffError(
                "no_pyramid" if len(self._levels) == 1 else "lowest_level_too_large",
                f"smallest level is {lowest.width}x{lowest.height} "
                f"(max {_MAX_LOWEST_LEVEL_PIXELS} pixels without a finer pyramid)",
            )

        w0, h0 = self._levels[0].width, self._levels[0].height
        self._dimensions = tuple((lv.width, lv.height) for lv in self._levels)
        self._downsamples = tuple(
            ((w0 / lv.width) + (h0 / lv.height)) / 2.0 for lv in self._levels
        )
        self._props.update(_image_metadata(root, w0, h0, getattr(main, "name", None)))
        try:
            significant = int(self._props.get("ome.significant-bits") or 0)
        except ValueError:
            significant = 0
        if 0 < significant <= self._dtype.itemsize * 8 and self._dtype.kind in "ui":
            self._bits = significant

    def _finish_properties(self) -> None:
        props = self._props
        props["openslide.level-count"] = str(len(self._levels))
        for i, lv in enumerate(self._levels):
            props[f"openslide.level[{i}].width"] = str(lv.width)
            props[f"openslide.level[{i}].height"] = str(lv.height)
            props[f"openslide.level[{i}].downsample"] = repr(self._downsamples[i])
            props[f"openslide.level[{i}].tile-width"] = str(lv.seg_w)
            props[f"openslide.level[{i}].tile-height"] = str(lv.seg_h)
        props["ome.color-mode"] = self._color
        if self._window is not None:
            props["ome.window-low"] = repr(self._window[0])
            props["ome.window-high"] = repr(self._window[1])

    # -- decoding ----------------------------------------------------------

    def _decode_raw(self, level: int, idx: int) -> Optional[np.ndarray]:
        return _decode_segment(self._fd, self._levels[level], self._channels, idx)

    def _to_uint8(self, a: np.ndarray) -> np.ndarray:
        n_color = 3 if self._color == "rgb" else 1
        color = a[..., :n_color]
        if self._window is None:
            out = color if color.dtype == np.uint8 else color.astype(np.uint8)
        else:
            lo, hi = self._window
            scaled = (color.astype(np.float32) - np.float32(lo)) * np.float32(255.0 / (hi - lo))
            out = np.clip(scaled + 0.5, 0, 255).astype(np.uint8)
        if self._invert:
            out = 255 - out
        if self._alpha:
            alpha = _alpha_to_uint8(a[..., n_color:n_color + 1])
            if self._premultiplied:
                # Associated alpha: colour was stored multiplied by it;
                # read_region hands out straight alpha, as OpenSlide does.
                safe = np.maximum(alpha, 1).astype(np.float32)
                out = np.where(
                    alpha > 0, np.clip(out.astype(np.float32) * 255.0 / safe + 0.5, 0, 255), 0,
                ).astype(np.uint8)
            out = np.concatenate([out, alpha], axis=-1)
        return np.ascontiguousarray(out)

    def _segment(self, level: int, idx: int) -> Optional[np.ndarray]:
        key = (self._uid, level, idx)
        cached = _segment_cache.get(key)
        if cached is not _MISS:
            return cached
        if self._closed:
            raise ValueError("slide is closed")
        try:
            raw = self._decode_raw(level, idx)
        except MemoryError:
            raise
        except Exception as exc:
            # Not a ValueError: the Deep Zoom endpoint reads ValueError as
            # "no such tile" (404, unlogged); a segment that fails to read or
            # decode is a server-side fault worth a log line.
            raise openslide.OpenSlideError(f"level {level} segment {idx}: {exc}") from exc
        value = None if raw is None else self._to_uint8(raw)
        if value is not None and value.base is not None and getattr(value.base, "nbytes", -1) != value.nbytes:
            # A view would pin (and hide from the byte count) the larger
            # buffer it was cut from, e.g. a full strip behind the last rows.
            value = value.copy()
        _segment_cache.put(key, value)
        return value

    def _compute_window(self) -> Optional[tuple[float, float]]:
        """The single intensity window mapping non-8-bit data to 0..255.

        Derived from the smallest level (bounded by _MAX_LOWEST_LEVEL_PIXELS)
        so it is deterministic across workers and identical for every tile.
        """
        if self._dtype == np.uint8:
            return None
        if self._dtype.kind == "b":
            return (0.0, 1.0)
        level = len(self._levels) - 1
        lv = self._levels[level]
        n_color = 3 if self._color == "rgb" else 1
        stride = max(1, int(math.ceil(math.sqrt(lv.width * lv.height / _WINDOW_SAMPLE_PIXELS))))
        samples = []
        for idx in range(lv.segs_across * lv.segs_down):
            try:
                raw = self._decode_raw(level, idx)
            except (OSError, MemoryError):
                raise
            except Exception as exc:
                raise UnsupportedOmeTiffError("codec", f"cannot decode segment {idx} of the smallest level: {exc!r}") from exc
            if raw is not None:
                samples.append(raw[::stride, ::stride, :n_color].reshape(-1, n_color))
        if not samples:
            return (0.0, 1.0)
        values = np.concatenate(samples, axis=0).astype(np.float64)
        values = values[np.isfinite(values).all(axis=1)]
        if values.size == 0:
            return (0.0, 1.0)
        if self._color == "rgb":
            # One window for all three channels keeps the colour balance. An
            # integer image is scaled from 0 to its bit depth's maximum: the
            # OME SignificantBits / TIFF BitsPerSample / MaxSampleValue when
            # the file states one (see _integer_bits), else the smallest
            # common camera depth covering the data (12-bit data in a 16-bit
            # container maps from 4095, not 65535); float data from 0..1
            # unless it exceeds that range.
            vmax = float(values.max())
            vmin = float(values.min())
            if self._dtype.kind in "ui":
                bits = self._bits
                if not bits:
                    need = int(math.ceil(math.log2(vmax + 1))) if vmax > 0 else 8
                    bits = next((b for b in _COMMON_BIT_DEPTHS if need <= b), max(need, 8))
                lo, hi = min(0.0, vmin), float(2 ** bits - 1)
            else:
                lo = min(0.0, vmin)
                hi = 1.0 if vmax <= 1.0 else float(np.percentile(values, 99.9))
        else:
            lo = float(np.percentile(values, 0.5))
            hi = float(np.percentile(values, 99.5))
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            hi = lo + 1.0
        return (lo, hi)

    def _probe_decode(self) -> None:
        """Decode the first stored segment of every level so a missing codec,
        an unsupported compression or a crafted segment header is caught when
        the file is opened (i.e. at upload) rather than as broken tiles later.
        Levels may use different compressions, hence one probe per level."""
        for level, lv in enumerate(self._levels):
            for idx in range(lv.segs_across * lv.segs_down):
                if not any(int(src.bytecounts[src.seg_offset + idx]) for src in lv.sources):
                    continue
                try:
                    raw = self._decode_raw(level, idx)
                except (OSError, MemoryError):
                    raise
                except Exception as exc:
                    raise UnsupportedOmeTiffError("codec", f"cannot decode level {level}: {exc!r}") from exc
                if raw is not None:
                    break

    # -- AbstractSlide -----------------------------------------------------
    # Every method below is abstract in AbstractSlide, so each must exist for
    # the class to be instantiable at all, even one nothing here calls
    # directly (detect_format).

    @classmethod
    def detect_format(cls, filename) -> Optional[str]:
        return VENDOR if is_ome_tiff(os.fspath(filename)) else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.close()
        finally:
            _segment_cache.drop_owner(self._uid)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._path!r})"

    @property
    def level_count(self) -> int:
        return len(self._levels)

    @property
    def level_dimensions(self) -> tuple[tuple[int, int], ...]:
        return self._dimensions

    @property
    def level_downsamples(self) -> tuple[float, ...]:
        return self._downsamples

    @property
    def properties(self) -> Mapping[str, str]:
        return types.MappingProxyType(self._props)

    @property
    def associated_images(self) -> Mapping[str, Image.Image]:
        return _AssociatedImages(self._path, dict(self._associated))

    def get_best_level_for_downsample(self, downsample: float) -> int:
        # DeepZoomGenerator picks the source level of every Deep Zoom level
        # through this. Same rule as openslide_get_best_level_for_downsample().
        if downsample < self._downsamples[0]:
            return 0
        for i in range(1, len(self._downsamples)):
            if downsample < self._downsamples[i]:
                return i - 1
        return len(self._downsamples) - 1

    def read_region(self, location: tuple[int, int], level: int, size: tuple[int, int]) -> Image.Image:
        """RGBA region at ``level``; ``location`` is in level-0 pixels.

        Pixels outside the image (or in an absent segment) are transparent,
        as OpenSlide returns them, so DeepZoomGenerator composites them onto
        the background colour.
        """
        w, h = int(size[0]), int(size[1])
        if w < 0 or h < 0:
            raise openslide.OpenSlideError(f"negative width ({w}) or negative height ({h}) not allowed")
        if w == 0 or h == 0 or not (0 <= level < len(self._levels)):
            return Image.new("RGBA", (w, h))
        if self._closed:
            raise ValueError("slide is closed")
        lv = self._levels[level]
        ds = self._downsamples[level]
        # Level-0 -> level coordinates. The epsilon absorbs the float error of
        # DeepZoomGenerator's level * downsample round trip, which would
        # otherwise floor 1234 to 1233.
        rx = int(math.floor(location[0] / ds + 1e-6))
        ry = int(math.floor(location[1] / ds + 1e-6))
        out = np.zeros((h, w, 4), dtype=np.uint8)
        n_color = 3 if self._color == "rgb" else 1
        x0, y0 = max(rx, 0), max(ry, 0)
        x1, y1 = min(rx + w, lv.width), min(ry + h, lv.height)
        if x0 < x1 and y0 < y1:
            for sy in range(y0 // lv.seg_h, (y1 - 1) // lv.seg_h + 1):
                for sx in range(x0 // lv.seg_w, (x1 - 1) // lv.seg_w + 1):
                    seg = self._segment(level, sy * lv.segs_across + sx)
                    if seg is None:
                        continue
                    gx, gy = sx * lv.seg_w, sy * lv.seg_h
                    ox0, oy0 = max(x0, gx), max(y0, gy)
                    ox1, oy1 = min(x1, gx + seg.shape[1]), min(y1, gy + seg.shape[0])
                    if ox0 >= ox1 or oy0 >= oy1:
                        continue
                    dst = out[oy0 - ry:oy1 - ry, ox0 - rx:ox1 - rx]
                    src = seg[oy0 - gy:oy1 - gy, ox0 - gx:ox1 - gx]
                    dst[..., :3] = src[..., :n_color]
                    dst[..., 3] = src[..., n_color] if self._alpha else 255
        return Image.fromarray(out)


class _AssociatedImages(Mapping):
    """Label / macro series, read lazily: the Deep Zoom path never asks for
    them, so opening a slide must not pay for decoding them."""

    def __init__(self, path: str, series_index: dict[str, int]) -> None:
        self._path = path
        self._index = series_index

    def __iter__(self):
        return iter(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def __getitem__(self, key: str) -> Image.Image:
        idx = self._index[key]
        with open(self._path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            with tifffile.TiffFile(fh, _multifile=False) as tf:
                # Re-screen the XML: the path may hold a different file than
                # the one validated when the slide was opened.
                _parse_ome_xml(tf.ome_metadata or "")
                series = _file_series(tf)[idx]
                try:
                    lay = _series_layout(series)
                    lv = _build_level(series.levels[-1], lay, size)
                except UnsupportedOmeTiffError as exc:
                    raise KeyError(key) from exc
            if lv.width * lv.height > _ASSOCIATED_MAX_PIXELS:
                raise KeyError(key)
            out = np.zeros((lv.height, lv.width, lay.channels), dtype=lay.dtype)
            for idx_seg in range(lv.segs_across * lv.segs_down):
                seg = _decode_segment(fh.fileno(), lv, lay.channels, idx_seg)
                if seg is None:
                    continue
                y = (idx_seg // lv.segs_across) * lv.seg_h
                x = (idx_seg % lv.segs_across) * lv.seg_w
                out[y:y + seg.shape[0], x:x + seg.shape[1]] = seg
        n_color = 3 if lay.color == "rgb" else 1
        image = _to_rgb_image(out[..., :n_color], lay.invert)
        if lay.alpha:
            # Composite onto white, as the Deep Zoom tiles are.
            alpha = _alpha_to_uint8(out[..., n_color:]).astype(np.float32) / 255.0
            rgb = np.asarray(image, dtype=np.float32)
            if not lay.premultiplied:
                rgb = rgb * alpha
            rgb = np.clip(rgb + 255.0 * (1.0 - alpha) + 0.5, 0, 255).astype(np.uint8)
            image = Image.fromarray(rgb)
        return image


# Bit depths cameras actually produce; a guessed depth snaps up to one.
_COMMON_BIT_DEPTHS = (8, 10, 12, 14, 16)


def _integer_bits(keyframe, dtype: np.dtype) -> int:
    """The bit depth an integer image states for itself, 0 when it doesn't:
    a BitsPerSample below the container width (12-bit samples unpacked into
    uint16), else a MaxSampleValue below the container's maximum."""
    if dtype.kind not in "ui":
        return 0
    width = dtype.itemsize * 8
    try:
        bps = keyframe.bitspersample
        bps = int(bps[0] if isinstance(bps, tuple) else bps)
    except Exception:
        bps = width
    if 0 < bps < width:
        return bps
    try:
        msv = keyframe.tags.valueof(281)  # MaxSampleValue
        msv = int(max(msv) if isinstance(msv, (tuple, list)) else msv)
    except Exception:
        msv = 0
    if 0 < msv < np.iinfo(dtype).max:
        return max(1, int(math.ceil(math.log2(msv + 1))))
    return 0


def _alpha_to_uint8(alpha: np.ndarray) -> np.ndarray:
    """An alpha sample in the file's dtype to 0..255 (integers span their
    type's range, floats 0..1)."""
    if alpha.dtype == np.uint8:
        return alpha
    if alpha.dtype.kind in "ui":
        scale = 255.0 / float(np.iinfo(alpha.dtype).max)
        return np.clip(alpha.astype(np.float32) * scale + 0.5, 0, 255).astype(np.uint8)
    if alpha.dtype.kind == "b":
        return alpha.astype(np.uint8) * 255
    return np.clip(np.nan_to_num(alpha.astype(np.float32)) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _to_rgb_image(arr: np.ndarray, invert: bool) -> Image.Image:
    """Label / macro pixels (h, w, 3|1) to an 8-bit RGB PIL image. Non-8-bit
    data is stretched min..max: these are small overview pictures, and the
    main image's window does not apply to them."""
    if arr.dtype != np.uint8:
        a = arr.astype(np.float32)
        lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
        if hi <= lo:
            hi = lo + 1.0
        arr = np.clip((a - lo) * (255.0 / (hi - lo)) + 0.5, 0, 255).astype(np.uint8)
    if invert:
        arr = 255 - arr
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    return Image.fromarray(np.ascontiguousarray(arr))
