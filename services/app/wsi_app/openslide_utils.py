from __future__ import annotations
import contextlib
import io
import logging
import math
import os
import subprocess
import sys
import threading
from typing import Tuple, Dict, Any, Optional
from PIL import Image
import openslide

from .ome_tiff import (
    _ASSOCIATED_MAX_PIXELS,
    OmeTiffSlide,
    UnsupportedOmeTiffError,
    UnsupportedSlideError,
    is_ome_tiff,
    level_gap_message,
    level_gap_too_wide,
)

_logger = logging.getLogger("wsi_app.openslide")


# Serialise mutations of the process-wide Image.MAX_IMAGE_PIXELS guard so
# concurrent uploads can't interleave save/restore and permanently disable
# the decompression-bomb cap. Without the lock, two threads entering the
# critical section near-simultaneously would each capture prev=None
# (because the other has just set None), and the last finally would
# restore None, leaving the process wide open until restart.
_PIL_BOMB_CAP_LOCK = threading.Lock()

# Finite replacement for MAX_IMAGE_PIXELS during the WSI IFD walk. Setting the
# guard to ``None`` disables PIL's decompression-bomb
# check *process-wide* for the whole window, so any OTHER decode running
# concurrently — a viewer tile, an associated-image re-encode — also loses the
# guard and can be steered into an unbounded allocation. A large *finite* cap
# keeps those concurrent decodes bounded while still clearing the one decode
# that must be allowed here: Pillow runs the bomb check against IFD 0 at
# ``Image.open`` time, and IFD 0 of a WSI is the multi-gigapixel pyramid base.
# The cap must therefore sit far above any real base page (a 200k x 200k scan
# is ~4e10 px); 1e12 clears real slides by >20x yet still refuses a genuinely
# pathological (multi-terapixel) allocation instead of permitting any size at
# all.
_IFD_WALK_MAX_IMAGE_PIXELS = 1_000_000_000_000


@contextlib.contextmanager
def _pil_bomb_cap_disabled():
    """Raise PIL's MAX_IMAGE_PIXELS guard to a large finite cap for the block.

    The guard targets untrusted user input, but WSI uploads come from
    authenticated admins and the TIFF IFD walk never decodes the
    pyramid-base page (only the tiny macro / label pages). The cap is
    raised — not disabled outright — so a bomb decode racing this window in
    another thread stays bounded by ``_IFD_WALK_MAX_IMAGE_PIXELS`` instead of
    losing the guard entirely. A lock is held around save / restore so
    concurrent uploads can't corrupt each other's prev-value capture; the
    critical sections are short (Pillow's seek-only IFD walk) so serialisation
    cost is negligible compared to the WSI upload itself.
    """
    with _PIL_BOMB_CAP_LOCK:
        prev = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = _IFD_WALK_MAX_IMAGE_PIXELS
        try:
            yield
        finally:
            Image.MAX_IMAGE_PIXELS = prev

# Cap stored associated images so a 30+ MB MRXS macro image doesn't bloat the
# DB. 1024 px on the long side keeps the on-screen rendering crisp at typical
# admin / viewer panel sizes while staying ~50–200 KB after JPEG encoding.
_ASSOCIATED_MAX_LONG_SIDE = 1024
_ASSOCIATED_JPEG_QUALITY = 85

# _ASSOCIATED_MAX_PIXELS (from ome_tiff) is the largest macro / label page
# decoded at all. Both paths below decode the whole page before downscaling
# it — OpenSlide as RGBA, Pillow in its own mode plus the RGB copy — and the
# IFD walk does so with the bomb guard raised to _IFD_WALK_MAX_IMAGE_PIXELS,
# so a crafted 16000² "label" page would cost gigabytes.

# SZI (zipped DZI) does not go through OpenSlide — the tile pyramid is
# pre-encoded inside the archive and the browser fetches tiles via Range.
# Dispatch is based on the file extension so callers (admin upload, dev
# seed) don't have to know about the format split.
def _is_szi(path: str) -> bool:
    return path.lower().endswith(".szi")


def open_wsi(path: str, *, strict_ome: bool = False) -> openslide.AbstractSlide:
    """Open a (non-SZI) WSI with the reader its content calls for.

    An OME-TIFF goes to :class:`OmeTiffSlide` — OpenSlide would read one as a
    single-level generic TIFF — and everything else to OpenSlide. Both expose
    the same ``AbstractSlide`` interface, so callers (DeepZoomGenerator, the
    thumbnailer, the associated-image extractor) don't care which they got.

    ``strict_ome`` decides what happens to an OME-TIFF the reader refuses
    (multi-channel, no pyramid, ...). The upload / replace / import paths
    validate through ``read_slide_properties``, which is strict so the admin
    is told why the file was rejected. Everywhere else the file falls back to
    OpenSlide as a generic TIFF, so a stored slide the reader declines (a
    file placed by hand, or one a future release's reader refuses) keeps
    serving.

    One refusal falls back even when strict: OME-XML that tifffile can't
    reconcile with the file's pages (``fallback_ok``). That is metadata the
    reader can't use, not a layout it rejects, and OpenSlide — which ignores
    the OME-XML — reads such a file as a generic TIFF.
    """
    if is_ome_tiff(path):
        # The OpenSlide route refuses a file with more directories than any
        # slide pyramid has (_open_openslide); the OME reader parses every one
        # of them on each open, so it gets the same bound - the upload's
        # (strict) or, for a slide already stored, the larger serving one.
        page_cap = _MAX_TIFF_PAGES_CHECKED if strict_ome else _MAX_TIFF_PAGES_STORED
        if tiff_directory_count_exceeds(path, page_cap):
            raise ValueError(
                f"TIFF has more than {page_cap} directories; "
                "not a supported slide layout"
            )
        try:
            return OmeTiffSlide(path)
        except UnsupportedOmeTiffError as exc:
            if strict_ome and not exc.fallback_ok:
                raise
            _logger.info("OME-TIFF reader declined %s (%s: %s); falling back to OpenSlide", path, exc.reason, exc)
            if strict_ome:
                try:
                    return _open_openslide(path, strict=strict_ome)
                except Exception:
                    # Neither reader can use it: the OME reason says more.
                    raise exc from None
    return _open_openslide(path, strict=strict_ome)


