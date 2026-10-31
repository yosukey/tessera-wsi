"""Per-slide export / import for cross-tessera migration.

This module moves *specific slides* (with their tours, steps, pins, shapes,
attributes and tour assets) between separate tessera installs. It is distinct
from :mod:`wsi_app.backup`, which snapshots and *replaces* the whole DB via a
service restart. Here:

  * **Export** bundles one or more selected slides into a portable
    ``.tar.zst`` / ``.tar.gz`` archive: ``manifest.json`` + ``slides.json``
    (a JSON representation of the rows, not raw SQLite) + the raw WSI file for
    each slide + each tour's on-disk assets. Thumbnails and the macro/label
    associated images are deliberately NOT shipped — they are regenerated from
    the WSI file on import, keeping the bundle small.
  * **Import** is purely ADDITIVE: every bundled slide becomes a NEW row (fresh
    internal id, ``public_id`` preserved when free / reminted on collision),
    merged into the current DB without touching existing data and without a
    restart. A partially successful apply keeps the extracted bundle (the
    "stage") so failed slides and refused attachments can be retried without
    re-uploading it; per-slide outcomes recorded in the stage make re-running
    apply idempotent and crash-safe (see "Import: apply state").

The heavy lifting — archive framing, path-traversal-safe extraction, streamed
sha256, atomic publish — is reused from :mod:`wsi_app.backup` rather than
re-implemented.
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import tarfile
import uuid
import zlib
from urllib.parse import unquote
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import Session, selectinload

from . import backup as _backup
from . import disk_space as _disk_space
from ._serializers import server_local_iso, slide_attribute_map, slide_credit_fields, step_to_out, tour_attribute_map
from .admin_helpers import (
    ATTR_KEY_MAX_LEN,
    _same_filesystem,
    attach_associated_images,
    mint_public_id,
    reserve_next_slide_id,
    safe_filename,
    slide_mutation_lock,
    storable_asset_name,
    thumb_filename,
    tour_asset_dir,
    tour_asset_lock,
    unsupported_extension_message,
    valid_public_id,
    validate_attribute_kv,
    validate_slide_metadata,
    validated_extension,
)
from .config import settings
from .db import SessionLocal
from .migrations import CURRENT_SCHEMA_VERSION
from .models import Slide, SlideAttribute, Step, StepPin, StepShape, Tour, TourAttribute, fresh_wsi_generation
from .ome_tiff import UnsupportedSlideError
from .openslide_utils import make_thumbnail, read_slide_properties
from .schemas import (
    DEFAULT_TRANSITION_MODE, PinItem, Shape, StepOut, StepUpdateIn, TRANSITION_MODES,
    _MAX_STEP_FREEHAND_POINTS, _MAX_STEP_PINS, _MAX_STEP_SHAPES, _MAX_TOUR_FREEHAND_POINTS,
    _MAX_TOUR_STEPS, freehand_point_count,
)
from .slide_credit import bundle_credit
from .version import APP_VERSION

_logger = logging.getLogger("wsi_app.slide_transfer")

# Marks a slide-export archive so the importer can tell it apart from a
# whole-DB backup (which carries a ``wsi.db`` and a different manifest) and
# refuse the wrong kind of file with a clear message. Bump the trailing
# integer if the on-disk layout ever changes incompatibly.
BUNDLE_FORMAT = "tessera-slide-export/1"
MANIFEST_NAME = "manifest.json"
SLIDES_DOC_NAME = "slides.json"

# Per-slide outcome of the applies run against one stage, kept next to the
# extracted bundle (see "Import: apply state" below). Written only by this
# module; a bundle that ships a file of this name is refused at staging, since
# the extracted archive and this bookkeeping share one directory.
APPLY_STATE_NAME = "apply-state.json"
_APPLY_STATE_FORMAT = "tessera-import-apply-state/1"

# stage/bundle ids are ``import-``/``export-`` + 12 lowercase hex chars. Pinning
# the shape keeps a URL-supplied id from naming arbitrary paths under
# ``backup_dir/.staging`` via "../" segments (mirrors admin_backup's
# _STAGE_ID_PATTERN).
_EXPORT_ID_RE = re.compile(r"^export-[0-9a-f]{12}\Z")
_IMPORT_ID_RE = re.compile(r"^import-[0-9a-f]{12}\Z")

# Caps for a hand-crafted bundle that bypasses the export-side EXPORT_MAX_SLIDES:
# bound the slides.json loaded into memory and the number of slides iterated
# (mirrors the export cap).
IMPORT_MAX_SLIDES = 500
# slides.json is parsed whole, and Python's json inflates it: a few megabytes of
# nested empty lists (``[[],[],...]``, three bytes per container) or of short
# strings (``["ab","ab",...]``, five bytes per element) cost 15-25x their size
# in objects, so the byte cap alone does not bound memory. Every element costs
# an object whatever it is, so the element count (every ``[``, ``{`` and ``,``
# of the text, strings' contents included: an over-count, never an under-count)
# is what bounds it; at the cap that is about a gigabyte. The byte cap stays
# generous: the app's own export writes ``indent=2`` JSON,
# ~137 bytes per freehand vertex, so a course with tours at the per-tour vertex
# budget (300,000) runs to tens of megabytes, and a bundle that already
# exported must still import. A real 256 MiB bundle holds about 6 million
# elements.
_MAX_METADATA_JSON_BYTES = 256 * 1024 * 1024
_MAX_JSON_ELEMENTS = 12_000_000
# The manifest is a handful of scalars plus ``slide_public_ids`` (at most
# IMPORT_MAX_SLIDES ids of <= 32 characters, ~20 KiB as written) and a note of
# <= 500 characters.
_MAX_MANIFEST_JSON_BYTES = 64 * 1024

# Exceptions that mean "the uploaded file isn't a readable archive" (a
# truncated or garbage tar, a bad zstd frame or checksum) rather than a server
# bug. stage_import re-raises them as a SlideTransferError (importCorrupt) so
# the job reports a coded failure instead of an opaque server error; a
# manifest/slides.json that does not parse is caught before them, as
# importInvalid (see _load_stage_json). zstd's error type is added only when
# the optional dependency is installed.
# zlib.error is what GzipFile raises for an undecodable deflate block (see
# backup._CORRUPT_ARCHIVE_ERRORS).
_CORRUPT_BUNDLE_ERRORS: tuple = (
    tarfile.TarError, OSError, EOFError, ValueError, TypeError, RecursionError,
    zlib.error,
)
if _backup._HAS_ZSTD:
    _CORRUPT_BUNDLE_ERRORS = _CORRUPT_BUNDLE_ERRORS + (_backup._zstd.ZstdError,)


def _reject_json_constant(name: str):
    raise ValueError(f"non-finite JSON number {name}")


def _strict_json_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text}")
    return value


def _load_bundle_json(path: str, max_bytes: int):
    """Read the bundle's own manifest.json / slides.json, refusing one larger
    than ``max_bytes`` (a crafted bundle can otherwise ship a multi-GB file
    under the far larger extract cap) with ``importInvalid``.

    Python's json accepts ``Infinity`` / ``NaN`` (and ``1e999``), but the app
    answers with strict JSON (allow_nan=False), and bundle values reach the
    preflight and the stored rows. Refuse them at the door; callers treat
    ValueError (and RecursionError, for nesting deeper than the parser
    recurses) as "not a readable bundle"."""
    if os.path.getsize(path) > max_bytes:
        raise _invalid_bundle(f"{os.path.basename(path)} is too large")
    with open(path, "r", encoding="utf-8") as fp:
        text = fp.read(max_bytes + 1)
    if len(text) > max_bytes:  # grew since the stat
        raise _invalid_bundle(f"{os.path.basename(path)} is too large")
    # Before the parser allocates a single object (str.count runs at C speed).
    if text.count("[") + text.count("{") + text.count(",") > _MAX_JSON_ELEMENTS:
        raise _invalid_bundle(f"{os.path.basename(path)} is too deeply structured")
    return json.loads(text, parse_constant=_reject_json_constant, parse_float=_strict_json_float)


def _validate_slides_doc(slides_doc: object) -> list:
    """Validate a bundle's parsed slides.json and return the slides list.

    The bundle is UNTRUSTED input. This guards structure, the slide-count cap,
    and — critically — that every slide/tour ``public_id`` is a well-formed
    token, because those values flow into filesystem paths (``wsi/<id>/``,
    ``assets/tours/<id>/``) and are stored on rows at apply time. Called in
    both stage_import and apply_import (apply re-reads slides.json from disk,
    a TOCTOU surface, so it must re-check rather than trust staging).
    """
    if not isinstance(slides_doc, dict) or not isinstance(slides_doc.get("slides"), list):
        raise _invalid_bundle("malformed slides.json")
    slides = slides_doc["slides"]
    if len(slides) > IMPORT_MAX_SLIDES:
        raise _invalid_bundle(f"too many slides (max {IMPORT_MAX_SLIDES})")
    # Tour public_ids must be unique across the whole batch. _build_tour_remap
    # keys its old->new map by the bundle public_id, so two tours sharing one
    # id collapse to a single remap entry and both resolve to the same minted
    # id — the second INSERT then trips the tours.public_id UNIQUE constraint
    # and fails that slide with an opaque error. A genuine export can't produce
    # duplicates; a hand-crafted bundle can. Reject up front with a clear code.
    #
    # Slide public_ids likewise: the apply state (apply-state.json) keeps one
    # record per bundle slide id, so two slides sharing one would overwrite
    # each other's record — a retry would then re-import the one whose record
    # was lost, again on every retry — and both would hard-link the same
    # staged WSI, leaving two live slides on one inode (which exports and
    # full backups then miscount).
    seen_tour_pids: set[str] = set()
    seen_slide_pids: set[str] = set()
    for sd in slides:
        if not isinstance(sd, dict) or not valid_public_id(sd.get("public_id")):
            raise _invalid_bundle("malformed or unsafe slide public_id")
        if sd["public_id"] in seen_slide_pids:
            raise _invalid_bundle("duplicate slide public_id")
        seen_slide_pids.add(sd["public_id"])
        # These flow into safe_filename / `in` set lookups / slicing in the
        # preflight and apply: a list or number there is a 500 (and an
        # import-* stage left behind) rather than a refused bundle.
        for field_name in ("title", "catalog_id", "wsi_original_basename"):
            val = sd.get(field_name)
            if val is not None and not isinstance(val, str):
                raise _invalid_bundle(f"non-string slide {field_name}")
        for field_name in ("is_published", "unlisted"):
            val = sd.get(field_name)
            if val is not None and not isinstance(val, bool):
                raise _invalid_bundle(f"non-boolean slide {field_name}")
        tours = sd.get("tours", []) or []
        # ``tours`` is untrusted: a crafted bundle can set it to a scalar, which
        # would TypeError the loop below. Require a list before iterating.
        if not isinstance(tours, list):
            raise _invalid_bundle("malformed tours list")
        for t in tours:
            if not isinstance(t, dict) or not valid_public_id(t.get("public_id")):
                raise _invalid_bundle("malformed or unsafe tour public_id")
            tour_pid = t["public_id"]
            if tour_pid in seen_tour_pids:
                raise _invalid_bundle("duplicate tour public_id")
            seen_tour_pids.add(tour_pid)
            # Same reasoning as the slide fields above: a list/dict title or a
            # "false" string for a flag is otherwise only found at apply time,
            # as an opaque per-slide failure (or silently read as True).
            for field_name in ("title", "description"):
                val = t.get(field_name)
                if val is not None and not isinstance(val, str):
                    raise _invalid_bundle(f"non-string tour {field_name}")
            for field_name in ("is_published", "unlisted"):
                val = t.get(field_name)
                if val is not None and not isinstance(val, bool):
                    raise _invalid_bundle(f"non-boolean tour {field_name}")
    return slides


# The keys this build reads (or knowingly ignores) in a bundled slide, tour
# and step: what _serialize_slide / _serialize_tour / step_to_out write (a
# step's ``revision`` included, being a StepOut field). Any other key comes
# from a release that added a field this one does not know; the import cannot
# carry it over, and says so (importUnknownField) rather than dropping it
# silently. Bundle compatibility
# follows schema_version (see _import_version_block), so a new bundle field
# should come with a schema bump — this is the backstop for one that did not.
_KNOWN_SLIDE_KEYS = frozenset({
    "public_id", "catalog_id", "title", "wsi_original_basename",
    "width", "height", "mpp_x_file", "mpp_y_file", "objective_power_file",
    "vendor", "file_size_bytes", "acquired_at",
    "credit_text", "license_id", "source_url",
    "mpp_x", "mpp_y",
    "image_adjust_brightness", "image_adjust_contrast", "image_adjust_saturation",
    "show_zoom_indicator", "show_scale_bar", "show_metadata_panel",
    "is_published", "unlisted", "source_created_at", "attributes", "tours",
})
_KNOWN_TOUR_KEYS = frozenset({
    "public_id", "title", "description", "is_published", "unlisted", "attributes", "steps",
})
_KNOWN_STEP_KEYS = frozenset(StepOut.model_fields)
# Bounds on what the preflight / result report of unknown keys (untrusted).
_MAX_UNKNOWN_FIELDS = 20
_UNKNOWN_FIELD_MAX_CHARS = 64


def _unknown_bundle_fields(slides: list) -> list:
    """The distinct slide / tour / step keys of a validated slides list that
    this build does not know, as ``"key"`` / ``"tour.key"`` / ``"step.key"``
    in first-seen order (at most _MAX_UNKNOWN_FIELDS)."""
    found: dict = {}

    def note(prefix: str, obj: object, known: frozenset) -> None:
        if not isinstance(obj, dict):
            return
        for key in obj:
            if key not in known and len(found) < _MAX_UNKNOWN_FIELDS:
                found.setdefault(f"{prefix}{key}"[:_UNKNOWN_FIELD_MAX_CHARS], None)

    for sd in slides:
        note("", sd, _KNOWN_SLIDE_KEYS)
        for t in sd.get("tours") or []:
            note("tour.", t, _KNOWN_TOUR_KEYS)
            steps = t.get("steps")
            for st in steps if isinstance(steps, list) else []:
                note("step.", st, _KNOWN_STEP_KEYS)
    return list(found)


_PIN_LIST_ADAPTER = TypeAdapter(List[PinItem])
_SHAPE_LIST_ADAPTER = TypeAdapter(List[Shape])


class SlideTransferError(RuntimeError):
    """Raised for user-facing export/import failures (bad bundle, version
    mismatch, missing WSI, ...). Carries an optional stable ``code`` so the
    router can translate it to a localised message; ``str(exc)`` is the
    developer-facing fallback."""

    def __init__(self, message: str, *, code: Optional[str] = None, params: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        # Interpolation values for the localised message (e.g. which tour /
        # step of the bundle was refused); JSON-serialisable scalars only.
        self.params = params or {}


def _invalid_bundle(detail: str) -> SlideTransferError:
    """A bundle whose manifest.json / slides.json parsed but does not hold
    what an export writes (a malformed or unsafe id, a field of the wrong
    type, too many slides, ...): ``importInvalid`` with the ``detail``.

    Kept apart from ``importNotABundle`` (no slide-export manifest at all),
    ``importCorrupt`` (an archive that cannot be read) and ``importLayout`` /
    ``importUnsafe`` (entries the bundle may not hold): showing every one of
    them as "this is not a slide-export bundle, use the whole-database
    Restore" would steer an operator holding a damaged or hand-edited bundle
    towards the one tool that replaces their database."""
    return SlideTransferError(
        f"slide-export bundle is invalid: {detail}",
        code="importInvalid", params={"detail": detail},
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_no_space(exc: BaseException) -> bool:
    """A full (or over-quota) volume, as opposed to any other failure."""
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)


def _staging_root() -> str:
    return os.path.join(settings.backup_dir, ".staging")


def _wsi_dir() -> str:
    return os.path.join(settings.data_dir, "wsi")


def _thumbs_dir() -> str:
    return os.path.join(settings.data_dir, "thumbs")


def _slide_wsi_path(slide: Slide) -> str:
    """Resolve a slide's WSI path under a realpath/strict-descent guard.

    The DB row is mutable (a restored or hand-edited ``wsi_filename`` could carry
    a ``..`` traversal), and export feeds this path straight to ``tar.add``. A
    bare os.path.join would let a crafted row exfiltrate an arbitrary host file
    (e.g. the SQLite DB with its password hashes) into the downloadable bundle.
    Mirrors ``dzi._slide_path``; callers already handle a missing file as
    ``exportWsiMissing``.
    """
    base = os.path.realpath(_wsi_dir())
    full = os.path.realpath(os.path.join(base, slide.wsi_filename or ""))
    if not full.startswith(base + os.sep):
        raise SlideTransferError(
            f"slide {slide.public_id} WSI path is invalid; refusing to export",
            code="exportWsiMissing",
        )
    return full


def _original_basename(slide: Slide) -> str:
    """Recover a slide's original upload filename from its on-disk name.

    Files are stored as ``{id}_{safe_name}``; strip the id prefix so the
    bundle carries the operator-facing name (mirrors ``admin_download_slide``).
    """
    fname = slide.wsi_filename or ""
    prefix = f"{slide.id}_"
    return fname[len(prefix):] if fname.startswith(prefix) else fname


def _add_bytes_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add an in-memory bytes payload as a regular file entry, routed through
    the same ``_archive_filter`` every other member uses (mirrors the manifest
    write in ``backup.create_backup``)."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(_backup._utc_now().timestamp())
    filtered = _backup._archive_filter(info)
    if filtered is None:
        raise RuntimeError(f"archive filter rejected the {name} entry")
    tar.addfile(filtered, io.BytesIO(data))


# Shared with the full backup, which archives tour attachments the same way.
_open_pinned = _backup._open_pinned


def _tour_asset_files(tour_pid: str) -> list:
    """The entries of a tour's asset dir that export archives (see
    :func:`backup._tour_asset_files`)."""
    return _backup._tour_asset_files(tour_asset_dir(tour_pid))


def _exported_tour_pids(slide: Slide) -> list:
    """The public ids of the slide's tours whose attachments export archives.
    A malformed stored token (restored / hand-edited DB) never names a path
    here: export_slides' preflight refuses such a tour up front
    (exportInvalidTourPublicId), and this filter stays as the path-safety
    guard for every other caller."""
    return [t.public_id for t in slide.tours if valid_public_id(t.public_id)]


def _add_tour_assets(tar: tarfile.TarFile, tour_pid: str) -> int:
    """Archive a tour's attachments (:func:`_tour_asset_files`) as
    ``assets/tours/<pid>/<name>``, each read from a pinned fd, and return how
    many were archived. Best-effort: an attachment deleted (or swapped for a
    symlink) while the export runs is skipped rather than failing the bundle —
    the WSI is what matters."""
    return _backup._add_tour_asset_files(tar, tour_asset_dir(tour_pid), tour_pid, "export")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@dataclass
class ExportRecord:
    id: str
    path: str
    size_bytes: int
    sha256: str
    created_at: str
    archive_format: str
    slide_count: int
    tour_count: int
    note: str = ""
    # Lightweight per-slide summary (public_id + title + catalog_id) so the
    # history view can show *which* slides a bundle contains — enough to tell
    # two same-count exports apart — without cracking open the archive. Empty
    # for stub rows and for sidecars that carry no ``slides`` array.
    slides: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "export_id": self.id,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "archive_format": self.archive_format,
            "slide_count": self.slide_count,
            "tour_count": self.tour_count,
            "note": self.note,
            "slides": self.slides,
        }


def _serialize_tour(tour: Tour) -> dict:
    return {
        "public_id": tour.public_id or "",
        "title": tour.title or "",
        "description": tour.description or "",
        "is_published": bool(tour.is_published),
        "unlisted": bool(tour.unlisted),
        # The tour's own metadata; what it inherits travels on the slide.
        "attributes": tour_attribute_map(tour),
        # step_to_out already sorts pins/shapes by insertion order and decodes
        # freehand points; drop the internal ids the importer would ignore,
        # and the edit counter, which means nothing on another install (an
        # imported step starts again at revision 0).
        "steps": [
            {k: v for k, v in step_to_out(s).items() if k not in ("id", "tour_id", "revision")}
            for s in tour.steps
        ],
    }


def _finite_json(obj):
    """A copy of ``obj`` with every non-finite float replaced by ``None``.

    SQLite can hold inf/NaN (a hand-edited row), and Python's json would
    write them as ``Infinity`` / ``NaN`` - which is not JSON, and which the
    import side refuses (see :func:`_load_bundle_json`), leaving an export that
    could never be imported. ``None`` reads back as "no value" everywhere the
    importer looks (override fields, step geometry defaults)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_json(v) for v in obj]
    return obj


