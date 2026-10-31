"""Shared utilities for admin API routers.

Centralises helpers used across the routers/ subpackage (slides, tours,
settings, ...).
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import disk_space as _disk_space
from .audit_log import persist_audit_event
from .config import settings
from .models import (
    AppMeta,
    Slide,
    SlideAttribute,
    Tour,
    TourAttribute,
    User,
)
from .openslide_utils import read_associated_images

audit_logger = logging.getLogger("wsi_app.audit")
logger = logging.getLogger("wsi_app.admin_helpers")


# Per-slide mutation lock (see ``slide_mutation_lock``). Held by replace_wsi,
# delete, publish and export so concurrent actions on the same slide_id can't
# interleave their on-disk swaps. Without it two concurrent replace_wsi calls
# observe the same ``old_wsi_filename`` snapshot before either commits, race
# their os.replace() into the wsi/ directory, and orphan the loser's bytes on
# disk (the winner's DB row points at its own file, while the loser's "old"
# delete is a FileNotFoundError no-op).
#
# These maps are only the in-process tier: production runs several uvicorn
# workers (see infra/systemd/start-wsi-app.sh), so ``slide_mutation_lock``
# layers a per-slide ``fcntl.flock`` under ``data_dir/locks`` on top to
# serialise across workers too.
_SLIDE_LOCKS_GUARD = threading.Lock()
_SLIDE_LOCKS: dict[int, threading.Lock] = {}


# app_meta key tracking the highest slide id ever assigned. See
# ``reserve_next_slide_id`` for the motivation; do not rename without
# migrating existing rows.
_SLIDE_ID_HWM_KEY = "slides_id_hwm"


def _read_slide_id_hwm(db: Session) -> int:
    row = db.get(AppMeta, _SLIDE_ID_HWM_KEY)
    if row is None or not row.value:
        return 0
    try:
        return int(row.value)
    except ValueError:
        # Corrupt or hand-edited value: ignore. ``reserve_next_slide_id``
        # falls back to ``max(slides.id)`` and overwrites with a fresh
        # integer on the next assignment, so the column self-heals.
        return 0


def reserve_next_slide_id(db: Session) -> int:
    """Reserve a fresh slide id that is strictly greater than every id
    ever assigned, even ids whose rows have since been deleted.

    SQLite ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT`` recycles
    row ids — the rowid algorithm picks ``max(rowid) + 1`` of *existing*
    rows, so deleting the only slide and uploading a new one lands on
    the same id as the deleted slide. That collision survives in:

      * the nginx /dzi/ cache (30-day TTL keyed by URL), serving the
        deleted slide's descriptor + tiles under the reused id.
      * each uvicorn worker's in-process ``_dz_cache``, which still
        holds the deleted slide's OpenSlide handle until LRU eviction.

    Both manifest in the public viewer as "I deleted slide X and
    uploaded slide Y but the viewer still shows X". Tracking a
    monotonic high-watermark in ``app_meta`` sidesteps the SQLite
    rowid reuse and works without a schema migration.

    Concurrency: two workers can read the same watermark before either
    commits. The losing INSERT will hit a UNIQUE constraint on
    ``slides.id``; callers should retry the reservation.
    """
    current_max = db.execute(select(func.max(Slide.id))).scalar() or 0
    hwm = _read_slide_id_hwm(db)
    new_id = max(current_max, hwm) + 1
    row = db.get(AppMeta, _SLIDE_ID_HWM_KEY)
    if row is None:
        db.add(AppMeta(key=_SLIDE_ID_HWM_KEY, value=str(new_id)))
    else:
        row.value = str(new_id)
    return new_id


# The exact alphabet ``secrets.token_urlsafe`` produces ([A-Za-z0-9_-]),
# bounded to the public_id column width (String(32)). Anything else — a "../"
# traversal, a "/" separator, an over-long value — is rejected. Canonical
# definition: slide_transfer imports this rather than keeping its own copy.
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")


def valid_public_id(pid: object) -> bool:
    """True iff ``pid`` is a well-formed public_id token (see PUBLIC_ID_RE)."""
    return isinstance(pid, str) and bool(PUBLIC_ID_RE.match(pid))


def staff_username_taken(db: Session, username: str, *, exclude_id: Optional[int] = None) -> bool:
    """True if another staff account's name equals ``username`` ignoring case.

    Folded with Python's ``str.lower`` (the analytics identity's
    normalisation, see ``models.auth_username_key``) rather than SQLite's
    ``lower()``, which only folds ASCII and so would let "Älice" sit beside
    "älice". The staff table is a handful of rows, so a full scan is fine."""
    key = (username or "").strip().lower()
    return any(
        uid != exclude_id and (name or "").strip().lower() == key
        for uid, name in db.execute(select(User.id, User.username))
    )


def mint_public_id() -> str:
    """Mint an unguessable, URL-safe public identifier for a slide or tour.

    ``secrets.token_urlsafe(8)`` yields a 64-bit, 11-char token — short URLs
    that are still infeasible to guess (the enumeration surface is nginx
    rate-limited to 120 req/min/IP). Must stay in sync with
    ``models._mint_public_id``. Called at row-creation
    time; the creation sites wrap this in a retry-on-IntegrityError loop so a
    (negligible) unique collision on ``public_id`` re-mints instead of 500-ing.
    """
    return secrets.token_urlsafe(8)


def resolve_slide_by_public_id(db: Session, public_id: str) -> Slide:
    """Return the slide with this public_id, or raise 404.

    Public read surfaces identify slides ONLY by this token — never the numeric
    PK — so an unlisted slide can't be found by walking sequential ids. A NULL
    public_id (only possible in a restored or hand-edited DB) never matches
    a real token.
    """
    slide = db.execute(
        select(Slide).where(Slide.public_id == public_id)
    ).scalar_one_or_none()
    if slide is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return slide


def resolve_tour_by_public_id(db: Session, public_id: str) -> Tour:
    """Return the tour with this public_id, or raise 404. See resolve_slide_by_public_id."""
    tour = db.execute(
        select(Tour).where(Tour.public_id == public_id)
    ).scalar_one_or_none()
    if tour is None:
        raise HTTPException(status_code=404, detail="Tour not found")
    return tour


def _slide_lock_path(slide_id: int) -> str:
    return os.path.join(settings.data_dir, "locks", f"slide-{int(slide_id)}.lock")


def _slide_thread_lock(slide_id: int) -> threading.Lock:
    """Return this process's lock object for ``slide_id``.

    The map grows monotonically — locks are intentionally not evicted because
    a slide that was uploaded once may be re-edited many times over the
    install's lifetime, and an evicted-then-recreated lock would let a
    concurrent caller acquire a *different* lock object than the one held by
    the in-flight mutation. The total number of slides per install is
    bounded by storage, so the map's memory footprint is negligible.
    """
    with _SLIDE_LOCKS_GUARD:
        lock = _SLIDE_LOCKS.get(slide_id)
        if lock is None:
            lock = threading.Lock()
            _SLIDE_LOCKS[slide_id] = lock
        return lock


@contextmanager
def slide_mutation_lock(slide_id: int):
    """Serialise mutations of one slide across every worker on the host.

    Callers rely on this for more than tidiness: the publish/unpublish gate
    holds it across ``_wsi_file_present`` → commit so a concurrent delete can't
    leave a slide published with no backing file, delete and replace hold it so
    two swaps can't orphan each other's bytes, and the export job holds it
    while it re-reads a slide's row and opens its file, so the metadata it
    writes describes the bytes it then archives.

    A bare ``threading.Lock`` could not deliver any of that: production runs
    ``uvicorn --workers N`` (see infra/systemd/start-wsi-app.sh), so a bare
    thread lock would never exclude a mutation handled by another worker
    process. A host-local ``fcntl.flock`` — the same primitive as
    ``jobs._cross_worker_lock`` and ``main._startup_schema_lock``, with lock
    files under the ``data_dir`` every worker on the host shares — is layered
    on top of the in-process lock. Taking the thread lock first keeps the
    acquisition order uniform, so the two tiers cannot deadlock against each
    other.

    Best-effort, matching ``_startup_schema_lock``: if the lock file can't be
    created or locked (unwritable ``data_dir``, fd exhaustion) this degrades to
    in-process-only serialisation rather than failing the mutation.

    One small lock file per slide accumulates under ``data_dir/locks``; the
    count is bounded by the slide count for the same reason the lock map is.
    """
    with _host_exclusive_lock(
        _slide_thread_lock(slide_id), _slide_lock_path(slide_id), f"slide {slide_id}"
    ):
        yield


@contextmanager
def _host_exclusive_lock(thread_lock: threading.Lock, lock_path: str, what: str):
    """The two-tier lock behind :func:`slide_mutation_lock`: ``thread_lock``
    for this process, then an ``fcntl.flock`` on ``lock_path`` for the other
    workers. Degrades to the thread tier alone (with a warning) when the lock
    file can't be used — see slide_mutation_lock."""
    with thread_lock:
        lock_fd = None
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            logger.warning(
                "could not acquire cross-worker lock for %s; proceeding "
                "with in-process serialisation only", what, exc_info=True,
            )
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
        try:
            yield
        finally:
            if lock_fd is not None:
                # close() alone releases the flock; unlock explicitly so the
                # release is not implicit in the fd's lifetime.
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)