# Largest tile dimension and area a TIFF-family slide may declare.
# libopenslide allocates width*height*4 bytes per tile with g_malloc, which
# *aborts the process* (not a Python exception) when the request is absurd, so
# a crafted TileWidth would take the worker down on the first tile read. Real
# slides use tiles of 128-2048 px (occasionally 4096); the bounds leave ample
# headroom above that.
_MAX_TIFF_SEGMENT_SIDE = 16384
_MAX_TIFF_SEGMENT_PIXELS = 1 << 26
# Directories inspected; a file with more pages than this is not a slide
# pyramid, and the cap keeps the check cheap on a crafted IFD chain.
_MAX_TIFF_PAGES_CHECKED = 4096
# Opening an already-stored slide must not refuse a file upload validation
# would not have caught (one placed by hand, or after a future release
# tightens the check), so serving scans further (a hard bound still, against a
# cyclic chain) and only upload validation refuses that many directories.
_MAX_TIFF_PAGES_STORED = 65536
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def tiff_directory_count_exceeds(path: str, limit: int) -> bool:
    """True when ``path`` is a TIFF whose directory (IFD) chain is longer than
    ``limit`` - or never ends.

    Walks the chain with a few bytes read per directory (classic TIFF and
    BigTIFF, either byte order) instead of parsing every page, so it is cheap
    on exactly the file it exists for: a crafted chain of a few hundred
    thousand tiny directories that a full parse would spend seconds and
    hundreds of megabytes on, again for every cold open. Anything it cannot
    read (not a TIFF, a truncated file) is "not exceeding": those are reported
    by the readers the usual way."""
    import struct

    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
            if head[:2] == b"II":
                order = "<"
            elif head[:2] == b"MM":
                order = ">"
            else:
                return False
            magic = struct.unpack(order + "H", head[2:4])[0]
            if magic == 42:
                (offset,) = struct.unpack(order + "I", head[4:8])
                count_fmt, count_size, entry_size, next_fmt, next_size = "H", 2, 12, "I", 4
            elif magic == 43:
                (offset,) = struct.unpack(order + "Q", head[8:16])
                count_fmt, count_size, entry_size, next_fmt, next_size = "Q", 8, 20, "Q", 8
            else:
                return False
            seen = set()
            while offset:
                if offset in seen or len(seen) >= limit:
                    return True  # more than `limit` directories, or a cycle
                seen.add(offset)
                fh.seek(offset)
                raw = fh.read(count_size)
                if len(raw) < count_size:
                    return False
                (entries,) = struct.unpack(order + count_fmt, raw)
                fh.seek(offset + count_size + entries * entry_size)
                raw = fh.read(next_size)
                if len(raw) < next_size:
                    return False
                (offset,) = struct.unpack(order + next_fmt, raw)
    except (OSError, struct.error, ValueError, OverflowError):
        # (A crafted offset or entry count beyond what seek() can take raises
        # ValueError / OverflowError.)
        return False
    return False