def _serialize_slide(slide: Slide) -> dict:
    return {
        "public_id": slide.public_id or "",
        "catalog_id": slide.catalog_id or "",
        "title": slide.title or "",
        # The WSI travels as wsi/<public_id>/<this name>.
        "wsi_original_basename": _original_basename(slide),
        # File-truth fields (informational; re-derived from the WSI on import).
        "width": slide.width,
        "height": slide.height,
        "mpp_x_file": slide.mpp_x_file,
        "mpp_y_file": slide.mpp_y_file,
        "objective_power_file": slide.objective_power_file,
        "vendor": slide.vendor or "",
        "file_size_bytes": slide.file_size_bytes,
        "acquired_at": slide.acquired_at or "",
        # Image credit (rights holder / license / source) travels with the image.
        **slide_credit_fields(slide),
        # Override-able fields that must round-trip (a manual MPP override must
        # survive; re-reading the file on import would silently discard it).
        "mpp_x": slide.mpp_x,
        "mpp_y": slide.mpp_y,
        "image_adjust_brightness": slide.image_adjust_brightness,
        "image_adjust_contrast": slide.image_adjust_contrast,
        "image_adjust_saturation": slide.image_adjust_saturation,
        "show_zoom_indicator": slide.show_zoom_indicator,
        "show_scale_bar": slide.show_scale_bar,
        "show_metadata_panel": slide.show_metadata_panel,
        # Visibility is preserved so a migration keeps the slide's publish state.
        "is_published": bool(slide.is_published),
        "unlisted": bool(slide.unlisted),
        "source_created_at": server_local_iso(slide.created_at),
        "attributes": slide_attribute_map(slide),
        "tours": [_serialize_tour(t) for t in slide.tours],
    }


def _load_slides_for_export(db: Session, slide_ids: List[int]) -> List[Slide]:
    rows = db.execute(
        select(Slide)
        .where(Slide.id.in_(slide_ids))
        .options(
            selectinload(Slide.attributes),
            selectinload(Slide.tours).selectinload(Tour.attributes),
            selectinload(Slide.tours)
            .selectinload(Tour.steps)
            .selectinload(Step.pins),
            selectinload(Slide.tours)
            .selectinload(Tour.steps)
            .selectinload(Step.shapes),
        )
    ).scalars().all()
    by_id = {s.id: s for s in rows}
    missing = [sid for sid in slide_ids if sid not in by_id]
    if missing:
        raise SlideTransferError(
            f"unknown slide id(s): {missing}", code="exportUnknownSlide"
        )
    # Preserve the caller's requested order, de-duplicated.
    seen: set[int] = set()
    ordered: List[Slide] = []
    for sid in slide_ids:
        if sid in seen:
            continue
        seen.add(sid)
        ordered.append(by_id[sid])
    return ordered


