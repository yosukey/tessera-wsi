from __future__ import annotations
import io
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Annotated, Optional, Union
from urllib.parse import unquote_plus
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import FileResponse, Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Slide, tile_version
from .app_auth import staff_token_valid
from .config import settings, env_int
from .db import SessionLocal
from . import slide_resolve_cache
from .openslide_utils import _flatten_region, _slide_background_rgb, open_wsi
from openslide import OpenSlide, OpenSlideError
from openslide.deepzoom import DeepZoomGenerator

logger = logging.getLogger(__name__)

router = APIRouter()

TILE_SIZE = env_int("DZI_TILE_SIZE", 256)
OVERLAP = env_int("DZI_OVERLAP", 1)
# Tune via DZI_CACHE_SIZE env var. Each entry holds an open OpenSlide handle
# plus OS page-cache pressure proportional to slide file size, so keep this
# small on memory-constrained servers (default: 8).
_CACHE_MAX = env_int("DZI_CACHE_SIZE", 8)
# Soft cap on the sum of cached slide file sizes. Used as a proxy for the
# OS page-cache footprint that OpenSlide pulls in. 0 disables the check.
# In production this env var is auto-derived from system RAM by
# infra/systemd/start-wsi-app.sh (a RAM fraction split across the worker
# count, clamped) and exported before the uvicorn master forks; the literal
# default below only applies to direct/dev runs that bypass that script.
_CACHE_MAX_BYTES = env_int("DZI_CACHE_MAX_BYTES", 8 * 1024 * 1024 * 1024)
FORMAT = "jpeg"  # OSD-friendly


def _read_jpeg_quality() -> int:
    """JPEG quality used when re-encoding tiles. Configurable via env.

    Lower values shrink tile bytes on the wire (helps slow networks and high
    concurrency); higher values preserve diagnostic detail. Clamped to
    Pillow's accepted 1-95 range.
    """
    raw = os.getenv("DZI_TILE_QUALITY", "80")
    try:
        q = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid DZI_TILE_QUALITY=%r; falling back to 80", raw)
        return 80
    if q < 1:
        return 1
    if q > 95:
        return 95
    return q


_JPEG_QUALITY = _read_jpeg_quality()

def _slide_path(slide: Slide) -> str:
    """Resolve the on-disk path for a slide, refusing values that escape
    `<data_dir>/wsi/`.

    Uploads are already sanitised by `safe_filename()` in admin_slides, so a
    traversal sequence reaching here implies a corrupted DB row, a manual
    edit, or a future code path that forgot to sanitise — at which point
    serving a file from outside the data root would be silent. Resolve
    both the base and the joined path with `realpath` and refuse anything
    that does not land under the base. Callers map FileNotFoundError → 404.
    """
    base = os.path.realpath(os.path.join(settings.data_dir, "wsi"))
    full = os.path.realpath(os.path.join(base, slide.wsi_filename))
    # Require strict descent: rejects "" (resolves to base itself), absolute
    # paths outside the data root, and any `..` sequence that escapes.
    if not full.startswith(base + os.sep):
        raise FileNotFoundError(slide.wsi_filename)
    return full

# ---------------------------------------------------------------------------
# Bounded tile reads
# ---------------------------------------------------------------------------
# DeepZoomGenerator reads each tile's whole source region in one read_region
# call: the pixels under the tile on the next-finer stored level, and for the
# lowest zooms the smallest stored level whole. On a real pyramid that is
# about a megapixel, but a slide without one (or with a wide gap between two
# levels) makes it the full base image: a 12000 x 12000 tiled TIFF would cost
# +1.7 GB of peak RSS and 11 s of CPU per low-zoom tile, a 40000 x 40000 one
# would ask for 6.4 GB of RGBA — enough to get the worker OOM-killed. Upload
# refuses such a layout (openslide_utils.check_pyramid_layout), but a file
# placed by hand, or an OME-TIFF the OME reader declines and OpenSlide
# opens instead, still reaches here, and keeps working: a region over this
# budget is read in blocks of at most this many pixels, each shrunk as soon as
# it is read, so a tile's peak memory is one block (~160 MB with its RGB
# copy, the OME reader's bound for one read) plus a canvas about twice the
# tile's size, however large the region. Such a tile still costs CPU in
# proportion to the region, which is what the upload check caps. Tiles of a
# 2x / 4x pyramid read far less than this and keep DeepZoomGenerator's own
# single read.
_TILE_READ_BUDGET_PX = 4096 * 4096