def check_tiff_segment_sizes(path: str, max_pages: Optional[int] = None) -> None:
    """Refuse a TIFF whose tile geometry would abort libopenslide.

    A no-op for anything that is not a TIFF (other vendor formats are left to
    OpenSlide) and for a TIFF tifffile reports as structurally broken
    (``TiffFileError``: a bad header, a corrupt or truncated directory —
    damage libtiff refuses too, so OpenSlide rejects it with a normal
    exception). Raises ``ValueError`` for an oversized segment, and for any
    other failure while walking the directories: tifffile and libtiff are
    different parsers, so a file tifffile gives up on part-way (a crafted
    private tag sending its format detection astray, say) may still open in
    libtiff with its tile geometry never checked.
    """
    try:
        with open(path, "rb") as fh:
            if fh.read(4) not in _TIFF_MAGICS:
                return
    except OSError:
        return
    import tifffile

    page_cap = _MAX_TIFF_PAGES_CHECKED if max_pages is None else max_pages
    oversize = None
    too_many_pages = False
    try:
        with tifffile.TiffFile(path, _multifile=False) as tf:
            for index, page in enumerate(tf.pages):
                if index >= page_cap:
                    # Not a slide pyramid. Stopping the scan here (rather than
                    # refusing) would leave the unchecked tail free to carry
                    # the oversized tile libopenslide aborts on.
                    too_many_pages = True
                    break
                tile_w = page.tags.get("TileWidth")
                tile_h = page.tags.get("TileLength")
                if tile_w is None or tile_h is None:
                    continue
                seg_w, seg_h = int(tile_w.value), int(tile_h.value)
                if (
                    seg_w > _MAX_TIFF_SEGMENT_SIDE
                    or seg_h > _MAX_TIFF_SEGMENT_SIDE
                    or seg_w * seg_h > _MAX_TIFF_SEGMENT_PIXELS
                ):
                    oversize = (index, seg_w, seg_h)
                    break
    except tifffile.TiffFileError:
        # Structurally broken: let OpenSlide report it the usual way.
        return
    except Exception as exc:
        # The walk stopped without proving the geometry safe. Handing the
        # file to OpenSlide anyway would let a crafted tile size abort the
        # worker, so it is refused instead.
        raise ValueError(
            f"TIFF directories could not be checked ({type(exc).__name__}: {exc!s:.200})"
        ) from exc
    if too_many_pages:
        raise ValueError(
            f"TIFF has more than {page_cap} directories; "
            "not a supported slide layout"
        )
    if oversize is None:
        return
    raise ValueError(
        f"TIFF directory {oversize[0]} declares a {oversize[1]}x{oversize[2]} tile, "
        f"beyond the supported {_MAX_TIFF_SEGMENT_SIDE}px side / "
        f"{_MAX_TIFF_SEGMENT_PIXELS}px area"
    )


def _open_openslide(path: str, strict: bool = False):
    # Upload validation (strict) refuses a file with more directories than any
    # slide pyramid has; serving a stored slide scans further instead of
    # refusing a file that is already in the library. An oversized tile is
    # refused either way: libopenslide aborts the whole process on it.
    check_tiff_segment_sizes(
        path, max_pages=_MAX_TIFF_PAGES_CHECKED if strict else _MAX_TIFF_PAGES_STORED
    )
    return openslide.OpenSlide(path)


# Upper bound for a file-reported MPP or objective power, matching the range
# the dashboard accepts for a manual MPP override (admin_helpers.parse_mpp_value)
# and the OME-TIFF reader's magnification sanity bound.
_MAX_FILE_NUMERIC_PROP = 1000.0


def _positive_float_prop(props, key: str) -> float:
    """A file-reported positive quantity, or 0.0 ("not recorded").

    ``float()`` accepts "inf", "Infinity" and "nan", so a converted value is
    not yet a usable one: stored in the slide row it would make every JSON
    response carrying the slide fail to serialise. Non-finite, non-positive
    and absurdly large values all degrade to the same "unknown" 0.0 an
    unparseable string gets, rather than rejecting an otherwise readable
    upload."""
    try:
        value = float(props.get(key, "0") or "0")
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0 or value > _MAX_FILE_NUMERIC_PROP:
        return 0.0
    return value


# How long the upload-time probe may take (see _probe_in_child). It opens the
# slide and decodes a few tiles, so this is far beyond any healthy file.
_PROBE_TIMEOUT_S = 120.0

# Logging and warnings are off in the child: nothing reads its output beyond
# the crash tail, and tifffile logs a line per malformed tag, so a small file
# with many bad tags would otherwise print hundreds of megabytes.
_PROBE_CODE = (
    "import logging, sys, warnings; logging.disable(logging.CRITICAL); warnings.simplefilter('ignore');"
    "sys.path.insert(0, sys.argv[1]);"
    "from wsi_app.openslide_utils import _probe_child; _probe_child(sys.argv[2])"
)

# How much of the end of the child's stderr a crash report quotes; the rest
# is read and dropped as it arrives.
_PROBE_STDERR_TAIL = 400


def _probe_child(path: str) -> None:
    """Body of the probe process: open the slide the way upload validation
    does, then decode a tile of every level and the whole lowest level the
    thumbnail reads (when that is small, as on any pyramid)."""
    slide = open_wsi(path, strict_ome=True)
    try:
        dict(slide.properties)
        last = slide.level_count - 1
        for level in range(last):
            slide.read_region((0, 0), level, (1, 1)).close()
        w, h = slide.level_dimensions[last]
        if w * h <= _THUMB_READ_BUDGET_PX:
            _read_level_bounded(slide, last, (w, h)).close()
        else:
            slide.read_region((0, 0), last, (1, 1)).close()
    finally:
        slide.close()