def export_slides(
    slide_ids: List[int],
    note: str = "",
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> ExportRecord:
    """Bundle the given slides (with tours + assets + WSI files) into an
    archive under ``backup_dir/.staging`` and return an :class:`ExportRecord`.

    Runs in a background thread; opens its own DB session.
    """
    if not slide_ids:
        raise SlideTransferError("no slides selected for export", code="exportNoSlides")

    _backup._ensure_backup_dirs()

    def emit(phase: str, written: int) -> None:
        if progress_cb is not None:
            progress_cb({"phase": phase, "bytes_written": written})

    db = SessionLocal()
    try:
        slides = _load_slides_for_export(db, slide_ids)

        # Preflight: every slide MUST have its WSI file on disk. A metadata-only
        # slide (row present, bytes gone) cannot be migrated — the target would
        # get an unopenable slide.
        #
        # And a well-formed public_id: it names the slide's directory in the
        # bundle (wsi/<public_id>/) and keys its records, and the import
        # refuses a bundle holding a malformed one as a whole (importInvalid).
        # A stored id can only be malformed through a restored or hand-edited
        # DB, but then the export would succeed and hand over a bundle no
        # system would import — refuse it here instead, naming the slide.
        for s in slides:
            if not valid_public_id(s.public_id):
                raise SlideTransferError(
                    f"slide {s.id} has a malformed public_id {s.public_id!r}; cannot export",
                    code="exportInvalidPublicId",
                    params={"slide": (s.title or "")[:200] or str(s.id)},
                )
            # The same holds for each of the slide's tours: _serialize_tour
            # writes its public_id into slides.json as is, and the importer
            # refuses the whole bundle over one malformed tour token
            # (importInvalid "malformed or unsafe tour public_id") — while
            # _exported_tour_pids merely skips that tour's attachments, so
            # without this check the export "succeeded" with a bundle no
            # system would take. Refuse it here, naming slide and tour, so
            # the admin can fix (or delete) that one tour.
            for t in s.tours:
                if not valid_public_id(t.public_id):
                    raise SlideTransferError(
                        f"slide {s.public_id} has a tour {t.id} with a malformed public_id "
                        f"{t.public_id!r}; cannot export",
                        code="exportInvalidTourPublicId",
                        params={
                            "slide": (s.title or "")[:200] or str(s.id),
                            "tour": (t.title or "")[:200] or str(t.id),
                        },
                    )
            if not s.wsi_filename or not os.path.isfile(_slide_wsi_path(s)):
                raise SlideTransferError(
                    f"slide {s.public_id} has no WSI file on disk; cannot export",
                    code="exportWsiMissing",
                )

        # Member-count preflight: the import refuses a bundle of more members
        # than _import_max_members, so refuse a selection whose bundle would
        # hold more before writing anything, rather than build one this
        # system could not import. The count is re-taken as the bundle is
        # written (attachments can be uploaded meanwhile).
        max_entries = _import_max_members()
        entries = _bundle_entries([
            [len(_tour_asset_files(pid)) for pid in _exported_tour_pids(s)]
            for s in slides
        ])
        if entries > max_entries:
            raise _export_too_many_entries(entries, max_entries)

        # Free-space preflight: refuse up front if the backup volume can't hold
        # the selected slides' WSI bytes, rather than streaming hundreds of GB
        # into ``.staging`` only to ENOSPC (which also starves the co-located
        # live snapshot / job records). Mirrors backup._preflight_backup_free_space.
        # WSI files dominate the bundle; the DB-recorded file_size_bytes is the
        # cheap authoritative source, with a live stat as fallback. The shared
        # reserve (disk_space.reserve_for: 10 %, at least 256 MiB) covers the
        # metadata doc + tour assets and leaves the volume — the data volume
        # by default — room for the running install. A bare 1.1x with no
        # floor would let a small export take a nearly-full volume down to its
        # last few MB when a backup of the same bytes was refused. Best-effort:
        # skip when free space can't be read or the volume reports no capacity
        # (the archive writer's own ENOSPC handling is the real safety net), or
        # when no size is known at all.
        free_bytes = _disk_space.free_bytes(settings.backup_dir)
        if free_bytes is not None:
            estimate = 0
            for s in slides:
                sz = s.file_size_bytes or 0
                if not sz:
                    try:
                        sz = os.path.getsize(_slide_wsi_path(s))
                    except OSError:
                        sz = 0
                estimate += sz
            short = (
                _disk_space.shortfall_given(estimate, free_bytes, settings.backup_dir)
                if estimate else None
            )
            if short is not None:
                raise SlideTransferError(
                    short.message("backup volume") + " (to build the export)",
                    code="exportNoSpace",
                )

        tour_count = sum(len(s.tours) for s in slides)
        created_at = _backup._utc_iso_z()
        archive_format = _backup._archive_extension()
        manifest = {
            "bundle_format": BUNDLE_FORMAT,
            "app_version": APP_VERSION,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": created_at,
            "archive_format": archive_format,
            "slide_count": len(slides),
            "tour_count": tour_count,
            "slide_public_ids": [s.public_id for s in slides],
            "note": note or "",
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

        export_id = "export-" + uuid.uuid4().hex[:12]
        staging_root = _staging_root()
        os.makedirs(staging_root, exist_ok=True)
        final_path = os.path.join(staging_root, f"{export_id}.{archive_format}")
        tmp_path = final_path + ".tmp"

        raw, tar, counter, compressor = _backup._open_archive_writer(tmp_path)
        write_failed = False
        try:
            _add_bytes_member(tar, MANIFEST_NAME, manifest_bytes)
            emit("writing_wsi", counter.written)
            # slides.json goes last: each slide is serialised under its lock,
            # in the same step that opens its WSI, so the metadata describes
            # the file that is actually in the bundle.
            slide_docs: list = []
            tour_file_counts: list = []
            for s in slides:
                # The per-slide mutation lock is held only while the row is
                # re-read and its file opened; the open fd pins that inode, so
                # a replace_wsi / delete that runs once the lock is released
                # can't change what is archived — and doesn't wait for a
                # multi-GB copy (mirrors backup's pinned snapshot files).
                with slide_mutation_lock(s.id):
                    # The rows were loaded before any lock was taken, possibly
                    # minutes ago in a long multi-slide export: another admin
                    # may since have replaced the file (new wsi_filename, new
                    # size/dimensions) or deleted the slide. Re-read it under
                    # the lock before resolving the path or serialising.
                    pid = s.public_id
                    try:
                        db.refresh(s)
                    except InvalidRequestError as exc:
                        raise SlideTransferError(
                            f"slide {pid} was deleted during the export",
                            code="exportUnknownSlide",
                        ) from exc
                    if not s.wsi_filename:
                        raise SlideTransferError(
                            f"slide {s.public_id} has no WSI file on disk; cannot export",
                            code="exportWsiMissing",
                        )
                    arcname = f"wsi/{s.public_id}/{_original_basename(s)}"
                    doc = _serialize_slide(s)
                    asset_pids = _exported_tour_pids(s)
                    try:
                        wsi_file = _open_pinned(_slide_wsi_path(s))
                    except OSError as exc:
                        # No bytes back the row (the state a metadata-scope
                        # restore leaves behind, or a delete that raced the
                        # window before this lock was taken), or a symlink was
                        # swapped in under the resolved path: a missing-WSI
                        # bundle error rather than an opaque OSError.
                        if not isinstance(exc, FileNotFoundError) and exc.errno != errno.ELOOP:
                            raise
                        raise SlideTransferError(
                            f"slide {s.public_id} WSI file is missing on disk",
                            code="exportWsiMissing",
                        ) from exc
                    if wsi_file is None:
                        raise SlideTransferError(
                            f"slide {s.public_id} WSI is not a regular file; refusing to export",
                            code="exportWsiMissing",
                        )
                slide_docs.append(doc)
                with wsi_file:
                    if not _backup._add_pinned_file(tar, wsi_file, arcname):
                        raise SlideTransferError(
                            f"slide {s.public_id} WSI is not a regular file; refusing to export",
                            code="exportWsiMissing",
                        )
                tour_file_counts.append([_add_tour_assets(tar, pid) for pid in asset_pids])
                entries = _bundle_entries(tour_file_counts)
                if entries > max_entries:
                    raise _export_too_many_entries(entries, max_entries)
                emit("writing_wsi", counter.written)
            _add_bytes_member(
                tar, SLIDES_DOC_NAME,
                json.dumps(
                    _finite_json({"slides": slide_docs}),
                    indent=2, default=str, allow_nan=False,
                ).encode("utf-8"),
            )
        except BaseException:
            write_failed = True
            raise
        finally:
            # Close tar -> compressor -> raw, collecting every failure so a
            # truncated archive is never published (mirrors create_backup).
            close_errors: list[tuple[str, BaseException]] = []
            for name, closer in (
                ("tar", tar.close),
                ("compressor", compressor.close),
                ("raw", raw.close),
            ):
                try:
                    closer()
                except BaseException as exc:  # noqa: BLE001
                    close_errors.append((name, exc))
            if close_errors or write_failed:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
            if close_errors:
                detail = ", ".join(f"{n}={exc!r}" for n, exc in close_errors)
                raise RuntimeError(f"export archive close failed: {detail}") from close_errors[0][1]

        # fsync then atomically publish; reclaim the .tmp on any failure.
        try:
            fd = os.open(tmp_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            emit("finalizing", os.path.getsize(tmp_path))
            sha = counter.sha256_hex
            os.replace(tmp_path, final_path)
            _backup._fsync_parent_dir(final_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        record = ExportRecord(
            id=export_id,
            path=final_path,
            size_bytes=os.path.getsize(final_path),
            sha256=sha,
            created_at=created_at,
            archive_format=archive_format,
            slide_count=len(slides),
            tour_count=tour_count,
            note=note or "",
            slides=[
                {
                    "public_id": s.public_id or "",
                    "title": s.title or "",
                    "catalog_id": s.catalog_id or "",
                }
                for s in slides
            ],
        )
        # Sidecar JSON so the export-history listing can show size / counts /
        # note without cracking open the archive, and so the metadata survives
        # the 7-day job-record prune (the job.result is the only other copy).
        # Atomic write mirrors create_backup's sidecar so a reader never sees a
        # half-written file. Best-effort: a sidecar write failure must not fail
        # the export itself — list_exports falls back to a stub row.
        try:
            _backup._atomic_write_json(_export_sidecar_path(final_path), record.to_dict())
        except OSError as exc:
            _logger.warning("failed to write export sidecar for %s: %s", export_id, exc)
        return record
    finally:
        db.close()


def _export_sidecar_path(archive_path: str) -> str:
    return archive_path + ".meta.json"


def _export_record_from_sidecar(export_id: str, path: str, archive_format: str) -> ExportRecord:
    """Build an ExportRecord for a bundle on disk, reading its sidecar for the
    real metadata. A missing/corrupt sidecar yields a stub (size from the file,
    empty sha / zero counts) so the operator can still see and delete the file —
    mirrors the fallback in backup.list_backups()."""
    data: dict = {}
    try:
        with open(_export_sidecar_path(path), "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    return ExportRecord(
        id=export_id,
        path=path,
        size_bytes=os.path.getsize(path),
        sha256=str(data.get("sha256", "") or ""),
        created_at=str(data.get("created_at", "") or ""),
        archive_format=str(data.get("archive_format", archive_format) or archive_format),
        slide_count=int(data.get("slide_count", 0) or 0),
        tour_count=int(data.get("tour_count", 0) or 0),
        note=str(data.get("note", "") or ""),
        slides=[s for s in (data.get("slides") or []) if isinstance(s, dict)],
    )


def find_export(export_id: str) -> Optional[ExportRecord]:
    """Locate a generated export bundle by id, or None. Path-traversal-guarded."""
    if not _EXPORT_ID_RE.match(export_id or ""):
        return None
    staging_root = _staging_root()
    for ext in ("tar.zst", "tar.gz"):
        path = os.path.join(staging_root, f"{export_id}.{ext}")
        if os.path.isfile(path):
            try:
                return _export_record_from_sidecar(export_id, path, ext)
            except OSError:
                # Bundle deleted between isfile() and the getsize() inside the
                # record builder — treat as not found rather than raising.
                return None
    return None


def list_exports() -> list[ExportRecord]:
    """List generated export bundles awaiting download under ``.staging/``.

    These are the same files the 24h ``sweep_stale_staging`` TTL reclaims; the
    history view lets an admin re-download or delete one early within that
    window. Newest first. A bundle whose sidecar is missing/corrupt still shows
    as a stub row so it can be deleted.
    """
    staging_root = _staging_root()
    if not os.path.isdir(staging_root):
        return []
    try:
        entries = os.listdir(staging_root)
    except OSError:
        return []
    out: list[ExportRecord] = []
    for name in entries:
        # Only the generated archives (not their ``.meta.json`` sidecars or the
        # ``.tmp`` a crashed export may leave behind).
        if not (name.endswith(".tar.zst") or name.endswith(".tar.gz")):
            continue
        export_id = name.rsplit(".tar.", 1)[0]
        if not _EXPORT_ID_RE.match(export_id):
            continue
        path = os.path.join(staging_root, name)
        if not os.path.isfile(path):
            continue
        ext = "tar.zst" if name.endswith(".tar.zst") else "tar.gz"
        try:
            out.append(_export_record_from_sidecar(export_id, path, ext))
        except OSError:
            # Bundle removed mid-listing (its getsize failed); skip the row
            # rather than 500 the whole history endpoint.
            continue
    # created_at is an ISO-8601 "…Z" string, so a lexical sort is chronological;
    # fall back keeps stub rows (empty created_at) last.
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out


def delete_export(export_id: str) -> bool:
    """Delete an export bundle (archive + sidecar) early. Returns False if no
    such bundle exists. Path-traversal-guarded via find_export's regex."""
    rec = find_export(export_id)
    if rec is None:
        return False
    for p in (rec.path, _export_sidecar_path(rec.path)):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return True


# ---------------------------------------------------------------------------
# Import: staging + preflight
# ---------------------------------------------------------------------------

def _stage_dir(stage_id: str) -> str:
    return os.path.join(_staging_root(), stage_id)


def _staged_wsi_file(stage_dir: str, slide_public_id: str) -> Optional[str]:
    """Return the single WSI file staged under ``wsi/<public_id>/`` or None.

    ``slide_public_id`` is bundle-controlled and flows into an ``os.path.join``,
    so an unvalidated "../" value would let a crafted bundle read/move an
    arbitrary host file. Reject anything that isn't a well-formed token.

    Staging refuses a slide directory holding more than one file
    (``importWsiExtraFile``, see _check_bundle_layout); one that holds several
    anyway (the stage edited on disk since) has no single WSI, and is treated
    as missing one rather than importing whichever file sorts first.
    """
    if not valid_public_id(slide_public_id):
        return None
    d = os.path.join(stage_dir, "wsi", slide_public_id)
    if not os.path.isdir(d):
        return None
    files = [p for p in (os.path.join(d, n) for n in os.listdir(d)) if os.path.isfile(p)]
    return files[0] if len(files) == 1 else None


def _import_max_members() -> int:
    """How many members (files and directories) a bundle may hold: the
    import refuses one with more while extracting it, and the export refuses
    a selection whose bundle would hold more (so whatever this system
    exports, it can import). Members are counted as :func:`_bundle_entries`
    counts them.

    The budget is that of IMPORT_MAX_SLIDES slides, each with its WSI and
    directory plus one tour directory holding a full tour's attachments:
    TOUR_ASSET_MAX_FILES, or its default of 200 when this system's cap is
    lower (attachments past the cap are kept in the stage for a retry, not
    refused). It is a total for the bundle, not a per-slide allowance: the
    same attachments may be spread over any number of tours and slides.
    That is about 100,000 by default — far above a typical bundle, where a
    tour uses a handful of images, and a tenth of the generic archive cap:
    tar headers are so small that, at that cap, a 5 MB upload of empty
    entries holds the import job slot for minutes and leaves a million
    inodes in staging for a day.
    """
    per_tour = settings.tour_asset_max_files
    if per_tour <= 0:  # the per-tour cap is disabled
        return _backup._MAX_ARCHIVE_MEMBERS
    per_slide = 3 + max(per_tour, 200)
    return min(_backup._MAX_ARCHIVE_MEMBERS, _BUNDLE_BASE_ENTRIES + IMPORT_MAX_SLIDES * per_slide)


# A bundle's members besides those of its slides: manifest.json, slides.json
# and the wsi/, assets/ and assets/tours/ directories.
_BUNDLE_BASE_ENTRIES = 5


def _bundle_entries(tour_file_counts: list) -> int:
    """The members of a bundle whose slides' tours hold ``tour_file_counts``
    attachments (one list of per-tour counts per slide): manifest.json,
    slides.json, and per slide its WSI and each tour's attachments. Export
    writes no directory entries, but a bundle re-packed with tar has one per
    directory — wsi/, assets/, assets/tours/, one per slide and one per tour
    with attachments — and those are counted too, so that a re-packed bundle
    imports as well."""
    return _BUNDLE_BASE_ENTRIES + sum(
        2 + sum(n + 1 for n in tours if n) for tours in tour_file_counts
    )


def _export_too_many_entries(entries: int, limit: int) -> SlideTransferError:
    return SlideTransferError(
        f"the selected slides make a bundle of {entries} files and directories, "
        f"more than the {limit} an import accepts; export them in smaller batches",
        code="exportTooManyFiles",
        params={"count": entries, "limit": limit},
    )


def _check_bundle_layout(stage_dir: str, slides: list) -> None:
    """Refuse an extracted bundle holding anything outside the layout export
    writes: ``manifest.json``, ``slides.json``, ``wsi/<public_id>/<file>`` for
    the slides of ``slides`` (slides.json, already validated) and
    ``assets/tours/<public_id>/<file>`` for their tours. Anything else would
    only sit in the stage, using disk space for up to a day, and the stage
    directory also holds this module's own bookkeeping (APPLY_STATE_NAME,
    which names slide rows, files and tour directories a retry deletes or
    writes to): one arriving inside the archive is a forgery, so a stray
    top-level entry is refused rather than ignored."""
    slide_pids = {sd["public_id"] for sd in slides}
    tour_pids = {t["public_id"] for sd in slides for t in (sd.get("tours") or [])}

    def refuse(entry: str):
        raise SlideTransferError(
            f"slide-export bundle has an entry outside the bundle layout: {entry}",
            code="importLayout", params={"entry": entry[:300]},
        )

    def check_id_dirs(parent: str, label: str, bundled: set, *, single_file: bool = False) -> None:
        # <parent>/<public_id>/<file>: one level of dirs named for the
        # bundle's own slides / tours, holding regular files only.
        with os.scandir(parent) as it:
            for d in it:
                if not (d.is_dir(follow_symlinks=False) and d.name in bundled):
                    refuse(f"{label}/{d.name}")
                with os.scandir(d.path) as files:
                    names = []
                    for f in files:
                        if not f.is_file(follow_symlinks=False):
                            refuse(f"{label}/{d.name}/{f.name}")
                        names.append(f.name)
                # Export writes exactly one file per slide, its WSI. A second
                # one — typically the ``._<name>`` AppleDouble file macOS adds
                # when the bundle is unpacked and re-packed there — would leave
                # the import taking whichever sorted first as the slide's image
                # (``._slide.svs`` before ``slide.svs``), failing it as
                # unreadable at best. Refuse the bundle, naming the extra
                # entry, instead of guessing which file is meant. (The file
                # name itself is not compared with ``wsi_original_basename``: a
                # re-pack may change its Unicode normalisation, and the slide's
                # single file is unambiguous.)
                if single_file and len(names) > 1:
                    names.sort()
                    extra = next((n for n in names if n.startswith("._")), names[-1])
                    entry = f"{label}/{d.name}/{extra}"
                    raise SlideTransferError(
                        f"slide-export bundle holds more than one file for slide "
                        f"{d.name}: {entry}",
                        code="importWsiExtraFile",
                        params={"slide": d.name, "entry": entry[:300]},
                    )

    with os.scandir(stage_dir) as it:
        for e in it:
            if e.name in (MANIFEST_NAME, SLIDES_DOC_NAME) and e.is_file(follow_symlinks=False):
                continue
            if e.name == "wsi" and e.is_dir(follow_symlinks=False):
                check_id_dirs(e.path, "wsi", slide_pids, single_file=True)
                continue
            if e.name == "assets" and e.is_dir(follow_symlinks=False):
                with os.scandir(e.path) as sub:
                    for a in sub:
                        if a.name != "tours" or not a.is_dir(follow_symlinks=False):
                            refuse(f"assets/{a.name}")
                        check_id_dirs(a.path, "assets/tours", tour_pids)
                continue
            refuse(e.name)


def _load_stage_json(path: str, max_bytes: int):
    """:func:`_load_bundle_json` for staging: a manifest.json / slides.json
    that is not JSON (or not UTF-8, or nested past the parser's recursion
    limit) is an invalid bundle, reported with what the parser said — not a
    corrupt archive, which the tar / codec errors are."""
    try:
        return _load_bundle_json(path, max_bytes)
    except (ValueError, RecursionError) as exc:
        detail = f"{os.path.basename(path)} is not valid JSON"
        if isinstance(exc, ValueError):
            detail = f"{detail}: {str(exc)[:200]}"
        raise _invalid_bundle(detail) from exc


def _looks_like_system_backup(stage_dir: str, manifest: object) -> bool:
    """Whether an extracted archive without a slide-export manifest is a
    whole-system backup (backup.create_backup's archive: a ``wsi.db`` and a
    manifest stating its ``scope``)."""
    if os.path.isfile(os.path.join(stage_dir, "wsi.db")):
        return True
    return (
        isinstance(manifest, dict) and "bundle_format" not in manifest
        and "scope" in manifest and "includes_analytics" in manifest
    )


def stage_import(archive_path: str) -> tuple[str, dict, dict]:
    """Extract a slide-export archive into a fresh staging dir.

    Returns ``(stage_id, manifest, slides_doc)``. Raises
    :class:`SlideTransferError` for a wrong/corrupt bundle.
    """
    _backup._ensure_backup_dirs()
    try:
        _backup.sweep_stale_staging()
    except Exception:
        _logger.exception("sweep_stale_staging failed; continuing")

    stage_id = "import-" + uuid.uuid4().hex[:12]
    stage_dir = _stage_dir(stage_id)
    os.makedirs(stage_dir, exist_ok=False)

    # Cheap free-space preflight (mirrors backup.stage_restore, including the
    # shared reserve the extraction keeps — see the comment there). It
    # compares against the *compressed* archive size, so it only catches the
    # obvious case; the extraction checks its running total against the same
    # free-space figure, and a disk that fills anyway is caught as ENOSPC
    # below.
    try:
        archive_size = os.path.getsize(archive_path)
    except OSError:
        archive_size = 0
    stage_parent = os.path.dirname(stage_dir)
    free_bytes = _disk_space.free_bytes(stage_parent)
    short = (
        _disk_space.shortfall_given(archive_size, free_bytes, stage_parent)
        if archive_size else None
    )
    if short is not None:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise SlideTransferError(
            f"insufficient free space to import: {short.message('staging volume')}",
            code="importNoSpace",
        )

    max_members = _import_max_members()
    try:
        with _backup._open_archive_reader(archive_path) as tar:
            try:
                _backup._safe_extract(
                    tar, stage_dir,
                    max_total_bytes=settings.backup_max_extract_bytes,
                    free_bytes=free_bytes,
                    max_members=max_members,
                )
            except RuntimeError as exc:
                # _safe_extract refuses unsafe members (links, "..", duplicate
                # names), an over-cap expansion, too many members and a
                # too-small staging volume with RuntimeError. All are the
                # uploader's or the disk's problem, not a server bug: report
                # them with their codes.
                if "insufficient free space" in str(exc):
                    raise SlideTransferError(
                        f"insufficient free space to import: {exc}",
                        code="importNoSpace",
                    ) from exc
                if str(exc).startswith(f"archive has more than {max_members} entries"):
                    raise SlideTransferError(
                        f"slide-export bundle has more than {max_members} files and "
                        "directories, the most an import accepts",
                        code="importTooManyFiles",
                        params={"limit": max_members},
                    ) from exc
                # The rest — a link, a "..", absolute or duplicate name, a
                # device, an expansion past BACKUP_MAX_EXTRACT_BYTES — is an
                # entry no export writes: refused with what was found.
                raise SlideTransferError(
                    f"archive holds an entry an import refuses: {exc}",
                    code="importUnsafe", params={"detail": str(exc)[:300]},
                ) from exc
        # An archive keeps the modes its files had when it was made (0644 /
        # 0755 from a hand-made or dev one), extraction preserves them, and an
        # imported WSI is a hard link to the staged file - so it would go
        # live world-readable. Restore staging normalises the same way.
        _backup._normalize_stage_modes(stage_dir)
        manifest_path = os.path.join(stage_dir, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            raise SlideTransferError(
                "archive is not a Tessera slide-export bundle (no manifest.json)",
                code="importNotABundle",
            )
        manifest = _load_stage_json(manifest_path, _MAX_MANIFEST_JSON_BYTES)
        if not isinstance(manifest, dict) or manifest.get("bundle_format") != BUNDLE_FORMAT:
            # Only a whole-system backup is worth pointing at Restore — and
            # then with its own message, since restoring one replaces the
            # whole database. Anything else is simply not a bundle.
            if _looks_like_system_backup(stage_dir, manifest):
                raise SlideTransferError(
                    "archive is a whole-system backup, not a slide-export bundle "
                    "(use the whole-DB Restore for it)",
                    code="importIsSystemBackup",
                )
            raise SlideTransferError(
                "archive is not a Tessera slide-export bundle",
                code="importNotABundle",
            )
        slides_path = os.path.join(stage_dir, SLIDES_DOC_NAME)
        if not os.path.isfile(slides_path):
            raise _invalid_bundle(f"{SLIDES_DOC_NAME} is missing")
        slides_doc = _load_stage_json(slides_path, _MAX_METADATA_JSON_BYTES)
        # Structure, count cap, and public_id safety (traversal guard).
        _validate_slides_doc(slides_doc)
        _check_bundle_layout(stage_dir, slides_doc["slides"])
        return stage_id, manifest, slides_doc
    except SlideTransferError:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    except _CORRUPT_BUNDLE_ERRORS as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        # The staging volume filled up while extracting (the preflight above
        # only sees the compressed size): report it as a space problem, not
        # as a corrupt upload.
        if _is_no_space(exc):
            raise SlideTransferError(
                "insufficient free space to import: the disk filled up while "
                "extracting the archive",
                code="importNoSpace",
            ) from exc
        # A truncated/garbage tar, a bad zstd frame or checksum — a bad
        # upload (or a damaged file), not a server fault, and not a reason
        # to reach for Restore. (A malformed manifest/slides.json is
        # importInvalid, see _load_stage_json.)
        raise SlideTransferError(
            "archive is corrupt or not a supported format",
            code="importCorrupt",
        ) from exc
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _norm_exists(db: Session, norm: str) -> bool:
    if not norm:
        return False
    row = db.execute(
        select(Slide.id).where(Slide.wsi_filename_norm == norm)
    ).first()
    return row is not None


def _live_public_ids(db: Session, public_ids: list) -> set:
    """Those of ``public_ids`` a slide of this library already carries. A
    placeholder (a reservation with no WSI yet) does not count: it is an
    upload or import in flight, or the leftover of a dead one that the next
    apply removes (see _reconcile_apply_state)."""
    if not public_ids:
        return set()
    return {
        p for (p,) in db.execute(
            select(Slide.public_id).where(
                Slide.public_id.in_(public_ids), Slide.wsi_filename != "",
            )
        ).all()
    }


def _conflict_reason(db: Session, sd: dict) -> Optional[str]:
    """Why a bundled slide collides with this library, or None.

    ``"public_id_conflict"``: a slide here already carries the bundle's
    public_id — almost always because this same bundle (or another export of
    the same slide) was imported before; importing it "as a copy" would
    create a second, published duplicate under a reminted id. Takes
    precedence over ``"filename_conflict"``: its WSI file name is taken (a
    copy gets a renamed file). A "skip" resolution skips the slide for
    either; "copy" imports it anyway (see _import_one_slide)."""
    if _live_public_ids(db, [sd.get("public_id")]):
        return "public_id_conflict"
    norm = safe_filename(sd.get("wsi_original_basename") or "slide.wsi").casefold()
    if _norm_exists(db, norm):
        return "filename_conflict"
    return None


# The oldest bundle schema_version this build imports: the first public
# release's. A bundle carries slides, their attributes, tours (with their own
# attributes), steps, pins, shapes and attachments — never ids, auth rows or
# analytics — so a schema bump only matters to the import when it changes one
# of those. When a migration changes a bundled field's meaning (a rename, a
# new unit, a split), raise this to that migration's version, or teach the
# import to read the older shape. A NEWER schema is always refused: this
# build cannot know what it added.
MIN_BUNDLE_SCHEMA_VERSION = 5
assert MIN_BUNDLE_SCHEMA_VERSION <= CURRENT_SCHEMA_VERSION


def _import_version_block(manifest: dict) -> tuple[Optional[str], Optional[str]]:
    """Decide whether a bundle may be imported into this system.

    ``schema_version`` is the source of truth for field compatibility: a
    bundle is allowed whenever its shape is one this binary reads — a
    schema_version from ``MIN_BUNDLE_SCHEMA_VERSION`` up to its own
    ``CURRENT_SCHEMA_VERSION`` — even across differing ``app_version``
    strings, so instances on different releases can exchange slides as long
    as the bundle's schema is not newer than the importer's (see
    MIN_BUNDLE_SCHEMA_VERSION for why older ones are safe). Anything outside
    that range is refused.

    The key is mandatory. Every bundle this app writes carries it, so an
    absent or unparseable value means the file is not one this app wrote, and
    falling back to an app_version comparison would let a bundle of unknown
    shape through on a string match alone.

    Returns ``(blocking_code, blocking_reason)`` — ``(None, None)`` when the
    import may proceed. Shared by :func:`plan_import` (preflight) and
    :func:`apply_import` (re-guard) so the two never disagree.
    """
    raw_sv = manifest.get("schema_version", None)
    if raw_sv is None:
        return (
            "VERSION_MISMATCH",
            "This bundle does not declare a schema_version, so its field "
            "layout cannot be checked against this system. It was not "
            "produced by tessera's slide export.",
        )
    # ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, so
    # a JSON ``true`` would read as schema_version 1 and be waved through for
    # as long as the baseline happens to sit there. Same reasoning as the
    # ``gen`` / ``tv`` claim checks.
    if isinstance(raw_sv, bool) or not isinstance(raw_sv, (int, str)):
        bundle_sv = None
    else:
        try:
            bundle_sv = int(raw_sv)
        except ValueError:
            bundle_sv = None
    if bundle_sv is None:
        return (
            "VERSION_MISMATCH",
            f"This bundle declares a non-numeric schema_version ({raw_sv!r}). "
            f"It was not produced by tessera's slide export.",
        )
    if bundle_sv > CURRENT_SCHEMA_VERSION:
        return (
            "VERSION_MISMATCH",
            f"This bundle is at schema_version {bundle_sv}, newer than this "
            f"system's {CURRENT_SCHEMA_VERSION}. Upgrade this system to import it.",
        )
    if bundle_sv < MIN_BUNDLE_SCHEMA_VERSION:
        return (
            "VERSION_MISMATCH",
            f"This bundle is at schema_version {bundle_sv}; this system imports "
            f"bundles from schema_version {MIN_BUNDLE_SCHEMA_VERSION} to "
            f"{CURRENT_SCHEMA_VERSION}.",
        )
    return (None, None)


def _data_volume_path() -> str:
    """Where imported WSI files land (``wsi/``, or the data dir before the
    first upload has created it), for free-space checks."""
    wsi_dir = _wsi_dir()
    return wsi_dir if os.path.isdir(wsi_dir) else settings.data_dir


def _wsi_copy_needed(stage_dir: str) -> bool:
    """Whether placing a staged WSI costs data-volume space: it is copied
    when the stage (BACKUP_DIR) and ``wsi/`` are on different filesystems,
    hard-linked otherwise (see _place_staged_wsi)."""
    return not _same_filesystem(stage_dir, _data_volume_path())


def plan_import(stage_id: str, manifest: dict, slides_doc: dict, db: Session) -> dict:
    """Compute the additive-import preflight: version guard + per-slide conflicts.

    For a stage an earlier apply already ran against (it was kept for a
    retry), each slide also carries that outcome: ``apply_status`` is
    ``"imported"`` (and not re-importable from this stage) only while the
    slide row it created still exists, ``"failed"`` with the recorded reason,
    ``"skipped"``, or None when the slide was never attempted (or its import
    was deleted since). Read-only: a record the DB contradicts is reported as
    the DB has it, and left for the next apply to clean up.
    """
    raw_version = manifest.get("app_version")
    bundle_v = raw_version[:64] if isinstance(raw_version, str) else "?"
    blocking_code, blocking_reason = _import_version_block(manifest)
    stage_dir = _stage_dir(stage_id)
    records = _load_apply_state(stage_dir)["slides"]
    slides = slides_doc["slides"]
    copy_needed = _wsi_copy_needed(stage_dir)

    # catalog_id is copied verbatim on import and carries no DB uniqueness
    # constraint, so a bundle can silently create a second slide sharing an
    # existing catalog number. Surface it as an *advisory* (no resolution
    # needed — the import still succeeds) so the operator isn't surprised by a
    # duplicated catalog id after the fact.
    existing_catalog_ids = {
        c for (c,) in db.execute(select(Slide.catalog_id)).all() if c
    }
    # A slide here already carrying a bundled slide's public_id: most likely
    # this bundle was imported before. Unlike a mere filename clash, that is
    # worth a decision — the dashboard defaults such a slide to "skip" rather
    # than to a second (published) copy under a reminted id.
    taken_public_ids = _live_public_ids(db, [sd["public_id"] for sd in slides])

    slides_report = []
    already_imported = 0
    failed_earlier = 0
    retryable_assets = 0
    wsi_bytes_to_copy = 0
    for sd in slides:
        raw_basename = sd.get("wsi_original_basename") or ""
        norm = safe_filename(raw_basename).casefold()
        public_id = sd["public_id"]
        catalog_id = _bundle_slide_text(sd, "catalog_id")
        content_errors = _bundle_content_errors(sd)
        ext_error = _bundle_extension_error(sd)
        if ext_error:
            # Shown with the invalid steps: the apply refuses the slide too.
            content_errors.append({"tour": "", "step": 0, "detail": ext_error})
        tours = sd.get("tours") or []
        assets_over_limit = 0
        for t in tours:
            src_assets = _staged_assets_dir(stage_dir, t["public_id"])
            if src_assets:
                assets_over_limit += _preview_tour_asset_skips(src_assets)
        rec = records.get(public_id)
        apply_status = _effective_status(db, rec) if rec is not None else None
        if apply_status == "imported":
            already_imported += 1
            retryable_assets += _retryable_asset_count(rec)
        elif apply_status == "failed":
            failed_earlier += 1
        staged_wsi = _staged_wsi_file(stage_dir, public_id)
        if copy_needed and staged_wsi is not None and apply_status != "imported":
            try:
                wsi_bytes_to_copy += os.path.getsize(staged_wsi)
            except OSError:
                pass
        slides_report.append({
            "public_id": public_id,
            "catalog_id": catalog_id,
            "title": _bundle_slide_text(sd, "title"),
            "tour_count": len(tours),
            "wsi_original_basename": raw_basename,
            "wsi_present_in_bundle": staged_wsi is not None,
            "filename_conflict": _norm_exists(db, norm),
            # A slide here already has this public_id (see above). Also set
            # for a slide this stage imported itself (apply_status
            # "imported"), which is not re-importable anyway.
            "public_id_conflict": public_id in taken_public_ids,
            "catalog_id_conflict": bool(catalog_id) and catalog_id in existing_catalog_ids,
            # Values the editors would refuse: applying fails this slide.
            "content_errors": content_errors[:5],
            "content_error_count": len(content_errors),
            # Attachments this system's limits will refuse (lower bound).
            "assets_over_limit": assets_over_limit,
            # Image-credit fields this system will not accept (imported empty).
            "credit_skipped": _bundle_credit(sd)[1],
            # Outcome of an earlier apply of this stage (see the docstring).
            "apply_status": apply_status,
            "failure": (
                {
                    "code": rec.get("code"),
                    "error": rec.get("error") or "",
                    "params": rec.get("params") or {},
                }
                if apply_status == "failed" else None
            ),
        })

    # The WSI files of the slides still to import are copied onto the data
    # volume when it is not the stage's (hard-linked otherwise): say up front
    # when they will not all fit — with the reserve each copy must leave free
    # (_check_wsi_copy_space) — rather than only as per-slide failures.
    # One measurement, so the figures reported are the ones that decided
    # (a second statvfs could report a free figure that disagreed with the
    # verdict). The per-slide check reserves for each
    # slide's own size and the plan for the total — both
    # disk_space.reserve_for, so a plan that fits never refuses a slide
    # that the plan counted, short of something else writing meanwhile.
    data_space = None
    if wsi_bytes_to_copy:
        short = _disk_space.check(_data_volume_path(), wsi_bytes_to_copy)
        if short is not None:
            data_space = short.as_params()

    return {
        "stage_id": stage_id,
        "bundle_app_version": bundle_v,
        "blocking_code": blocking_code,
        "blocking_reason": blocking_reason,
        "slide_count": len(slides_report),
        "tour_count": sum(s["tour_count"] for s in slides_report),
        "slides": slides_report,
        # slides.json keys this build does not know (a newer exporter's
        # fields): they cannot be imported.
        "unknown_fields": _unknown_bundle_fields(slides),
        # Set when the data volume cannot take the WSI files still to copy.
        "data_space": data_space,
        # True once an apply has run against this stage (it was kept because
        # something is left to retry).
        "applied": bool(records),
        "already_imported_count": already_imported,
        "retry": {"failed_slides": failed_earlier, "skipped_assets": retryable_assets},
    }


def stage_plan(stage_id: str, db: Session) -> dict:
    """Re-read a stage that is still on disk and recompute its preflight —
    how the dashboard returns to a bundle that was kept for a retry (or
    staged and not yet applied) without uploading it again. Same shape as
    the staging job's result."""
    _stage_dir_path, manifest, slides_doc = _read_stage(stage_id)
    _validate_slides_doc(slides_doc)
    return {
        "stage_id": stage_id,
        "plan": plan_import(stage_id, manifest, slides_doc, db),
    }


# ---------------------------------------------------------------------------
# Import: apply
# ---------------------------------------------------------------------------

def _rewrite_asset_paths(md: str, tour_remap: dict) -> str:
    """Rewrite ``/assets/tours/<old>/`` -> ``/assets/tours/<new>/`` in step
    markdown for every tour whose public_id was reminted on import."""
    if not md:
        return md
    for old, new in tour_remap.items():
        if old and new and old != new:
            md = md.replace(f"/assets/tours/{old}/", f"/assets/tours/{new}/")
    return md


def _build_tour_remap(db: Session, slides: list, prefer: Optional[dict] = None) -> dict:
    """Assign each bundled tour its final public_id up front (kept when free,
    reminted on collision), across ALL slides in the batch.

    ``prefer`` is the mapping an earlier apply of the same stage planned. A
    slide imported in that run may already carry markdown rewritten to the
    id planned for a tour of a slide that then failed; reusing that id when
    it is still free keeps the reference valid once the retry imports it,
    where a fresh mint would leave it dangling.

    ``slides`` must have passed :func:`_validate_slides_doc`, so every bundle
    tour id is a well-formed token (``prefer`` comes from a validated apply
    state); the ids this returns therefore are too, and are safe to store
    and to build ``tour_asset_dir`` paths from.
    """
    used = {p for (p,) in db.execute(select(Tour.public_id)).all() if p}
    prefer = prefer or {}
    remap: dict = {}
    for sd in slides:
        for t in sd.get("tours") or []:
            old = t["public_id"]
            planned = prefer.get(old)
            if planned is not None and planned not in used:
                new = planned
            elif old not in used:
                new = old
            else:
                new = mint_public_id()
                for _ in range(10):
                    if new not in used:
                        break
                    new = mint_public_id()
            remap[old] = new
            used.add(new)
    return remap


def _dodge_basename(db: Session, basename: str) -> str:
    """Derive a non-colliding filename for an import-as-copy of a slide whose
    original name already exists on the target."""
    stem, ext = os.path.splitext(basename)
    for _ in range(20):
        cand = safe_filename(f"{stem}-imported-{secrets.token_hex(3)}{ext}")
        if not _norm_exists(db, cand.casefold()):
            return cand
    raise SlideTransferError(
        "could not find a free filename for the imported slide",
        code="importFilenameExhausted",
    )


def _reserve_slide_row(
    db: Session,
    desired_public_id: Optional[str],
    before_commit: Optional[Callable[[int, str], None]] = None,
) -> Slide:
    """Insert an empty slide row with a reserved monotonic id, preserving the
    bundle's public_id when free and reminting on collision.

    Mirrors the reserve-and-retry loop in ``admin_slides.upload_slide``.

    ``before_commit(id, public_id)`` runs once per attempt with the values
    about to be committed, before anything is flushed (so no SQLite write
    lock is held while it runs). apply_import records them there, ahead of
    the row existing: whatever a crash leaves behind afterwards — a
    placeholder, or a fully imported slide whose outcome was never recorded
    — is then findable by id. Recording after this commit would leave a
    window in which an orphan placeholder holds the bundle's public_id
    unbeknownst to the retry, which would then remint it.
    """
    slide: Optional[Slide] = None
    # Preserve the bundle public_id only when it is a well-formed token;
    # never store an unsafe/traversal value on the row.
    pid = desired_public_id if valid_public_id(desired_public_id) else mint_public_id()
    for _attempt in range(6):
        candidate_id = reserve_next_slide_id(db)
        if before_commit is not None:
            try:
                before_commit(candidate_id, pid)
            except BaseException:
                # reserve_next_slide_id staged a watermark bump in the session.
                db.rollback()
                raise
        slide = Slide(id=candidate_id, public_id=pid)
        db.add(slide)
        try:
            db.commit()
            db.refresh(slide)
            return slide
        except IntegrityError:
            # A collision: the id (a concurrent upload reserved it first) or
            # the public_id. Roll back and retry with a freshly-advanced
            # watermark — reminting the token only when a row now holds it,
            # so losing an id race doesn't cost the bundle its public_id.
            db.rollback()
            slide = None
            if db.execute(select(Slide.id).where(Slide.public_id == pid)).first() is not None:
                pid = mint_public_id()
        except Exception:
            # Any other commit failure (SQLITE_BUSY -> OperationalError, ENOSPC,
            # ...) MUST still roll back before propagating, or the session is
            # left in a needs-rollback state and every subsequent slide in the
            # batch fails with PendingRollbackError. Mirrors upload_slide.
            db.rollback()
            raise
    raise SlideTransferError("failed to reserve a slide id", code="importReserveFailed")


def _bundle_step_fields(st: object) -> dict:
    """A bundled step's scalar fields, held to the step editor's rules.

    The same checks ``admin_add_step`` / ``StepUpdateIn`` apply — finite
    coordinates and rotation, a positive field of view, a single-line label
    within the length cap, an ``order`` >= 1 — plus the pin / shape schemas.
    Without them a bundle value such as ``"Infinity"`` (a legal JSON string
    that ``float()`` accepts) would be stored as-is and every later JSON
    response for the tour would fail to serialise. Raises ``ValueError`` naming the field.

    ``transition_mode`` keeps its lenient mapping (an unknown value falls back
    to the default): it is a presentation choice, and an out-of-set value
    can't break anything downstream. A pin shape outside the accepted set is
    refused, as it is for every other writer (``PinItem``).
    """
    if not isinstance(st, dict):
        raise ValueError("step is not an object")
    order = st.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 2**31 - 1:
        raise ValueError("order must be an integer between 1 and 2147483647")

    def num(key: str, default: float) -> object:
        v = st.get(key, default)
        return default if v is None else v

    tm = st.get("transition_mode") or DEFAULT_TRANSITION_MODE
    try:
        v = StepUpdateIn(
            label=st.get("label") or "",
            description_md=st.get("description_md") or "",
            center_x=num("center_x", 0.0),
            center_y=num("center_y", 0.0),
            fov_um_w=num("fov_um_w", 600.0),
            fov_um_h=num("fov_um_h", 450.0),
            rotation_deg=num("rotation_deg", 0.0),
            transition_mode=tm if tm in TRANSITION_MODES else DEFAULT_TRANSITION_MODE,
        )
        pins = _PIN_LIST_ADAPTER.validate_python(st.get("pins") or [])
        shapes = _SHAPE_LIST_ADAPTER.validate_python(st.get("shapes_l0") or [])
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(part) for part in err.get("loc", ())) or "step"
        raise ValueError(f"{loc}: {err.get('msg')}") from None
    # The editor's per-step caps (schemas._MAX_STEP_PINS / _MAX_STEP_SHAPES /
    # _MAX_STEP_FREEHAND_POINTS, enforced by step create, PATCH and append).
    # An imported step past them could never be saved from the editor again —
    # every save sends the full lists — so refuse it here, where the preflight
    # can name it.
    if len(pins) > _MAX_STEP_PINS:
        raise ValueError(f"pins: at most {_MAX_STEP_PINS} per step (got {len(pins)})")
    if len(shapes) > _MAX_STEP_SHAPES:
        raise ValueError(f"shapes_l0: at most {_MAX_STEP_SHAPES} per step (got {len(shapes)})")
    points = freehand_point_count(shapes)
    if points > _MAX_STEP_FREEHAND_POINTS:
        raise ValueError(
            f"shapes_l0: at most {_MAX_STEP_FREEHAND_POINTS} freehand points per step"
            f" (got {points})"
        )
    # UNIQUE(step_id, pin_id / shape_id): caught here, the preflight names
    # the step instead of the apply failing the slide on an IntegrityError.
    if len({p.id for p in pins}) != len(pins):
        raise ValueError("pins: duplicate id values")
    if len({sh.id for sh in shapes}) != len(shapes):
        raise ValueError("shapes_l0: duplicate id values")
    return {
        "order": order,
        "label": v.label,
        "description_md": v.description_md,
        "center_x": v.center_x,
        "center_y": v.center_y,
        "fov_um_w": v.fov_um_w,
        "fov_um_h": v.fov_um_h,
        "rotation_deg": v.rotation_deg,
        "transition_mode": v.transition_mode,
        "pins": pins,
        "shapes": shapes,
    }


def _bundle_extension_error(sd: dict) -> Optional[str]:
    """The reason ``sd``'s WSI would be refused for its file extension (the
    upload endpoint's allow-list), or None. The apply refuses such a slide, so
    the preflight reports it instead of saying OK."""
    name = safe_filename(sd.get("wsi_original_basename", "") or "slide.wsi")
    try:
        validated_extension(name)
    except HTTPException:
        return unsupported_extension_message(name)
    return None


def _bundle_slide_text(sd: dict, field_name: str) -> str:
    """A bundled slide's title / catalog_id as the slide editor stores it
    (surrounding whitespace stripped)."""
    return (sd.get(field_name) or "").strip()


def _bundle_slide_field_error(sd: dict) -> Optional[str]:
    """Why the slide editor would refuse ``sd``'s title or catalog_id (the
    same validate_slide_metadata checks: length caps, control characters), or
    None. Imported as-is, such a value would make every later save of the slide
    fail, since the editor always sends both fields back."""
    try:
        validate_slide_metadata(
            _bundle_slide_text(sd, "catalog_id"), _bundle_slide_text(sd, "title"), {},
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        params = detail.get("params") or {}
        if detail.get("code") == "adminDash.validationMaxLen":
            return f"{params.get('field')} is longer than {params.get('max')} characters"
        return f"{params.get('field')} contains control characters"
    return None


def _bundle_tour_fields(t: dict) -> tuple[str, str]:
    """A bundled tour's ``(title, description)``, held to the tour editor's
    rules (admin_create_tour / TourUpdateIn): a single-line title within the
    length cap — stripped, and "Tour" when the bundle has none, as a new tour
    is named — and a description within the 8000-character cap, without NUL
    bytes. Raises ``ValueError`` with the editor's reason."""
    from .routers.admin_tours import _bounded_text, _validate_tour_title
    from .schemas import _MAX_TOUR_DESCRIPTION_LEN

    try:
        title = _validate_tour_title((t.get("title") or "").strip() or "Tour")
        description = _bounded_text(
            t.get("description") or "", field="description", max_len=_MAX_TOUR_DESCRIPTION_LEN,
        )
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from None
    return title, description


def _bundle_content_errors(sd: dict) -> list:
    """Every value of ``sd`` the editors would refuse — its title or
    catalog_id, a tour's title or description, a step — as ``{"tour",
    "step", "detail"}`` dicts: ``tour`` is "" for the slide's own fields,
    ``step`` the step's 1-based position in the bundle's list, 0 for the
    tour's own fields. Shared by the preflight, so the operator sees the
    problem before applying, and by the apply, which refuses the slide."""
    errors: list = []
    slide_error = _bundle_slide_field_error(sd)
    if slide_error:
        errors.append({"tour": "", "step": 0, "detail": slide_error})
    for t in sd.get("tours") or []:
        try:
            tour_label = _bundle_tour_fields(t)[0]
        except ValueError as exc:
            tour_label = t["public_id"]
            errors.append({"tour": tour_label, "step": 0, "detail": str(exc)})
        steps = t.get("steps", []) or []
        if not isinstance(steps, list):
            errors.append({"tour": tour_label, "step": 0, "detail": "steps is not a list"})
            continue
        # The editor's step cap (admin_add_step refuses a step past it).
        if len(steps) > _MAX_TOUR_STEPS:
            errors.append({
                "tour": tour_label, "step": 0,
                "detail": f"steps: at most {_MAX_TOUR_STEPS} per tour (got {len(steps)})",
            })
        seen_orders: set = set()
        points = 0
        for pos, st in enumerate(steps, start=1):
            try:
                fields = _bundle_step_fields(st)
            except ValueError as exc:
                errors.append({"tour": tour_label, "step": pos, "detail": str(exc)})
                continue
            points += freehand_point_count(fields["shapes"])
            if fields["order"] in seen_orders:
                errors.append({
                    "tour": tour_label, "step": pos,
                    "detail": f"order {fields['order']} is used by another step of this tour",
                })
            seen_orders.add(fields["order"])
        # The editor's tour-wide vertex budget (step create, PATCH, append).
        if points > _MAX_TOUR_FREEHAND_POINTS:
            errors.append({
                "tour": tour_label, "step": 0,
                "detail": (
                    f"shapes_l0: at most {_MAX_TOUR_FREEHAND_POINTS} freehand points per tour"
                    f" (got {points})"
                ),
            })
    return errors


def _bundle_attributes(raw: object) -> tuple[list, list]:
    """``(pairs, skipped)`` for a bundled slide's ``attributes``: the
    (key, value) pairs the metadata editor would accept, values stripped like
    the editor does, and the distinct keys of those it would refuse (a ':' or
    ',' in the key, control characters, over-long text, non-strings). The
    public metadata filter splits on ':' and matches exact stripped values, so
    a pair the editor refuses would be stored but never findable."""
    pairs: list = []
    skipped: list = []
    if not isinstance(raw, dict):
        return pairs, skipped
    seen: set = set()
    for key, values in raw.items():
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        for v in values:
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    continue
            try:
                validate_attribute_kv(key, [v])
            except HTTPException:
                if key not in skipped:
                    skipped.append(key)
                continue
            if (key, v) not in seen:
                seen.add((key, v))
                pairs.append((key, v))
    return pairs, skipped


_CREDIT_FIELDS = ("credit_text", "license_id", "source_url")


def _bundle_credit(sd: dict) -> tuple[dict, list]:
    """``(credit, skipped)``: bundle_credit's lenient reading of ``sd``'s
    image credit, and the fields it dropped — a value present in the bundle
    that this system does not accept (a license id it does not know, over-long
    text, a non-http(s) URL, a non-string), which is imported as "". Reported
    like a refused attribute, since a lost credit can be a lost license
    obligation."""
    credit = bundle_credit(sd)
    skipped = []
    for key in _CREDIT_FIELDS:
        raw = sd.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or (raw.strip() and not credit[key]):
            skipped.append(key)
    return credit, skipped


def _create_step(db: Session, tour_id: int, st: dict, tour_remap: dict) -> None:
    """Reconstruct one Step (+ pins + shapes) from a bundle record, with the
    construction/validation of ``admin_tours.admin_add_step`` (see
    ``_bundle_step_fields``; the caller has already refused a slide whose
    steps fail it)."""
    f = _bundle_step_fields(st)
    step = Step(
        tour_id=tour_id,
        order=f["order"],
        label=f["label"],
        center_x=f["center_x"],
        center_y=f["center_y"],
        fov_um_w=f["fov_um_w"],
        fov_um_h=f["fov_um_h"],
        rotation_deg=f["rotation_deg"],
        description_md=_rewrite_asset_paths(f["description_md"], tour_remap),
        transition_mode=f["transition_mode"],
    )
    db.add(step)
    db.flush()

    for p in f["pins"]:
        db.add(StepPin(
            pin_id=p.id, step_id=step.id, x=p.x, y=p.y,
            shape=p.shape or "circle", color=p.color or "red",
            rotation_deg=p.rotation_deg, label=p.label or "",
            z=p.z,
        ))
    for sh in f["shapes"]:
        if sh.kind == "freehand" and sh.points:
            points_json = json.dumps([{"x": pt.x, "y": pt.y} for pt in sh.points])
        else:
            points_json = ""
        db.add(StepShape(
            shape_id=sh.id, step_id=step.id, kind=sh.kind,
            x=sh.x, y=sh.y, w=sh.w, h=sh.h,
            stroke_color=(sh.stroke_color or "red")[:32],
            fill_enabled=bool(sh.fill_enabled), fill_opacity=float(sh.fill_opacity),
            points_json=points_json, smoothing=bool(sh.smoothing),
            label=sh.label or "",
            label_position=sh.label_position,
            z=sh.z,
        ))


def _bundle_mpp(value: object) -> float:
    """A bundle's MPP override as a stored value: 0.0 (unset) unless it is a
    finite number in the range ``parse_mpp_value`` accepts. A crafted NaN would
    otherwise reach the NOT NULL column as NULL and fail the slide."""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and 0 <= v <= 1000 else 0.0


def _bundle_adjust(value: object, channel: str) -> Optional[float]:
    """A bundle's image-adjust preset, normalised like the PATCH endpoints'
    ``_parse_adjust_value``: clamped to the channel range, identity -> None,
    and anything that isn't a finite number -> None (no preset)."""
    from .routers.admin_slides import _ADJUST_RANGES

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if not math.isfinite(v):
        return None
    lo, hi = _ADJUST_RANGES[channel]
    v = max(lo, min(hi, v))
    return None if v == 1.0 else v


def _bundle_tri_bool(value: object) -> Optional[bool]:
    """A bundle's tri-state overlay flag: a real bool, else None (default)."""
    return value if isinstance(value, bool) else None


def _tour_asset_entries(src_dir: str):
    """A staged tour-asset directory's entries, in name order."""
    with os.scandir(src_dir) as it:
        return sorted(it, key=lambda e: e.name)


def _asset_limit_reason(entry, *, count: int, total: int) -> tuple[Optional[str], int]:
    """Why the upload limits would refuse ``entry`` given what was already
    accepted for the tour (``count`` files, ``total`` bytes), else None —
    plus the entry's size. Name / type rules first, then this system's
    per-file and per-tour caps (TOUR_ASSET_MAX_BYTES / _MAX_FILES /
    _MAX_TOTAL_BYTES), which may well be tighter than the source system's."""
    # Regular files only (no symlinks, no nested dirs), under a name the
    # upload path could itself have stored (no dotfiles: those are its
    # in-flight temps).
    if not storable_asset_name(entry.name) or not entry.is_file(follow_symlinks=False):
        return "invalid_name", 0
    size = entry.stat(follow_symlinks=False).st_size
    if size > settings.tour_asset_max_bytes:
        return "too_large", size
    max_files = settings.tour_asset_max_files
    if max_files > 0 and count >= max_files:
        return "tour_file_limit", size
    max_total = settings.tour_asset_max_total_bytes
    if max_total > 0 and total + size > max_total:
        return "tour_size_limit", size
    return None, size


def _preview_tour_asset_skips(src_dir: str) -> int:
    """How many of a staged tour's assets the limits alone will refuse — the
    preflight's cheap estimate (no content decoding, so it is a lower bound
    on what :func:`_import_tour_assets` will skip)."""
    count = total = skipped = 0
    for entry in _tour_asset_entries(src_dir):
        reason, size = _asset_limit_reason(entry, count=count, total=total)
        if reason:
            skipped += 1
            continue
        count += 1
        total += size
    return skipped


# Attachment skip reasons a retry of the same stage can overcome: each is one
# of THIS system's own settings — the per-file / per-tour caps
# (TOUR_ASSET_MAX_BYTES / _MAX_FILES / _MAX_TOTAL_BYTES; a tour's quota also
# frees up when an author deletes attachments) and the served-type allow-list
# (TOUR_ASSET_ALLOWED_MIME_PREFIXES, image-only by default, so a PDF from a
# source that allowed PDFs lands here). The other reasons are properties of
# the bundled file itself — its name (invalid_name), its bytes
# (invalid_content), an extension that contradicts its bytes
# (extension_mismatch) — and a retry would refuse it again, so they never keep
# a multi-GB stage around on their own.
_RETRYABLE_ASSET_REASONS = frozenset({
    "too_large", "tour_file_limit", "tour_size_limit", "type_not_allowed",
})


def _import_tour_assets(
    src_dir: str,
    dst_dir: str,
    *,
    only: Optional[set] = None,
    copied: Optional[list] = None,
    dry_run: bool = False,
) -> list:
    """Copy a bundle's tour assets through the same checks as an upload.

    The bundle is operator-supplied but otherwise untrusted content, and these
    files end up served from the app's own origin under ``/assets/``. A plain
    ``copytree`` would install whatever the archive holds — ``.html``/``.svg``
    documents, oversized files, more than the per-tour quota — bypassing every
    guard ``admin_upload_tour_asset`` applies. Files that fail a check are
    skipped rather than failing the whole import: a slide with one bad
    attachment is still worth importing.

    Returns the skipped files as ``{"name", "reason"}`` dicts. Were they only
    logged, an import whose target had tighter limits than its source would
    report plain success while the tours' images were missing.

    Additive, so the same pass also serves a retry into a tour that is
    already live: whatever ``dst_dir`` already holds counts against the
    per-tour limits (as the upload endpoint counts it) and is never
    overwritten — a name that is already there is left as is, neither copied
    nor reported. ``only`` restricts the pass to those staged names (the
    attachments an earlier pass skipped); ``copied`` collects the names this
    pass installed. Callers writing into a live tour hold its
    ``tour_asset_lock`` so an author's concurrent upload can't race the quota.

    ``dry_run`` runs every check and returns the skips, counting each file
    that passes as if installed, but writes nothing.
    """
    import mimetypes

    from .routers.admin_tours import (
        _is_allowed_asset_mime,
        _validate_asset_content,
        asset_extension_matches,
    )

    if not dry_run:
        os.makedirs(dst_dir, exist_ok=True)
    present: set = set()
    count = 0
    total = 0
    # Mirrors _enforce_tour_asset_quota: every non-dot entry is a file and
    # counts; dot-prefixed ones are in-flight upload temps.
    with (os.scandir(dst_dir) if os.path.isdir(dst_dir) else contextlib.nullcontext([])) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            present.add(e.name)
            count += 1
            try:
                total += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    skipped: list = []
    for entry in _tour_asset_entries(src_dir):
        name = entry.name
        if (only is not None and name not in only) or name in present:
            continue
        reason, size = _asset_limit_reason(entry, count=count, total=total)
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        try:
            family, _ext = _validate_asset_content(entry.path, None)
        except HTTPException:
            skipped.append({"name": name, "reason": "invalid_content"})
            continue
        # An extension the detected bytes contradict (a PNG-prefixed document
        # named .html) is the file's own defect; told apart from the type
        # allow-list below because only the latter is a setting a retry can
        # see changed.
        if not asset_extension_matches(name, family):
            skipped.append({"name": name, "reason": "extension_mismatch"})
            continue
        # Same MIME allow-list the upload endpoint applies to the multipart
        # Content-Type (TOUR_ASSET_ALLOWED_MIME_PREFIXES; images only by
        # default), judged here on the type the file will be served as.
        if not _is_allowed_asset_mime(mimetypes.guess_type(name)[0]):
            skipped.append({"name": name, "reason": "type_not_allowed"})
            continue
        if dry_run:
            present.add(name)
            count += 1
            total += size
            continue
        dst = os.path.join(dst_dir, name)
        # Through a hidden temp + rename: on a retry the tour is live, and a
        # viewer must never be served a half-copied image. The temp follows
        # the upload path's ``.{name}.upload-{token}`` shape so the existing
        # stale-temp sweep reclaims one a crash strands.
        tmp = os.path.join(dst_dir, f".{name}.upload-{secrets.token_hex(6)}")
        try:
            shutil.copyfile(entry.path, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        present.add(name)
        if copied is not None:
            copied.append(name)
        count += 1
        total += size
    if skipped:
        _logger.warning(
            "import: skipped %d tour asset(s) in %s: %s",
            len(skipped), src_dir,
            ", ".join(f"{s['name']} ({s['reason']})" for s in skipped[:20]),
        )
    return skipped


def _staged_assets_dir(stage_dir: str, tour_pid: object) -> str:
    """The staged attachment directory of a bundled tour, or "" when there is
    none. ``tour_pid`` is bundle-controlled and flows into os.path.join; only
    a well-formed token is used (rejects "" -> the parent tours/ dir and any
    "../" traversal that would read an arbitrary directory)."""
    if not valid_public_id(tour_pid):
        return ""
    d = os.path.join(stage_dir, "assets", "tours", tour_pid)
    return d if os.path.isdir(d) else ""


def _check_wsi_copy_space(src: str, dest_dir: str, src_pid: str) -> None:
    """Refuse (``importNoSpace``) to copy the staged WSI ``src`` into
    ``dest_dir`` when its volume cannot take it and still keep the shared
    reserve (disk_space.reserve_for: 10%, at least 256 MiB) free. That volume
    is the data volume the live DB and every upload share: a copy that
    leaves it with a few bytes — as a check that only asks that it fit would
    allow — takes the running install down with it, as an extraction or an upload spool
    would (both keep the same reserve). Unknown capacity passes; the copy's
    own ENOSPC is the safety net."""
    short = _disk_space.check(dest_dir, os.path.getsize(src))
    if short is not None:
        raise SlideTransferError(
            f"{short.message('data volume')} (slide {src_pid})",
            code="importNoSpace",
        )


def _place_staged_wsi(src: str, dst: str) -> None:
    """Put a staged WSI at its live path WITHOUT consuming the staged copy.

    A hard link when stage and data share a filesystem (instant, no extra
    space while both names exist), else a copy — which costs no more than a
    cross-device ``shutil.move`` would: that, too, copies the bytes onto
    the data volume, and the staged copy only lingers until the stage is
    cleaned up. Keeping it is what makes a kept stage retryable: a slide whose
    finalise failed, or whose import was deleted afterwards, can be imported
    again from the stage, and a crash mid-import can never strand the only
    copy of the bytes under ``wsi/`` where the boot-time reservation reaper
    deletes ``{id}_*`` files of unfinished rows.
    """
    try:
        os.link(src, dst)
        return
    except OSError as exc:
        # EXDEV (different mounts), EPERM / ENOTSUP / EMLINK (no — or no more —
        # hard links on this filesystem): copy instead. An existing dst is a
        # real conflict, not something to paper over with a copy.
        if exc.errno == errno.EEXIST:
            raise
    # The link was expected to cost nothing, so nothing has checked the
    # space this copy takes when stage and data share a filesystem that
    # refused the link (_import_one_slide checks up front only for a stage on
    # another filesystem). Check it before writing the bytes.
    _check_wsi_copy_space(src, os.path.dirname(dst), os.path.basename(os.path.dirname(src)))
    shutil.copy2(src, dst)


def _import_one_slide(
    db: Session, stage_dir: str, sd: dict, tour_remap: dict, resolution: str,
    on_reserved: Optional[Callable[[dict], None]] = None,
) -> Optional[dict]:
    """Additively import a single bundled slide. Returns a result dict on
    success, ``None`` when skipped, and raises on failure (the caller records
    it and moves on to the next slide).

    ``on_reserved(info)`` is called just before the slide's row is first
    committed, with everything a crash would leave behind from then on: the
    reserved ``slide_id`` / ``public_id``, the ``wsi_filename`` and
    ``thumb_filename`` it will get, the ``tours`` id mapping, and the tour
    asset dirs this import will create (``new_asset_dirs``). See
    ``_reconcile_apply_state`` for how a retry uses them.

    The result carries a private ``_state`` entry (the thumbnail name, the
    tour mapping and the skipped attachments per bundled tour id) for the
    caller's bookkeeping; apply_import pops it before reporting.
    """
    src_pid = sd["public_id"]
    raw_basename = sd.get("wsi_original_basename") or "slide.wsi"
    final_basename = safe_filename(raw_basename)
    try:
        validated_extension(final_basename)
    except HTTPException as exc:
        raise SlideTransferError(
            unsupported_extension_message(final_basename), code="importUnsupportedExtension",
            params={"ext": os.path.splitext(final_basename)[1].lower() or "(none)"},
        ) from exc
    renamed = False

    # "skip" leaves out a slide that collides with this library by its WSI
    # name or by its public_id (see _conflict_reason); "copy" imports it
    # anyway, under a renamed file and/or a reminted public_id.
    name_taken = _norm_exists(db, final_basename.casefold())
    if resolution == "skip" and (name_taken or _live_public_ids(db, [src_pid])):
        return None
    if name_taken:
        final_basename = _dodge_basename(db, final_basename)
        renamed = True
    norm = final_basename.casefold()

    # Refuse the slide over a value the editors themselves would refuse,
    # before any row or file is touched (see _bundle_content_errors).
    content_errors = _bundle_content_errors(sd)
    if content_errors:
        first = content_errors[0]
        if first["step"]:
            raise SlideTransferError(
                f"tour {first['tour']!r} step {first['step']}: {first['detail']}",
                code="importStepInvalid",
                params={"tour": first["tour"], "step": first["step"], "detail": first["detail"]},
            )
        if first["tour"]:
            raise SlideTransferError(
                f"tour {first['tour']!r}: {first['detail']}",
                code="importTourInvalid",
                params={"tour": first["tour"], "detail": first["detail"]},
            )
        raise SlideTransferError(
            f"slide {src_pid}: {first['detail']}",
            code="importSlideFieldInvalid",
            params={"detail": first["detail"]},
        )

    # Locate the staged WSI before reserving a row so a bundle that is missing
    # the file doesn't leave an orphan placeholder behind.
    src_wsi = _staged_wsi_file(stage_dir, src_pid)
    if src_wsi is None:
        raise SlideTransferError(
            f"bundle is missing the WSI file for slide {src_pid}",
            code="importWsiMissing",
        )
    # Placing it copies the bytes onto the data volume when that is not the
    # stage's own (see _place_staged_wsi): refuse up front, before any row is
    # reserved, when they cannot fit, rather than filling the volume the live
    # DB and every upload share.
    if _wsi_copy_needed(stage_dir):
        _check_wsi_copy_space(src_wsi, _data_volume_path(), src_pid)

    # Each bundled tour's final id, fixed before the row is reserved so the
    # record written ahead of that commit (on_reserved) already names them.
    bundled_tours = []
    for t in sd.get("tours") or []:
        old_pid = t["public_id"]
        bundled_tours.append((t, old_pid, tour_remap.get(old_pid, old_pid)))

    # thumb_filename mints a random token per call, so the name announced
    # before the commit is the one used below.
    reserved_thumb: dict = {}

    def _announce(new_id: int, pid: str) -> None:
        reserved_thumb[new_id] = thumb_filename(new_id, "")
        on_reserved({
            "slide_id": new_id,
            "public_id": pid,
            "wsi_filename": f"{new_id}_{final_basename}",
            "thumb_filename": reserved_thumb[new_id],
            "title": _bundle_slide_text(sd, "title"),
            "renamed": renamed,
            "tours": {old: new for _t, old, new in bundled_tours},
            # Only directories that do not exist yet: a retry's cleanup must
            # never remove one that was already there.
            "new_asset_dirs": [
                new for _t, old, new in bundled_tours
                if _staged_assets_dir(stage_dir, old) and not os.path.exists(tour_asset_dir(new))
            ],
        })

    slide = _reserve_slide_row(
        db, src_pid, before_commit=_announce if on_reserved is not None else None,
    )
    new_id = slide.id
    stored_name = f"{new_id}_{final_basename}"
    dst = os.path.join(_wsi_dir(), stored_name)
    thumb_name = reserved_thumb.get(new_id) or thumb_filename(new_id, "")
    thumb_path = os.path.join(_thumbs_dir(), thumb_name)
    copied_asset_dirs: list[str] = []

    def _cleanup() -> None:
        # The staged WSI is linked or copied, never moved (_place_staged_wsi),
        # so removing the live copy leaves the stage intact for a retry.
        for p in (dst, thumb_path):
            try:
                os.remove(p)
            except OSError:
                pass
        for d in copied_asset_dirs:
            shutil.rmtree(d, ignore_errors=True)
        try:
            db.delete(slide)
            db.commit()
        except Exception as exc:
            # The placeholder stays, holding the bundle's public_id. Not
            # fatal here: the slide is recorded as failed together with its
            # reservation (apply_import's _failed_record), and the next apply
            # of the stage removes the placeholder before retrying the slide
            # (_reconcile_apply_state), so the public_id is kept rather than
            # reminted. The boot-time reaper is the backstop for a stage
            # never retried.
            db.rollback()
            _logger.warning(
                "import: could not remove the placeholder slide %s after a "
                "failed import: %s", new_id, exc,
            )

    # Hold the slide's mutation lock — cross-process (a flock under
    # data_dir/locks) — from here to the final commit or _cleanup, as uploads
    # do. Every worker's and wsi-sync's startup sweep
    # (admin_helpers.reap_orphan_slide_reservations) deletes placeholder rows
    # older than 30 minutes, with their wsi/{id}_* files, unless this lock is
    # held, and one slide's import can run that long (a multi-GB copy onto
    # another volume, a slow thumbnail). The few statements since the
    # reservation commit are covered by that age cutoff. Released in the
    # ``finally`` below, after the except clause has run _cleanup.
    held = contextlib.ExitStack()
    held.enter_context(slide_mutation_lock(new_id))
    try:
        os.makedirs(_wsi_dir(), exist_ok=True)
        os.makedirs(_thumbs_dir(), exist_ok=True)
        # Linked or copied, never moved — works across mounts (os.replace
        # would raise EXDEV) and leaves the stage able to retry this slide.
        _place_staged_wsi(src_wsi, dst)

        # The upload path's codes for the same failures (a layout the readers
        # refuse is mapped below, with the other exceptions).
        try:
            props = read_slide_properties(dst)
        except UnsupportedSlideError:
            raise
        except Exception as exc:
            if _is_no_space(exc):
                raise
            raise SlideTransferError(str(exc), code="wsiUnreadable") from exc
        try:
            make_thumbnail(dst, thumb_path)
        except Exception as exc:
            if _is_no_space(exc):
                raise
            raise SlideTransferError(str(exc), code="thumbnailFailed") from exc

        slide.wsi_filename = stored_name
        slide.wsi_filename_norm = norm
        slide.thumb_filename = thumb_name
        # Tile URLs are /dzi/{public_id}...?v={wsi_generation}. A reminted
        # token is brand new, so the default generation is safe; a kept bundle
        # token may have served (and nginx still cache) tiles of an earlier,
        # deleted incarnation with other bytes under low generations, so start
        # it in an unused one (see models.fresh_wsi_generation).
        if slide.public_id == src_pid:
            slide.wsi_generation = fresh_wsi_generation()
        # Held to the editor's limits by _bundle_content_errors above.
        slide.title = _bundle_slide_text(sd, "title")
        slide.catalog_id = _bundle_slide_text(sd, "catalog_id")
        slide.width = props["width"]
        slide.height = props["height"]
        # Override-able MPP comes from the bundle (a manual override); file-truth
        # MPP/vendor/etc. come from the actual bytes just transferred.
        slide.mpp_x = _bundle_mpp(sd.get("mpp_x"))
        slide.mpp_y = _bundle_mpp(sd.get("mpp_y"))
        slide.mpp_x_file = props["mpp_x"]
        slide.mpp_y_file = props["mpp_y"]
        slide.objective_power_file = props.get("objective_power", 0.0) or 0.0
        slide.vendor = props["vendor"]
        slide.acquired_at = props.get("acquired_at", "") or ""
        try:
            slide.file_size_bytes = os.path.getsize(dst)
        except OSError:
            slide.file_size_bytes = 0
        slide.image_adjust_brightness = _bundle_adjust(sd.get("image_adjust_brightness"), "brightness")
        slide.image_adjust_contrast = _bundle_adjust(sd.get("image_adjust_contrast"), "contrast")
        slide.image_adjust_saturation = _bundle_adjust(sd.get("image_adjust_saturation"), "saturation")
        slide.show_zoom_indicator = _bundle_tri_bool(sd.get("show_zoom_indicator"))
        slide.show_scale_bar = _bundle_tri_bool(sd.get("show_scale_bar"))
        slide.show_metadata_panel = _bundle_tri_bool(sd.get("show_metadata_panel"))
        credit, credit_skipped = _bundle_credit(sd)
        slide.credit_text = credit["credit_text"]
        slide.license_id = credit["license_id"]
        slide.source_url = credit["source_url"]
        # Same gate as admin_publish_slide: a slide without a catalog_id and a
        # title imports as a draft rather than as an unidentified public card.
        # (The WSI-present half of that gate is met: the file was just placed.)
        slide.is_published = (
            bool(sd.get("is_published", False))
            and bool(slide.catalog_id.strip())
            and bool(slide.title.strip())
        )
        slide.unlisted = bool(sd.get("unlisted", False))
        attach_associated_images(slide, dst)

        # Attributes (_bundle_attributes de-duplicates the pairs, so the
        # (slide_id, key, value) UNIQUE constraint is satisfied).
        attr_pairs, attributes_skipped = _bundle_attributes(sd.get("attributes"))
        for key, value in attr_pairs:
            db.add(SlideAttribute(slide_id=new_id, key=key, value=value))

        # Tours + steps + pins + shapes + asset dirs. Assets are copied in a
        # first pass, before the first flush below: that flush opens SQLite's
        # write transaction, and holding it across the per-file validation and
        # copying would stall every other writer (uploads, edits, the audit
        # log) for the duration.
        assets_skipped: list = []
        skipped_by_tour: dict = {}
        for t, old_pid, new_pid in bundled_tours:
            src_assets = _staged_assets_dir(stage_dir, old_pid)
            if src_assets:
                dst_assets = tour_asset_dir(new_pid)
                os.makedirs(os.path.dirname(dst_assets), exist_ok=True)
                # Register for rollback BEFORE the copy so a partial tree (e.g.
                # ENOSPC mid-copy) is still cleaned up on failure — but never a
                # directory that was already there: the tour row isn't inserted
                # yet, so a (racing) id clash must not let the rollback delete
                # another tour's files.
                if not os.path.exists(dst_assets):
                    copied_asset_dirs.append(dst_assets)
                tour_skips = _import_tour_assets(src_assets, dst_assets)
                if tour_skips:
                    skipped_by_tour[old_pid] = tour_skips
                for skip in tour_skips:
                    assets_skipped.append({"tour": _bundle_tour_fields(t)[0], **skip})

        tours_created = 0
        steps_created = 0
        for t, _old_pid, new_pid in bundled_tours:
            # Held to the tour editor's rules by _bundle_content_errors above.
            title, description = _bundle_tour_fields(t)
            tour = Tour(
                slide_id=new_id, public_id=new_pid,
                title=title,
                description=description,
                # Mirrors admin_tours' publish gate: a tour is only public
                # when its slide is.
                is_published=slide.is_published and bool(t.get("is_published", False)),
                unlisted=bool(t.get("unlisted", False)),
            )
            db.add(tour)
            db.flush()
            # Same lenient reading as the slide's attributes: a pair the editor
            # would refuse is skipped and its key reported with the slide's.
            tour_pairs, tour_attrs_skipped = _bundle_attributes(t.get("attributes"))
            for key, value in tour_pairs:
                db.add(TourAttribute(tour_id=tour.id, key=key, value=value))
            for key in tour_attrs_skipped:
                if key not in attributes_skipped:
                    attributes_skipped.append(key)
            for st in t.get("steps", []) or []:
                _create_step(db, tour.id, st, tour_remap)
                steps_created += 1
            tours_created += 1

        db.commit()
    except BaseException as exc:
        db.rollback()
        _cleanup()
        if isinstance(exc, (SlideTransferError, ValidationError)):
            raise
        # A UNIQUE(wsi_filename_norm) violation at commit time means a concurrent
        # upload/replace committed the same filename between the _norm_exists
        # preflight above and this commit — uploads do NOT take the transfer job
        # slot, so they race an in-flight import. Surface it as an actionable
        # filename conflict (re-running the import dodges the now-committed name)
        # rather than the opaque generic failure. Mirrors the IntegrityError ->
        # duplicateFilename translation in admin_slides.upload_slide/replace_wsi.
        # Match on the driver message ("UNIQUE constraint failed: slides.
        # wsi_filename_norm"), not str(exc): the latter embeds the whole UPDATE
        # statement, whose column list names wsi_filename_norm for ANY
        # constraint failure on the slide row (e.g. a NOT NULL violation).
        if isinstance(exc, IntegrityError) and "wsi_filename_norm" in str(exc.orig):
            raise SlideTransferError(
                f"a WSI named {final_basename!r} was created concurrently during "
                "the import; re-run the import to retry this slide",
                code="importFilenameConflict",
            ) from exc
        # A layout the readers refuse: report the same reason the upload
        # endpoint would (adminDash.apiErr.omeTiff.<reason> / slideLayout.<reason>).
        if isinstance(exc, UnsupportedSlideError):
            raise SlideTransferError(str(exc), code=exc.code) from exc
        if _is_no_space(exc):
            raise SlideTransferError(str(exc), code="importNoSpace") from exc
        raise SlideTransferError(str(exc), code="importSlideFailed") from exc
    finally:
        held.close()

    return {
        "source_public_id": src_pid,
        "slide_id": new_id,
        "public_id": slide.public_id,
        "title": slide.title,
        "wsi_filename": stored_name,
        "renamed": renamed,
        "tours": tours_created,
        "steps": steps_created,
        "assets_skipped": assets_skipped,
        "attributes_skipped": attributes_skipped,
        "credit_skipped": credit_skipped,
        "_state": {
            "thumb_filename": thumb_name,
            "tours": {old: new for _t, old, new in bundled_tours},
            "assets_skipped": skipped_by_tour,
        },
    }


# ---------------------------------------------------------------------------
# Import: apply state (partial-success retry)
# ---------------------------------------------------------------------------
#
# A stage is kept after an apply whenever a retry could still do something — a
# failed slide, or an attachment refused by one of this system's own settings
# (_RETRYABLE_ASSET_REASONS) — so the operator can retry without re-uploading a
# potentially multi-GB bundle. What makes running apply again on the same stage
# safe is ``apply-state.json`` next to the extracted bundle: one record per
# bundled slide, keyed by its bundle public_id, the whole file rewritten
# atomically (tmp + fsync + rename) on every change:
#
#   pending   slide_id, public_id, wsi_filename, thumb_filename, title,
#             renamed, tours {bundle tour id: new id}, new_asset_dirs [new id]
#   imported  the same minus new_asset_dirs, plus
#             assets_skipped {bundle tour id: [{name, reason}]}
#   failed    code, error, params, and — when the attempt had reserved a row
#             — reserved {the pending fields} (see _failed_record)
#   skipped   reason (filename_conflict / public_id_conflict), resolution
#
# plus a top-level ``tour_remap``: every tour id planned so far (see
# _build_tour_remap's ``prefer``).
#
# Crash safety rests on one ordering. "pending" — naming the slide id and
# public_id about to be committed — is durable BEFORE the slide row first
# exists (_reserve_slide_row's before_commit), and "imported" is written after
# the final commit. So for every slide row this stage ever created, the state
# names its id, and _reconcile_apply_state decides by looking at that one row:
#
#   * no row: never committed, or deleted since -> not imported. Files a dead
#     attempt left are removed; a retry imports the slide afresh.
#   * a placeholder (empty wsi_filename) with the recorded public_id: the
#     attempt died between reservation and finalise -> the row and its files
#     are removed, then as above (the bundle public_id is free again, so the
#     retry still preserves it).
#   * a finished row matching the record: imported — also when the process
#     died after the commit but before "imported" was recorded — so a slide is
#     never imported twice.
#
# A crash before the "pending" write leaves nothing behind (the reservation
# had not been flushed). The staged WSI is never consumed (_place_staged_wsi
# links or copies it), so each of those cases can be retried from the stage.

_STATE_STATUSES = frozenset({"pending", "imported", "failed", "skipped"})
_STATE_ERROR_MAX_CHARS = 500


class _StateWriteError(SlideTransferError):
    """The apply state could not be written (a full or failing staging
    volume). Aborts the whole run instead of failing one slide: a slide must
    not be committed without its "pending" record, or a retry after a crash
    could import it twice. What was already imported stays imported and
    recorded; the stage is kept for a retry once the volume is fixed."""


def _apply_state_path(stage_dir: str) -> str:
    return os.path.join(stage_dir, APPLY_STATE_NAME)


def _new_apply_state() -> dict:
    return {"format": _APPLY_STATE_FORMAT, "slides": {}, "tour_remap": {}}


def _plain_name(value: object) -> bool:
    """A bare file name (no directory part, no NUL, not "." / "..")."""
    return (
        isinstance(value, str) and bool(value) and "\x00" not in value
        and os.path.basename(value) == value and value not in (".", "..")
    )


def _valid_state_record(rec: object) -> bool:
    """Structure + safety check of one recorded outcome. The names a record
    holds are later used to delete files (``wsi/``, ``thumbs/``,
    ``assets/tours/``) and to copy into tours, so they are held to exactly
    the shapes this module writes."""
    if not isinstance(rec, dict) or rec.get("status") not in _STATE_STATUSES:
        return False
    if rec["status"] == "failed" and "reserved" in rec:
        # The reservation of the failed attempt (see _failed_record): held to
        # exactly what a "pending" record holds, since a retry deletes by it.
        reserved = rec["reserved"]
        return isinstance(reserved, dict) and _valid_state_record({**reserved, "status": "pending"})
    if rec["status"] in ("failed", "skipped"):
        return True
    sid = rec.get("slide_id")
    if isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0:
        return False
    wsi = rec.get("wsi_filename")
    if not (
        valid_public_id(rec.get("public_id"))
        and _plain_name(wsi) and wsi.startswith(f"{sid}_")
        and _plain_name(rec.get("thumb_filename"))
    ):
        return False
    tours = rec.get("tours")
    if not isinstance(tours, dict) or not all(
        valid_public_id(k) and valid_public_id(v) for k, v in tours.items()
    ):
        return False
    if rec["status"] == "pending":
        dirs = rec.get("new_asset_dirs")
        return isinstance(dirs, list) and all(valid_public_id(d) for d in dirs)
    skipped = rec.get("assets_skipped")
    return isinstance(skipped, dict) and all(
        valid_public_id(k) and isinstance(v, list) and all(
            isinstance(s, dict) and isinstance(s.get("name"), str)
            and isinstance(s.get("reason"), str)
            for s in v
        )
        for k, v in skipped.items()
    )


def _failed_record(code: Optional[str], error: str, params: dict, prior: Optional[dict]) -> dict:
    """The "failed" record of a slide whose attempt raised. ``prior`` is its
    record when the attempt began failing: "pending" once the attempt has
    announced its reservation (_reserve_slide_row's before_commit).

    That reservation is kept, under ``reserved``, whatever the failure: the
    attempt's own cleanup (_import_one_slide's _cleanup) may not have removed
    the placeholder row — its delete or commit failed — and a reservation
    whose commit raised may have landed all the same (a failure after the
    COMMIT, e.g. in the refresh that follows it). Recording a bare failure
    would leave nothing pointing at that row: it would go on holding the
    bundle's public_id, so the retry would remint the slide's public_id and
    break every link to it, and its files would stay until the boot-time
    reaper. With the
    reservation recorded, the next apply removes whatever is left of it
    (_reconcile_apply_state -> _discard_uncommitted, which only touches a row
    still a placeholder and files no row references) before retrying, and
    the public_id is preserved. Harmless when the cleanup did succeed."""
    rec = {
        "status": "failed", "code": code,
        "error": error[:_STATE_ERROR_MAX_CHARS], "params": params,
    }
    if prior is not None and prior.get("status") == "pending":
        rec["reserved"] = {k: v for k, v in prior.items() if k != "status"}
    return rec


def _load_apply_state(stage_dir: str) -> dict:
    """Read a stage's apply state — an empty one when no apply has run yet.

    An unreadable or malformed file is refused rather than read as empty:
    that would forget which slides are already imported and import them a
    second time. (These writes are atomic, so this means tampering or a disk
    fault; the operator discards the stage.)"""
    try:
        with open(_apply_state_path(stage_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _new_apply_state()
    except (OSError, ValueError) as exc:
        raise SlideTransferError(
            f"import apply state unreadable: {exc}", code="importStateUnreadable"
        ) from exc
    slides = data.get("slides") if isinstance(data, dict) else None
    remap = data.get("tour_remap") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != _APPLY_STATE_FORMAT
        or not isinstance(slides, dict)
        or not isinstance(remap, dict)
        or not all(valid_public_id(k) and _valid_state_record(v) for k, v in slides.items())
        or not all(valid_public_id(k) and valid_public_id(v) for k, v in remap.items())
    ):
        raise SlideTransferError(
            "import apply state is malformed", code="importStateUnreadable"
        )
    data["tour_remap"] = remap
    return data


def _save_apply_state(stage_dir: str, state: dict) -> None:
    state["updated_at"] = _backup._utc_iso_z()
    try:
        _backup._atomic_write_json(_apply_state_path(stage_dir), state)
    except OSError as exc:
        raise _StateWriteError(
            f"could not record the import's progress: {exc}",
            code="importStateWriteFailed",
        ) from exc


def _record_matches_row(rec: dict, slide: Slide) -> bool:
    """Whether ``slide`` — the row at the recorded id — is the finished import
    the record describes: a finished row (it has a WSI) carrying the recorded
    public_id. Nothing ever changes a slide's public_id, so that is the
    identity check; the id alone is not enough. A crash after the "pending"
    record but before the reservation committed leaves the id unused (the
    watermark bump rolled back with it), and a later ordinary upload — or a
    whole-DB restore — can put an unrelated slide at that id, even under the
    same ``{id}_{name}`` file name. The file name is deliberately not
    required: a WSI replace after the import may change it."""
    return bool(slide.wsi_filename) and slide.public_id == rec["public_id"]


def _effective_status(db: Session, rec: dict) -> Optional[str]:
    """A record's status as the DB has it now: "imported" only while the row
    it created exists (a "pending" record whose final commit landed counts),
    "failed" / "skipped" as recorded, else None — not imported."""
    if rec["status"] in ("failed", "skipped"):
        return rec["status"]
    slide = db.get(Slide, rec["slide_id"])
    return "imported" if slide is not None and _record_matches_row(rec, slide) else None


def _retryable_asset_count(rec: dict) -> int:
    return sum(
        1
        for skips in (rec.get("assets_skipped") or {}).values()
        for s in skips
        if s.get("reason") in _RETRYABLE_ASSET_REASONS
    )


def _remaining_work(state: dict) -> dict:
    """What a retry of this stage could still do (call on a reconciled
    state); the stage is kept iff either count is non-zero. A failed slide
    always counts, whatever its code: the operator may fix the cause in ways
    this module can't see (free disk, install a reader, edit the staged
    slides.json), and the 24 h sweep / the discard button bound the cost."""
    records = state["slides"].values()
    return {
        "failed_slides": sum(1 for r in records if r["status"] == "failed"),
        "skipped_assets": sum(
            _retryable_asset_count(r) for r in records if r["status"] == "imported"
        ),
    }


def _discard_uncommitted(db: Session, rec: dict, slide: Optional[Slide]) -> None:
    """Remove what an attempt that died before its final commit left behind:
    its placeholder row (only while it is still a placeholder with the
    recorded public_id), the live WSI copy and thumbnail (only while no row
    references them), and the tour asset dirs it created (only while no tour
    owns the id). The boot-time reservation reaper may have removed some of it
    already; everything here tolerates that.

    The recorded slide id may since have been taken by someone else. A
    reservation whose commit never landed (rolled back, e.g. importReserveFailed
    on SQLITE_BUSY) also rolled back the id watermark, so a later ordinary
    upload can reserve the very same id N — and its finalise os.replace's
    wsi/N_<name> (and writes thumbs/N_*) BEFORE its row's wsi_filename is
    set. "No row references the name" would then be true for a file that does
    not belong to the import, and a retry in that window would delete the
    upload's bytes. Two guards:

    * the slide's mutation lock is held across the check and the removal.
      Uploads (and this module's own import) hold it from reservation to the
      final commit or cleanup, so the discard waits for an in-flight
      finalise and then sees its committed row;
    * a row at the recorded id whose public_id is not the recorded one
      means that id's ``{id}_*`` file namespace belongs to that row (a
      placeholder whose finalise has not yet taken the lock, or a finished
      slide), so nothing is removed by name. Whatever this attempt left
      under those names is then an orphan for the file-status view / the
      reaper, never another slide's bytes."""
    with slide_mutation_lock(rec["slide_id"]):
        # Re-read under the lock: the caller's row was loaded before it, and
        # an upload may have committed (or a placeholder appeared) since.
        slide = db.get(Slide, rec["slide_id"], populate_existing=True)
        if slide is not None and slide.public_id != rec["public_id"]:
            _logger.info(
                "import: slide id %s is now held by another slide; leaving its "
                "files alone", rec["slide_id"],
            )
            _discard_new_asset_dirs(db, rec)
            return
        if slide is not None and not slide.wsi_filename:
            try:
                db.delete(slide)
                db.commit()
            except Exception:
                db.rollback()
                raise
        for base, column, name in (
            (_wsi_dir(), Slide.wsi_filename, rec["wsi_filename"]),
            (_thumbs_dir(), Slide.thumb_filename, rec["thumb_filename"]),
        ):
            if db.execute(select(Slide.id).where(column == name)).first() is not None:
                continue
            try:
                os.remove(os.path.join(base, name))
            except FileNotFoundError:
                pass
            except OSError as exc:
                _logger.warning("import: could not remove leftover %s: %s", name, exc)
    _discard_new_asset_dirs(db, rec)


def _discard_new_asset_dirs(db: Session, rec: dict) -> None:
    """The tour asset dirs a dead attempt created, removed while no tour owns
    the id. Named by public ids this attempt minted, so — unlike the
    ``{slide_id}_*`` files — they never belong to a slide that later took
    the recorded id."""
    for pid in rec.get("new_asset_dirs") or []:
        if db.execute(select(Tour.id).where(Tour.public_id == pid)).first() is None:
            shutil.rmtree(tour_asset_dir(pid), ignore_errors=True)


def _recover_assets_skipped(db: Session, stage_dir: str, rec: dict) -> dict:
    """The skipped-attachment lists of an import whose outcome was never
    recorded. Its copy pass had finished (it runs before the final commit),
    so a dry run of that pass reports what it skipped. Only a dry run: the
    slide has been live since its commit, and an attachment an author
    deleted meanwhile must stay deleted, not be copied back from the
    stage."""
    out: dict = {}
    tour_ids = {
        pid: tid for tid, pid in db.execute(
            select(Tour.id, Tour.public_id).where(Tour.slide_id == rec["slide_id"])
        ).all()
    }
    for old, new in rec["tours"].items():
        src = _staged_assets_dir(stage_dir, old)
        tid = tour_ids.get(new)
        if not src or tid is None:
            continue
        with tour_asset_lock(tid):
            skips = _import_tour_assets(src, tour_asset_dir(new), dry_run=True)
        if skips:
            out[old] = skips
    return out


def _reconcile_apply_state(db: Session, stage_dir: str, state: dict) -> bool:
    """Bring the recorded outcomes in line with the DB before an apply or an
    attachment retry acts on them (see the section comment above). Runs in
    the job slot, so no other import is creating rows for this stage.
    Returns whether anything changed; the caller saves."""
    changed = False
    records = state["slides"]
    for src_pid, rec in list(records.items()):
        if rec["status"] == "failed" and "reserved" in rec:
            # A failed attempt's reservation (see _failed_record). Read like
            # a "pending" record: a placeholder (or files) it left behind are
            # removed, keeping the failure; should its final commit have
            # landed after all, the slide is live and recorded as imported.
            failed_rec = {k: v for k, v in rec.items() if k != "reserved"}
            rec = {**rec["reserved"], "status": "pending"}
            slide = db.get(Slide, rec["slide_id"])
            if slide is None or not _record_matches_row(rec, slide):
                _discard_uncommitted(db, rec, slide)
                records[src_pid] = failed_rec
                changed = True
                continue
        elif rec["status"] not in ("pending", "imported"):
            continue
        slide = db.get(Slide, rec["slide_id"])
        if slide is not None and _record_matches_row(rec, slide):
            if rec["status"] == "pending":
                _logger.warning(
                    "import: slide %s was imported as %s but not recorded "
                    "(interrupted apply); recording it now",
                    src_pid, rec["slide_id"],
                )
                promoted = {k: v for k, v in rec.items() if k != "new_asset_dirs"}
                promoted["status"] = "imported"
                promoted["assets_skipped"] = _recover_assets_skipped(db, stage_dir, rec)
                records[src_pid] = promoted
                changed = True
            continue
        if rec["status"] == "pending":
            _discard_uncommitted(db, rec, slide)
        else:
            _logger.info(
                "import: slide %s imported earlier as %s no longer exists; "
                "it can be imported again", src_pid, rec["slide_id"],
            )
        del records[src_pid]
        changed = True
    return changed


def _read_stage(stage_id: str) -> tuple:
    """Locate a stage and re-read its manifest + slides.json from disk;
    returns ``(stage_dir, manifest, slides_doc)``. Callers re-validate the doc
    rather than trusting what staging checked: the files on disk are a TOCTOU
    surface, and a kept stage may be applied again hours later."""
    if not _IMPORT_ID_RE.match(stage_id or ""):
        raise SlideTransferError("import stage not found (re-upload the bundle)", code="importStageMissing")
    stage_dir = _stage_dir(stage_id)
    if not os.path.isdir(stage_dir):
        raise SlideTransferError("import stage not found (re-upload the bundle)", code="importStageMissing")
    try:
        manifest = _load_bundle_json(
            os.path.join(stage_dir, MANIFEST_NAME), _MAX_MANIFEST_JSON_BYTES,
        )
        slides_doc = _load_bundle_json(
            os.path.join(stage_dir, SLIDES_DOC_NAME), _MAX_METADATA_JSON_BYTES,
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise SlideTransferError(f"staged bundle unreadable: {exc}", code="importStageMissing") from exc
    if not isinstance(manifest, dict):
        raise _invalid_bundle("malformed manifest.json")
    return stage_dir, manifest, slides_doc


def _touch_stage(stage_dir: str) -> None:
    # Keep the stage dir's mtime fresh while it is worked on, for the same
    # reason create_backup touches its snapshot tmpdir: ``sweep_stale_staging``
    # reclaims ``import-*`` by mtime, and this dir's mtime otherwise stays at
    # extraction time. A bundle staged yesterday and applied today would otherwise be
    # rmtree'd mid-apply by any sibling call that sweeps (another restore /
    # import upload, or a worker boot) — the sweep runs outside the job slot —
    # leaving the batch half-imported. Best-effort: a failed touch just leaves
    # that window unmitigated.
    try:
        os.utime(stage_dir)
    except OSError:
        pass


def _finish_stage(stage_id: str, stage_dir: str, state: dict) -> tuple:
    """Keep the stage while a retry could still do something, else remove
    it. Returns ``(stage_kept, remaining)``.

    A kept stage is touched once more so the 24 h staging sweep counts from
    this run: the operator gets the full window to retry after each attempt,
    and an abandoned stage still goes a day after its last one. A batch that
    only skipped (filename conflicts) or only lost attachments to their own
    defects is removed — retrying it would change nothing."""
    remaining = _remaining_work(state)
    keep = remaining["failed_slides"] > 0 or remaining["skipped_assets"] > 0
    if keep:
        _touch_stage(stage_dir)
    else:
        cleanup_import_stage(stage_id)
    return keep, remaining


def _invalidate_slide_meta() -> None:
    # New slides may be published; refresh the cross-worker slide-meta cache
    # so the /dzi/ tile path resolves them immediately (mirrors the
    # upload/delete invalidation in admin_slides).
    try:
        from . import slide_resolve_cache
        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        _logger.exception("could not invalidate slide-meta cache after import")


def apply_import(
    stage_id: str,
    resolutions: Optional[dict] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Apply a staged slide-export bundle additively. Runs in a background
    thread with its own session; never restarts the service.

    May run again on the same stage (a retry; see "Import: apply state"
    above): a slide an earlier run imported is left alone while the slide it
    created still exists; a slide it skipped stays skipped unless
    ``resolutions`` names it; failed and never-attempted slides are attempted,
    with ``resolutions`` (default copy). The stage is kept while a retry could
    still do something (``stage_kept`` / ``retry`` in the result).
    """
    stage_dir, manifest, slides_doc = _read_stage(stage_id)

    # Re-guard the version at apply time (the staged files could have been
    # swapped between stage and apply). Same rule as plan_import via the
    # shared helper so preflight and apply never disagree.
    block_code, block_reason = _import_version_block(manifest)
    if block_code is not None:
        raise SlideTransferError(block_reason, code="importVersionMismatch")

    # Re-validate the re-read doc (structure, count cap, and public_id safety):
    # apply reads slides.json from disk independently of stage_import, a TOCTOU
    # surface, and _build_tour_remap / src_pid below run OUTSIDE the per-slide
    # try — a non-dict entry or unsafe id here would otherwise abort the whole
    # batch instead of failing one slide.
    slides = _validate_slides_doc(slides_doc)
    resolutions = resolutions or {}
    total = len(slides)
    imported: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    already: list[dict] = []

    def _pid(sd: dict) -> str:
        return sd.get("public_id", "") or ""

    db = SessionLocal()
    try:
        _touch_stage(stage_dir)
        state = _load_apply_state(stage_dir)
        records = state["slides"]
        if _reconcile_apply_state(db, stage_dir, state):
            _save_apply_state(stage_dir, state)

        def _to_attempt(pid: str) -> bool:
            rec = records.get(pid)
            if rec is None or rec["status"] == "failed":
                return True
            if rec["status"] == "skipped":
                return pid in resolutions
            return False  # imported (and still there, after reconciling)

        attempt = {_pid(sd) for sd in slides if _to_attempt(_pid(sd))}

        # Map tour ids only for slides that will actually be imported. A
        # slide skipped over its filename conflict creates no tour, so a
        # remap entry for its tours would rewrite another slide's markdown
        # reference to them (e.g. a shared image, still present on the
        # target under the original tour id) to a directory that is never
        # created. Entries are also dropped below for a slide that ends up
        # skipped or failed after all; anything still dangling is reported
        # by the reference check at the end. Slides an earlier run imported
        # contribute the ids they were actually given, so a retried slide's
        # references into them are rewritten too.
        will_skip = {
            _pid(sd)
            for sd in slides
            if _pid(sd) in attempt
            and resolutions.get(_pid(sd), "copy") == "skip"
            and _conflict_reason(db, sd) is not None
        }
        tour_remap: dict = {}
        for rec in records.values():
            if rec["status"] == "imported":
                tour_remap.update(rec["tours"])
        planned = _build_tour_remap(
            db,
            [sd for sd in slides if _pid(sd) in attempt and _pid(sd) not in will_skip],
            prefer=state["tour_remap"],
        )
        state["tour_remap"].update(planned)
        tour_remap.update(planned)

        def _forget_tours(sd: dict) -> None:
            for t in sd.get("tours", []) or []:
                tour_remap.pop(t.get("public_id", "") or "", None)

        for idx, sd in enumerate(slides):
            src_pid = _pid(sd)
            rec = records.get(src_pid)
            if src_pid not in attempt:
                if rec["status"] == "imported":
                    already.append({
                        "source_public_id": src_pid,
                        "slide_id": rec["slide_id"],
                        "public_id": rec["public_id"],
                        "title": rec.get("title") or "",
                    })
                else:
                    skipped.append({"public_id": src_pid, "reason": rec.get("reason") or "filename_conflict"})
                if progress_cb is not None:
                    progress_cb({"phase": "importing", "done": idx + 1, "total": total})
                continue

            resolution = resolutions.get(src_pid, "copy")

            def _on_reserved(info: dict, src_pid: str = src_pid) -> None:
                records[src_pid] = {"status": "pending", **info}
                _save_apply_state(stage_dir, state)

            try:
                result = _import_one_slide(
                    db, stage_dir, sd, tour_remap, resolution, on_reserved=_on_reserved,
                )
                if result is None:
                    _forget_tours(sd)
                    reason = _conflict_reason(db, sd) or "filename_conflict"
                    skipped.append({"public_id": src_pid, "reason": reason})
                    records[src_pid] = {
                        "status": "skipped", "reason": reason,
                        "resolution": resolution,
                    }
                else:
                    # The slide is committed (possibly published, under a kept
                    # public_id a worker may have cached as a draft placeholder
                    # meanwhile): make it current on every worker now, not only
                    # once the whole batch is through.
                    _invalidate_slide_meta()
                    bits = result.pop("_state")
                    imported.append(result)
                    records[src_pid] = {
                        "status": "imported",
                        "slide_id": result["slide_id"],
                        "public_id": result["public_id"],
                        "wsi_filename": result["wsi_filename"],
                        "thumb_filename": bits["thumb_filename"],
                        "title": result["title"],
                        "renamed": result["renamed"],
                        "tours": bits["tours"],
                        "assets_skipped": bits["assets_skipped"],
                    }
            except _StateWriteError:
                raise
            except SlideTransferError as exc:
                _forget_tours(sd)
                prior = records.get(src_pid)
                # Roll back before continuing so a failure that originated
                # OUTSIDE _import_one_slide's own try (e.g. an OperationalError
                # in reserve_next_slide_id's SELECTs) can't leave the shared
                # batch session in a needs-rollback state and poison every
                # remaining slide. Idempotent for in-try failures (already
                # rolled back).
                db.rollback()
                # A layout the readers refuse is the file's, not a server
                # fault: INFO, as on the upload path, keeps it off the admin
                # error-log screen.
                log = (
                    _logger.info if (exc.code or "").startswith(("omeTiff.", "slideLayout."))
                    else _logger.warning
                )
                log("import: slide %s failed: %s", src_pid, exc)
                failed.append({
                    "public_id": src_pid, "error": str(exc), "code": exc.code,
                    "params": exc.params,
                })
                records[src_pid] = _failed_record(exc.code, str(exc), exc.params, prior)
            except Exception as exc:  # noqa: BLE001
                _forget_tours(sd)
                prior = records.get(src_pid)
                db.rollback()
                _logger.exception("import: slide %s failed", src_pid)
                failed.append({"public_id": src_pid, "error": str(exc)})
                records[src_pid] = _failed_record(None, str(exc), {}, prior)
            _save_apply_state(stage_dir, state)
            _touch_stage(stage_dir)
            if progress_cb is not None:
                progress_cb({"phase": "importing", "done": idx + 1, "total": total})

        # Bundle-wide first: fields of a newer exporter that nothing here
        # could carry over.
        warnings = [
            {"public_id": "", "code": "importUnknownField", "params": {"field": name}}
            for name in _unknown_bundle_fields(slides)
        ]
        warnings += _import_warnings(db, imported)
    finally:
        db.close()
        # Also after a run aborted part-way: what it imported is live.
        _invalidate_slide_meta()

    # The per-slide skip lists are reported through the (capped) warnings
    # only; left on the results they would bloat the job record unbounded.
    for res in imported:
        for key in ("assets_skipped", "attributes_skipped", "credit_skipped"):
            res.pop(key, None)
    stage_kept, remaining = _finish_stage(stage_id, stage_dir, state)
    return {
        "stage_id": stage_id,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        # Imported by an earlier apply of this stage and left alone.
        "already_imported": already,
        "already_imported_count": len(already),
        # Bounded so a bundle with thousands of problems can't bloat the job
        # record; warning_count keeps the true total.
        "warnings": warnings[:_IMPORT_MAX_WARNINGS],
        "warning_count": len(warnings),
        "stage_kept": stage_kept,
        "retry": remaining,
    }


def retry_import_assets(
    stage_id: str,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Copy the attachments an earlier apply of this stage skipped because of
    one of this system's settings (_RETRYABLE_ASSET_REASONS) into the tours
    that apply created — once the admin has raised the limit, widened the
    type allow-list or freed the tour's quota — without re-importing anything
    else. Runs in the job slot like an apply, with its own session.

    Only attachments recorded as skipped are retried, never every staged file
    the tour lacks: one an author deleted after the import stays deleted.
    Each goes through _import_tour_assets' checks with the tour's current
    contents counted against its limits, under the tour's tour_asset_lock
    (the tour is live; an author may be uploading to it). The tour is found by
    the id the import gave it — reminted on a collision, hence the recorded
    mapping — and only while it still belongs to the slide the import
    created; a deleted tour has nothing left to retry. The reference check is
    re-run for the affected slides afterwards, so the warnings returned
    reflect what now resolves.
    """
    stage_dir, manifest, slides_doc = _read_stage(stage_id)
    block_code, block_reason = _import_version_block(manifest)
    if block_code is not None:
        raise SlideTransferError(block_reason, code="importVersionMismatch")
    slides = _validate_slides_doc(slides_doc)
    by_pid = {sd.get("public_id", "") or "": sd for sd in slides}

    copied: list = []
    still_skipped = 0
    affected: list = []
    db = SessionLocal()
    try:
        _touch_stage(stage_dir)
        state = _load_apply_state(stage_dir)
        records = state["slides"]
        if not records:
            raise SlideTransferError(
                "this bundle has not been applied yet; there is nothing to retry",
                code="importNothingToRetry",
            )
        if _reconcile_apply_state(db, stage_dir, state):
            _save_apply_state(stage_dir, state)

        work = []
        for src_pid, rec in records.items():
            if rec["status"] != "imported" or src_pid not in by_pid:
                continue
            names = {
                old: {s["name"] for s in skips if s["reason"] in _RETRYABLE_ASSET_REASONS}
                for old, skips in rec["assets_skipped"].items()
            }
            names = {old: n for old, n in names.items() if n}
            if names:
                work.append((src_pid, rec, names))
        total = sum(len(names) for _pid, _rec, names in work)
        done = 0
        if progress_cb is not None:
            progress_cb({"phase": "copying_assets", "done": 0, "total": total})

        for src_pid, rec, names in work:
            titles = {
                t.get("public_id"): (t.get("title") or "")
                for t in by_pid[src_pid].get("tours", []) or []
            }
            tour_ids = {
                pid: tid for tid, pid in db.execute(
                    select(Tour.id, Tour.public_id).where(Tour.slide_id == rec["slide_id"])
                ).all()
            }
            for old, retry_names in names.items():
                new = rec["tours"].get(old)
                tid = tour_ids.get(new) if new else None
                src = _staged_assets_dir(stage_dir, old)
                # Reasons a retry can't change stay on the record as they were.
                kept = [
                    s for s in rec["assets_skipped"][old]
                    if s["reason"] not in _RETRYABLE_ASSET_REASONS
                ]
                again: list = []
                if tid is not None and src:
                    got: list = []
                    with tour_asset_lock(tid):
                        again = _import_tour_assets(
                            src, tour_asset_dir(new), only=retry_names, copied=got,
                        )
                    copied.extend(
                        {"public_id": src_pid, "tour": titles.get(old) or new, "name": n}
                        for n in got
                    )
                    still_skipped += len(again)
                if kept or again:
                    rec["assets_skipped"][old] = kept + again
                else:
                    rec["assets_skipped"].pop(old, None)
                done += 1
                _touch_stage(stage_dir)
                if progress_cb is not None:
                    progress_cb({"phase": "copying_assets", "done": done, "total": total})
            _save_apply_state(stage_dir, state)
            affected.append({
                "source_public_id": src_pid,
                "slide_id": rec["slide_id"],
                "assets_skipped": [
                    {"tour": titles.get(old) or rec["tours"].get(old, old), **s}
                    for old, skips in rec["assets_skipped"].items()
                    for s in skips
                ],
            })
        warnings = _import_warnings(db, affected)
    finally:
        db.close()

    stage_kept, remaining = _finish_stage(stage_id, stage_dir, state)
    return {
        "stage_id": stage_id,
        "copied": copied[:_IMPORT_MAX_WARNINGS],
        "copied_count": len(copied),
        # Retried attachments refused again (a limit still too tight).
        "still_skipped_count": still_skipped,
        "warnings": warnings[:_IMPORT_MAX_WARNINGS],
        "warning_count": len(warnings),
        "stage_kept": stage_kept,
        "retry": remaining,
    }


_IMPORT_MAX_WARNINGS = 200

# A local tour-asset reference in step markdown: /assets/tours/<pid>/<name>.
_ASSET_REF_RE = re.compile(r"/assets/tours/([A-Za-z0-9_-]{1,32})/([^\s)\]\"'<>?#]+)")


def _import_warnings(db: Session, imported: list) -> list:
    """What an otherwise successful import did not carry over.

    Per imported slide: metadata attributes and image-credit fields the
    editors would refuse (``importAttributeSkipped`` /
    ``importCreditSkipped``), attachments the target's checks or limits
    refused (``importAssetSkipped``), and step markdown that references a
    tour asset which does not exist on the target after the whole batch
    (``importAssetRefMissing``) — a refused attachment, a reference into a
    slide that was skipped or failed, or one the bundle never contained.
    The latter is checked against the files on disk rather than the remap,
    because only the files say whether an image will actually load.
    """
    warnings: list = []
    for res in imported:
        src_pid = res["source_public_id"]
        for key in res.get("attributes_skipped") or []:
            warnings.append({
                "public_id": src_pid, "code": "importAttributeSkipped",
                "params": {"key": key[:ATTR_KEY_MAX_LEN]},
            })
        for key in res.get("credit_skipped") or []:
            warnings.append({
                "public_id": src_pid, "code": "importCreditSkipped", "params": {"field": key},
            })
        for skip in res.get("assets_skipped") or []:
            warnings.append({
                "public_id": src_pid, "code": "importAssetSkipped",
                "params": {"tour": skip["tour"], "name": skip["name"], "reason": skip["reason"]},
            })
        rows = db.execute(
            select(Tour.title, Step.order, Step.description_md)
            .join(Step, Step.tour_id == Tour.id)
            .where(Tour.slide_id == res["slide_id"])
            .order_by(Tour.id, Step.order)
        ).all()
        for tour_title, order, md in rows:
            seen: set = set()
            for m in _ASSET_REF_RE.finditer(md or ""):
                ref_pid, name = m.group(1), unquote(m.group(2))
                url = f"/assets/tours/{ref_pid}/{m.group(2)}"
                if url in seen:
                    continue
                seen.add(url)
                path = os.path.join(tour_asset_dir(ref_pid), name)
                if storable_asset_name(name) and os.path.isfile(path):
                    continue
                warnings.append({
                    "public_id": src_pid, "code": "importAssetRefMissing",
                    "params": {"tour": tour_title or "", "step": order, "url": url},
                })
    return warnings


def cleanup_import_stage(stage_id: str) -> None:
    """Remove a staged import tree. Regex-guarded against path traversal."""
    if not _IMPORT_ID_RE.match(stage_id or ""):
        return
    shutil.rmtree(_stage_dir(stage_id), ignore_errors=True)