_TOUR_ASSET_LOCKS_GUARD = threading.Lock()
_TOUR_ASSET_LOCKS: dict[int, threading.Lock] = {}


@contextmanager
def tour_asset_lock(tour_id: int):
    """Serialise one tour's asset uploads across every worker on the host.

    The per-tour file-count / byte quotas are a check on the directory
    followed by a write into it; two uploads that both check before either
    writes each see the same headroom and together overshoot the cap. The
    request body is already spooled by the time the handler runs, so holding
    this across check → copy → validate → publish costs one local file copy,
    and only uploads to the same tour ever wait on each other. Same two-tier
    shape (and the same degraded mode) as :func:`slide_mutation_lock`."""
    with _TOUR_ASSET_LOCKS_GUARD:
        thread_lock = _TOUR_ASSET_LOCKS.setdefault(int(tour_id), threading.Lock())
    lock_path = os.path.join(settings.data_dir, "locks", f"tour-assets-{int(tour_id)}.lock")
    with _host_exclusive_lock(thread_lock, lock_path, f"tour {tour_id} assets"):
        yield


@contextmanager
def try_slide_mutation_lock(slide_id: int):
    """Non-blocking :func:`slide_mutation_lock`; yields whether it was acquired.

    For callers that must never *wait* on a slide but also must not act on one
    another worker is mutating — the boot-time orphan reaper being the case this
    exists for. Blocking there would stall lifespan startup behind an in-flight
    multi-GB upload on a sibling worker; ignoring the lock would delete that
    upload's staged bytes out from under it.

    Yields False when the lock is held elsewhere (caller should skip the slide).
    A failure to even create the lock file degrades to yielding True with
    in-process serialisation only, matching :func:`slide_mutation_lock`.
    """
    thread_lock = _slide_thread_lock(slide_id)
    if not thread_lock.acquire(blocking=False):
        # Another thread in THIS process holds it; nothing acquired, nothing to
        # release. Returning before the try below keeps the release paths to
        # exactly one.
        yield False
        return
    # Release the thread lock exactly once, in exactly one finally: a second
    # release path would either raise "release unlocked lock" out of the
    # reaper or free a lock another thread holds, putting two callers inside
    # the critical section at once.
    try:
        lock_fd = None
        acquired = True
        try:
            os.makedirs(os.path.join(settings.data_dir, "locks"), exist_ok=True)
            lock_fd = os.open(_slide_lock_path(slide_id), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held by another process — the whole point of the check.
            acquired = False
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
        except OSError:
            logger.warning(
                "could not probe the cross-worker lock for slide %s; proceeding "
                "with in-process serialisation only", slide_id, exc_info=True,
            )
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
        try:
            yield acquired
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
    finally:
        thread_lock.release()


# Field-length limits. Mirror the String(...) lengths declared on the
# matching model columns; bump these here AND in models.py if you ever
# widen them, otherwise SQLite would silently truncate / SQLAlchemy would
# reject on flush.
CATALOG_ID_MAX_LEN = 128       # Slide.catalog_id
TITLE_MAX_LEN = 255            # Slide.title
ATTR_KEY_MAX_LEN = 128         # Slide/TourAttribute.key, MetadataOption.key
ATTR_VALUE_MAX_LEN = 255       # Slide/TourAttribute.value, MetadataOption.value


BASE_FILTER_KEYS = ["stain", "organ", "lab", "tags"]

CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def error_detail(code: str, **params) -> Dict[str, object]:
    """Build a language-independent error payload for an HTTPException's detail.

    Returns a stable error *code* plus interpolation ``params`` rather than a
    human-readable string, so the frontend can translate ``{code, params}``
    through its i18n bundle (``parseApiError`` → toast) and render messages in
    the admin's active locale. ``code`` is an i18n key (e.g.
    ``"adminDash.apiErr.saveFailed"``); ``params`` values must be
    JSON-serialisable scalars.
    """
    detail: Dict[str, object] = {"code": code}
    if params:
        detail["params"] = params
    return detail


def api_error(status_code: int, code: str, **params) -> HTTPException:
    """Convenience wrapper, e.g. ``raise api_error(422, code, count=n)``."""
    return HTTPException(status_code=status_code, detail=error_detail(code, **params))


def audit(actor: Optional[User], action: str, target: Optional[str] = None, **extra) -> None:
    """Emit a structured admin audit record at INFO level.

    The record is JSON-serialized so log shippers (or `journalctl -o json`)
    can parse it without an extra format. Audit instrumentation must never
    break an endpoint, but a silently-dropped audit event is itself a
    security issue: an attacker who can trigger a serialization error
    erases their tracks. So a fallback INFO line is always logged *and* a
    higher-severity warning that includes enough context to correlate
    with the event that triggered the failure.

    The event is also persisted to the ``audit_logs`` table for the admin
    history UI (best-effort; the log line above is the primary record).
    ``**extra`` therefore lands in the DB: never pass passwords, tokens,
    or other secrets.
    """
    # Snapshot actor scalars once: the actor may be a detached ORM instance
    # (e.g. the background-thread backup.create audit), and both the log
    # payload and the DB row need the same values.
    actor_id = getattr(actor, "id", None)
    actor_username = getattr(actor, "username", None)
    actor_role = getattr(actor, "role", None)
    payload = {
        "event": "admin_audit",
        "action": action,
        "actor_id": actor_id,
        "actor_username": actor_username,
        "target": target,
    }
    if extra:
        payload.update(extra)
    try:
        audit_logger.info(json.dumps(payload, default=str, ensure_ascii=False))
    except Exception:
        audit_logger.exception(
            "audit emit failed; falling back to repr() — action=%s target=%s",
            action,
            target,
        )
        # Fallback: try once more without the user payload (which is the
        # most common source of un-serialisable values) so the action name
        # and target at least make it into the log.
        try:
            audit_logger.info(
                "admin_audit_fallback action=%s target=%s actor_id=%s",
                action,
                target,
                actor_id,
            )
        except Exception:
            # Logging itself is broken; nothing left to do that wouldn't
            # risk masking the original endpoint error.
            pass
    persist_audit_event(actor_id, actor_username, actor_role, action, target, extra)


def validate_slide_metadata(catalog_id: str, title: str, attrs: dict) -> None:
    for field, max_len, val in (
        ("catalog_id", CATALOG_ID_MAX_LEN, catalog_id or ""),
        ("title",      TITLE_MAX_LEN,      title      or ""),
    ):
        if len(val) > max_len:
            raise api_error(
                422, "adminDash.validationMaxLen", field=field, max=max_len, len=len(val)
            )
        if CTRL_CHAR_RE.search(val):
            raise api_error(422, "adminDash.validationCtrlChar", field=field)
    for key, vals in attrs.items():
        validate_attribute_kv(key, vals)


def validate_attribute_kv(key: str, values: List[str]) -> None:
    """Validate a single attribute key and its values.

    Factored out of validate_slide_metadata so the bulk-attribute endpoint
    enforces the exact same length / control-char / reserved-char rules as
    the single-slide metadata editor.

    The single-slide metadata PATCH parses attributes from raw JSON, so a
    caller can supply a key whose value is a bare string (``{"k": "HE"}``) or
    a non-iterable (``{"k": null}``). Without an explicit type guard the
    former would silently split into one row per character and the latter
    would raise a 500 on ``for val in values``. Reject both up front.
    """
    if not isinstance(key, str):
        raise api_error(422, "adminDash.apiErr.attrKeyNotString")
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise api_error(422, "adminDash.apiErr.attrValNotArray", key=str(key))
    if len(key) > ATTR_KEY_MAX_LEN:
        raise api_error(
            422, "adminDash.validationAttrKeyMaxLen", key=key, max=ATTR_KEY_MAX_LEN
        )
    if CTRL_CHAR_RE.search(key):
        raise api_error(422, "adminDash.validationAttrKeyCtrlChar", key=key)
    if "," in key or ":" in key:
        raise api_error(422, "adminDash.validationAttrKeyInvalidChars", key=key)
    for val in values:
        if len(val) > ATTR_VALUE_MAX_LEN:
            raise api_error(
                422, "adminDash.validationAttrValMaxLen", key=key, max=ATTR_VALUE_MAX_LEN, len=len(val)
            )
        if CTRL_CHAR_RE.search(val):
            raise api_error(422, "adminDash.validationAttrValCtrlChar", key=key)


def parse_mpp_value(raw: str, field: str) -> float:
    """Parse a user-supplied MPP override. 'unset' is stored as 0.0.

    Both the empty string and the literal "null"/"none" mean unset. The
    sentinel exists because an empty form field cannot express "clear this":
    FastAPI / python-multipart deliver it as None for ``Optional[str] =
    Form(None)``, which the caller's ``is not None`` guard reads as "field
    omitted, leave the stored value alone". The tri-state overlay flags use the
    same "null" sentinel (see _parse_tri_bool) for the same reason.
    """
    s = (raw or "").strip()
    if not s or s.lower() in ("null", "none"):
        return 0.0
    try:
        val = float(s)
    except ValueError:
        raise api_error(422, "adminDash.apiErr.mppNotNumber", field=field)
    if not math.isfinite(val) or val < 0 or val > 1000:
        raise api_error(422, "adminDash.validationMppRange", field=field)
    return val


def _rebuild_attributes(rows: list, row_cls, db: Session, attrs: Dict[str, List[str]]):
    """Replace an owner's attribute ``rows`` (Slide.attributes /
    Tour.attributes) with ``attrs``, values stripped and de-duplicated."""
    rows.clear()
    # flush DELETE before INSERT to avoid UniqueConstraint violation when
    # the same (owner, key, value) combinations are being re-inserted
    db.flush()
    for key, values in attrs.items():
        cleaned = sorted({v.strip() for v in values if v and v.strip()})
        for value in cleaned:
            rows.append(row_cls(key=key, value=value))


def rebuild_slide_attributes(slide: Slide, db: Session, attrs: Dict[str, List[str]]):
    _rebuild_attributes(slide.attributes, SlideAttribute, db, attrs)


def rebuild_tour_attributes(tour: Tour, db: Session, attrs: Dict[str, List[str]]):
    _rebuild_attributes(tour.attributes, TourAttribute, db, attrs)


def _apply_attribute_delta(
    rows: list, row_cls, key: str, values: List[str], mode: str
) -> bool:
    """Add or remove one key's (key, value) pairs without clobbering others.

    Unlike _rebuild_attributes (which clears every attribute first), this
    surgically touches only the named key of an owner's attribute ``rows``
    (Slide.attributes / Tour.attributes) so a bulk category operation across
    a heterogeneous selection behaves predictably:

      * mode "add":    append any (key, value) the owner does not already
                       have, skipping existing ones so the UniqueConstraint on
                       (owner_id, key, value) is never violated. An owner that
                       already carries the value is left untouched.
      * mode "remove": drop matching (key, value) rows; an owner that does not
                       carry the value is a no-op.

    Returns True iff the owner's attribute set actually changed, so callers
    can report the count of owners genuinely affected (vs. no-ops).
    """
    cleaned = {v.strip() for v in values if v and v.strip()}
    if not cleaned:
        return False
    existing = {(a.key, a.value) for a in rows}
    if mode == "add":
        changed = False
        for value in sorted(cleaned):
            if (key, value) not in existing:
                rows.append(row_cls(key=key, value=value))
                changed = True
        return changed
    if mode == "remove":
        keep = [a for a in rows if not (a.key == key and a.value in cleaned)]
        if len(keep) == len(rows):
            return False
        # Reassign in place so cascade-delete-orphan removes the dropped rows.
        rows[:] = keep
        return True
    raise api_error(422, "adminDash.apiErr.invalidMode", mode=mode)


def apply_slide_attribute_delta(slide: Slide, key: str, values: List[str], mode: str) -> bool:
    return _apply_attribute_delta(slide.attributes, SlideAttribute, key, values, mode)


def apply_tour_attribute_delta(tour: Tour, key: str, values: List[str], mode: str) -> bool:
    return _apply_attribute_delta(tour.attributes, TourAttribute, key, values, mode)


# Filenames are stored on disk and referenced via URLs, so the allowlist is
# restricted to conservative ASCII (alnum, dot, dash, underscore). Anything
# else becomes "_" — strict but predictable.
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Windows reserved device names (case-insensitive, with or without extension).
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_FILENAME_MAX_BYTES = 200  # leave headroom under typical FS limits (255 bytes)
# Longest extension (dot included) kept whole when the name has to be cut to
# _FILENAME_MAX_BYTES. Every accepted slide / asset type is a few bytes; a
# longer "extension" is just the tail of a name with a dot in it.
_FILENAME_EXT_MAX_BYTES = 16


def safe_filename(name: str) -> str:
    """Sanitise an uploaded filename for safe filesystem and URL use.

    - Strips path separators and parent-traversal sequences.
    - Removes NUL bytes (which can truncate paths on some FSes).
    - Removes leading dots so attackers can't write `.htaccess`, `.env`, etc.
    - Replaces any character outside [A-Za-z0-9._-] with `_`; when that
      drops non-ASCII characters, appends a short hash of the (NFC) original
      stem so distinct originals keep distinct names.
    - Truncates to a safe byte length, preserving an extension of up to
      _FILENAME_EXT_MAX_BYTES (a longer one is cut to that length).
    - Rewrites Windows reserved device names so deployments that ever get
      mounted on / shared with Windows don't trip over them.
    - Always returns a non-empty string.
    """
    raw = (name or "").strip()
    # Clients sometimes include a path; keep only the basename.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # Drop NULs and any other C0 control characters outright.
    raw = "".join(ch for ch in raw if ch >= " " and ch != "\x7f")
    # One spelling per name: macOS hands over NFD ("が" as "か" + U+3099),
    # most other clients NFC. The result below is ASCII either way, but the
    # hash appended for non-ASCII names must not tell the two forms apart,
    # or the same file slips past the duplicate-name check.
    raw = unicodedata.normalize("NFC", raw)

    # Split the extension off *before* sanitising the stem. Otherwise a stem
    # made entirely of disallowed characters (e.g. a Japanese filename like
    # "標本.svs") collapses to underscores, and the leading-dot strip below
    # then eats those underscores together with the separating dot — dropping
    # the extension so validated_extension() rejects the upload with a 400.
    # splitext keeps only the final ".ext", which is exactly what
    # validated_extension() checks against wsi_allowed_extensions.
    stem, ext = os.path.splitext(raw)
    if ext:
        # Collapse ".." in the stem only, after the extension is split off:
        # done over the whole name, "case12..svs" would lose its extension dot
        # ("case12_svs") and the upload be refused as having no extension. A
        # trailing dot left on the stem ("case12." from "case12..svs") is
        # dropped for the same reason. Traversal is already neutralised by the
        # basename step above and the leading-dot strip below.
        stem = stem.replace("..", "_").rstrip(".")
    else:
        # No extension as splitext sees it — none at all, or a name of leading
        # dots ("...svs"): the ".." runs are collapsed first and the collapsed
        # form is split ("_.svs" -> "file.svs").
        stem, ext = os.path.splitext(raw.replace("..", "_"))
    # Keep the pre-sanitisation unicode stem so a fully-non-ASCII name that
    # collapses below can still be told apart from another one (it must be
    # captured BEFORE the sub, which would otherwise turn every distinct
    # non-ASCII stem into the same run of underscores).
    original_stem = stem

    # Sanitise stem and extension independently so the extension's leading dot
    # survives the lstrip that guards the stem.
    stem = _UNSAFE_FILENAME_CHARS_RE.sub("_", stem)
    ext = _UNSAFE_FILENAME_CHARS_RE.sub("_", ext)
    # Strip leading dots / dashes / underscores from the stem so the result
    # isn't a hidden file or a name that argparse-style tools mistake for a
    # flag. Applied to the stem only, so a valid extension is preserved.
    stem = stem.lstrip("._-")
    # Non-ASCII characters are dropped, not transliterated, so what remains
    # may no longer tell two originals apart: "標本.svs" and "検体.svs" both
    # sanitise to nothing, "胃生検0a1b.svs" and "大腸生検0a1b.svs" both to
    # "0a1b.svs" — and _assert_unique_wsi_filename then rejects the second as
    # a duplicate. A short stable hash of the ORIGINAL (NFC) stem keeps them
    # distinct. ASCII-only names are left as they were: their replacements
    # ("a b" -> "a_b") keep the name recognisable, and changing them would
    # re-key every existing slide's stored name.
    # The hash is taken over the LOWER-CASED stem: duplicate detection
    # (_assert_unique_wsi_filename, bundle import) is case-insensitive, so
    # "標本A.svs" and "標本a.svs" must still land on names that differ only in
    # case. lower() rather than casefold() so the browser's mirror
    # (TourAssetsPanel's clientSafeFilename, toLowerCase) computes the same.
    digest = ""
    if not original_stem.isascii():
        digest = hashlib.sha1(original_stem.lower().encode("utf-8")).hexdigest()[:8]
    if not stem:
        # A truly empty stem (no original at all) stays the bare "file".
        stem = f"file{digest}"
        digest = ""
    suffix = f"_{digest}" if digest else ""

    # Byte-length cap. Keep the extension and the hash intact; trim the rest
    # of the stem. Both are ASCII by now, so bytes == characters. An
    # extension too long to fit beside the stem is cut first: kept whole, it
    # alone would carry the name past the cap (and past the filesystem's
    # 255-byte limit: ENAMETOOLONG). Cut to _FILENAME_EXT_MAX_BYTES it is
    # still longer than any accepted type, so it cannot turn into one.
    if (
        len(ext) > _FILENAME_EXT_MAX_BYTES
        and len(stem) + len(suffix) + len(ext) > _FILENAME_MAX_BYTES
    ):
        ext = ext[:_FILENAME_EXT_MAX_BYTES]
    stem = stem[:max(1, _FILENAME_MAX_BYTES - len(ext) - len(suffix))] + suffix

    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return stem + ext


# Tour assets are served as UNAUTHENTICATED static files under /assets/tours/.
# They are keyed by the tour's unguessable public_id (not the numeric id) so an
# unlisted tour's embedded images can't be found by walking sequential ids — the
# token itself is the capability, matching the rest of the public surface.
def tour_asset_dir(tour_public_id: str) -> str:
    """Resolve a tour's on-disk asset directory from its public_id token.

    The token is validated, not merely joined. It reaches here straight from a
    DB row, and this project's threat model explicitly includes a restored or
    hand-edited malicious database (the same reason ``_guarded_data_path`` and
    ``_slide_path`` re-validate stored filenames). A whole-DB restore imports
    tour rows verbatim — only the per-slide transfer bundle validates
    public_ids — so a row carrying ``public_id = "../.."`` would otherwise make
    the callers' ``shutil.rmtree`` target an arbitrary directory outside the
    assets tree. An empty token would resolve to the shared ``assets/tours/``
    root, which the tours router already refuses for the same reason.

    Raises ValueError on a malformed token; use :func:`valid_public_id` first at
    best-effort cleanup sites that must not turn a bad row into a 500.
    """
    if not valid_public_id(tour_public_id):
        raise ValueError(f"malformed tour public_id: {tour_public_id!r}")
    return os.path.join(settings.data_dir, "assets", "tours", tour_public_id)


def tour_asset_url(tour_public_id: str, filename: str) -> str:
    return f"/assets/tours/{tour_public_id}/{filename}"


# The names a tour attachment can be stored under: what the upload endpoint
# writes — safe_filename's [A-Za-z0-9._-] charset, never a leading dot — and
# rename mode's "-<8 hex>" suffix, added after safe_filename's 200-byte cap,
# bounded here by the 255-byte file-name limit instead. (A round trip through
# safe_filename would rewrite those longer names into different ones.) No
# separators, no "." / "..": joined to a tour's asset directory, a name that
# passes names a file directly inside it.
_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,254}")