def _thumbnail_size(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """The size ``Image.thumbnail(box)`` gives an image of ``size`` (Pillow's
    aspect-preserving rounding), so a tile read in blocks comes out exactly
    as large as DeepZoomGenerator's one-read path would have made it."""
    w, h = size
    x, y = box
    if x >= w and y >= h:
        return w, h
    aspect = w / h
    if x / y >= aspect:
        x = max(min(math.floor(y * aspect), math.ceil(y * aspect),
                    key=lambda n: abs(aspect - n / y)), 1)
    else:
        y = max(min(math.floor(x / aspect), math.ceil(x / aspect),
                    key=lambda n: 0 if n == 0 else abs(aspect - x / n)), 1)
    return x, y


def _read_region_bounded(osr, location, level: int, size, out_box, background) -> Image.Image:
    """``read_region`` + composite + shrink to fit ``out_box``, as
    DeepZoomGenerator.get_tile does, reading at most _TILE_READ_BUDGET_PX
    pixels at a time.

    Each block is box-reduced by an integer factor ``f`` straight after it is
    read and pasted into a canvas of the whole region at 1/f scale; one
    LANCZOS pass then brings the canvas to the tile size. ``f`` keeps the
    canvas at least twice the tile per side — what thumbnail()'s
    reducing_gap=2 hands its LANCZOS pass too — and blocks are f-aligned so
    each lands on whole canvas pixels.
    """
    lw, lh = int(size[0]), int(size[1])
    tw, th = _thumbnail_size((lw, lh), out_box)
    side = math.isqrt(_TILE_READ_BUDGET_PX)
    # Capped at one block side so a block always holds at least one f x f cell.
    f = max(1, min(lw // (2 * tw), lh // (2 * th), side))
    canvas_w, canvas_h = -(-lw // f), -(-lh // f)
    if canvas_w * canvas_h > max(_TILE_READ_BUDGET_PX, 16 * tw * th):
        # Only the capped f gets here: a level some 16 M px on a side. No
        # file a scanner writes; refused (a 404) rather than allocated.
        raise ValueError(f"tile source region {lw}x{lh} is too large to read")
    bw = min(lw, max(f, side // f * f))
    bh = min(lh, max(f, _TILE_READ_BUDGET_PX // bw // f * f))
    downsample = osr.level_downsamples[level]
    canvas = Image.new("RGB", (canvas_w, canvas_h), background)
    try:
        for by in range(0, lh, bh):
            for bx in range(0, lw, bw):
                # read_region takes its location in level-0 coordinates.
                region = osr.read_region(
                    (location[0] + int(round(bx * downsample)),
                     location[1] + int(round(by * downsample))),
                    level,
                    (min(bw, lw - bx), min(bh, lh - by)),
                )
                try:
                    block = _flatten_region(region, background)
                finally:
                    region.close()
                try:
                    if f > 1:
                        small = block.reduce(f)
                        try:
                            canvas.paste(small, (bx // f, by // f))
                        finally:
                            small.close()
                    else:
                        canvas.paste(block, (bx, by))
                finally:
                    block.close()
        if canvas.size == (tw, th):
            out, canvas = canvas, None
            return out
        # The canvas spans lw/f x lh/f region pixels, not its whole size: the
        # last column / row holds a partial cell (reduce() averages what is
        # there), so resizing the whole canvas would stretch the image by up
        # to ~0.5 output px towards the right and bottom edges. A fractional
        # box maps exactly the region's extent onto the tile.
        return canvas.resize((tw, th), Image.LANCZOS, box=(0, 0, lw / f, lh / f))
    finally:
        if canvas is not None:
            canvas.close()


# Block-read tiles rendered at once in this worker process. One such tile on
# a flat slide can cost ~160 MB and over a minute of CPU, and nginx's
# proxy_cache_lock only holds concurrent misses for the same URL for 10 s
# before letting each through, so without a bound every viewer opening the
# slide would start its own render of the same tiles: N x 160 MB and the
# threadpool's threads all decoding. One at a time caps a worker's extra
# memory at one render; the tiles of a 2x / 4x pyramid never get here. A
# request queued here still holds its threadpool thread, as it did while
# rendering, but idle — and with the memo below most of them return at once.
_BOUNDED_RENDER_SLOTS = 1
_bounded_render_slots = threading.BoundedSemaphore(_BOUNDED_RENDER_SLOTS)
# Block-read tile requests a worker lets wait (or render) at once; one more is
# refused with BoundedRenderBusy (a 503 with Retry-After, which nginx does not
# cache) instead of queueing. The queue above is serial and each render can
# take a minute, while a flat slide's first screen asks for a few dozen such
# tiles: left unbounded, the waiters held the worker's threadpool threads
# (Starlette's default limit is 40) for minutes and starved every other
# request on it, normal tiles and the API included. Eight keeps most of the
# pool free; a refused tile is fetched again on the viewer's next pan / zoom.
_BOUNDED_MAX_QUEUED = 8
_bounded_queued = 0
_bounded_queued_lock = threading.Lock()


class BoundedRenderBusy(Exception):
    """This worker already has _BOUNDED_MAX_QUEUED block-read tiles in hand."""


class BoundedRenderLane:
    """A render slot and queue of its own, for a generator that must neither
    take production's (``_bounded_render_slots`` / ``_bounded_queued``) nor
    wait behind it: the admin performance benchmark (perf_bench), whose
    parallel phase would otherwise fill the worker's eight-tile queue —
    refusing a viewer's block-read tile with a 503 — and whose timings would
    include waits for whatever a class was rendering at the time. Serial like
    production (one slot), so a benchmark tile costs what a viewer's does;
    the price is one more block-read render's memory in the worker while a
    benchmark runs (one run at a time, a time limit) — plus one for each
    earlier run whose render is still stuck in a read, as every run gets a
    lane of its own (perf_bench._new_bench_lane) rather than wait behind
    such a render for ever."""

    def __init__(self, slots: int = _BOUNDED_RENDER_SLOTS, max_queued: int = _BOUNDED_MAX_QUEUED) -> None:
        self.slots = threading.BoundedSemaphore(slots)
        self.max_queued = max_queued
        self.queued = 0
        self.lock = threading.Lock()
# Block-read tiles each generator keeps after rendering, so the requests that
# queued behind a render of the same tile (above) get its result instead of
# rendering it again. A few hundred KB each; a slide has a few dozen such
# tiles at most, nginx stores them once served, and the memo goes with the
# handle when the file changes.
_BOUNDED_MEMO_TILES = 8


class BoundedDeepZoomGenerator(DeepZoomGenerator):
    """DeepZoomGenerator whose tile reads have bounded peak memory (see
    "Bounded tile reads" above). Every tile it renders through
    DeepZoomGenerator itself unless the tile's source region exceeds
    _TILE_READ_BUDGET_PX; those are rendered one at a time per process
    (_bounded_render_slots), once per tile for concurrent requests, and
    refused with BoundedRenderBusy past _BOUNDED_MAX_QUEUED in hand. A
    ``lane`` (BoundedRenderLane) replaces that process-wide slot and queue
    with its own; production generators never pass one."""

    def __init__(self, osr, *, lane: Optional[BoundedRenderLane] = None, **kwargs) -> None:
        super().__init__(osr, **kwargs)
        self._bounded_lane = lane
        self._bounded_osr = osr
        # The colour DeepZoomGenerator composites onto, parsed once.
        self._bounded_background = _slide_background_rgb(osr)
        # Held across a block-read render of this slide, so a request for a
        # tile being rendered waits for it and then finds it memoised.
        self._bounded_lock = threading.Lock()
        self._bounded_memo: OrderedDict = OrderedDict()

    def get_tile(self, level: int, address: tuple[int, int]) -> Image.Image:
        # Both raise ValueError for a level / address outside the pyramid,
        # exactly as get_tile does (get_tile maps that to a 404).
        location, slide_level, size = self.get_tile_coordinates(level, address)
        if size[0] * size[1] <= _TILE_READ_BUDGET_PX:
            return super().get_tile(level, address)
        lane = self._bounded_lane
        if lane is not None:
            with lane.lock:
                if lane.queued >= lane.max_queued:
                    raise BoundedRenderBusy(f"{lane.queued} block-read tiles already queued")
                lane.queued += 1
            try:
                return self._get_bounded_tile(level, address, location, slide_level, size)
            finally:
                with lane.lock:
                    lane.queued -= 1
        global _bounded_queued
        with _bounded_queued_lock:
            if _bounded_queued >= _BOUNDED_MAX_QUEUED:
                raise BoundedRenderBusy(f"{_bounded_queued} block-read tiles already queued")
            _bounded_queued += 1
        try:
            return self._get_bounded_tile(level, address, location, slide_level, size)
        finally:
            with _bounded_queued_lock:
                _bounded_queued -= 1

    def _get_bounded_tile(self, level, address, location, slide_level, size) -> Image.Image:
        out_box = self.get_tile_dimensions(level, address)
        key = (level, tuple(address))
        with self._bounded_lock:
            # The caller closes what it gets, so hand out copies.
            done = self._bounded_memo.get(key)
            if done is not None:
                self._bounded_memo.move_to_end(key)
                return done.copy()
            # Per-slide lock first, process-wide (or the lane's) slot second:
            # no path takes them the other way round.
            lane = self._bounded_lane
            with (lane.slots if lane is not None else _bounded_render_slots):
                tile = _read_region_bounded(
                    self._bounded_osr, location, slide_level, size,
                    out_box, self._bounded_background,
                )
            self._bounded_memo[key] = tile.copy()
            while len(self._bounded_memo) > _BOUNDED_MEMO_TILES:
                self._bounded_memo.popitem(last=False)[1].close()
            return tile


# ---------------------------------------------------------------------------
# LRU cache with explicit OpenSlide handle lifecycle
# ---------------------------------------------------------------------------
# lru_cache cannot call cleanup code when entries are evicted; the OpenSlide
# object would stay open until Python's GC decided to collect it, which on
# a memory-constrained server could mean never before OOM. The cache is
# managed manually so osr.close() can run on every eviction.
# ---------------------------------------------------------------------------
_dz_lock = threading.Lock()


class _DZEntry:
    """One cached slide's heavy resources plus a refcount for safe teardown.

    ``refs`` counts in-flight callers currently holding the handle (a tile or
    descriptor read). ``osr.close()`` must never run while a thread might still
    be inside ``dz.get_tile`` / ``dz.get_dzi`` for the same handle — that is a
    use-after-free in OpenSlide's C layer and can segfault the worker. So
    eviction / clear / stale-drop only *retire* an entry (remove it from the
    cache and mark it ``closing``); whichever side then drops ``refs`` to zero
    performs the single actual close. An entry with ``closing`` set is never in
    the cache, so it can never gain a new ref.
    """

    __slots__ = ("osr", "dz", "etag", "size", "path", "refs", "closing", "generation")

    def __init__(self, osr, dz, etag, size, path, generation=None):
        self.osr = osr
        self.dz = dz
        self.etag = etag
        self.size = size
        self.path = path
        self.refs = 0
        self.closing = False
        # The row's tile generation read right AFTER the file was opened (see
        # _open_dz), or None when unknown.
        self.generation = generation


def _file_etag(slide_id: int, st: os.stat_result) -> str:
    """Content ETag for a WSI/SZI file.

    Includes ``st_size`` alongside the mtime so a same-mtime replace still
    changes the tag: a restore/rollback promotion done with ``shutil.move``
    (or ``rsync -t``, or a coarse-timestamp filesystem) can preserve the source
    mtime, and an mtime-only tag would then keep every worker serving the old
    cached handle and browsers 304'ing forever. Size differs in the vast
    majority of real replacements, closing that window cheaply.
    """
    return f'"{slide_id}-{st.st_mtime}-{st.st_size}"'


# Rendering parameters baked into every OpenSlide-generated descriptor
# (TileSize/Overlap) and tile (JPEG quality). They are part of the DZI/tile
# ETag so a restart with a changed DZI_TILE_SIZE / DZI_OVERLAP /
# DZI_TILE_QUALITY stops browsers 304'ing the old descriptor, which would pair
# stale TileSize/Overlap with tiles cut on the new grid. SZI archives are
# served as-is, so get_szi keeps the plain _file_etag.
_RENDER_PARAMS = f"t{TILE_SIZE}o{OVERLAP}q{_JPEG_QUALITY}"


# The tile grid every OpenSlide pyramid is cut on. A tile URL names a grid
# cell (/dzi/<id>_files/<level>/<col>_<row>.jpeg?v=<generation>), so a change
# of either value re-cuts every pyramid under the same URLs — and tiles under
# a current ?v= are cached by nginx for 30 days and by browsers for a day
# ($wsi_dzi_tile_cache_control). reconcile_tile_geometry moves every slide to
# a new generation when it changes, the way a replace does for one slide.
# (JPEG quality is left out: other bytes on the same grid draw correctly.)
_TILE_GEOMETRY = f"t{TILE_SIZE}o{OVERLAP}"
KEY_TILE_GEOMETRY = "dzi_tile_geometry"


def reconcile_tile_geometry(engine) -> bool:
    """Give every slide a new tile generation if ``DZI_TILE_SIZE`` /
    ``DZI_OVERLAP`` changed since the last boot; return whether it did.

    Called by the lifespan under the startup schema lock, so one worker does
    it. A boot that finds no recorded geometry (a fresh database) only
    records it: there is no earlier grid to move away from.
    """
    from sqlalchemy import update
    from sqlalchemy.sql import func

    from . import app_meta
    from .models import fresh_wsi_generation

    with Session(engine) as db:
        recorded = app_meta.get(db, KEY_TILE_GEOMETRY)
        if recorded == _TILE_GEOMETRY:
            return False
        moved = recorded is not None
        if moved:
            db.execute(update(Slide).values(
                wsi_generation=func.max(Slide.wsi_generation + 1, fresh_wsi_generation())
            ))
            logger.warning(
                "Deep Zoom tile geometry changed (%s -> %s): every slide moved to a "
                "new tile generation, so viewers fetch tiles cut on the new grid",
                recorded, _TILE_GEOMETRY,
            )
        app_meta.set_value(db, KEY_TILE_GEOMETRY, _TILE_GEOMETRY)
        db.commit()
    return moved


def _dz_etag(slide_id: int, st: os.stat_result) -> str:
    """ETag for an OpenSlide-rendered descriptor/tile: file identity plus
    the rendering parameters (see _RENDER_PARAMS)."""
    return f'"{slide_id}-{st.st_mtime}-{st.st_size}-{_RENDER_PARAMS}"'


# Cache value: a _DZEntry (OpenSlide handle, DeepZoomGenerator, etag,
# file_size_bytes, path, refcount).
# etag is derived from the WSI file's mtime at open time and changes whenever
# the file is replaced, allowing browsers to detect stale cached tiles.
#
# Neither is_published NOR the file's freshness is trusted from the cache
# across workers. Each uvicorn worker holds its own _dz_cache, and
# clear_dz_cache only evicts on the worker that handled the publish/replace
# call. For the publish flag, trusting this cache would mean patchy tiles
# (requests load-balanced to a worker with a stale draft flag would 404); so
# it is resolved live from the DB (via _resolve_slide) rather than
# trusted from this handle cache, and that resolution is itself served by the
# short-lived, sentinel-invalidated slide_resolve_cache so a crowd doesn't
# read the DB per tile. For file *content*, a slide whose WSI was replaced on
# one worker would otherwise be served — by every other worker — from a
# cached OpenSlide handle still pointing at the old inode (Unix keeps the
# unlinked file alive via the open fd) with the old mtime etag, so browsers
# 304 and never see the new image until LRU eviction. To avoid that, the
# cache-hit path re-stat()s the file and reopens when the mtime no longer
# matches the cached etag (see _entry_is_fresh). The stored path lets that
# check run without a DB lookup.
_dz_cache: "OrderedDict[int, _DZEntry]" = OrderedDict()


def _cache_bytes_locked() -> int:
    return sum(entry.size for entry in _dz_cache.values())


def _is_admin_request(request: Request) -> bool:
    """True iff the request carries a valid *staff* Bearer token.

    Used to gate draft-slide tile access: the public /api/slides/{public_id}
    response 404s on unpublished slides so the existence of a draft cannot be
    probed; the same contract must hold on /dzi/* tile bytes or anyone with a
    token could pull the actual image. Staff previews ride through with their
    JWT (passed by the frontend via OSD's ajaxHeaders / szi-tile-source's
    fetchOptions). Accepts any staff role — parity with the public draft-preview
    bypass and with require_staff — so managers/instructors can preview the
    unpublished tours/slides they are allowed to author.
    """
    return staff_token_valid(request.headers.get("authorization", ""))


def _resolve_slide(
    public_id: str, *, fresh: bool = False,
) -> tuple[Optional[int], Optional[bool], Optional[int]]:
    """Map a public_id token to the internal ``(id, is_published,
    tile_version)``, or ``(None, None, None)``.

    The /dzi/ family is addressed by the unguessable public_id — never the
    numeric PK — so an unlisted slide's tiles can't be pulled by walking ids.
    Everything downstream (the DZ cache, OpenSlide handle, file path) stays
    keyed by the internal PK, so this one indexed lookup is the only bridge.
    The third slot is the slide's WSI content generation (``models.tile_version``
    of ``Slide.wsi_generation``), which every versioned tile URL is checked
    against; it rides along in the same row so the check costs no extra query.

    A crowd viewing slides turns this into hundreds of identical DB reads per
    second, so the result is served from ``slide_resolve_cache`` — a per-worker
    cache kept coherent across workers by a sentinel that a publish / delete /
    WSI replace bumps (see that module). The cache re-checks the sentinel on
    each call, so a hide/delete/replace still takes effect promptly; it can be
    disabled with WSI_SLIDE_META_CACHE_TTL_SECONDS=0 to restore the read-live
    behaviour. ``fresh=True`` skips the cached value (and re-seats it) — for a
    caller holding evidence the entry predates a change another worker made.

    Both cache calls re-check that sentinel, so this is also where the worker
    learns of a delete / replace done elsewhere and drops its own handles on
    the files involved (_retire_handles_of_changed_files).
    """
    if not fresh:
        cached = slide_resolve_cache.get(public_id)
        if cached is not slide_resolve_cache.MISS:
            _retire_handles_of_changed_files()
            return cached
    # Capture the generation BEFORE the read so that if a publish/unpublish/
    # delete/replace invalidates the cache while this query is in flight, the
    # put below is dropped instead of re-seating a stale value until the TTL.
    gen_token = slide_resolve_cache.current_generation()
    _retire_handles_of_changed_files()
    db: Session = SessionLocal()
    try:
        row = db.execute(
            select(Slide.id, Slide.is_published, Slide.wsi_generation)
            .where(Slide.public_id == public_id)
        ).first()
        value = (
            (None, None, None) if row is None
            else (row[0], bool(row[1]), tile_version(row[2]))
        )
    finally:
        db.close()
    slide_resolve_cache.put(public_id, value, gen_token)
    return value


# ---------------------------------------------------------------------------
# Tile URL generation (``?v=``)
# ---------------------------------------------------------------------------
# The viewer opens ``/dzi/{public_id}.dzi?v={tile_version}``; OpenSeadragon's
# DziTileSource copies that query onto every tile URL, so tiles are fetched as
# ``.../{level}/{col}_{row}.jpeg?v=N`` too. nginx keys its 30-day shared tile
# cache on ``$uri?v=$arg_v`` (infra/nginx/wsi-locations.conf), so a WSI replace
# — which bumps the generation — moves every viewer onto URLs no old tile was
# ever stored under. The routes below keep each such key single-generation:
#
# * ``v`` equal to the current generation -> served, and nginx may store it.
# * ``v`` absent / empty (a script or other non-SPA caller) -> still served,
#   but never stored: nginx bypasses the cache for an empty $arg_v, and the
#   response also says ``X-Accel-Expires: 0`` (honoured in the /dzi/
#   locations, which ignore only Cache-Control) so a custom nginx config
#   that keys on the bare path cannot store it either.
# * any other ``v`` (stale, malformed, duplicated) -> 404 with
#   ``X-Accel-Expires: 0``. Not cacheable, so a stale tab cannot park a 404
#   under a key, and a ``?v=<random>`` spray cannot mint cache entries.
#
# nginx strips X-Accel-* headers before the response reaches the browser.
_NOT_EDGE_CACHEABLE = {"X-Accel-Expires": "0"}

# Canonical decimal only, like the tile coordinates (see _CANONICAL_UINT
# below): "03", "+3", "3.0" would each be a distinct nginx cache key for one
# generation. 18 digits leaves room for fresh_wsi_generation()'s millisecond
# values while staying well inside int64.
_CANONICAL_GENERATION = re.compile(r"0|[1-9][0-9]{0,17}")


class _InvalidGeneration:
    """Sentinel: the query carries a ``v`` the routes refuse to serve."""


_INVALID_V = _InvalidGeneration()


def _requested_generation(request: Request) -> Union[None, int, _InvalidGeneration]:
    """The tile generation the request URL asks for.

    ``None`` when the query carries no ``v`` (or an empty one), the integer
    for exactly one canonical ``v``, ``_INVALID_V`` for anything else.

    Parsed from the RAW query string, mirroring what nginx keys the cache on:
    ``$arg_v`` is the raw (undecoded) value of the FIRST argument whose name is
    ``v`` compared case-insensitively. Starlette's query_params instead decode
    names and keep the LAST duplicate, so ``?v=3&v=1`` would key the entry
    under v=3 while serving generation 1 — i.e. park one generation's bytes
    under another's key for 30 days. Any query where the two readings could
    disagree (a duplicate, a percent-encoded ``%76``) is therefore refused
    rather than resolved.
    """
    raw = request.scope.get("query_string") or b""
    if not raw:
        return None
    found: list[str] = []
    for part in raw.decode("latin-1").split("&"):
        name, _sep, value = part.partition("=")
        if unquote_plus(name).lower() != "v":
            continue
        if name.lower() != "v":
            # A spelling Starlette reads as ``v`` but nginx's $arg_v does not.
            return _INVALID_V
        found.append(value)
    if not found:
        return None
    if len(found) > 1:
        return _INVALID_V
    if found[0] == "":
        return None  # nginx's $arg_v is empty too: unversioned, never stored
    if not _CANONICAL_GENERATION.fullmatch(found[0]):
        return _INVALID_V
    return int(found[0])


def _resolve_for_request(
    public_id: str, requested: Union[None, int, _InvalidGeneration],
) -> tuple[Optional[int], Optional[bool], Optional[int]]:
    """``_resolve_slide`` for a /dzi/ request carrying ``requested``.

    A ``v`` NEWER than the cached generation means the cache may predate a
    replace another worker committed (it learns of the bump only on its next
    sentinel check, up to WSI_SLIDE_META_GEN_CHECK_SECONDS later) — a
    viewer that just reloaded after the replace would otherwise get a 404 for
    the brand-new generation. Re-read once instead. Generations only grow, so
    an older ``v`` is stale without a second look; a junk huge ``v`` costs the
    same one indexed read an unknown public_id already does.
    """
    value = _resolve_slide(public_id)
    if value[0] is not None and isinstance(requested, int) and requested > value[2]:
        value = _resolve_slide(public_id, fresh=True)
    return value


def _check_generation(
    requested: Union[None, int, _InvalidGeneration], current: Optional[int],
) -> None:
    """404 (uncacheable) unless ``requested`` is absent or current. Called only
    after the publish gate, so a draft stays indistinguishable from a missing
    slide for a caller that may not see it."""
    if requested is None:
        return
    if requested is _INVALID_V or requested != current:
        raise HTTPException(
            status_code=404, detail="Slide not found", headers=_NOT_EDGE_CACHEABLE,
        )


def _get_dz_serving(slide_id: int, requested) -> "_DZEntry":
    """``_get_dz`` for a request already checked against the resolved current
    generation, refusing (404, uncacheable) a handle opened on another
    generation's bytes — the last line against a resolve cache that still
    called the request's ``v`` current while a replace swapped the file.

    A handle that predates the requested generation is dropped and reopened
    once: a replace that rolled back leaves the old file (same mtime, so the
    handle looks fresh) under a newer generation, and the handle has to learn
    it. ``_check_generation`` already refused malformed values."""
    entry = _get_dz(slide_id)
    if requested is None or entry.generation is None or entry.generation == requested:
        return entry
    outdated = requested > entry.generation
    _release(entry)
    if outdated:
        clear_dz_cache(slide_id)
        entry = _get_dz(slide_id)
        if entry.generation is None or entry.generation == requested:
            return entry
        _release(entry)
    raise HTTPException(
        status_code=404, detail="Slide not found", headers=_NOT_EDGE_CACHEABLE,
    )


def _versioned_headers(headers: dict, requested) -> dict:
    """Add the don't-store hint to an unversioned response (see above)."""
    if requested is None:
        return {**headers, **_NOT_EDGE_CACHEABLE}
    return headers


# ---------------------------------------------------------------------------
# Per-slide edge authorization (nginx auth_request -> /api/edge-authz)
# ---------------------------------------------------------------------------
# nginx serves cached /dzi/ objects without reaching these routes, so the
# publish gate above cannot protect a HIT. The edge probe therefore re-applies
# it per slide, from the in-memory resolve cache, before every HIT or MISS.
#
# The token is parsed from the raw request URI nginx forwards (X-Original-URI
# = $request_uri) with exactly the expression nginx's $wsi_authz_object map
# (infra/nginx/wsi.conf) uses to fold the slide into the cached verdict's key,
# so a verdict stored under one slide's key was always computed for that
# slide. Raw and normalised paths agree because the /dzi/ locations 400 any
# path containing "//", a dot segment or a "%" before auth runs. Anything under
# /dzi/ that does not parse — which no viewer sends — is denied (and nginx maps
# it to a key of its own, so it can never ride another URI's cached allow).
_EDGE_DZI_PATH = re.compile(r"/dzi/([A-Za-z0-9_-]{1,64})(?:_files/.*|\.dzi|\.szi)")


def edge_slide_access(original_uri: str, *, staff: bool) -> bool:
    """Whether the edge may serve ``original_uri`` (a raw request URI).

    Non-/dzi/ URIs (``/thumbs/``, ``/assets/``) are not judged here and return
    True: the app-access gate the probe already passed is all they need. For a
    /dzi/ URI the slide must exist and be published — or the request must carry
    a valid staff bearer (``staff``), the same draft-preview rule the /dzi/
    routes apply. The ``v`` is NOT judged here: a stale generation is the
    content route's 404, and keeping it out keeps one cached verdict per
    (client, slide) rather than per generation.
    """
    path = original_uri.split("?", 1)[0]
    if not path.startswith("/dzi/"):
        return True
    m = _EDGE_DZI_PATH.fullmatch(path)
    if m is None:
        return False
    slide_id, published, _generation = _resolve_slide(m.group(1))
    if slide_id is None:
        return False
    return bool(published) or staff


def _close_entry(entry: "_DZEntry") -> None:
    """Close one retired handle. Must only be called for an entry that is no
    longer in the cache and has no live refs (see _retire_locked / _release)."""
    try:
        entry.osr.close()
    except Exception:
        logger.exception("Error closing OpenSlide handle")


def _retire_locked(entry: "_DZEntry") -> Optional["_DZEntry"]:
    """Mark a just-removed-from-cache entry for closing. Caller MUST hold
    _dz_lock. Returns the entry when it can be closed immediately (no live
    refs); otherwise None — the last _release() will close it. Closing itself
    is deferred to after the lock is released (it can be slow I/O)."""
    entry.closing = True
    return entry if entry.refs <= 0 else None


def _release(entry: "_DZEntry") -> None:
    """Drop one in-flight ref taken by _get_dz. Closes the handle if this was
    the last ref and the entry has already been retired from the cache."""
    to_close: Optional["_DZEntry"] = None
    with _dz_lock:
        entry.refs -= 1
        if entry.refs <= 0 and entry.closing:
            to_close = entry
    if to_close is not None:
        _close_entry(to_close)


def _retire_cached(slide_id: int, entry: "_DZEntry") -> None:
    """Drop ``entry`` from the cache so the next request reopens the slide.

    Identity-checked, so a fresh replacement that raced in is left alone. The
    caller still holds its ref; its ``_release()`` performs the close once the
    last in-flight user is done, as with every other retirement path.
    """
    with _dz_lock:
        if _dz_cache.get(slide_id) is entry:
            _dz_cache.pop(slide_id, None)
            entry.closing = True


def _pop_lru_locked() -> Optional["_DZEntry"]:
    """Pop and return the LRU entry (still needs retiring/closing by the
    caller). Caller MUST hold _dz_lock. Returns None when the cache is empty."""
    if not _dz_cache:
        return None
    _evicted_id, entry = _dz_cache.popitem(last=False)
    return entry


def _file_identity(st: os.stat_result) -> tuple:
    """Which file a path named when it was stat()ed: the inode as well as the
    mtime / size the ETag uses, so a replacement that happens to keep both
    still reads as a different file."""
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


# Opens _open_dz attempts while replaces keep swapping the file under it. Each
# retry means a whole replace (or its rollback) landed during one open, so
# running out takes several back to back; the caller then gets a 500 and the
# viewer's next request opens the settled file.
_OPEN_ATTEMPTS = 3


def _open_dz(slide_id: int) -> "_DZEntry":
    for _attempt in range(_OPEN_ATTEMPTS):
        entry = _open_dz_once(slide_id)
        if entry is not None:
            return entry
    raise RuntimeError(f"slide {slide_id}'s file kept changing while it was opened")


def _open_dz_once(slide_id: int) -> Optional["_DZEntry"]:
    """Open the slide's file and read the generation it is served under, or
    return None when the file changed while that happened (retry)."""
    db: Session = SessionLocal()
    try:
        # Only the file name: loading the ORM row would drag the slide's
        # macro / label blobs along on every cold open.
        wsi_filename = db.execute(
            select(Slide.wsi_filename).where(Slide.id == slide_id)
        ).scalar_one_or_none()
        if not wsi_filename:
            raise KeyError(slide_id)
        # SZI slides carry a pre-rendered pyramid inside a zip archive that
        # get_szi serves via HTTP Range; OpenSlide cannot open one. A .dzi /
        # tile request for an SZI slide would otherwise raise
        # OpenSlideUnsupportedFormatError -> 500 + an error_log row that an
        # anonymous caller could loop. Reject it as a clean 404 here (mirrors
        # get_szi rejecting non-.szi filenames): the callers map
        # FileNotFoundError -> 404.
        if wsi_filename.lower().endswith(".szi"):
            raise FileNotFoundError(wsi_filename)
        # _slide_path only reads .wsi_filename (as in get_szi).
        path = _slide_path(Slide(wsi_filename=wsi_filename))
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        # The generation before the stat, compared with the one read after the
        # open below. The stat comparison alone can't see a swap that was
        # undone in between: a replace whose commit fails renames the old
        # file back (same inode, mtime and size), so an open that landed on
        # the new file would look like one on the old. But every swap is
        # bracketed by a generation bump — retired before the bytes move,
        # moved again on success or rollback — so with the generation
        # unchanged no swap settled in the window: one still in flight shows
        # in the stat, or leaves the handle on the interim generation, which
        # is never current over settled bytes (a request for the next one
        # reopens it, _get_dz_serving).
        generation_before = db.execute(
            select(Slide.wsi_generation).where(Slide.id == slide_id)
        ).scalar_one_or_none()
        st = os.stat(path)
        etag = _dz_etag(slide_id, st)
        # OpenSlide, or the OME-TIFF reader for an OME-TIFF (same interface).
        osr = open_wsi(path)
        # If DeepZoomGenerator construction raises (rare, but seen with
        # corrupt pyramids), or anything after it does, the OpenSlide handle
        # would otherwise leak.
        try:
            dz = BoundedDeepZoomGenerator(osr, tile_size=TILE_SIZE, overlap=OVERLAP, limit_bounds=False)
            # The generation these bytes may be served under, read only now
            # that the file is open. A WSI replace commits a fresh generation
            # before it moves any bytes, so a handle on the new file can never
            # record the pre-replace generation — while a request that
            # resolved that generation from a slightly stale resolve cache
            # (another worker's, up to its check interval) could otherwise
            # still reach these bytes and have nginx store them under the old
            # ?v= (see _get_dz_serving).
            generation = db.execute(
                select(Slide.wsi_generation).where(Slide.id == slide_id)
            ).scalar_one_or_none()
            # ... and the converse: an open that stalled across a whole
            # replace (swap AND commit) would read the NEW generation for a
            # handle on the OLD file, and the current ?v= would be served —
            # and stored by nginx for 30 days — the old bytes. So the path
            # must still name the file stat()ed before the open now that the
            # generation is read, and the generation must be the one read
            # before that stat (see generation_before); if not, open again.
            unchanged = (
                generation == generation_before
                and _file_identity(os.stat(path)) == _file_identity(st)
            )
        except BaseException:
            try:
                osr.close()
            except Exception:
                logger.exception("Error closing OpenSlide for slide %s after open failure", slide_id)
            raise
        if not unchanged:
            osr.close()
            return None
        return _DZEntry(osr, dz, etag, st.st_size, path, tile_version(generation))
    finally:
        db.close()


def _entry_is_fresh(slide_id: int, entry: "_DZEntry") -> bool:
    """True if the cached entry still matches the file on disk.

    Re-stat()s the stored path and compares against the cached mtime etag so
    a WSI replaced on another worker (same or new filename) is detected and
    reopened instead of served stale. A vanished/renamed path (OSError)
    counts as not-fresh so the entry is dropped and reopened from the live
    DB row. Called *outside* _dz_lock so a slow stat (e.g. a stalled mount)
    can't block every other tile request behind the lock, matching the
    module's "no I/O under the lock" rule.
    """
    try:
        st = os.stat(entry.path)
    except OSError:
        return False
    return entry.etag == _dz_etag(slide_id, st)


def _get_dz(slide_id: int) -> "_DZEntry":
    """Resolve (and ref-acquire) the cache entry for a slide.

    The returned entry has one ref held on the caller's behalf; the caller MUST
    call ``_release(entry)`` when done with ``entry.dz`` so the handle can be
    closed once it is no longer in use. The cache only holds the heavy
    resources; the publish flag is read live from the DB by callers — see the
    module-level note on cross-worker cache coherence.
    """
    with _dz_lock:
        entry = _dz_cache.get(slide_id)
        if entry is not None:
            _dz_cache.move_to_end(slide_id)
            # Acquire tentatively under the lock so the handle can't be closed
            # by a concurrent eviction/clear while freshness is checked or the
            # entry is handed back to the caller.
            entry.refs += 1
    if entry is not None:
        if _entry_is_fresh(slide_id, entry):
            return entry
        # File was replaced (this worker didn't handle it, or did but via a
        # path-only swap), so the cached handle/etag are stale.
        _drop_stale(slide_id, entry)

    # Open outside the lock — OpenSlide may do blocking I/O.
    opened = _open_dz(slide_id)

    # Collect evicted entries inside the lock and close them after release.
    # Closing OpenSlide can be slow (sync I/O against the WSI file); doing
    # it under the lock would stall every concurrent tile request behind a
    # single eviction.
    evicted: list["_DZEntry"] = []

    while True:
        with _dz_lock:
            existing = _dz_cache.get(slide_id)
            if existing is None:
                opened.refs += 1
                _dz_cache[slide_id] = opened
                _dz_cache.move_to_end(slide_id)

                # Evict by entry count first (always honoured) ...
                while len(_dz_cache) > _CACHE_MAX:
                    popped = _pop_lru_locked()
                    if popped is None:
                        break
                    closeable = _retire_locked(popped)
                    if closeable is not None:
                        evicted.append(closeable)
                # ... then by total estimated bytes; never evict the entry that
                # was just inserted (would be infinite-loop on a single oversize slide).
                if _CACHE_MAX_BYTES > 0:
                    while len(_dz_cache) > 1 and _cache_bytes_locked() > _CACHE_MAX_BYTES:
                        popped = _pop_lru_locked()
                        if popped is None:
                            break
                        closeable = _retire_locked(popped)
                        if closeable is not None:
                            evicted.append(closeable)
                break
            # Another thread won the race. Acquire its entry tentatively, as
            # on a cache hit above.
            existing.refs += 1
            _dz_cache.move_to_end(slide_id)
        # ... and check it like one: it may have opened the file a replace
        # has swapped since, while the handle built here opened the new one.
        # Handed back unchecked, it would serve the old bytes to requests
        # already on the new generation's ?v=.
        if _entry_is_fresh(slide_id, existing):
            # The entry opened here was never inserted and no refs were taken
            # on it, so it is safe to close immediately.
            _close_entry(opened)
            return existing
        _drop_stale(slide_id, existing)

    for e in evicted:
        _close_entry(e)

    return opened


def _drop_stale(slide_id: int, entry: "_DZEntry") -> None:
    """Retire a cached entry found stale, and release the tentative ref the
    caller took on it (closing it once its last in-flight user is done).
    Identity-checked, so a fresh replacement that raced in isn't evicted."""
    to_close: Optional["_DZEntry"] = None
    with _dz_lock:
        if _dz_cache.get(slide_id) is entry:
            _dz_cache.pop(slide_id, None)
            entry.closing = True
        entry.refs -= 1
        if entry.refs <= 0 and entry.closing:
            to_close = entry
    if to_close is not None:
        _close_entry(to_close)


# slide_resolve_cache.change_count() as of this worker's last sweep of its
# handle cache; None until the first one.
_handles_checked_at: Optional[int] = None


def _retire_handles_of_changed_files() -> None:
    """Drop cached handles whose file was deleted or replaced, once for each
    generation change this worker learns of.

    A cached entry is otherwise re-checked only when its own slide is asked
    for again (_get_dz). A slide deleted on another worker never is — its
    public_id stops resolving — so its entry would stay in this worker's LRU,
    holding the unlinked file open (the OME-TIFF reader keeps a descriptor;
    OpenSlide 3.x pools them), until eviction: the disk space would not be
    given back, and uploads would keep failing the free-space check (507,
    upload_space_short) after the admin had deleted slides to make room. A
    slide replaced elsewhere and not viewed here since would hold its old
    file the same way. Every delete and replace bumps the resolve cache's sentinel, so
    when this worker sees it move, each cached entry (at most DZI_CACHE_SIZE)
    is stat()ed against its file like a cache hit would be, and the stale
    ones are retired — closed once no in-flight read holds them. Only a
    worker that serves no /dzi/ request at all never looks.
    """
    global _handles_checked_at
    seen = slide_resolve_cache.change_count()
    if seen == _handles_checked_at:
        return  # the per-request cost: one comparison
    with _dz_lock:
        if seen == _handles_checked_at:
            return
        _handles_checked_at = seen
        cached = list(_dz_cache.items())
    # stat() outside the lock (see _entry_is_fresh).
    stale = [(sid, entry) for sid, entry in cached if not _entry_is_fresh(sid, entry)]
    to_close: list["_DZEntry"] = []
    with _dz_lock:
        for sid, entry in stale:
            # Identity-checked: a fresh handle that replaced it is left alone.
            if _dz_cache.get(sid) is entry:
                _dz_cache.pop(sid, None)
                closeable = _retire_locked(entry)
                if closeable is not None:
                    to_close.append(closeable)
    for entry in to_close:
        _close_entry(entry)


def clear_dz_cache(slide_id: int | None = None) -> int:
    """Close cached OpenSlide handles and remove entries.

    Pass slide_id to evict a single entry, or None to evict all.
    Returns the number of entries removed. Entries still in use by an in-flight
    tile/descriptor read are retired (so no new request can pick them up) and
    closed when that read finishes, never under it.

    Note: this only affects the calling worker's cache. Cross-worker
    coherence of slide metadata (publish flag, file path) is handled by
    reading the live row from the DB on each request rather than relying
    on this eviction; other workers drop their handles on a deleted or
    replaced file when they next see the resolve cache's sentinel move
    (_retire_handles_of_changed_files).
    """
    to_close: list["_DZEntry"] = []
    with _dz_lock:
        if slide_id is None:
            entries = list(_dz_cache.values())
            _dz_cache.clear()
        elif slide_id in _dz_cache:
            entries = [_dz_cache.pop(slide_id)]
        else:
            return 0
        for entry in entries:
            closeable = _retire_locked(entry)
            if closeable is not None:
                to_close.append(closeable)
    for e in to_close:
        _close_entry(e)
    return len(entries)


def get_dz_cache_info() -> dict:
    with _dz_lock:
        return {
            "entries": len(_dz_cache),
            "max": _CACHE_MAX,
            "bytes": _cache_bytes_locked(),
            "max_bytes": _CACHE_MAX_BYTES,
        }


# Every /dzi/ response says no-cache here, tiles included. The browser's
# tile policy — a day-long "private, max-age=86400" for a tile under the
# current ?v= — is nginx's alone ($wsi_dzi_tile_cache_control in
# infra/nginx/wsi.conf), not the app's: nginx stores the app's header with
# each cached tile, so a policy sent from here would outlive a rollback of the
# nginx config in every tile already cached (up to 30 days).
_CACHE_HEADERS = {"Cache-Control": "no-cache"}

# Bound the token path segment the same way the public API does (public.py
# _PublicIdPath) so an unbounded segment can't reach the DB layer. The column
# is String(32); 64 leaves slack while still slamming the door on huge inputs.
_PublicIdPath = Annotated[str, Path(min_length=1, max_length=64)]


@router.get("/dzi/{public_id}.dzi")
def get_dzi(public_id: _PublicIdPath, request: Request):
    # Mirror the /api/slides/{public_id} contract: a draft slide is
    # indistinguishable from a missing one for unauthenticated callers, so a
    # probe with a guessed token can't pull the image bytes. Resolve the
    # unguessable token to the internal PK and read the publish flag live so a
    # publish toggle on another worker takes effect on the next request.
    requested = _requested_generation(request)
    slide_id, published, generation = _resolve_for_request(public_id, requested)
    if slide_id is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    # Order matters: `_is_admin_request` opens a SessionLocal and runs a users
    # PK read (staff_token_valid), so evaluate the cheap `published` flag first.
    # A published slide short-circuits here and never pays the DB round-trip —
    # important because a staff user panning a published slide sends a Bearer
    # token on every one of the hundreds of tile/Range requests, and the draft
    # gate only actually needs the token check when the slide is NOT published.
    if not published and not _is_admin_request(request):
        raise HTTPException(status_code=404, detail="Slide not found")
    # A descriptor of another generation would point the viewer at that
    # generation's tile URLs; see "Tile URL generation" above.
    _check_generation(requested, generation)
    try:
        entry = _get_dz_serving(slide_id, requested)
    except HTTPException:
        raise
    except (KeyError, FileNotFoundError):
        # Slide row missing or backing file absent: legitimate 404.
        raise HTTPException(status_code=404, detail="Slide not found")
    except Exception as exc:
        # Anything else — corrupt OpenSlide pyramid, IO error, permission
        # failure — is a server-side problem that returning a bare 404 would
        # mask. Log with context so operators can diagnose, then surface 500.
        _log_open_failure(slide_id, exc)
        raise HTTPException(status_code=500, detail="Failed to open slide")
    try:
        etag = entry.etag
        headers = _versioned_headers({**_CACHE_HEADERS, "ETag": etag}, requested)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        xml = entry.dz.get_dzi(FORMAT)
        return Response(content=xml, media_type="application/xml", headers=headers)
    finally:
        _release(entry)

class _OpenFileResponse(FileResponse):
    """FileResponse over a file descriptor the handler already opened.

    FileResponse opens its path only at send time, after the handler's checks;
    serving through ``/dev/fd/<fd>`` reads the very inode those checks were
    made against, whatever has since been moved onto the original path. The
    response owns ``fd`` and closes it once sent.
    """

    def __init__(self, fd: int, st: os.stat_result, **kwargs) -> None:
        self._fd = fd
        super().__init__(f"/dev/fd/{fd}", stat_result=st, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        # Never hand the server the /dev/fd path to open on its own
        # (http.response.pathsend): it would do so after the fd is closed
        # below, when the number may name another file.
        extensions = scope.get("extensions") or {}
        if "http.response.pathsend" in extensions:
            scope = {
                **scope,
                "extensions": {k: v for k, v in extensions.items() if k != "http.response.pathsend"},
            }
        try:
            await super().__call__(scope, receive, send)
        finally:
            os.close(self._fd)


# SZI slides bypass OpenSlide tile generation: the .szi archive holds the
# pre-encoded DZI pyramid and the szi-tile-source frontend library reads
# tiles directly via HTTP Range. The endpoint just serves the archive with
# FileResponse, which already handles Range / 206 / Content-Range correctly.
# Reusing the /dzi/ prefix means the endpoint inherits the existing
# app_auth gating and nginx proxy config without touching either.
@router.get("/dzi/{public_id}.szi")
def get_szi(public_id: _PublicIdPath, request: Request):
    requested = _requested_generation(request)
    db: Session = SessionLocal()
    try:
        # Select only the scalar columns this handler touches. `.szi` is served
        # with Cache-Control: no-cache (deliberately NOT nginx-cached), so the
        # szi-tile-source library re-hits this endpoint once per HTTP Range
        # request — loading the full ORM row would drag the LargeBinary
        # macro/label blobs off disk on every one of those. id / is_published /
        # wsi_filename / wsi_generation are all this path needs.
        row = db.execute(
            select(Slide.id, Slide.is_published, Slide.wsi_filename, Slide.wsi_generation)
            .where(Slide.public_id == public_id)
        ).one_or_none()
        if (
            row is None
            or not row.wsi_filename
            or not row.wsi_filename.lower().endswith(".szi")
        ):
            raise HTTPException(status_code=404, detail="Slide not found")
        slide_id = row.id
        # Mirror the publish gate enforced on /api/slides/{id} and the DZI
        # tile path: a draft SZI must not be discoverable through this URL.
        if not row.is_published and not _is_admin_request(request):
            raise HTTPException(status_code=404, detail="Slide not found")
        # The archive is never edge-cached, but szi-tile-source reads it as many
        # Range requests against one central directory: a range served from a
        # replaced archive would splice the new zip's bytes into the old
        # directory's offsets. A versioned URL turns that into a clean 404.
        # (Read live per request here, so no stale-cache re-read is needed.)
        _check_generation(requested, tile_version(row.wsi_generation))
        try:
            # _slide_path only reads .wsi_filename; hand it a detached Slide so
            # the traversal guard stays the single source of truth rather than
            # being duplicated at this call site.
            path = _slide_path(Slide(wsi_filename=row.wsi_filename))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Slide not found")
        # Open once and serve that inode: a FileResponse given the path opens
        # it again at send time, and a replace swapping the file in between
        # would put the new archive's bytes under this generation's URL. A
        # single open + fstat also closes the delete race (a slide removed
        # in between is a 404 here, not an OSError -> 500 plus an error_logs
        # row an anonymous caller could loop on).
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            raise HTTPException(status_code=404, detail="Slide not found")
        try:
            st = os.fstat(fd)
            # Re-read the row now that the inode is pinned. A replace moves
            # the slide off its generation (and commits that) before it swaps
            # the file, so a file swapped before the open above shows up here
            # as a moved generation; one swapped after it no longer reaches
            # this response. That leaves the interim generation the replace
            # moved to: under it the path holds the old archive, then the new
            # one, while the row still describes the old one until the final
            # commit. Serving only the file the row's size describes keeps
            # range reads under that generation from mixing the two archives.
            # (0 = size not recorded: nothing to compare against.)
            db.rollback()
            current = db.execute(
                select(Slide.wsi_generation, Slide.file_size_bytes).where(Slide.id == slide_id)
            ).one_or_none()
            if current is None:
                raise HTTPException(status_code=404, detail="Slide not found", headers=_NOT_EDGE_CACHEABLE)
            _check_generation(requested, tile_version(current.wsi_generation))
            if current.file_size_bytes and current.file_size_bytes != st.st_size:
                raise HTTPException(status_code=404, detail="Slide not found", headers=_NOT_EDGE_CACHEABLE)
            etag = _file_etag(slide_id, st)
            # The szi-tile-source library reads the archive piecewise via
            # Range. Returning 304 to a Range-conditional request would hand
            # back an empty body when the client asked for partial content;
            # only short-circuit when no Range was requested.
            if (
                request.headers.get("if-none-match") == etag
                and not request.headers.get("range")
            ):
                os.close(fd)
                return Response(status_code=304, headers={"ETag": etag})
            # Cache-Control parity with the rest of the /dzi/ family: rely on
            # ETag revalidation rather than browser heuristic freshness so a
            # replaced archive shows up immediately instead of waiting out an
            # opaque Last-Modified-derived TTL.
            response = _OpenFileResponse(
                fd,
                st,
                media_type="application/zip",
                headers={
                    "ETag": etag,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                },
            )
        except BaseException:
            os.close(fd)
            raise
        return response
    finally:
        db.close()


# Tile coordinates in canonical decimal only. Typed as ``int``, FastAPI would
# accept "010", "+1", "1_0", "1.0", " 1", ... — each one the same tile to
# the generator but a distinct nginx cache key ($uri), so every spelling of a
# hot tile would be a fresh render and a fresh 30-day cache entry. Anything
# else is a 404, answered before the slide lookup. Nine digits is far past any
# real pyramid's level / column / row count.
_CANONICAL_UINT = re.compile(r"0|[1-9][0-9]{0,8}")


# A corrupt tile is requested again on every pan/zoom by every viewer, and each
# failure would otherwise log a ~16 KB traceback at ERROR (mirrored into the
# error_logs table, which has a row cap). The full traceback is kept once per
# tile per window; repeats are a one-line DEBUG record.
_TILE_ERR_LOG_INTERVAL_S = 60.0
_TILE_ERR_LOG_MAX_KEYS = 1024
_tile_err_last: dict[tuple, float] = {}
_tile_err_lock = threading.Lock()


def _first_in_window(key: tuple) -> bool:
    """True when ``key`` has not been logged in full within the window."""
    now = time.monotonic()
    with _tile_err_lock:
        last = _tile_err_last.get(key)
        first = last is None or now - last >= _TILE_ERR_LOG_INTERVAL_S
        if first:
            if len(_tile_err_last) >= _TILE_ERR_LOG_MAX_KEYS:
                # Bounded: drop what has already expired, then the oldest.
                for k in [k for k, t in _tile_err_last.items()
                          if now - t >= _TILE_ERR_LOG_INTERVAL_S]:
                    del _tile_err_last[k]
                while len(_tile_err_last) >= _TILE_ERR_LOG_MAX_KEYS:
                    del _tile_err_last[next(iter(_tile_err_last))]
            _tile_err_last[key] = now
    return first


def _log_open_failure(slide_id: int, exc: BaseException) -> None:
    """Log a slide that would not open, at most once per slide per window.

    Nothing caches the failure, so every descriptor and tile request for the
    slide (hundreds per viewport, from anyone when it is published) retries
    the open and fails again; logging each one in full would flood journald
    and the error_logs row cap. Call from inside the ``except`` block.
    """
    if _first_in_window(("open", slide_id)):
        logger.exception("Failed to open DZI generator for slide %s", slide_id)
    else:
        logger.debug("Failed again to open DZI generator for slide %s: %s", slide_id, exc)


def _log_tile_failure(slide_id: int, level: int, col: int, row: int, exc: BaseException) -> None:
    first = _first_in_window((slide_id, level, col, row))
    if first:
        logger.exception(
            "Failed to read tile slide=%s level=%s col=%s row=%s",
            slide_id, level, col, row,
        )
    else:
        logger.debug(
            "Failed again to read tile slide=%s level=%s col=%s row=%s: %s",
            slide_id, level, col, row, exc,
        )


@router.get("/dzi/{public_id}_files/{level}/{col}_{row}.jpeg")
def get_tile(public_id: _PublicIdPath, level: str, col: str, row: str, request: Request):
    if not all(_CANONICAL_UINT.fullmatch(v) for v in (level, col, row)):
        raise HTTPException(status_code=404, detail="Tile not found")
    level, col, row = int(level), int(col), int(row)
    # Same draft-gate contract as get_dzi: the publish flag is read live so
    # cross-worker publish toggles aren't masked by another worker's stale
    # cache entry (which would otherwise 404 a subset of tiles and produce
    # the "patchy" pyramid users see after a fresh publish). The generation
    # comes from the same cached row, so the ``?v=`` check adds no query.
    requested = _requested_generation(request)
    slide_id, published, generation = _resolve_for_request(public_id, requested)
    if slide_id is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    # Order matters: `_is_admin_request` opens a SessionLocal and runs a users
    # PK read (staff_token_valid), so evaluate the cheap `published` flag first.
    # A published slide short-circuits here and never pays the DB round-trip —
    # important because a staff user panning a published slide sends a Bearer
    # token on every one of the hundreds of tile/Range requests, and the draft
    # gate only actually needs the token check when the slide is NOT published.
    if not published and not _is_admin_request(request):
        raise HTTPException(status_code=404, detail="Slide not found")
    # A tile URL of another generation must not be answered with this one's
    # bytes: nginx would store them under that generation's key for 30 days.
    _check_generation(requested, generation)
    try:
        entry = _get_dz_serving(slide_id, requested)
    except HTTPException:
        raise
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Slide not found")
    except Exception as exc:
        _log_open_failure(slide_id, exc)
        raise HTTPException(status_code=500, detail="Failed to open slide")
    try:
        etag = entry.etag
        headers = _versioned_headers({**_CACHE_HEADERS, "ETag": etag}, requested)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        try:
            tile = entry.dz.get_tile(level, (col, row))
        except ValueError:
            # OpenSlide raises ValueError for out-of-bounds tile indices, which
            # maps cleanly to "tile not found" — the requested coordinate
            # genuinely does not exist in this pyramid.
            raise HTTPException(status_code=404, detail="Tile not found")
        except BoundedRenderBusy:
            # Transient: see _BOUNDED_MAX_QUEUED. Not a fault of the slide, so
            # neither logged as a tile failure nor allowed to retire the handle.
            raise HTTPException(
                status_code=503, detail="Tile render busy", headers={"Retry-After": "2"},
            )
        except Exception as exc:
            _log_tile_failure(slide_id, level, col, row, exc)
            if isinstance(exc, OpenSlideError) and isinstance(entry.osr, OpenSlide):
                # An OpenSlide handle is sticky-failed after its first read
                # error: every later call on it raises too. Keeping it cached
                # would turn one corrupt tile into a 500 on every tile of the
                # slide, for everyone this worker serves, until LRU eviction
                # or restart. Retire it so the next request reopens a clean
                # handle (only the genuinely bad region keeps failing). The
                # OME-TIFF reader raises the same exception for a bad segment
                # but does not latch, so its handle stays cached rather than
                # being reopened on every request for the broken tile.
                # The flip side, accepted: every request for that one broken
                # tile retires the handle again, so the next request on this
                # worker (for any tile of the slide) pays a full reopen. It is
                # bounded by how often the bad tile is asked for, and a fresh
                # handle is the only way to keep the slide's good tiles
                # serving.
                _retire_cached(slide_id, entry)
            raise HTTPException(status_code=500, detail="Failed to read tile")
        buf = io.BytesIO()
        # convert() can raise on a corrupt tile; close the source image in a
        # finally so its decoder buffer is released on that path too rather
        # than waiting on the GC (a repeatedly-failing tile would otherwise
        # accumulate open images on a memory-constrained server).
        try:
            rgb = tile.convert("RGB")
        finally:
            tile.close()
        try:
            rgb.save(buf, "JPEG", quality=_JPEG_QUALITY)
        finally:
            rgb.close()
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers=headers)
    finally:
        _release(entry)