def _probe_in_child(path: str) -> None:
    """Refuse a file that crashes the slide reader, without crashing this worker.

    libopenslide and the libtiff / codec stack under it answer some malformed
    files by aborting the process (g_malloc of an absurd size) rather than
    raising, and a worker taken down mid-upload takes every request it was
    serving with it. check_tiff_segment_sizes refuses the one such layout
    known; this covers whatever else opening the file and decoding its first
    tiles reaches, by doing exactly that in a throwaway process first. Only a
    crash (death by a signal) or a hang refuses the file: an ordinary error
    there is left to the in-process read that follows, which reports it the
    usual way. Best-effort: when no child can be started, validation goes on
    in-process only.
    """
    if not sys.executable:
        return
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", _PROBE_CODE, package_root, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError:
        _logger.warning("could not start the slide probe for %s; validating in-process", path, exc_info=True)
        return
    # The child's stderr is drained as it is written and only its end kept,
    # so however much the reader stack prints, this worker holds a bounded
    # tail of it (and the child never blocks on a full pipe).
    tail = bytearray()

    def drain() -> None:
        with proc.stderr:
            while chunk := proc.stderr.read1(65536):
                tail.extend(chunk)
                del tail[:-_PROBE_STDERR_TAIL]

    reader = threading.Thread(target=drain, name="slide-probe-stderr", daemon=True)
    try:
        reader.start()
        returncode = proc.wait(timeout=_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ValueError(f"slide reader did not finish within {_PROBE_TIMEOUT_S:.0f}s") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if reader.ident is not None:
            reader.join()
        else:
            proc.stderr.close()
    if returncode < 0:
        text = bytes(tail).decode("utf-8", "replace").strip()
        raise ValueError(f"slide reader crashed (signal {-returncode}): {text}")


def read_slide_properties(path: str) -> Dict[str, Any]:
    if _is_szi(path):
        from .szi_utils import read_szi_properties
        return read_szi_properties(path)
    # Every upload path validates here, before anything else reads the file
    # in this process (the thumbnail comes next).
    _probe_in_child(path)
    # try/finally guarantees the slide handle is released even if a
    # property lookup or float() conversion below raises (a corrupt slide
    # or unexpected MPP string would otherwise leak the file descriptor).
    # Strict: this is the validation step of every upload path, so an
    # OME-TIFF outside the reader's scope surfaces as UnsupportedOmeTiffError
    # (with its reason) instead of being accepted as a single-level TIFF, and
    # any other layout Deep Zoom can't serve as UnsupportedSlideError.
    slide = open_wsi(path, strict_ome=True)
    try:
        # The OME-TIFF reader applies its own (tighter) version of this check
        # when it opens a file; everything OpenSlide reads — vendor formats,
        # generic TIFF, an OME-TIFF that fell back — is checked here.
        if not isinstance(slide, OmeTiffSlide):
            check_pyramid_layout(slide)
        props = slide.properties
        w, h = slide.dimensions
        # Guard the MPP parse the same way objective_power / acquired_at are
        # below: a malformed vendor string (e.g. "0,25" with a comma decimal)
        # would otherwise raise ValueError and make the caller reject an
        # otherwise-valid upload as "WSI unreadable".
        mpp_x = _positive_float_prop(props, "openslide.mpp-x")
        mpp_y = _positive_float_prop(props, "openslide.mpp-y")
        # ``openslide.objective-power`` is OpenSlide's normalised view of the
        # vendor-specific magnification field (Aperio ``AppMag``, Hamamatsu
        # ``hamamatsu.SourceLens``, MIRAX ``OBJECTIVE_MAGNIFICATION``, etc.).
        # When present it is preferred over the MPP-derived estimate (10/mpp_x);
        # 0 means the file did not record one.
        objective_power = _positive_float_prop(props, "openslide.objective-power")
        vendor = props.get("openslide.vendor", "") or ""
        # Defensive: a malformed vendor date string must not abort the entire
        # upload. The caller (admin_slides.upload_slide) treats any exception
        # from this function as "WSI couldn't be read" and rolls the slide
        # back, so a bad date would silently swallow an otherwise valid file.
        try:
            acquired_at = _extract_acquired_at(props)
        except Exception:
            _logger.warning("acquired_at extraction failed for %s", path, exc_info=True)
            acquired_at = ""
    finally:
        slide.close()
    return {
        "width": w, "height": h, "mpp_x": mpp_x, "mpp_y": mpp_y,
        "objective_power": objective_power,
        "vendor": vendor, "acquired_at": acquired_at,
    }


def _extract_acquired_at(props) -> str:
    """Return the slide's scan/acquisition timestamp as ISO 8601 (UTC-naive).

    Vendor formats encode the scan date in different keys:
      * Aperio SVS:     ``aperio.Date`` (MM/DD/YY) + ``aperio.Time`` (HH:MM:SS)
      * Hamamatsu NDPI: ``hamamatsu.Created`` (YYYY-MM-DD HH:MM:SS)
      * 3DHISTECH MRXS: ``mirax.GENERAL.SLIDE_CREATIONDATETIME`` (YYYY-MM-DD HH:MM:SS)
      * Leica SCN:      ``leica.creation-date`` (ISO-ish)
      * OME-TIFF:       ``ome.acquisition-date`` (xsd:dateTime, may carry a
                        zone; converted to UTC)

    Each is tried in turn; the first that parses cleanly wins. Returns ``""``
    when the file carries no recognised date — the dashboard renders that
    as an em-dash. Always normalised to ``YYYY-MM-DDTHH:MM:SS`` so the
    frontend can format it with a single Date parser.
    """
    from datetime import datetime, timezone

    # OME-TIFF: xsd:dateTime ("2019-03-21T10:15:12.1234567Z"). Before Python
    # 3.11 fromisoformat takes a fraction of exactly three or six digits and
    # no "Z", so the fraction is normalised to six digits.
    o_date = (props.get("ome.acquisition-date") or "").strip()
    if o_date:
        import re

        normalised = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], o_date, count=1)
        if normalised.endswith(("Z", "z")):
            normalised = normalised[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(normalised)
        except ValueError:
            d = None
        if d is not None:
            if d.tzinfo is not None:
                d = d.astimezone(timezone.utc).replace(tzinfo=None)
            return d.replace(microsecond=0).isoformat(timespec="seconds")

    # Aperio SVS
    a_date = (props.get("aperio.Date") or "").strip()
    if a_date:
        a_time = (props.get("aperio.Time") or "").strip()
        # Aperio writes the date as MM/DD/YY and (less commonly) MM/DD/YYYY.
        for date_fmt in ("%m/%d/%y", "%m/%d/%Y"):
            try:
                d = datetime.strptime(a_date, date_fmt)
            except ValueError:
                continue
            if a_time:
                for time_fmt in ("%H:%M:%S", "%H:%M"):
                    try:
                        t = datetime.strptime(a_time, time_fmt)
                        return d.replace(
                            hour=t.hour, minute=t.minute, second=t.second
                        ).isoformat(timespec="seconds")
                    except ValueError:
                        continue
            return d.date().isoformat()

    # Hamamatsu NDPI / 3DHISTECH MRXS / Leica SCN
    for key in (
        "hamamatsu.Created",
        "mirax.GENERAL.SLIDE_CREATIONDATETIME",
        "leica.creation-date",
        "tiff.DateTime",  # generic-TIFF fallback: "YYYY:MM:DD HH:MM:SS"
    ):
        raw = (props.get(key) or "").strip()
        if not raw:
            continue
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(raw, fmt).isoformat(timespec="seconds")
            except ValueError:
                continue
    return ""

def save_thumbnail_atomic(img: Image.Image, out_path: str) -> None:
    """Publish ``img`` to ``out_path`` as a JPEG via tmp-file + ``os.replace``.

    The ``/thumbs`` mount is static and public, and the slide row references
    the thumbnail by name, so a half-written file is served as-is: a crash or
    ENOSPC part-way through ``Image.save`` would leave a truncated JPEG that
    renders as a broken image in the gallery until an operator re-runs
    rebuild-thumbnails. Writing to a sibling temp and renaming makes the
    publish atomic — readers see either the previous thumbnail or the complete
    new one — matching every other publish path in this codebase (backup
    archives, sidecars, the resolve cache).

    The temp is a sibling (same directory) so the rename is a same-filesystem
    os.replace rather than a cross-device copy, and it carries pid + thread id
    so two workers rebuilding the same slide cannot collide on the temp name.
    It is removed on any failure, so a failing rebuild loop cannot accumulate
    partials. The leading dot only keeps it out of an operator's ``ls`` and
    mirrors the ``.upload-`` convention in admin_tours: nothing in this
    codebase enumerates ``thumbs/`` by walking it (backup reads the names from
    the snapshot DB precisely to avoid racing live uploads), so the prefix is
    cosmetic rather than load-bearing.
    """
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = os.path.join(
        out_dir, f".{os.path.basename(out_path)}.tmp-{os.getpid()}-{threading.get_ident():x}"
    )
    try:
        img.save(tmp_path, "JPEG", quality=85, optimize=True)
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def make_thumbnail(path: str, out_path: str, max_size: Tuple[int, int] = (512, 512)) -> None:
    if _is_szi(path):
        from .szi_utils import make_szi_thumbnail
        make_szi_thumbnail(path, out_path, max_size=max_size)
        return
    slide = open_wsi(path)
    img = None
    try:
        # Use the lowest resolution level to minimise memory usage.
        # get_thumbnail() may pick a higher level; explicitly using level_count-1
        # keeps peak RSS low for large WSI files on memory-constrained servers.
        level = slide.level_count - 1
        level_dims = slide.level_dimensions[level]
        img = _read_level_bounded(slide, level, level_dims)
        img.thumbnail(max_size, Image.LANCZOS)
        save_thumbnail_atomic(img, out_path)
    finally:
        # Free PIL/OpenSlide resources whether or not the save succeeded.
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
        slide.close()


# Peak-RSS ceiling for a single read_region() during thumbnailing, in pixels.
# OpenSlide hands back RGBA (4 bytes/px), so ~64 Mpx ≈ 256 MB per read. A
# non-pyramidal (single-level) slide has its lowest level == the full base
# image, so ``read_region((0,0), level, level_dims)`` on a 100k×100k scan would
# otherwise attempt a ~40 GB allocation and OOM-kill the worker. Reading in
# row-strips capped at this budget keeps peak memory bounded regardless of the
# level's total size.
_THUMB_READ_BUDGET_PX = 64_000_000

# Largest smallest level an OpenSlide-read slide may have at upload. Deep
# Zoom reads the smallest level whole for every low-zoom tile, so a slide
# with no pyramid makes each of those tiles read the full base image: one
# 40000 x 40000 tiled TIFF (5 MB on disk) would ask for 6.4 GB of RGBA per tile.
# dzi reads any oversized region in bounded blocks, so this caps the CPU such
# a tile costs rather than its memory. It sits above the OME-TIFF reader's
# 16 Mpx (WSI_OME_MAX_LOWEST_LEVEL_PIXELS) on purpose: vendor pyramids step
# 4x per level, so the smallest level of a large scan's SVS, 16x down, can be
# 30-50 Mpx — sizes the thumbnailer already reads in one call
# (_THUMB_READ_BUDGET_PX). What it refuses is a file with no pyramid at all
# beyond ~8000 x 8000 (``no_pyramid``) and, under its own reason
# (``lowest_level_too_large``), a pyramid that stops above it — a
# 130000 x 128000 scan whose 4x pyramid ends at 8125 x 8000. That one is not
# waved through on the strength of its pyramid: the lowest zooms read the
# smallest level whole however it came to be that large, so the CPU a tile
# costs is the same as for a flat file of that size.
_MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS = _THUMB_READ_BUDGET_PX


def check_pyramid_layout(slide) -> None:
    """Refuse a slide whose pyramid Deep Zoom can't serve within bounds.

    The OpenSlide-read counterpart of the OME-TIFF reader's ``no_pyramid`` /
    ``lowest_level_too_large`` / ``level_gap`` refusals, raised as ``UnsupportedSlideError`` with the same
    reasons so the admin is told what to fix (code ``slideLayout.<reason>``).
    Deep Zoom reads each tile from the next-finer stored level, so the region
    one tile reads is up to the tile size times the gap between two levels on
    a side — and the smallest level whole for the lowest zooms. The level gap
    shares the OME reader's 16x (real pyramids step 2x or 4x); see
    ``_MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS`` for the smallest level. Upload
    validation only (upload / replace / import): a stored slide is never
    newly refused — a file placed by hand keeps opening, and dzi bounds what
    its tiles read instead.
    """
    dims = [(int(w), int(h)) for w, h in slide.level_dimensions]
    for parent, child in zip(dims, dims[1:]):
        # Allows for the writer's rounding of each level's size (an exact 16x
        # step floored can be 16.0009x), as the OME reader does.
        if level_gap_too_wide(parent, child):
            raise UnsupportedSlideError("level_gap", level_gap_message(parent, child))
    lw, lh = dims[-1]
    if lw * lh > _MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS:
        # A pyramid that stops too early is not "no pyramid": the admin's fix
        # is more levels, not a pyramid. The limit is the same either way —
        # what the lowest zooms read scales with the smallest level, however
        # it came to be that large.
        raise UnsupportedSlideError(
            "no_pyramid" if len(dims) == 1 else "lowest_level_too_large",
            f"smallest level is {lw}x{lh} "
            f"(max {_MAX_OPENSLIDE_LOWEST_LEVEL_PIXELS} pixels without a finer pyramid)",
        )


def _slide_background_rgb(slide) -> Tuple[int, int, int]:
    """The colour OpenSlide's DeepZoom composites transparent pixels onto: the
    ``openslide.background-color`` property (``RRGGBB``) when the slide has a
    valid one, else white."""
    try:
        raw = str(slide.properties.get("openslide.background-color") or "").strip().lstrip("#")
        if len(raw) == 6:
            return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except Exception:
        pass
    return (255, 255, 255)


def _flatten_region(region: Image.Image, background: Tuple[int, int, int]) -> Image.Image:
    """Composite a ``read_region`` result onto ``background`` as an RGB image.

    A bare ``convert("RGB")`` drops the alpha channel, so the transparent
    (premultiplied zero) pixels of an OpenSlide empty region or an OME sparse
    segment come out BLACK — while the viewer's tiles (DeepZoomGenerator)
    composite them onto the slide background, normally white.
    """
    if region.mode != "RGBA":
        return region.convert("RGB")
    out = Image.new("RGB", region.size, background)
    out.paste(region, (0, 0), region)
    return out


def _read_level_bounded(slide, level: int, level_dims: Tuple[int, int]) -> Image.Image:
    """Read an OpenSlide level as an RGB image with bounded peak memory.

    For levels within the budget this is a single ``read_region``; for larger
    levels (typically a single-level slide whose lowest level is full-res) it
    reads f-aligned row strips, downscales each to an intermediate integer
    downsample, and stitches them so peak RSS stays near one strip.
    """
    w, h = int(level_dims[0]), int(level_dims[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid level dimensions {level_dims!r}")

    background = _slide_background_rgb(slide)
    if w * h <= _THUMB_READ_BUDGET_PX:
        region = slide.read_region((0, 0), level, (w, h))
        try:
            return _flatten_region(region, background)
        finally:
            region.close()

    # Integer downsample factor that brings the whole level under budget, then
    # strip height (in level rows) sized so one strip's RGBA read stays under
    # budget. Strips are aligned to a multiple of f so paste offsets are exact.
    f = max(1, math.ceil(math.sqrt((w * h) / _THUMB_READ_BUDGET_PX)))
    out_w = max(1, w // f)
    out_h = max(1, h // f)
    rows_per_strip = max(f, (max(1, _THUMB_READ_BUDGET_PX // w) // f) * f)
    l0_downsample = slide.level_downsamples[level]

    out = Image.new("RGB", (out_w, out_h))
    ry = 0
    while ry < h:
        rh = min(rows_per_strip, h - ry)
        # read_region location is in level-0 coordinates; convert the strip's
        # level-row offset back to the base reference frame.
        loc_y = int(round(ry * l0_downsample))
        region = slide.read_region((0, loc_y), level, (w, rh))
        try:
            strip = _flatten_region(region, background)
        finally:
            region.close()
        try:
            strip_out_h = max(1, rh // f)
            scaled = strip.resize((out_w, strip_out_h), Image.LANCZOS)
            try:
                paste_y = ry // f
                if paste_y < out_h:
                    out.paste(scaled, (0, paste_y))
            finally:
                scaled.close()
        finally:
            strip.close()
        ry += rh
    return out


def _encode_associated_jpeg(img: Image.Image) -> bytes:
    work = img.convert("RGB")
    try:
        work.thumbnail(
            (_ASSOCIATED_MAX_LONG_SIDE, _ASSOCIATED_MAX_LONG_SIDE),
            Image.LANCZOS,
        )
        buf = io.BytesIO()
        work.save(buf, "JPEG", quality=_ASSOCIATED_JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    finally:
        try:
            work.close()
        except Exception:
            pass


# TIFF extensions the IFD-walk fallback is tried against. SZI / MRXS aren't
# TIFF containers so the fallback would just error out; OpenSlide's own
# extractor remains the only path for those.
_TIFF_EXTS = (".svs", ".tif", ".tiff", ".ndpi", ".scn", ".bif")


def _read_associated_images_via_tiff_ifds(path: str) -> Dict[str, bytes]:
    """Fallback: pull macro/label IFDs straight out of the TIFF container.

    OpenSlide's ``slide.associated_images`` is populated by the per-vendor
    detector — for Aperio SVS specifically it only fires when IFD 0's
    ImageDescription starts with the literal ``Aperio`` marker. Files
    written by other scanners that *follow* the Aperio convention
    (``SubFileType=9`` + ``ImageDescription`` prefix of ``"macro "`` /
    ``"label "``) but don't carry the vendor banner fall back to the
    generic TIFF reader, which only surfaces ``thumbnail``.

    Walk the IFDs directly with Pillow and pick up any whose
    ``ImageDescription`` matches the convention. Cheap because Pillow is
    lazy: each ``img.seek(i)`` only parses one directory entry, and
    ``img.copy()`` (which materialises the decoded image) is only called
    on the small macro/label pages.
    """
    if not path.lower().endswith(_TIFF_EXTS):
        return {}
    out: Dict[str, bytes] = {}
    # Pillow guards Image.open() with a decompression-bomb check against
    # MAX_IMAGE_PIXELS (~178M by default) computed from IFD 0's dimensions.
    # A WSI's IFD 0 is the pyramid base — easily several billion pixels —
    # so the check trips before the code can seek to the small
    # macro/label IFDs. ``_pil_bomb_cap_disabled`` raises the cap to a large
    # finite ceiling under a process-wide lock so concurrent uploads can't race
    # the save/restore and leave the cap permanently weakened.
    with _pil_bomb_cap_disabled():
        try:
            # This walk only ever reads TIFF containers (_TIFF_EXTS above), so
            # pin the decoder rather than letting Pillow sniff the upload
            # against every plugin it ships.
            img = Image.open(path, formats=["TIFF"])
        except Exception:
            _logger.warning("PIL failed to open %s for IFD walk", path, exc_info=True)
            return {}
        try:
            # Pillow exposes ``n_frames`` for multi-page TIFFs; missing on a
            # corrupt header. Fall through to a manual ``seek`` loop in that
            # case rather than bailing — a single bad page shouldn't suppress
            # the rest of the container.
            try:
                n_frames = img.n_frames
            except Exception:
                n_frames = None
            i = 0
            while True:
                if n_frames is not None and i >= n_frames:
                    break
                try:
                    img.seek(i)
                except EOFError:
                    break
                except Exception:
                    _logger.warning("IFD %d unreadable in %s", i, path, exc_info=True)
                    break
                try:
                    # Tag 270 = ImageDescription per the TIFF 6.0 spec.
                    desc_raw = img.tag_v2.get(270)
                except Exception:
                    desc_raw = None
                i += 1
                if not desc_raw:
                    continue
                try:
                    desc = str(desc_raw)
                except Exception:
                    continue
                head = desc.strip().lower()
                for key, prefix in (("macro", "macro"), ("label", "label")):
                    if key in out:
                        continue
                    # Aperio writes "macro " / "label " (trailing space) but bare
                    # names and case differences are tolerated as well.
                    if head == prefix or head.startswith(prefix + " ") or head.startswith(prefix + "\t"):
                        # img.size is the current IFD's, read from its tags;
                        # nothing has been decoded yet.
                        if img.size[0] * img.size[1] > _ASSOCIATED_MAX_PIXELS:
                            _logger.warning(
                                "skipping TIFF IFD %r in %s: %dx%d exceeds %d pixels",
                                key, path, img.size[0], img.size[1], _ASSOCIATED_MAX_PIXELS,
                            )
                            break
                        try:
                            out[key] = _encode_associated_jpeg(img)
                        except Exception:
                            _logger.warning(
                                "failed to encode TIFF IFD %r (%s) in %s",
                                key, desc[:32], path, exc_info=True,
                            )
                        break
                if "macro" in out and "label" in out:
                    break
        finally:
            try:
                img.close()
            except Exception:
                pass
    return out


def _associated_image_dims(slide, name: str) -> Tuple[int, int]:
    """Pixel size of an associated image, read BEFORE decoding it.

    The ``openslide.associated.<name>.width/height`` properties only exist in
    libopenslide >= 4.0; with 3.x (what install.sh puts on Ubuntu <= 24.04)
    they are absent, which reads as (0, 0) and would let the size guard pass
    anything. The low-level call exists in every version, so ask it first and
    fall back to the properties (an OME slide has no native handle).
    """
    try:
        w, h = openslide.lowlevel.get_associated_image_dimensions(slide._osr, name)
        if int(w) > 0 and int(h) > 0:
            return int(w), int(h)
    except Exception:
        pass
    try:
        props = slide.properties
        return (
            int(props.get(f"openslide.associated.{name}.width") or 0),
            int(props.get(f"openslide.associated.{name}.height") or 0),
        )
    except Exception:
        return (0, 0)


def read_associated_images(path: str) -> Dict[str, bytes]:
    """Return JPEG bytes for the slide's macro and label associated images.

    Vendor formats (Aperio SVS, Hamamatsu NDPI, 3DHISTECH MRXS, Leica SCN, ...)
    typically expose a ``macro`` (slide overview) and / or ``label`` (barcode /
    handwritten id) view alongside the tile pyramid. OpenSlide normalises
    these as a ``slide.associated_images`` mapping; the code simply plucks
    the two keys of interest, downscales, and re-encodes as JPEG.

    Returns a dict with at most ``"macro"`` and ``"label"`` keys; absent images
    (TIFF, SZI, vendor file that doesn't carry one) just don't appear.
    """
    if _is_szi(path):
        return {}
    out: Dict[str, bytes] = {}
    try:
        slide = open_wsi(path)
    except Exception:
        _logger.warning("failed to open %s for associated-image read", path, exc_info=True)
        return {}
    # An OME-TIFF names its label / macro as OME Images, which OmeTiffSlide
    # already surfaces; the Aperio-convention IFD walk below has nothing to
    # find there (and Pillow can't open the BigTIFF container most use).
    is_ome = isinstance(slide, OmeTiffSlide)
    try:
        try:
            associated = slide.associated_images
        except Exception:
            _logger.warning("slide.associated_images access failed for %s", path, exc_info=True)
            return {}
        # Diagnostic so operators can see why their file produced no macro:
        # some SVS test extracts ship without associated images, and TIFFs /
        # SZIs never have any.
        #
        # INFO, not WARNING: a slide simply having no macro/label page is
        # normal for several supported formats, and DBLogHandler mirrors every
        # WARNING+ record from ``wsi_app.*`` into the ``error_logs`` table that
        # backs the admin Error-log screen. At WARNING this success-path line
        # would land there on every upload, spending the table's row cap on
        # non-errors and burying the failures the screen exists to surface.
        # The two genuine failure paths around this call stay at WARNING.
        # Operators diagnosing a missing macro can set WSI_ERROR_LOG_LEVEL=INFO
        # to bring it back into that view.
        try:
            available = list(associated)
        except Exception:
            available = []
        _logger.info("associated images for %s: %s", path, available or "(none)")
        # Map normalised name -> actual key so vendor variations are tolerated.
        # OpenSlide's Aperio reader is meant to insert "macro" / "label" into
        # the names list (no trailing space, lowercase), but some writers
        # preserve the on-disk Aperio convention of "macro " / "label " with
        # a trailing space (the same prefix the format spec uses to tag the
        # IFDs). Matching case-insensitively and after .strip() means either
        # form is picked up without the operator having to know which their
        # scanner produced.
        canonical = {}
        for raw_name in available:
            try:
                norm = raw_name.strip().lower()
            except Exception:
                continue
            if norm in ("macro", "label") and norm not in canonical:
                canonical[norm] = raw_name
        for key in ("macro", "label"):
            actual_key = canonical.get(key)
            if actual_key is None:
                continue
            # Check the declared size before read_associated_image allocates
            # w*h*4 bytes.
            dims = _associated_image_dims(slide, actual_key)
            if dims[0] * dims[1] > _ASSOCIATED_MAX_PIXELS:
                _logger.warning(
                    "skipping associated image %r of %s: %dx%d exceeds %d pixels",
                    actual_key, path, dims[0], dims[1], _ASSOCIATED_MAX_PIXELS,
                )
                continue
            img: Optional[Image.Image] = None
            try:
                img = associated[actual_key]
            except KeyError:
                continue
            except Exception:
                # Some vendor formats raise on access of a declared-but-broken
                # entry. Log and continue so a corrupt label doesn't block
                # ingestion of the rest of the slide.
                _logger.warning(
                    "associated image %r unreadable for %s", actual_key, path, exc_info=True,
                )
                continue
            if img is None:
                continue
            try:
                out[key] = _encode_associated_jpeg(img)
            except Exception:
                _logger.warning(
                    "failed to encode associated image %r for %s",
                    actual_key, path, exc_info=True,
                )
            finally:
                try:
                    img.close()
                except Exception:
                    pass
    finally:
        slide.close()
    # OpenSlide skipped macro/label (or one of them) — fall back to a
    # direct TIFF IFD walk so Aperio-convention files that don't carry the
    # "Aperio Image Library" banner in IFD 0 still surface their overview
    # to the dashboard.
    if not is_ome and ("macro" not in out or "label" not in out):
        try:
            fb = _read_associated_images_via_tiff_ifds(path)
        except Exception:
            _logger.warning("TIFF-IFD fallback failed for %s", path, exc_info=True)
            fb = {}
        if fb:
            _logger.warning(
                "TIFF-IFD fallback recovered %s for %s",
                sorted(k for k in fb if k not in out) or "(none)", path,
            )
        for key, data in fb.items():
            out.setdefault(key, data)
    return out