def storable_asset_name(name: str) -> bool:
    return _ASSET_NAME_RE.fullmatch(name) is not None


# SVG is intentionally absent: the logo is served same-origin from
# /api/viewer/logo with the stored mimetype, and an SVG can carry <script> /
# on*-handlers that would execute in the app origin if the URL is opened
# directly. There is no HTML/XML sanitizer wired up, so SVG is rejected here
# for the same reason detect_favicon_mimetype rejects it.
LOGO_EXT_TO_MIMETYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

FAVICON_EXT_TO_MIMETYPE = {
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def detect_logo_mimetype(data: bytes) -> Optional[str]:
    """Return the detected mimetype for the supported raster logo formats.

    Mirrors detect_favicon_mimetype: a renamed payload (HTML/SVG saved as
    foo.png) must not slip through on extension alone, because the logo is
    served same-origin from /api/viewer/logo with the stored mimetype.
    Returns None when the bytes match none of png / jpeg / gif / webp.
    """
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # RIFF container with a "WEBP" form type (bytes 8-12).
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def detect_favicon_mimetype(data: bytes) -> Optional[str]:
    """Return the detected mimetype for ICO/PNG bytes, else None.

    Verifies magic bytes so a renamed payload (e.g. HTML saved as foo.ico) is
    rejected. SVG is deliberately not supported: it can carry <script> and
    on*-handlers that would execute in the application origin if the favicon
    URL is ever opened directly, and the upload path has no safe HTML/XML
    sanitizer wired up.
    """
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # ICONDIR: reserved=0, type=1 (icon; type=2 is cursor — reject), count >= 1.
    if len(data) >= 6 and data[:4] == b"\x00\x00\x01\x00" and data[4:6] != b"\x00\x00":
        return "image/x-icon"
    return None

# Header / seasonal backgrounds accept PNG (incl. APNG, which carries the same
# 8-byte signature and is rendered as animation by all modern browsers based on
# its internal chunks regardless of file extension) and animated GIF. Only the
# extension is checked against this set; the stored mimetype comes from the
# content (detect_animated_image_mimetype).
HEADER_IMAGE_EXTS = frozenset({".png", ".apng", ".gif"})


def detect_animated_image_mimetype(data: bytes) -> Optional[str]:
    """Return the detected mimetype for PNG/APNG/GIF bytes, else None.

    Verifies the magic bytes so a renamed file (e.g. JPEG saved as foo.png)
    is rejected. Used for header default + seasonal background uploads.
    """
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def get_or_create_singleton(db: Session, model_cls, row_id: int = 1):
    """Return the fixed-id row of a settings table, creating it if absent.

    Used for ViewerSettings / NetworkSettings / AuthSettings (the single id=1
    row each) and for the per-slot SeasonalBackground / RolePermissions rows,
    which pass an explicit ``row_id``.

    Concurrency: under multiple uvicorn workers two requests can both observe
    no row and both try to INSERT the same id, so one loses on the PRIMARY KEY.
    The insert is wrapped in a SAVEPOINT (``begin_nested``) so the IntegrityError
    rolls back only the failed INSERT — not any work the caller already did in
    the surrounding transaction — and the winner's row is then re-read.
    """
    obj = db.get(model_cls, row_id)
    if obj is not None:
        return obj
    obj = model_cls(id=row_id)
    try:
        with db.begin_nested():
            db.add(obj)
            db.flush()
    except IntegrityError:
        # A racing worker committed this id first. Re-read its row.
        obj = db.get(model_cls, row_id)
        if obj is None:
            raise
    return obj


def unsupported_extension_message(filename: str) -> str:
    """The English reason ``filename``'s extension is refused, for the slide
    import's preflight / apply reports (which carry text, not error codes)."""
    ext = os.path.splitext(filename)[1].lower()
    return (
        f"Unsupported WSI extension: {ext or '(none)'}. "
        f"Allowed: {', '.join(settings.wsi_allowed_extensions)}"
    )


def validated_extension(filename: str) -> str:
    """The lower-cased extension of ``filename``, or a 400 with a localisable
    ``{code, params}`` detail when WSI_ALLOWED_EXTENSIONS does not list it."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.wsi_allowed_extensions:
        raise api_error(
            400, "adminDash.apiErr.wsiExtensionNotAllowed",
            ext=ext or "(none)", allowed=", ".join(settings.wsi_allowed_extensions),
        )
    return ext


def copy_spooled_upload(src: UploadFile, dst, max_bytes: int, too_large) -> Optional[int]:
    """Copy an upload's spool file into the open file ``dst`` inside the kernel.

    Starlette has already written every file part to an anonymous temp file
    (TMPDIR, which start-wsi-app.sh points at the data volume) before the
    endpoint runs; that file has no name, so it cannot simply be renamed into
    place. ``copy_file_range`` gets closest: on the same filesystem it shares
    the blocks instead of writing them again where the filesystem can
    (XFS with reflink, btrfs), and elsewhere copies without a round trip
    through Python. The size is known up front, so an oversized upload is
    refused (``too_large()`` raises) before anything is copied.

    Returns the byte count, or None when the fast path doesn't apply (a
    non-file stream, no ``copy_file_range``, a filesystem that refuses it);
    ``dst`` is then left empty at offset 0 for the caller's chunked copy.
    """
    copy_range = getattr(os, "copy_file_range", None)
    # A small upload still held in memory has no spool file yet, and fileno()
    # would roll it over to TMPDIR — a write that, if it fails (TMPDIR full),
    # has already swapped the in-memory bytes for a truncated file the chunked
    # fallback would then copy as if it were complete. Leave it to the caller.
    if getattr(src.file, "_rolled", True) is False:
        return None
    try:
        # flush(): Starlette's last write may still sit in the buffered
        # writer.
        src.file.flush()
        src_fd = src.file.fileno()
        size = os.fstat(src_fd).st_size
    except (AttributeError, OSError, ValueError):
        return None
    if size > max_bytes:
        too_large()
    if copy_range is None:
        return None
    dst_fd = dst.fileno()
    done = 0
    try:
        while done < size:
            n = copy_range(src_fd, dst_fd, size - done, done, done)
            if n <= 0:
                break
            done += n
    except OSError:
        done = -1
    if done != size:
        dst.seek(0)
        dst.truncate()
        return None
    dst.seek(size)
    return size


def _same_filesystem(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` live on one device. Unknown counts as the same
    (the stricter, single-volume rule)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


# How many copies of its body a slide upload writes before the endpoint has a
# staged WSI: the multipart spool and the copy under data_dir/wsi (see
# upload_space_short). A named constant, not just the default argument,
# because GET /api/admin/storage reports it to the bulk-upload preflight,
# which simulates this check client-side and must not hard-code a "2" that
# could drift from it.
SLIDE_UPLOAD_COPIES = 2


def upload_space_volumes() -> tuple[str, str, bool]:
    """``(target, spool, same_volume)``: where a slide upload's bytes land.

    ``target`` is the directory the staged WSI is written to, resolved through
    a ``data_dir/wsi`` symlink to the volume it really lives on (documented
    setup) and falling back to ``data_dir`` while ``wsi/`` does not exist yet;
    ``spool`` is the directory Starlette spools multipart bodies to
    (``tempfile.gettempdir()`` - TMPDIR, ``DATA_DIR/.upload-tmp`` under
    systemd); ``same_volume`` is whether they share a filesystem (unknown
    counts as the same: the stricter rule).

    One function for both :func:`upload_space_short` and GET
    /api/admin/storage, which reports these volumes' free space so the
    bulk-upload screen can predict the server's verdict file by file. Were
    each to resolve the paths itself, nothing would keep the screen measuring
    the volume the check measures — it could measure ``data_dir`` while the
    check measures a symlinked ``wsi/`` on another disk, and warn about (or
    wave through) the wrong one."""
    target = os.path.realpath(os.path.join(settings.data_dir, "wsi"))
    if not os.path.isdir(target):
        target = settings.data_dir
    spool = tempfile.gettempdir()
    return target, spool, _same_filesystem(target, spool)


def upload_space_short(size: int, factor: int = SLIDE_UPLOAD_COPIES) -> bool:
    """True when the volume(s) cannot take a ``size``-byte slide upload.

    A slide upload is written twice: Starlette spools the body to a temp file
    (TMPDIR - the data volume under systemd) and the endpoint then copies it to
    the staged WSI under ``data_dir/wsi`` - a reflinking filesystem shares the
    blocks, others do not. ``factor`` is how many of those copies are still to
    be written: 2 before the body is read, 1 once it has been spooled.
    ``data_dir/wsi`` may be a symlink to another volume (documented setup), so
    the destinations are measured where they really are: on one filesystem it
    must hold ``factor`` x ``size``; on two, ``data_dir/wsi`` must hold the
    staged copy (``size``) and the spool volume whatever is left to spool
    (``(factor - 1)`` x ``size``, nothing once the body is in).

    Each volume must also keep the shared reserve (disk_space.reserve_for of
    what is written there) free, as every other large write does. Asking only
    that the bytes fit would make a slide upload the one write allowed to
    take the data volume — the live DB's — down to its last byte.
    Best-effort (disk_space): an unreadable or capacity-less volume passes
    (the write's own ENOSPC handling is the safety net). ``size <= 0`` is an
    unknown size, not a zero-byte write: nothing to check.
    """
    if size <= 0:
        return False
    target, spool, same_volume = upload_space_volumes()
    if same_volume:
        return _disk_space.check(target, size * factor) is not None
    return _disk_space.check(target, size) is not None or (
        factor > 1 and _disk_space.check(spool, size * (factor - 1)) is not None
    )


def archive_upload_space_short(size: int) -> bool:
    """True when the upload spool cannot take a ``size``-byte restore /
    slide-import upload and keep the shared reserve (disk_space.reserve_for)
    free.

    Starlette spools the multipart body to TMPDIR before the endpoint runs —
    ``DATA_DIR/.upload-tmp`` under systemd (infra/systemd/start-wsi-app.sh),
    i.e. the data volume the live DB writes to, and the nginx cap for these
    routes is 1 TiB. Only after that does the job copy the archive into
    BACKUP_DIR/.staging and extract it, and those steps check the space they
    take themselves (the copy's ENOSPC -> uploadNoSpace, shared with the
    block-sharing copy_file_range on XFS-reflink / btrfs; the extraction's
    preflight). The spool is what nothing measured: an archive larger than
    the data volume's free space filled it to ENOSPC — failing the live DB's
    writes meanwhile — before the request was refused. So this measures the
    spool directory alone, where it really is (``tempfile.gettempdir()``),
    not the staging copy: requiring room for that too would refuse restores
    a reflinking filesystem takes without a second copy. Best-effort, like
    :func:`upload_space_short`."""
    if size <= 0:
        return False
    return _disk_space.check(tempfile.gettempdir(), size) is not None


def upload_write_error(exc: BaseException) -> HTTPException:
    """The localisable error for a failure while storing an upload: a full
    (or over-quota) volume is 507 uploadNoSpace, anything else 500 uploadFailed."""
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return api_error(507, "adminDash.apiErr.uploadNoSpace")
    return api_error(500, "adminDash.apiErr.uploadFailed")


def stream_upload_to_file(src: UploadFile, dst_path: str) -> int:
    """Stream a multipart upload to dst_path, enforcing the configured size cap.

    On *any* failure (size cap exceeded, write error, client disconnect, ...)
    the partially-written file is removed so the data directory does not
    accumulate orphan junk.
    """
    def too_large():
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max allowed is {settings.wsi_max_upload_bytes} bytes",
        )

    written = 0
    try:
        with open(dst_path, "wb") as f:
            copied = copy_spooled_upload(src, f, settings.wsi_max_upload_bytes, too_large)
            if copied is not None:
                return copied
            while True:
                chunk = src.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > settings.wsi_max_upload_bytes:
                    too_large()
                f.write(chunk)
    except BaseException:
        try:
            os.remove(dst_path)
        except OSError:
            pass
        raise
    return written


def thumb_filename(slide_id: int, existing: str = "") -> str:
    """Unguessable on-disk thumbnail filename for a slide.

    Thumbnails are served by the public ``/thumbs`` static mount, so a
    deterministic ``{slide_id}.jpg`` name lets anyone fetch a *draft* slide's
    thumbnail (and confirm its existence) by guessing the id — defeating the
    publish gate the rest of the read surface enforces. The id keeps the file
    operationally identifiable; the random token makes the URL a capability:
    a draft's filename only ever appears in bearer-gated admin responses, so
    an anonymous probe can't learn it. This keeps thumbnails servable by a
    plain ``<img src>`` (which can't carry an admin token) for both the public
    index and the admin draft list, which an auth-gated endpoint could not.

    Reuses the slide's existing filename when present so replace / rebuild
    operate on the same file and the staging/rollback paths stay stable.

    ``existing`` comes straight off a mutable DB row, and the callers join it
    onto ``<data_dir>/thumbs/`` and then *write* through it — rebuild-thumbnails
    calls ``make_thumbnail`` on the result, and the WSI-replace path
    ``os.replace``s the target away and a JPEG into its place. A stored
    ``"../wsi.db"`` (restored or hand-edited DB — the same threat model
    ``_guarded_data_path`` and ``rotate_thumb_filename`` already defend against)
    would therefore clobber a file outside the thumbs directory. Anything that
    is not a plain basename is refused and a fresh token minted instead, which
    also self-heals the row on the next write.
    """
    existing = (existing or "").strip()
    if (
        existing
        and os.path.basename(existing) == existing
        # basename("..") == ".." would otherwise pass; a leading dot is never
        # part of a minted name ("{id}-{hex}.jpg") anyway.
        and not existing.startswith(".")
    ):
        return existing
    if existing:
        logger.warning(
            "slide %s has a malformed thumb_filename %r; minting a fresh name",
            slide_id, existing,
        )
    return f"{slide_id}-{secrets.token_hex(8)}.jpg"


def rotate_thumb_filename(slide: Slide) -> Optional[str]:
    """Mint a fresh capability-token thumbnail name and rename the file on disk.

    The ``/thumbs`` static mount is public and ungated, so the random token in a
    slide's thumbnail filename is the *only* thing gating access (see
    ``thumb_filename``). A thumbnail URL handed out in a public index response
    while the slide was live therefore keeps resolving forever after the slide
    is unpublished — a past viewer or scraper can still fetch the (now-draft)
    thumbnail. Rotating the token and renaming the backing file invalidates
    every previously-shared URL.

    Best-effort by contract: returns the new filename on success (the caller
    assigns it to ``slide.thumb_filename`` and commits), or ``None`` when there
    is nothing to rotate or the rename failed — the unpublish toggle must never
    500 over thumbnail housekeeping.
    """
    old_name = (slide.thumb_filename or "").strip()
    # Refuse to touch a name that isn't a plain basename: a crafted value from a
    # restored DB (e.g. "../../wsi.db") must not be os.replace'd into.
    if not old_name or os.path.basename(old_name) != old_name:
        return None
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    new_name = thumb_filename(slide.id)  # existing="" -> fresh token
    old_path = os.path.join(thumbs_dir, old_name)
    new_path = os.path.join(thumbs_dir, new_name)
    try:
        os.replace(old_path, new_path)
    except FileNotFoundError:
        # No file on disk (e.g. after a metadata-only restore). Still rotate the
        # stored name so any leaked URL stops resolving once a thumbnail is
        # rebuilt under the new token.
        return new_name
    except OSError:
        logger.warning("Could not rotate thumbnail for slide %s", slide.id, exc_info=True)
        return None
    return new_name


def revert_thumb_rotation(old_name: str, new_name: str) -> None:
    """Undo a :func:`rotate_thumb_filename` rename after the commit failed.

    The rotation renames the file on disk *before* the caller commits, so a
    failed commit (e.g. SQLite busy timeout under concurrent write load) would
    otherwise leave the DB row pointing at the old name while only the new name
    exists — the slide's thumbnail 404s in the gallery and admin list until an
    operator runs rebuild-thumbnails.

    Best-effort and never raises: the caller is already on an error path, and a
    stale-but-present thumbnail is strictly better than propagating a second
    failure out of cleanup.
    """
    if not old_name or not new_name:
        return
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    try:
        os.replace(
            os.path.join(thumbs_dir, new_name), os.path.join(thumbs_dir, old_name)
        )
    except FileNotFoundError:
        # Nothing was renamed in the first place: rotate_thumb_filename returns
        # the new name even when no file existed on disk (the state a
        # metadata-only restore leaves), precisely so a leaked URL stops
        # resolving once a thumbnail is rebuilt. Not an error worth logging.
        pass
    except OSError:
        logger.warning(
            "Could not restore thumbnail name %s after a failed commit",
            old_name, exc_info=True,
        )


def reap_orphan_slide_reservations(db: Session, *, older_than_minutes: int = 30) -> int:
    """Delete stale id-reservation placeholder slide rows and their orphan files.

    ``admin_slides.upload_slide`` commits an empty-``wsi_filename`` placeholder
    row to reserve a monotonic slide id BEFORE streaming the (potentially
    multi-GB) body; the slide import reserves its rows the same way, and both
    hold the slide's mutation lock until they finalise or clean up.
    Request-path aborts clean the placeholder up themselves, but
    a hard kill between the reservation commit and the finalising commit leaves
    a permanent ghost row (rendered as a broken card, counted by backup) plus
    any partially-written ``{id}_*`` file under ``wsi/``.

    Intended to run once at boot from the app lifespan. Reaps only rows whose
    ``wsi_filename`` is empty (the sole marker of an unfinished reservation — a
    metadata-only restore keeps the filename set), that are older than
    ``older_than_minutes``, AND whose per-slide mutation lock is free. The age
    cutoff alone does not prove an upload is dead — the body cap defaults to
    8 GiB, so a real upload can still be streaming long past it — so the lock is
    what actually guarantees a reservation still finalising on another worker is
    never removed. Idempotent and safe to run concurrently across workers.
    Returns the number of rows removed.
    """
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    rows = db.execute(
        select(Slide).where(
            (Slide.wsi_filename.is_(None)) | (Slide.wsi_filename == "")
        )
    ).scalars().all()
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    doomed_ids: List[int] = []
    for slide in rows:
        created = coerce_aware(slide.created_at)
        if created is not None and created > cutoff:
            continue  # too young; an in-flight upload may still finalise it
        # The age cutoff alone is not proof the upload is dead: the body cap
        # defaults to 8 GiB, so a genuine upload over a slow link can still be
        # streaming well past ``older_than_minutes``. Its staging file is
        # ``wsi/{id}_{name}.upload-<token>``, which the id-prefix match below
        # would happily delete, and the row would go with it — a live upload
        # destroyed by a sibling worker's boot sweep. Take the slide's mutation
        # lock (which the upload holds for its duration) without waiting, so a
        # still-running upload is skipped rather than reaped, and startup never
        # blocks behind one.
        with try_slide_mutation_lock(slide.id) as acquired:
            if not acquired:
                logger.info(
                    "reaper: slide %s is being mutated elsewhere; leaving it for "
                    "the next sweep", slide.id,
                )
                continue
            # Re-read the row now that the lock is held: the SELECT above ran
            # without it, and the upload / import that owns the reservation
            # may have finalised it (set wsi_filename, then released the lock)
            # in between — a live slide, whose files the prefix match below
            # would delete. A column SELECT, not the session's cached object.
            current = db.execute(
                select(Slide.wsi_filename).where(Slide.id == slide.id)
            ).first()
            if current is None or current[0]:
                continue
            # Remove any partially-written body files this reservation started.
            # The finalise path writes to wsi/{id}_{safe_name}; a reaped
            # placeholder never set wsi_filename, so match by the id prefix.
            prefix = f"{slide.id}_"
            try:
                for fname in os.listdir(wsi_dir):
                    if fname.startswith(prefix):
                        try:
                            os.remove(os.path.join(wsi_dir, fname))
                        except OSError:
                            logger.warning(
                                "reaper: could not remove orphan WSI file %s", fname, exc_info=True
                            )
            except OSError:
                pass
            thumb_name = (slide.thumb_filename or "").strip()
            if thumb_name and os.path.basename(thumb_name) == thumb_name:
                try:
                    os.remove(os.path.join(thumbs_dir, thumb_name))
                except OSError:
                    pass
            # An upload killed after make_thumbnail but before the finalising
            # commit leaves ``{id}-<token>.jpg`` (or a staged ``{id}.upload-*``)
            # that the row never named. Ids are never reused, so every thumb
            # carrying this placeholder's id prefix is its leftover.
            try:
                for fname in os.listdir(thumbs_dir):
                    if fname.startswith((f"{slide.id}-", f"{slide.id}.")):
                        try:
                            os.remove(os.path.join(thumbs_dir, fname))
                        except OSError:
                            pass
            except OSError:
                pass
            doomed_ids.append(slide.id)
    if not doomed_ids:
        return 0
    # Core DELETE (not per-object ORM delete) so a concurrent worker that
    # already removed the same rows doesn't trip StaleDataError. Placeholders
    # only, as selected: it runs after each slide's lock was let go, and must
    # never take a row that has become a slide whatever happened since.
    from sqlalchemy import delete as _sa_delete
    try:
        db.execute(_sa_delete(Slide).where(
            Slide.id.in_(doomed_ids),
            (Slide.wsi_filename.is_(None)) | (Slide.wsi_filename == ""),
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("reaper: failed to commit orphan slide cleanup")
        return 0
    return len(doomed_ids)


# The names admin_replace_slide_wsi / _do_upload_slide_finalise give their
# temporary files, token and all (``uuid4().hex[:12]``). Matched exactly: a
# slide's own file may legitimately contain ".upload-" or ".rollback-"
# ("case.rollback-1.svs" is a valid upload), and must never be taken for one.
_TOKEN = r"[0-9a-f]{12}"
_WSI_STAGED_RE = re.compile(rf"^(\d+)_.+\.upload-({_TOKEN})(\.[A-Za-z0-9_-]+)?\Z")
_THUMB_STAGED_RE = re.compile(rf"^(\d+)\.upload-({_TOKEN})\Z")
_WSI_PARK_RE = re.compile(rf"^(\d+)_(.+)\.rollback-({_TOKEN})\Z")
_THUMB_PARK_RE = re.compile(rf"^(\d+)([-.].*)\.rollback-({_TOKEN})\Z")


def _file_size_or_none(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _same_bytes_sampled(a: str, b: str, size: int, samples: int = 16, chunk: int = 1 << 20) -> bool:
    """Whether two files of ``size`` bytes agree at ``samples`` evenly spaced
    ``chunk``-sized windows (the whole file when it is that small). Cheap
    enough for a boot sweep over multi-GB slides; a mismatch is certain, a
    match only probable — which is what the caller needs it for (see
    reap_interrupted_slide_swaps)."""
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            if size <= samples * chunk:
                offsets = [0]
                chunk = max(size, 1)
            else:
                step = (size - chunk) // (samples - 1)
                offsets = [i * step for i in range(samples)]
            for off in offsets:
                fa.seek(off)
                fb.seek(off)
                if fa.read(chunk) != fb.read(chunk):
                    return False
    except OSError:
        return False
    return True


def reap_interrupted_slide_swaps(db: Session, *, older_than_minutes: int = 60) -> dict:
    """Settle the files a killed upload / WSI replace left in ``wsi/`` and
    ``thumbs/``.

    The request-path cleanup of ``admin_replace_slide_wsi`` cannot run when the
    worker is SIGKILLed / OOM-killed / restarted, which leaves:

    * staged ``wsi/{id}_{name}.upload-<tok>.<ext>`` and
      ``thumbs/{id}.upload-<tok>`` copies (up to the upload cap each). Removed
      once older than ``older_than_minutes`` and only while the slide's
      mutation lock is free. The body is spooled before the endpoint runs, so
      a live staging file is a fast local copy whose mtime stays fresh;
    * ``.rollback-<tok>`` parks: the OLD bytes moved aside before the swap,
      named after the NEW destination. A kill before the finalising commit
      leaves the row describing the old file, which then exists only under the
      parked name. Under the slide lock (the replace's swap-and-commit holds
      it, so none is in flight), each WSI park is put back or dropped by which
      file the row's ``file_size_bytes`` describes:

      - the row's file is missing, or only the park matches the row → the
        commit never landed: the park goes back over the row's path (the
        unadopted new bytes of a different-name replace are dropped) and the
        slide moves to a fresh tile generation, as the request's own undo
        does, so tiles cached from the interim bytes never become current;
      - only the row's file matches → the commit landed; the park is dropped;
      - both match (same size): dropped when a sampled comparison finds the
        two identical (the same file re-uploaded), otherwise kept;
      - anything else is kept and logged for the operator.

      A thumbnail park follows its WSI park's verdict (kept when that one is
      kept). Without a WSI park of the same token it is put back when the WSI
      swap never happened (its staged copy is still there) or the row's file
      is missing, and dropped otherwise.

    Only names of exactly those shapes are touched, and never a name a slide
    row references. Intended to run once at boot from the app lifespan;
    idempotent and safe to run concurrently across workers. Returns counters
    for logging / tests.
    """
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_minutes * 60
    stats = {"staged_removed": 0, "parks_restored": 0, "parks_removed": 0, "parks_kept": 0}

    def _listdir(d: str) -> List[str]:
        try:
            return sorted(os.listdir(d))
        except OSError:
            return []

    # kind -> pattern, per directory
    patterns = (
        (wsi_dir, "staged", _WSI_STAGED_RE),
        (wsi_dir, "park", _WSI_PARK_RE),
        (thumbs_dir, "staged", _THUMB_STAGED_RE),
        (thumbs_dir, "park", _THUMB_PARK_RE),
    )
    found: Dict[int, List[tuple]] = {}
    listings = {wsi_dir: _listdir(wsi_dir), thumbs_dir: _listdir(thumbs_dir)}
    for d, kind, rx in patterns:
        for fname in listings[d]:
            m = rx.match(fname)
            if m:
                found.setdefault(int(m.group(1)), []).append((d, kind, fname, m))
    if not found:
        return stats

    from .dzi import _slide_path

    for slide_id, entries in sorted(found.items()):
        with try_slide_mutation_lock(slide_id) as acquired:
            if not acquired:
                continue
            row = db.execute(
                select(Slide.wsi_filename, Slide.file_size_bytes, Slide.thumb_filename)
                .where(Slide.id == slide_id)
            ).first()
            # Every name any row references (a hand-edited row may name another
            # slide's file): never a leftover, whatever it looks like.
            referenced = {
                n for pair in db.execute(select(Slide.wsi_filename, Slide.thumb_filename)).all()
                for n in pair if n
            }
            entries = [e for e in entries if e[2] not in referenced]
            staged_tokens = {
                e[3].group(2) for e in entries if e[0] == wsi_dir and e[1] == "staged"
            }

            ref_name = row[0] if row is not None else ""
            ref_size = row[1] if row is not None else 0
            try:
                ref_path = _slide_path(SimpleNamespace(wsi_filename=ref_name or "")) if ref_name else ""
            except FileNotFoundError:
                ref_path = ""
            cur_size = _file_size_or_none(ref_path) if ref_path else None

            # WSI parks first: their verdict decides the thumbnail parks'.
            verdicts: Dict[str, Optional[bool]] = {}  # token -> restored / dropped / kept(None)
            for d, kind, fname, m in entries:
                if d != wsi_dir or kind != "park":
                    continue
                token = m.group(3)
                park = os.path.join(wsi_dir, fname)
                if row is None:
                    # The slide is gone; its parked bytes belong to nothing.
                    _remove_logged(park, "parked WSI of deleted slide")
                    stats["parks_removed"] += 1
                    verdicts[token] = False
                    continue
                park_size = _file_size_or_none(park)
                cur_ok = cur_size is not None and bool(ref_size) and cur_size == ref_size
                park_ok = park_size is not None and bool(ref_size) and park_size == ref_size
                if ref_path and (cur_size is None or (park_ok and not cur_ok)):
                    verdict: Optional[bool] = True
                elif cur_ok and not park_ok:
                    verdict = False
                elif cur_ok and park_ok and _same_bytes_sampled(ref_path, park, cur_size):
                    verdict = False  # the same file: either one will do
                else:
                    verdict = None
                if verdict is None:
                    stats["parks_kept"] += 1
                    verdicts[token] = None
                    logger.warning(
                        "swap reaper: slide %s: cannot tell whether parked %s or "
                        "the current file is the one the slide row describes; "
                        "left in place for the operator", slide_id, fname,
                    )
                    continue
                if not verdict:
                    _remove_logged(park, "stale parked WSI")
                    stats["parks_removed"] += 1
                    verdicts[token] = False
                    continue
                try:
                    os.replace(park, ref_path)
                except OSError:
                    logger.exception("swap reaper: could not restore %s", fname)
                    stats["parks_kept"] += 1
                    verdicts[token] = None
                    continue
                # A different-name replace left its new bytes under a name the
                # row never adopted.
                new_path = os.path.join(wsi_dir, f"{slide_id}_{m.group(2)}")
                if (
                    os.path.abspath(new_path) != os.path.abspath(ref_path)
                    and os.path.basename(new_path) not in referenced
                ):
                    _remove_logged(new_path, "unadopted replacement WSI")
                cur_size = _file_size_or_none(ref_path)
                _retire_slide_generation(db, slide_id)
                stats["parks_restored"] += 1
                verdicts[token] = True
                logger.warning(
                    "swap reaper: slide %s: an interrupted WSI replace was "
                    "rolled back (restored %s)", slide_id, ref_name,
                )

            for d, kind, fname, m in entries:
                if d != thumbs_dir or kind != "park":
                    continue
                token = m.group(3)
                tpark = os.path.join(thumbs_dir, fname)
                base = os.path.join(thumbs_dir, f"{slide_id}{m.group(2)}")
                if token in verdicts:
                    restore = verdicts[token]
                    if restore is None:
                        continue  # its WSI park is kept: keep this one with it
                else:
                    restore = row is not None and (
                        token in staged_tokens  # killed before the WSI swap
                        or (bool(ref_name) and cur_size is None)  # the commit never landed
                        or not os.path.exists(base)
                    )
                if restore:
                    try:
                        os.replace(tpark, base)
                    except OSError:
                        logger.exception("swap reaper: could not restore %s", fname)
                else:
                    _remove_logged(tpark, "stale parked thumbnail")

            # Staged copies last (a thumbnail decision above reads them):
            # age-gated, the slide's lock held.
            for d, kind, fname, m in entries:
                if kind != "staged":
                    continue
                path = os.path.join(d, fname)
                try:
                    if os.stat(path).st_mtime > cutoff:
                        continue
                    os.remove(path)
                    stats["staged_removed"] += 1
                    logger.info("swap reaper: removed staged upload remnant %s", fname)
                except OSError:
                    pass
    return stats


def _retire_slide_generation(db: Session, slide_id: int) -> None:
    """Move ``slide_id`` to a fresh tile generation and tell every worker, as
    admin_slides._retire_generation does when a replace undoes itself: tiles
    nginx cached while the interrupted swap had the new bytes in place must
    never be current over the restored old ones. Best-effort."""
    from sqlalchemy import update

    from .models import fresh_wsi_generation

    try:
        db.execute(
            update(Slide)
            .where(Slide.id == slide_id)
            .values(wsi_generation=func.max(Slide.wsi_generation + 1, fresh_wsi_generation())),
            execution_options={"synchronize_session": False},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("swap reaper: could not retire the tile generation of slide %s", slide_id)
        return
    try:
        from . import slide_resolve_cache

        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        logger.warning("swap reaper: could not signal the slide-meta change", exc_info=True)


def _remove_logged(path: str, what: str) -> None:
    try:
        os.remove(path)
        logger.info("swap reaper: removed %s %s", what, os.path.basename(path))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("swap reaper: could not remove %s %s", what, path, exc_info=True)


def attach_associated_images(slide: Slide, wsi_path: str) -> None:
    """Refresh slide.macro_*/label_* from the WSI file's associated images.

    Always overwrites — on a replace_wsi the previous slide's macro/label
    must not stick around. Failure to extract is non-fatal: most TIFF/SZI
    uploads have no associated images and the columns just stay empty.
    """
    try:
        images = read_associated_images(wsi_path)
    except Exception:
        logger.warning("read_associated_images failed for %s", wsi_path, exc_info=True)
        images = {}
    macro_bytes = images.get("macro")
    label_bytes = images.get("label")
    # Log at INFO so "I uploaded an SVS with a macro but it doesn't show"
    # is debuggable without flipping per-slide debug state. INFO is visible
    # in journalctl with `-p info` and avoids flooding WARNING with the
    # "macro=none, label=none" line that is the normal outcome for TIFF/SZI
    # slides which carry no associated images.
    logger.info(
        "slide %s: associated images for %s — macro=%s, label=%s",
        slide.id, wsi_path,
        f"{len(macro_bytes)} bytes" if macro_bytes else "none",
        f"{len(label_bytes)} bytes" if label_bytes else "none",
    )
    if macro_bytes:
        slide.macro_data = macro_bytes
        slide.macro_mimetype = "image/jpeg"
        slide.macro_etag = hashlib.sha1(macro_bytes).hexdigest()[:16]
    else:
        slide.macro_data = None
        slide.macro_mimetype = ""
        slide.macro_etag = ""
    if label_bytes:
        slide.label_data = label_bytes
        slide.label_mimetype = "image/jpeg"
        slide.label_etag = hashlib.sha1(label_bytes).hexdigest()[:16]
    else:
        slide.label_data = None
        slide.label_mimetype = ""
        slide.label_etag = ""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def coerce_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; treat stored UTC values as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
