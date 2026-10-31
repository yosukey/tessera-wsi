"""Backup / restore core logic.

Layout of an archive (tarball, optionally zstd-compressed):

    manifest.json
    wsi.db                 # SQLite snapshot taken via sqlite3.Connection.backup
    assets/tours/...       # tour markdown attachments
    wsi/...                # raw WSI files (only when scope == "full")
    thumbs/...             # slide thumbnails (only when scope == "full")

``cache/`` (DZI tiles) is intentionally excluded — it is regenerated on
demand from the WSI files on the first tile request. ``thumbs/`` is NOT
regenerated on demand (thumbnails are produced only at upload / replace
time), so a ``full`` archive bundles it; a metadata-scope restore wipes
``thumbs/`` along with ``wsi/`` and the operator regenerates them via the
admin "Rebuild thumbnails" action (or by re-uploading the WSI files).

Compression: tries ``zstandard`` if importable, otherwise falls back to
``.tar.gz``. The archive's filename suffix records which was used so list
endpoints can offer the correct content-type.

Threading model: creating a backup, staging a restore and building a reset
stage each run on a daemon thread the endpoint starts, tracked by a
:mod:`jobs` record the dashboard polls; the job store's single slot keeps a
second backup / restore / reset from starting beside one.
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import functools
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import app_meta as _app_meta
from . import disk_space as _disk_space
from . import restore_apply as _restore_apply
from . import version as _version
from .admin_helpers import slide_mutation_lock, valid_public_id
from .config import settings
from .migrations import BASELINE_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION
from .version import APP_VERSION

_logger = logging.getLogger("wsi_app.backup")


def _utc_now() -> datetime.datetime:
    """tz-aware UTC ``now``. Centralised so call sites don't need to spell
    out ``timezone.utc`` every time."""
    return datetime.datetime.now(datetime.timezone.utc)


# "now" as ISO-8601 with a ``Z`` suffix rather than ``+00:00``, the form every
# manifest / sidecar / restore request timestamp uses.
_utc_iso_z = _restore_apply.utc_now_z

MANIFEST_NAME = "manifest.json"
DB_ENTRY = "wsi.db"


try:
    import zstandard as _zstd  # type: ignore
    _HAS_ZSTD = True
except Exception:  # pragma: no cover - optional dep
    _HAS_ZSTD = False


# Exceptions that mean "this file isn't a readable archive of the expected
# shape" (a truncated/garbage tar, a bad zstd frame, a malformed manifest.json,
# or a manifest field of the wrong type) rather than a server bug. stage_restore
# re-raises them as a RuntimeError so the router surfaces a 400 (bad upload)
# instead of a 500. zstd's error type is added only when the optional dep is
# present.
# zlib.error: GzipFile raises it, not an OSError, for a deflate block that
# does not decode (a flipped byte in a block header), and tarfile passes it
# through past the first member — so a damaged .tar.gz would escape as a 500.
_CORRUPT_ARCHIVE_ERRORS: tuple = (
    tarfile.TarError, OSError, EOFError, ValueError, TypeError, zlib.error,
)
if _HAS_ZSTD:
    _CORRUPT_ARCHIVE_ERRORS = _CORRUPT_ARCHIVE_ERRORS + (_zstd.ZstdError,)


SCOPE_METADATA = "metadata"
SCOPE_FULL = "full"
# ``reset`` is not a backup scope — it never appears in an archive manifest.
# It is an apply-time directive used by the in-process / start-script swap to
# replace the live DB with a freshly-built one that keeps only admin/config
# rows and wipes the rest, and to wipe the live ``assets/``, ``wsi/`` and
# ``thumbs/`` trees.
SCOPE_RESET = "reset"
# Valid scopes for a *backup* archive's manifest.
VALID_SCOPES = (SCOPE_METADATA, SCOPE_FULL)
# Valid scopes for an *apply* operation (restore or reset).
VALID_APPLY_SCOPES = (SCOPE_METADATA, SCOPE_FULL, SCOPE_RESET)


# Tables whose contents survive a reset. The User table holds admin login
# credentials (the operator's only path back into the dashboard) and the
# *Settings tables hold operator-tuned dashboard configuration. AppMeta
# carries the schema's app_version stamp, without which the next service
# start would refuse to open the DB. role_permissions holds the
# operator-configured RBAC grants (which non-admin roles may see Analytics /
# Server views): it is policy, not content, so wiping it would silently
# revoke access an admin had deliberately granted.
#
# seasonal_backgrounds and metadata_options are likewise operator-configured
# dashboard settings, not content: seasonal_backgrounds holds the scheduled
# header-background images (the always-on header_default_image_* fields, which
# live in viewer_settings, already survive, so the scheduled siblings are kept
# for symmetry), and metadata_options is the curated metadata vocabulary
# maintained on the dashboard's Metadata tab (the per-slide values in
# slide_attributes are content and get wiped with their slides). The reset UI
# promises "dashboard settings are preserved", so both are kept here.
#
# Everything else is content data (slides, tours, page views, …) and gets
# emptied.
_RESET_PRESERVE_TABLES = (
    "app_meta",
    "users",
    "viewer_settings",
    "network_settings",
    "auth_settings",
    "auth_users",
    "role_permissions",
    "seasonal_backgrounds",
    "metadata_options",
)

# Ancillary (operational) table groups an operator may exclude from a backup.
# The wsi.db snapshot always captures every table; when one of these groups is
# opted out, ``_strip_tables_in_place`` empties just those tables in the
# *snapshot copy* before it is archived (the schema survives, so a restore
# repopulates them as empty tables). Excluding them keeps visitor PII
# (``page_views`` client IPs / emails) and the admin action trail
# (``audit_logs``) out of an archive that may be shared off-site, and trims
# the archive size.
#
# "Access statistics": the three analytics event tables, and ``error_logs`` —
# its WARNING+ records carry visitor IPs too (sync_room's join/leave warnings,
# the analytics beacon's client_host), so excluding visitor PII has to take
# them out as well.
# "Activity history": the admin audit trail.
_ANALYTICS_TABLES = (
    "page_views",
    "slide_dwell_events",
    "tour_sessions",
    "error_logs",
)
_HISTORY_TABLES = ("audit_logs",)


@dataclass
class Manifest:
    scope: str
    app_version: str
    created_at: str
    slide_count: int
    tour_count: int
    # Whether the ancillary table groups were captured. WSI/thumbnail presence
    # is governed by ``scope`` (full vs metadata); these two cover the DB-side
    # opt-outs. Required: every archive this app writes states them, so an
    # absent value means a corrupt manifest rather than "everything included".
    includes_analytics: bool
    includes_history: bool
    # Integer data-shape revision (see the migrations package), independent of
    # app_version. Required for the same reason, and load-bearing: plan_restore
    # weighs it ahead of the app_version strings.
    schema_version: int
    # Declared after the required fields so the dataclass accepts the defaults;
    # every construction site passes keywords, so the order is not observable.
    note: str = ""
    archive_format: str = "tar.zst"  # or "tar.gz"
    # Diagnostics only (never consulted by restore): the interpreter and the
    # installed Python distributions the archive was written by, so an
    # incident on a restored host can be reproduced against the same tree.
    # An absent value reads as empty and a malformed one never fails the
    # parse (python_packages that is not a mapping is dropped).
    python_version: str = ""
    python_packages: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        """Parse a ``manifest.json`` body, strictly.

        The single reader for every restore-side path — staging, the
        pre-apply re-guard, and the preflight response — so all three agree on
        what a valid manifest is. Strict parsing here means a manifest missing
        ``schema_version`` is rejected instead of reading as ``0`` and
        sailing straight past :func:`plan_restore`'s downgrade guard.

        Descriptive fields still tolerate an absent value: a blank ``note`` or
        an unstated ``scope`` cannot cause a wrong *decision*. ``scope`` is
        checked against ``VALID_SCOPES`` on every path that acts on it —
        directly in :func:`stage_restore`, and via
        :func:`validate_archive_integrity` (which the apply endpoint refuses
        on) for the two router paths. The three fields that do drive decisions
        here are required.

        **Every** parse failure raises ``RuntimeError``, which is the whole
        contract callers rely on: the routers map that one type to a 400 and
        let anything else become a 500. A bare ``int(data["slide_count"])``
        would leak ValueError/TypeError past them, turning a corrupt manifest
        into an opaque server error plus an error_logs row instead of a
        message naming the bad field.
        """
        if not isinstance(data, dict):
            # A JSON scalar/array manifest would AttributeError on .get below.
            raise RuntimeError("archive is corrupt or not a supported format")
        missing = [
            k for k in ("schema_version", "includes_analytics", "includes_history")
            if k not in data
        ]
        if missing:
            raise RuntimeError(
                f"archive manifest is missing required field(s): "
                f"{', '.join(missing)}"
            )

        def _int(field: str, default: int = 0) -> int:
            raw = data.get(field, default)
            try:
                return int(raw)
            except (TypeError, ValueError, OverflowError):
                # OverflowError: json.load accepts Infinity / NaN-adjacent
                # literals and int(float('inf')) raises it.
                raise RuntimeError(
                    f"archive manifest has a non-numeric {field}: {raw!r}"
                ) from None

        return cls(
            # str() rather than a bare read: a JSON list or object here is
            # corrupt, and carrying it into a dataclass typed ``str`` would
            # push the surprise downstream instead of keeping it in the parse.
            scope=str(data.get("scope", "?")),
            app_version=str(data.get("app_version", "?")),
            created_at=str(data.get("created_at", "")),
            slide_count=_int("slide_count"),
            tour_count=_int("tour_count"),
            note=str(data.get("note", "")),
            archive_format=str(data.get("archive_format", "tar.zst")),
            includes_analytics=bool(data["includes_analytics"]),
            includes_history=bool(data["includes_history"]),
            schema_version=_int("schema_version"),
            python_version=str(data.get("python_version", "")),
            # Diagnostic, so a malformed value is dropped rather than failing
            # the parse of an otherwise valid archive.
            python_packages=(
                {str(k): str(v) for k, v in data["python_packages"].items()}
                if isinstance(data.get("python_packages"), dict) else {}
            ),
        )

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "slide_count": self.slide_count,
            "tour_count": self.tour_count,
            "note": self.note,
            "archive_format": self.archive_format,
            "includes_analytics": self.includes_analytics,
            "includes_history": self.includes_history,
            "schema_version": self.schema_version,
            "python_version": self.python_version,
            "python_packages": self.python_packages,
        }


def _installed_python_packages() -> dict:
    """``{distribution: version}`` of the running interpreter's environment,
    sorted by name. Best effort: a broken dist-info entry is skipped."""
    from importlib import metadata

    out: dict = {}
    for dist in metadata.distributions():
        try:
            name = dist.metadata["Name"]
            if name:
                out[str(name)] = str(dist.version)
        except Exception:
            continue
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


@dataclass
class BackupRecord:
    """Listing-row for a snapshot stored under settings.backup_dir."""
    id: str
    path: str
    size_bytes: int
    created_at: str
    scope: str
    app_version: str
    sha256: str
    note: str
    archive_format: str
    includes_analytics: bool = True
    includes_history: bool = True
    schema_version: int = 0
    # What create_backup left out of an archive it still published (a tour
    # attachment removed mid-run, a thumbnail that changed after the snapshot,
    # a tour whose token is unusable as a path), capped at
    # _MAX_BACKUP_WARNINGS. Only the run that made the archive knows them, so
    # they ride on the job result (the router copies them there) rather than
    # in to_dict(): the sidecar and the listing describe the archive, and a
    # list a re-read could never reproduce does not belong in either.
    warnings: list = field(default_factory=list)
    # The same warnings as ``{code, params}`` (index-aligned; see
    # _BackupWarnings), for the dashboard to translate.
    warning_items: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "scope": self.scope,
            "app_version": self.app_version,
            "sha256": self.sha256,
            "note": self.note,
            "archive_format": self.archive_format,
            "includes_analytics": self.includes_analytics,
            "includes_history": self.includes_history,
            "schema_version": self.schema_version,
        }


@dataclass
class RestorePlan:
    """Pre-apply summary surfaced to the UI."""
    backup_app_version: str
    current_app_version: str
    blocking_reason: Optional[str] = None  # set when restore must be refused
    blocking_code: Optional[str] = None    # machine-readable code
    backup_schema_version: int = 0
    current_schema_version: int = 0

    def to_dict(self) -> dict:
        return {
            "backup_app_version": self.backup_app_version,
            "current_app_version": self.current_app_version,
            "blocking_reason": self.blocking_reason,
            "blocking_code": self.blocking_code,
            "backup_schema_version": self.backup_schema_version,
            "current_schema_version": self.current_schema_version,
        }


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _ensure_backup_dirs() -> None:
    os.makedirs(settings.backup_dir, exist_ok=True)
    os.makedirs(os.path.join(settings.backup_dir, ".staging"), exist_ok=True)


def _archive_extension() -> str:
    return "tar.zst" if _HAS_ZSTD else "tar.gz"


def _archive_filename(backup_id: str) -> str:
    return f"{backup_id}.{_archive_extension()}"


def _archive_path(backup_id: str) -> str:
    return os.path.join(settings.backup_dir, _archive_filename(backup_id))


def _preflight_backup_free_space(scope: str) -> None:
    """Refuse to start a backup that obviously can't fit on the backup volume.

    Rough estimate: live DB size + total assets bytes + (for full scope) total
    WSI bytes, all uncompressed. Real archives are smaller (zstd compresses
    the DB and assets well; WSI files are typically already-compressed image
    formats and so compress poorly — sum-of-WSI-sizes is a close upper bound).
    The estimate must fit with the shared reserve (disk_space.reserve_for:
    10 %, at least 256 MiB) left free: BACKUP_DIR defaults to a directory on
    the data volume, so a backup that fits with nothing to spare leaves the
    live DB / WAL, uploads and the job store writing into a full disk. It is
    the one rule every large write uses. The 10 % covers the estimate's error
    too: the estimate is already an upper bound (uncompressed sizes, the DB
    counted twice), so a separate 1.1x on top would double-count it.

    Best-effort: if any size cannot be determined, or the volume reports no
    capacity, the check is skipped. The archive writer's own ENOSPC handling
    is the real safety net; this is just to fail fast on obvious cases
    instead of after gigabytes of write work.
    """
    free_bytes = _disk_space.free_bytes(settings.backup_dir)
    if free_bytes is None:
        return

    estimate = 0
    try:
        db_path = _live_db_path()
        db_bytes = os.path.getsize(db_path)
    except (OSError, RuntimeError):
        return
    # The DB lands on this volume twice at once: the snapshot staged under
    # backup_dir/.staging, and the copy streamed into the growing archive.
    # Counting it once under-reports by a whole DB on analytics-heavy installs
    # where the DB, not the WSI files, dominates.
    estimate += db_bytes * 2
    # A busy WAL can approach the size of the DB itself and is checkpointed
    # into the snapshot, so it belongs in the estimate too.
    try:
        estimate += os.path.getsize(db_path + "-wal")
    except OSError:
        pass

    assets_root = os.path.join(settings.data_dir, "assets")
    if os.path.isdir(assets_root):
        estimate += _dir_total_bytes(assets_root)

    if scope == SCOPE_FULL:
        wsi_root = os.path.join(settings.data_dir, "wsi")
        if os.path.isdir(wsi_root):
            estimate += _dir_total_bytes(wsi_root)
        # Thumbnails ride along in full-scope archives; tiny relative to the
        # WSI bytes but counted so the headroom estimate stays honest.
        thumbs_root = os.path.join(settings.data_dir, "thumbs")
        if os.path.isdir(thumbs_root):
            estimate += _dir_total_bytes(thumbs_root)

    short = _disk_space.shortfall_given(estimate, free_bytes, settings.backup_dir)
    if short is not None:
        raise RuntimeError(short.message("backup volume"))


# ---------------------------------------------------------------------------
# DB snapshot
# ---------------------------------------------------------------------------

def _live_db_path() -> str:
    """Resolve the SQLite file path from DATABASE_URL via SQLAlchemy URL parsing."""
    from sqlalchemy.engine.url import make_url

    url = make_url(settings.database_url)
    if not url.drivername.startswith("sqlite"):
        raise RuntimeError("Backup feature only supports SQLite for now")
    db = url.database or ""
    if not db:
        raise RuntimeError("DATABASE_URL has no database path")
    return db


def _snapshot_sqlite(src_path: str, dst_path: str) -> None:
    """Take a consistent online snapshot of ``src_path`` into ``dst_path``.

    Uses the SQLite C-level backup API (exposed as ``Connection.backup``)
    which copies pages cooperatively with WAL writers, so the snapshot is
    safe to take while the live app is serving traffic.
    """
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _use_rollback_journal(db_path: str) -> None:
    """Switch the snapshot copy at ``db_path`` out of WAL mode.

    The backup API copies page 1 verbatim, so a snapshot of the (WAL-mode)
    live DB is itself a WAL database: every later write to it — the table
    strip, the app_version / schema_version stamps — lands in ``wsi.db-wal``
    and reaches ``wsi.db`` only through the checkpoint SQLite runs when the
    last connection closes. ``tar.add`` archives ``wsi.db`` alone, so a
    checkpoint that did not happen (a connection left open, a checkpoint
    that failed quietly at close) would ship a DB without its stamps or
    with the opted-out PII still in it, and nothing would say so. In
    rollback-journal mode each commit is in the main file once it returns.

    Harmless for the restored DB: db.py's connect hook sets
    ``journal_mode=WAL`` on every connection to the live DB, so a restored
    snapshot is back in WAL mode the first time the app opens it.
    """
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        conn.close()
    if not mode or str(mode[0]).lower() != "delete":
        raise RuntimeError(
            f"could not switch the database copy {os.path.basename(db_path)!r} "
            f"out of WAL mode ({mode!r})"
        )


def _read_db_counts(db_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        slides = conn.execute("SELECT COUNT(*) FROM slides").fetchone()[0]
        tours = conn.execute("SELECT COUNT(*) FROM tours").fetchone()[0]
        return int(slides), int(tours)
    except sqlite3.Error:
        return 0, 0
    finally:
        conn.close()


def _check_sqlite_integrity(db_path: str) -> None:
    """Raise ``RuntimeError`` unless ``PRAGMA quick_check`` passes on ``db_path``.

    The codec checksums and the sidecar sha256 cover the archive in transit;
    this catches a DB that was already damaged when it was archived (or an
    uploaded archive with no sidecar) before it is promoted over the live one.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"archive's wsi.db failed its integrity check: {exc}") from exc
    if rows != [("ok",)]:
        detail = "; ".join(str(r[0]) for r in rows[:3])
        raise RuntimeError(f"archive's wsi.db failed its integrity check: {detail}")


def _enumerate_snapshot(db_path: str) -> tuple[list[str], list[str]]:
    """Read the wsi filenames and tour asset-dir keys referenced by the *snapshot* DB.

    The second element is the list of tour ``public_id`` tokens (the on-disk
    ``assets/tours/{token}/`` directory names).

    The archive must contain exactly the files that the snapshot DB knows
    about — not the live disk state at the time of the directory walk.
    Otherwise concurrent slide upload/delete/replace produces archives whose
    wsi/assets entries disagree with their DB rows: orphan files (file on
    disk, no row) or missing files (row, no file), and in the worst case a
    deleted file vanishes mid-walk and aborts the whole backup with
    ``FileNotFoundError`` from ``tar.add(..., recursive=True)``.

    Raises ``sqlite3.Error`` on a read failure rather than swallowing it to an
    empty result: an empty list is also the legitimate "no WSI files" answer,
    and the create / validate integrity checks key off this set. Masking a
    read error as "empty" would skip the WSI cross-check and pass an archive
    whose file coverage was never verified. Callers that only want a best-
    effort summary already wrap this in try/except.
    """
    conn = sqlite3.connect(db_path)
    try:
        wsi_filenames = [
            row[0]
            for row in conn.execute(
                "SELECT wsi_filename FROM slides WHERE wsi_filename IS NOT NULL AND wsi_filename != ''"
            ).fetchall()
        ]
        # Tour assets live under assets/tours/{public_id}/ (the unguessable
        # token, not the numeric id), so enumerate the tokens to drive the
        # archive walk. ``tours.public_id`` is NOT NULL, so every row has one.
        tour_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT public_id FROM tours WHERE public_id IS NOT NULL AND public_id != ''"
            ).fetchall()
        ]
        return wsi_filenames, tour_ids
    finally:
        conn.close()


# Same-generation guarantee for full backups.
#
# Enumerating files from the frozen snapshot (above) settles WHICH files go in
# the archive, but not WHICH BYTES: a same-filename WSI replace keeps the
# ``{slide_id}_{name}`` path, so a replace landing between the DB snapshot and
# the slide's ``tar.add`` would archive the new image next to the snapshot's
# old width / height / mpp / annotations — and validate_archive_integrity,
# which only checks presence, would pass it.
#
# Options considered: one lock shared by every WSI mutation and the backup's
# whole snapshot -> archive window would make replace/upload/delete wait out a
# multi-hour tar; per-generation immutable filenames would ripple through the
# upload / replace / transfer naming and still need the backup to fail on a
# vanished name. Instead, each file is opened under the slide's existing
# ``slide_mutation_lock`` — which replace / delete / upload-finalise hold
# across their file swap AND DB commit, so while it is held the live row and
# the on-disk file always agree — after checking that the live row still
# carries the snapshot's file-generation columns. If it does, the file at the
# path IS the snapshot's generation; the open fd pins that inode, so the lock
# is released before streaming and a replace that lands mid-copy (an
# os.replace onto a new inode) cannot change the bytes being read. If it does
# not, the backup fails with a retry message, like the existing
# deleted-during-backup check. The lock is held for a SELECT and an open(2) —
# never across the copy — and tile serving does not take it at all.
#
# The columns are the ones replace / upload derive from the file itself, plus
# wsi_generation, the counter every WSI replace increments in the same commit:
# it is what catches a replace whose file-derived columns all come out
# identical (the same scan re-exported, a byte-level fix that keeps size and
# metadata), which the derived columns alone cannot see. They stay in the key
# for rows whose generation never moved (rows written outside the replace
# path: a hand-edited or script-written DB).
# thumb_filename is deliberately left out of the WSI key (an unpublish rotates
# it without touching the image) and added only for the thumbnail check;
# mpp_x / mpp_y are operator overrides, so their *_file twins stand in.
_FILE_GENERATION_COLUMNS = (
    "wsi_filename",
    "file_size_bytes",
    "width",
    "height",
    "mpp_x_file",
    "mpp_y_file",
    "objective_power_file",
    "vendor",
    "acquired_at",
    "macro_etag",
    "label_etag",
    # Last: create_backup reads wsi_filename positionally (gen[1]).
    "wsi_generation",
)
_GENERATION_SELECT = (
    "SELECT id, thumb_filename, " + ", ".join(_FILE_GENERATION_COLUMNS) + " FROM slides"
)
# Slices of a generation tuple ``(thumb_filename, *_FILE_GENERATION_COLUMNS)``.
_WSI_GENERATION = slice(1, None)
_THUMB_GENERATION = slice(0, None)


def _read_file_generations(
    conn: sqlite3.Connection, slide_id: Optional[int] = None,
) -> dict[int, tuple]:
    """``{slide_id: (thumb_filename, *_FILE_GENERATION_COLUMNS)}``.

    Always ``fetchall``: a half-read cursor keeps its read transaction — and
    with it a WAL snapshot — open, so on the live connection every later check
    would compare against that stale view instead of what a replace committed.
    """
    if slide_id is None:
        rows = conn.execute(_GENERATION_SELECT).fetchall()
    else:
        rows = conn.execute(_GENERATION_SELECT + " WHERE id = ?", (slide_id,)).fetchall()
    return {int(r[0]): tuple(r[1:]) for r in rows}


class _GenerationChanged(Exception):
    """The live slide row no longer describes the snapshot's file generation."""


def _open_snapshot_generation(
    live_conn: sqlite3.Connection,
    slide_id: int,
    expected: tuple,
    path: str,
    key: slice,
):
    """Open ``path`` for archiving iff slide ``slide_id`` still describes the
    file generation the snapshot recorded (``expected[key]``). See the block
    comment above ``_FILE_GENERATION_COLUMNS``.

    Returns a binary file object whose fd pins the verified inode, or None
    when what is at ``path`` is not a regular file. Raises
    ``_GenerationChanged`` if the slide was replaced / deleted since the
    snapshot, ``FileNotFoundError`` if the row is unchanged but no file backs
    it, and OSError ELOOP for a symlink. The open is :func:`_open_pinned`'s:
    O_NOFOLLOW backs up the caller's islink check against a swap in between,
    and O_NONBLOCK matters more here than anywhere — a FIFO at the path
    would otherwise block the open(2) until something wrote to it, with the
    slide's mutation lock held, wedging every replace / delete of that slide
    and the backup itself (whose job slot then blocks every other backup and
    restore) indefinitely.
    """
    with slide_mutation_lock(slide_id):
        current = _read_file_generations(live_conn, slide_id).get(slide_id)
        if current is None or current[key] != expected[key]:
            raise _GenerationChanged(slide_id)
        return _open_pinned(path)


# No hard link can be made here at all: another filesystem (wsi/ symlinked to
# a volume other than BACKUP_DIR's), or one that does not support links.
# EPERM (fs.protected_hardlinks on a file the service user does not own) and
# EMLINK are about one file and only leave that one unpinned.
_NO_LINK_ERRNOS = frozenset(
    e for e in (
        errno.EXDEV,
        getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None),
    ) if e is not None
)


def _pin_snapshot_wsi_files(
    generations: dict[int, tuple],
    wsi_root: str,
    pin_dir: str,
    cancel: Optional["_CancelCheck"] = None,
) -> tuple[dict[int, str], int]:
    """Hard-link each snapshot WSI into ``pin_dir`` right after the snapshot,
    while the slide still describes that file generation.

    Without it each file would be checked against the snapshot only when the
    archive loop reached it, hours into a large backup: a slide replaced or
    deleted at any point since the snapshot would fail the whole backup there,
    and a retry on a busy library would face the same odds. A link taken now, under
    the slide's mutation lock and after the same generation check, holds the
    snapshot's inode whatever a later replace / delete does to the name, so
    the archive pairs the snapshot's rows with their own bytes. The links
    share the files' blocks (no extra space; a deleted slide's blocks are
    freed when the snapshot dir goes) and are removed with the snapshot dir.

    Returns ``({slide_id: pin_path}, total_bytes)``. Best-effort: a slide that
    is unusable here (gone, changed, a symlink, not a regular file, outside
    wsi/) is simply not pinned, and the archive loop reads it from its path;
    when links cannot be made at all (another filesystem), nothing is pinned.
    """
    pins: dict[int, str] = {}
    total = 0
    entries = [(sid, gen[1]) for sid, gen in generations.items() if gen[1]]
    if not entries or not os.path.isdir(wsi_root):
        return pins, total
    os.makedirs(pin_dir, exist_ok=True)
    wsi_root_real = os.path.realpath(wsi_root)
    live_conn = sqlite3.connect(_live_db_path())
    try:
        for slide_id, fname in entries:
            if cancel is not None:
                cancel.check()
            path = os.path.join(wsi_root, fname)
            # Only a file the archive may carry: a pinned name skips the
            # pre-scan, which must still refuse a name restore would refuse.
            if (
                not _is_safe_wsi_filename(fname)
                or os.path.islink(path)
                or not _under_root(wsi_root_real, path)
            ):
                continue
            pin = os.path.join(pin_dir, str(slide_id))
            try:
                with slide_mutation_lock(slide_id):
                    current = _read_file_generations(live_conn, slide_id).get(slide_id)
                    if (
                        current is None
                        or current[_WSI_GENERATION] != generations[slide_id][_WSI_GENERATION]
                    ):
                        continue
                    os.link(path, pin, follow_symlinks=False)
            except OSError as exc:
                if exc.errno in _NO_LINK_ERRNOS:
                    _logger.info(
                        "backup: WSI files cannot be hard-linked into the "
                        "snapshot (%s); each is checked when it is archived", exc,
                    )
                    break
                continue
            try:
                st = os.lstat(pin)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                _remove_quietly(pin)
                continue
            pins[slide_id] = pin
            total += st.st_size
    finally:
        live_conn.close()
    return pins, total


class _ProgressReader:
    """Pass-through reader that counts the bytes read and reports them on
    ``on_bytes`` — so the WSI phase reports progress while one large file
    streams, not only once it is done. ``fileno`` is forwarded for the fstat
    in :func:`_add_pinned_file`."""

    def __init__(self, f, on_bytes: Callable[[int], None]) -> None:
        self._f = f
        self._on_bytes = on_bytes

    def read(self, size: int = -1) -> bytes:
        b = self._f.read(size)
        if b:
            self._on_bytes(len(b))
        return b

    def fileno(self) -> int:
        return self._f.fileno()


class BackupCancelled(Exception):
    """An operator cancelled the running backup (``should_cancel`` returned
    True). Raised from :func:`create_backup` before anything is published:
    the partial ``.tmp`` archive and the snapshot dir are removed on the way
    out, exactly as for a failed run, and the router records the job as
    ``cancelled`` rather than ``failed``."""


# How often a backup consults its ``should_cancel`` callable while streaming
# one file into the archive. The callable is a file-existence check on the
# shared job store (another worker may have taken the request), so it is
# throttled by time rather than called per 16 KiB tar read; the byte gate
# keeps even the clock read off the hot path. Half a second bounds how long a
# multi-hundred-GB WSI keeps streaming after a cancel.
_CANCEL_CHECK_INTERVAL_SEC = 0.5
_CANCEL_CHECK_BYTES = 4 * 1024 * 1024


class _CancelCheck:
    """Throttled wrapper over a ``should_cancel`` callable.

    ``check()`` asks unconditionally (phase / file boundaries — cheap, a few
    per file); ``maybe_check(nbytes)`` asks only once ``_CANCEL_CHECK_BYTES``
    have streamed since the last clock read and ``_CANCEL_CHECK_INTERVAL_SEC``
    since the last ask (inside one huge file). Both raise
    :class:`BackupCancelled`. A callable that raises is logged and treated as
    "not cancelled": a broken job-store read must not fail an otherwise good
    multi-hour backup."""

    def __init__(self, should_cancel: Optional[Callable[[], bool]]) -> None:
        self._fn = should_cancel
        self._bytes = 0
        self._last = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self._fn is not None

    def check(self) -> None:
        if self._fn is None:
            return
        self._last = time.monotonic()
        self._bytes = 0
        try:
            cancelled = bool(self._fn())
        except Exception:
            _logger.exception("backup: cancel check failed; continuing")
            return
        if cancelled:
            raise BackupCancelled("backup cancelled by an administrator")

    def maybe_check(self, nbytes: int) -> None:
        if self._fn is None:
            return
        self._bytes += nbytes
        if self._bytes < _CANCEL_CHECK_BYTES:
            return
        self._bytes = 0
        if time.monotonic() - self._last >= _CANCEL_CHECK_INTERVAL_SEC:
            self.check()


class _CancellableReader:
    """The pinned file object handed to ``tar.addfile``, polling for a cancel
    as its bytes stream out (see :class:`_CancelCheck`), so cancelling during
    one multi-hour WSI does not wait for that file to finish. Only ``read``
    is used by tarfile's copy loop; ``fileno`` is forwarded for the fstat in
    :func:`_add_pinned_file`. Raising out of the copy leaves the tar stream
    mid-member — the caller discards the whole archive in that case."""

    def __init__(self, f, cancel: "_CancelCheck") -> None:
        self._f = f
        self._cancel = cancel

    def read(self, size: int = -1) -> bytes:
        self._cancel.maybe_check(size if size and size > 0 else 0)
        return self._f.read(size)

    def fileno(self) -> int:
        return self._f.fileno()


def _cancellable(f, cancel: Optional[_CancelCheck]):
    """``f`` ready to hand to :func:`_add_pinned_file`: after a cancel check,
    wrapped so the copy loop keeps checking, when ``cancel`` is active; ``f``
    itself otherwise (slide export, or a backup nobody can cancel)."""
    if cancel is None or not cancel.enabled:
        return f
    cancel.check()
    return _CancellableReader(f, cancel)


def _add_pinned_file(tar: tarfile.TarFile, f, arcname: str) -> bool:
    """Archive an already-open file under ``arcname`` (header from fstat, bytes
    from the same fd, so size and content describe one inode). Returns False
    when it is not a regular file or the filter drops it.

    The header is built from ``os.fstat`` here rather than by
    ``tar.gettarinfo(fileobj=f)``: gettarinfo remembers every regular file's
    (st_ino, st_dev) in ``tar.inodes`` and writes the next name it meets for
    the same inode as a hard-link member (LNKTYPE, no bytes) pointing at the
    first — which ``_archive_filter`` then drops, because restore refuses
    links. So two hard-linked tour attachments (or two slides' WSI names on
    one inode, or one an operator hard-linked by hand) would lose the second
    copy silently, and a WSI would go missing from a "successful" archive or
    fail the backup with a retry message no retry could fix. Every member written
    here is a regular file carrying its own bytes, whatever else shares its
    inode; the snapshot is read from one fd per name, so the archive is a
    little larger and restores exactly what the names held.

    A backup passes ``f`` through :func:`_cancellable`, so a cancel is
    noticed while its bytes stream (:class:`BackupCancelled`)."""
    st = os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode):
        return False
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.REGTYPE
    info.size = st.st_size
    info.mtime = int(st.st_mtime)
    info.mode = stat.S_IMODE(st.st_mode)
    info = _archive_filter(info)
    if info is None:
        return False
    tar.addfile(info, f)
    return True


def _open_pinned(path: str):
    """Open ``path`` for archiving: a binary file object whose fd pins the
    inode, or None when that is not a regular file. O_NOFOLLOW refuses a
    symlink swapped in under an already-resolved path (OSError ELOOP), and
    O_NONBLOCK keeps a FIFO from blocking the open; a missing file raises
    FileNotFoundError."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    f = os.fdopen(os.open(path, flags), "rb")
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        f.close()
        return None
    return f


def _is_tour_upload_temp(name: str) -> bool:
    """An in-flight (or stranded) tour-asset upload: ``.<name>.upload-<token>``,
    matched the way admin_tours' stale-temp sweep matches it."""
    return name.startswith(".") and ".upload-" in name


def _scan_tour_asset_dir(tour_dir: str) -> tuple[list, list]:
    """``(files, left_out)`` for a tour's asset dir: the entries a backup or a
    slide export archives — its regular, non-dot files, in name order — and
    the entries it leaves out other than in-flight upload temps.

    The upload path only ever writes flat, non-dot regular files (plus its
    ``.<name>.upload-<token>`` temps), so ``left_out`` is whatever was copied
    in by hand: a subdirectory, a dotfile, a symlink, a FIFO. These are not
    archived — rather than recursed into — because nothing else in the app
    knows them either: the asset list, quota and delete endpoints see only
    the flat files, and a slide import refuses a bundle with anything nested
    under ``assets/tours/<pid>/``, so the export cannot carry them and the
    backup keeping them would make the two disagree on what a tour's assets
    are. The caller logs each one, so an operator who placed nested files
    there by hand finds out at backup time, not after a restore. A tour with no
    uploads has no dir (no files)."""
    try:
        with os.scandir(tour_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except FileNotFoundError:
        return [], []
    files, left_out = [], []
    for e in entries:
        if not e.name.startswith(".") and e.is_file(follow_symlinks=False):
            files.append(e)
        elif not _is_tour_upload_temp(e.name):
            left_out.append(e)
    return files, left_out


def _tour_asset_files(tour_dir: str) -> list:
    """The entries of a tour's asset dir that a backup or a slide export
    archives (see :func:`_scan_tour_asset_dir`)."""
    return _scan_tour_asset_dir(tour_dir)[0]


# A tour step's (or tour description's) markdown reference to one of a tour's
# own attachments. Same shape as slide_transfer._ASSET_REF_RE, which the
# import uses to flag references the target lacks; backup.py cannot import
# slide_transfer (slide_transfer imports it).
_TOUR_ASSET_REF_RE = re.compile(r"/assets/tours/([A-Za-z0-9_-]{1,32})/[^\s)\]\"'<>?#]+")


class _MissingTourDirCheck:
    """Says why a snapshot tour has no asset dir at archive time, when that is
    worth a job warning.

    An absent ``assets/tours/<pid>/`` is what every tour without uploads has,
    so on its own it says nothing — which is why a tour deleted between the
    DB snapshot and the asset walk could go unreported: the archive keeps
    the tour's row (it is in the snapshot) but none of its attachments, and
    the restored tour's images do not load. Two questions tell the cases
    apart, each answered only for a tour whose dir is missing:

    * Is the tour still in the live DB? If not, it was deleted after the
      snapshot, and whatever attachments it had are gone with it.
    * Does the snapshot's own markdown (the tour description, its steps')
      reference this tour's attachments? Then the archived tour points at
      files the archive lacks, however the dir came to be missing.

    A tour that is still live and references nothing of its own is the plain
    "never had uploads" case: no warning. The connections are opened on
    first use — most tours have a dir, or none and nothing to check — and
    read only. Failing to answer is not worth failing a backup over: the
    question is then left unanswered (logged)."""

    def __init__(self, snapshot_db: str) -> None:
        self._snapshot_db = snapshot_db
        self._snap: Optional[sqlite3.Connection] = None
        self._live: Optional[sqlite3.Connection] = None

    def warning_for(self, tid: str) -> Optional[tuple[str, str]]:
        """``(code, message)`` for the job's warnings (see
        :class:`_BackupWarnings`), or None when there is nothing to say."""
        try:
            if self._live is None:
                self._live = sqlite3.connect(_live_db_path())
            if self._live.execute(
                "SELECT 1 FROM tours WHERE public_id = ?", (tid,)
            ).fetchone() is None:
                return "tourDeletedAfterSnapshot", (
                    f"tour {tid} was deleted after the DB snapshot; the archive "
                    "keeps its row but none of its attachments"
                )
            if self._snap is None:
                self._snap = sqlite3.connect(self._snapshot_db)
            texts = [
                r[0] for r in self._snap.execute(
                    "SELECT description FROM tours WHERE public_id = ? "
                    "UNION ALL SELECT s.description_md FROM steps s "
                    "JOIN tours t ON s.tour_id = t.id WHERE t.public_id = ?",
                    (tid, tid),
                )
            ]
        except sqlite3.Error as exc:
            _logger.warning(
                "backup: could not check why tour %s has no asset dir: %s", tid, exc,
            )
            return None
        if any(
            m.group(1) == tid
            for text in texts for m in _TOUR_ASSET_REF_RE.finditer(text or "")
        ):
            return "tourAssetsMissing", (
                f"tour {tid}'s markdown references its attachments, but its "
                "asset folder is missing; those images will not load after a "
                "restore"
            )
        return None

    def close(self) -> None:
        for conn in (self._snap, self._live):
            if conn is not None:
                conn.close()
        self._snap = self._live = None


def _left_out_kind(entry) -> str:
    """How to describe a tour-asset entry the archive leaves out, for the log
    (lstat; a racing removal just reads as "entry")."""
    try:
        if entry.is_symlink():
            return "symlink"
        if entry.is_dir(follow_symlinks=False):
            return "directory"
        if entry.is_file(follow_symlinks=False):
            return "dotfile"
        return "non-regular file"
    except OSError:
        return "entry"


def _add_tour_asset_files(
    tar: tarfile.TarFile, tour_dir: str, tour_pid: str, what: str,
    warn: Optional[Callable[..., None]] = None,
    cancel: Optional[_CancelCheck] = None,
) -> int:
    """Archive a tour's attachments (:func:`_tour_asset_files`) as
    ``assets/tours/<pid>/<name>``, each read from a pinned fd, and return how
    many were archived. ``what`` ("backup" / "export") only labels the log.
    ``warn``, when given, is also called as ``warn(description, code,
    tour=..., name=...)`` for each entry left out, for a caller that reports
    them beyond the log (the backup job's result; see _BackupWarnings for
    the codes).

    One file at a time, never a recursive ``tar.add`` of the dir: that lists
    the dir and then stats and opens each name, so an attachment deleted in
    between would raise out of the whole add and silently drop every file
    after it in the tour; it would also archive the in-flight upload temps,
    and it reads each file by path after taking its size, so one overwritten
    in the gap would be stored cut at the old size. Here a file deleted (or swapped for a
    symlink) while the archive runs costs only that file, and header and
    bytes come from the one inode the fd pins. Each entry that is left out
    (see :func:`_scan_tour_asset_dir`) gets its own WARNING, as the archive
    filter does for the links and FIFOs a recursive add meets.

    ``cancel`` (a backup's :class:`_CancelCheck`; exports pass none) is
    checked per file and while each streams."""
    def _skipped(detail: str, code: str, name: str, **extra) -> None:
        _logger.warning("%s: %s", what, detail)
        if warn is not None:
            warn(detail, code, tour=str(tour_pid), name=name, **extra)

    files, left_out = _scan_tour_asset_dir(tour_dir)
    for entry in left_out:
        kind = _left_out_kind(entry)
        _skipped(
            f"tour {tour_pid} asset {entry.name} is a {kind}, "
            "which tour uploads never create; not archived (only the dir's "
            "regular, non-dot files are)",
            "assetLeftOut", entry.name, kind=kind,
        )
    added = 0
    for entry in files:
        try:
            f = _open_pinned(entry.path)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError) and exc.errno != errno.ELOOP:
                raise
            _skipped(
                f"tour {tour_pid} asset {entry.name} was removed or replaced "
                "mid-archive; skipped",
                "assetRemoved", entry.name,
            )
            continue
        if f is None:
            _skipped(
                f"tour {tour_pid} asset {entry.name} was replaced by a "
                "non-regular file mid-archive; skipped",
                "assetNotRegular", entry.name,
            )
            continue
        with f:
            if _add_pinned_file(
                tar, _cancellable(f, cancel), f"assets/tours/{tour_pid}/{entry.name}",
            ):
                added += 1
            else:
                _skipped(
                    f"tour {tour_pid} asset {entry.name} is not a regular "
                    "file; skipped",
                    "assetNotRegular", entry.name,
                )
    return added


# ---------------------------------------------------------------------------
# tar streaming
# ---------------------------------------------------------------------------

class _CountingWriter:
    """Duck-typed file-like that wraps a writer to stream progress
    + sha256 as bytes pass through.

    A plain class rather than an ``io.RawIOBase`` subclass: gzip / zstd's
    streaming writers only need ``write`` (and optionally ``flush``), and
    ``RawIOBase``'s ``__del__`` emits a ResourceWarning when the
    ``closed`` flag isn't set — which would happen here because the
    archive close loop in :func:`create_backup` only closes ``tar``,
    ``compressor``, and ``raw`` (this object is a pass-through with no
    resources of its own).
    """

    def __init__(self, inner, on_progress: Optional[Callable[[int], None]] = None):
        self._inner = inner
        self._hash = hashlib.sha256()
        self._written = 0
        self._on_progress = on_progress

    def write(self, b) -> int:
        # Write the WHOLE buffer, looping on short writes. gzip / zstd treat
        # their fileobj.write like BufferedWriter.write (it must consume
        # everything); if the inner writer reports a short count the tail must
        # be re-offered. Hashing only b[:n] and advancing
        # by n without re-offering b[n:] would BOTH drop
        # bytes from the archive and desync the recorded sha256 from what hit
        # disk — surfacing only later as a verify/download integrity failure.
        mv = memoryview(b)
        total = 0
        while total < len(mv):
            n = self._inner.write(mv[total:])
            if not n:  # 0 or None -> no forward progress
                raise OSError("backup writer made no progress")
            total += n
        # The hash reflects exactly the bytes that hit disk (the full buffer).
        self._hash.update(mv)
        self._written += total
        if self._on_progress is not None:
            self._on_progress(self._written)
        return total

    def flush(self) -> None:
        # gzip.GzipFile calls .flush() on its fileobj in some sync paths;
        # forward to the wrapped writer so the buffered bytes hit disk
        # before sha256 finalisation runs.
        flush = getattr(self._inner, "flush", None)
        if flush is not None:
            flush()

    @property
    def sha256_hex(self) -> str:
        return self._hash.hexdigest()

    @property
    def written(self) -> int:
        return self._written


# Mode of every archive _open_archive_writer creates (backups, slide export
# bundles); see its docstring for why group read and never other.
_ARCHIVE_FILE_MODE = 0o640


def _open_archive_writer(path: str):
    """Open ``path`` for write and return the full archive stack.

    Returns ``(raw_file, tar_writer, counting_writer, compressor)``. The
    caller MUST close the tar writer, the compressor and the raw file — in
    that order — and collect every failure rather than letting one
    short-circuit the others; ``compressor.close()`` flushes the codec
    trailer that makes the archive readable and ``raw.close()`` flushes the
    page cache, so skipping either publishes a silently-truncated tar.zst /
    tar.gz. The counting writer holds no resources of its own.

    ``counting_writer`` wraps ``raw_file`` so the caller can stream
    progress + an sha256 over the bytes that actually hit disk.

    The file is created 0640 (owner read-write, group read, nothing for
    others) whatever the umask: a backup holds the whole wsi.db — password
    hashes, OAuth / LDAP secrets, session signing material — and a slide
    bundle can carry unpublished slides, so a plain ``open(path, "wb")``
    (0644 under the usual 022 umask) would leave them readable by every local
    account on the host. Group read is kept on purpose: it is what the
    service's ``UMask=0027`` gives these files in production (the
    ``wsi`` group — an operator's off-host copy job, say — reads the
    archives), and the same 0640 the restore stage normalises to
    (``_STAGED_FILE_MODE``); an owner-only 0600 would silently break such readers
    for no gain, since the group is the service's own. Applies to slide
    export bundles too (same writer). The fchmod covers a file that already
    existed under the name (O_TRUNC keeps its old mode) and a process
    running under a looser umask (a dev server, a test); the rename
    that publishes the archive keeps the inode, and with it the mode.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        _ARCHIVE_FILE_MODE,
    )
    try:
        os.fchmod(fd, _ARCHIVE_FILE_MODE)
        raw = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise
    counter = _CountingWriter(raw)
    if _HAS_ZSTD:
        # write_checksum embeds a per-frame xxhash the decompressor verifies
        # automatically on read (restore drains the stream to EOF so the
        # trailer is reached). It is the end-to-end guard for uploaded
        # archives, which carry no sidecar sha256; archives restored from
        # backup_dir are additionally hashed against their sidecar in the
        # same pass (see _HashingReader).
        cctx = _zstd.ZstdCompressor(level=3, write_checksum=True)
        compressor = cctx.stream_writer(counter, closefd=False)
        tar = tarfile.open(fileobj=compressor, mode="w|")
        return raw, tar, counter, compressor
    else:
        import gzip
        gz = gzip.GzipFile(fileobj=counter, mode="wb")
        tar = tarfile.open(fileobj=gz, mode="w|")
        return raw, tar, counter, gz


class ArchiveChecksumMismatch(RuntimeError):
    """The archive's bytes do not hash to the sha256 its sidecar recorded."""


class BackupDamaged(RuntimeError):
    """:func:`verify_backup` read the archive and it is bad — a codec or tar
    error, a checksum that does not match, no manifest — as opposed to it
    not being readable at all right now (an I/O or permission error, a
    missing codec library), which is a plain RuntimeError."""


class _HashingReader:
    """Read-side twin of :class:`_CountingWriter`: sha256 over the raw archive
    bytes in the order the decompressor pulls them.

    Lets restore check the sidecar sha256 in the same pass as extraction
    instead of a second full read of a possibly multi-TB file. Only valid
    while reads stay contiguous; a seek to any other offset (tarfile's
    ``r:*`` probing, a GzipFile rewind) marks the digest unusable and
    :meth:`verify` then skips the comparison rather than report a false
    mismatch.
    """

    def __init__(self, inner):
        self._inner = inner
        self._hash = hashlib.sha256()
        self._pos = 0
        self._sequential = True

    def read(self, n: int = -1) -> bytes:
        b = self._inner.read(n)
        if self._sequential:
            self._hash.update(b)
        self._pos += len(b)
        return b

    def seek(self, offset: int, whence: int = 0) -> int:
        new_pos = self._inner.seek(offset, whence)
        if new_pos != self._pos:
            self._sequential = False
        self._pos = new_pos
        return new_pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def verify(self, expected_sha256: str, path: str) -> None:
        """Hash the rest of the file and compare against ``expected_sha256``."""
        while self.read(1 << 20):
            pass
        if not self._sequential:
            _logger.warning(
                "backup: %s was read non-sequentially; sidecar sha256 not "
                "checked", path,
            )
            return
        if self._hash.hexdigest() != expected_sha256.lower():
            raise ArchiveChecksumMismatch(
                "archive does not match the sha256 recorded when it was "
                "created; the file is damaged or was modified"
            )


_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}\Z")


def _sidecar_sha256(archive_path: str) -> Optional[str]:
    """The sha256 recorded in ``archive_path``'s ``.meta.json`` sidecar, if any.

    Only archives in ``backup_dir`` have one (uploaded archives do not), and a
    missing or unreadable sidecar is tolerated exactly as :func:`list_backups`
    tolerates it — the archive is then restored on the codec checksums alone.
    """
    try:
        with open(archive_path + ".meta.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    sha = data.get("sha256") if isinstance(data, dict) else None
    if isinstance(sha, str) and _SHA256_HEX_RE.match(sha):
        return sha
    return None


# Extended headers: blocks tarfile reads into memory whole — at the size the
# header itself declares — before it hands over the member they describe, and
# so before any of _safe_extract's limits can run: pax extended / global
# headers (and Solaris' variant) and GNU long names / link names. Uncapped,
# ~24 KB of zstd declaring a 768 MiB pax header of zeros would peak at 1.6 GB RSS.
_EXTENDED_HEADER_TYPES = (
    tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
    tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
)
# What the extended headers in front of one member may declare in all. The
# app's writers emit at most one pax header per member: a path (PATH_MAX is 4 KiB)
# and a size / mtime record or two.
_MAX_EXTENDED_HEADER_BYTES = 1024 * 1024


def _pax_headers_size(headers: dict) -> int:
    """About what ``headers`` would take as pax records: each key and value
    encoded, plus the length field, space, ``=`` and newline around them.
    tarfile stores keys and values decoded (surrogateescape keeps any bytes),
    so this is the declared-size measure the cap uses for one header."""
    total = 0
    for key, value in headers.items():
        total += (
            len(str(key).encode("utf-8", "surrogateescape"))
            + len(str(value).encode("utf-8", "surrogateescape"))
            + 10
        )
    return total


class _ArchiveHeaderRefused(tarfile.TarError):
    """An archive header refused before tarfile reads what it declares.

    A TarError (so ``_CORRUPT_ARCHIVE_ERRORS`` reports a bad archive) but not
    a HeaderError: ``TarFile.next`` turns some of those, past the first
    member, into a silent end of archive."""


class _BoundedTarInfo(tarfile.TarInfo):
    """The TarInfo every archive is read with (``tarinfo=`` of tarfile.open):
    checks an extended header's declared size before tarfile reads it.

    ``_proc_member`` is where tarfile dispatches on a freshly parsed header
    block, its data still unread, for the first member, every later one and
    each header of a chain (3.12's ``fromtarfile`` and 3.13's
    ``_fromtarfile`` both instantiate this class). A chain's headers each stay
    in memory until the member at its end is read, so the cap is on their sum,
    tracked on the TarFile. Global headers' keys stay with the archive, so
    every member's budget starts from what they hold: tarfile merges them
    into one dict (``tf.pax_headers``), a repeated key replacing the last, so
    that is the dict's size once a global header is parsed — not the sum of
    every global header read, which would refuse an archive with a small global
    header in front of each of its members. A negative size (base-256)
    would read to EOF."""

    def _proc_member(self, tf):
        if self.type in _EXTENDED_HEADER_TYPES:
            spent = getattr(tf, "_wsi_extended_bytes", 0) + self.size
            if self.size < 0 or spent > _MAX_EXTENDED_HEADER_BYTES:
                raise _ArchiveHeaderRefused(
                    f"archive extended header declares {self.size} bytes; the "
                    f"headers of one member may declare at most "
                    f"{_MAX_EXTENDED_HEADER_BYTES}"
                )
            tf._wsi_extended_bytes = spent
        else:
            tf._wsi_extended_bytes = getattr(tf, "_wsi_global_bytes", 0)
        info = super()._proc_member(tf)
        if self.type == tarfile.XGLTYPE:
            # tarfile's _proc_pax parses the header into tf.pax_headers and
            # then reads the rest of the chain, member included, before it
            # returns here; from now on the globals hold what that dict does.
            # The member's own branch above already reset the budget to the
            # old figure, so reset it again to the new one.
            tf._wsi_global_bytes = _pax_headers_size(tf.pax_headers)
            tf._wsi_extended_bytes = tf._wsi_global_bytes
        return info

    # GNU sparse maps are the other header data read before a member is
    # handed over — old-style extension blocks, chained by a flag, and the
    # version 1.0 map at the start of the member's data, its length a count
    # in the map itself — with no size declared up front to check. Nothing
    # this app writes is sparse (tarfile's writer never emits it).
    def _proc_sparse(self, tf):
        raise _ArchiveHeaderRefused("archive has a GNU sparse member; not supported")

    def _proc_gnusparse_10(self, next, pax_headers, tf):
        raise _ArchiveHeaderRefused("archive has a GNU sparse member; not supported")


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"


def archive_format_from_magic(head: bytes) -> Optional[str]:
    """``"tar.zst"`` / ``"tar.gz"`` from an archive's first bytes, else None."""
    if head.startswith(_ZSTD_MAGIC):
        return "tar.zst"
    if head.startswith(_GZIP_MAGIC):
        return "tar.gz"
    return None


def _archive_format(path: str) -> Optional[str]:
    """How the archive at ``path`` is compressed. The content decides: a
    filename is whatever the uploader called it (``BACKUP.TAR.GZ``, a renamed
    or suffix-less file), and trusting it would make a valid gzip archive
    fail as zstd. The suffix (case-insensitive) is only the fallback for a file whose
    magic is not recognised, which then gets the tar auto-detect."""
    try:
        with open(path, "rb") as f:
            fmt = archive_format_from_magic(f.read(4))
    except OSError:
        fmt = None
    if fmt is not None:
        return fmt
    lower = path.lower()
    if lower.endswith(".tar.zst"):
        return "tar.zst"
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    return None


class _ZstdFrameTracker:
    """Passes the compressed bytes of a zstd archive through, following the
    frame's block structure (headers only; payloads are skipped by count),
    so :attr:`complete` says whether the last block and the checksum were
    read.

    python-zstandard's stream_reader ends the decompressed stream silently
    when its input runs out mid-frame, so a truncated .tar.zst (a partial
    copy, an interrupted transfer) would read as a shorter but valid
    stream: tarfile ends the archive quietly at a member boundary, the
    frame's checksum is never reached, and staging would go on without the
    trailing members. See :class:`_ZstdTruncationCheck`."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._state = "header"  # "header" (magic + descriptor) / "block" / "done"
        self._buf = bytearray()
        self._skip = 0
        self._then = ""
        self._checksum = False
        self.complete = False

    def read(self, n: int = -1) -> bytes:
        data = self._inner.read(n)
        if data and self._state != "done":
            self._feed(data)
        return data

    def _feed(self, data: bytes) -> None:
        i, n = 0, len(data)
        while i < n and self._state != "done":
            if self._skip:
                take = min(self._skip, n - i)
                self._skip -= take
                i += take
                if not self._skip:
                    self._enter(self._then)
                continue
            want = 5 if self._state == "header" else 3
            chunk = data[i:i + want - len(self._buf)]
            self._buf += chunk
            i += len(chunk)
            if len(self._buf) < want:
                continue
            head, self._buf = bytes(self._buf), bytearray()
            if self._state == "header":
                if head[:4] != _ZSTD_MAGIC:
                    # Not a frame this can follow: leave it to the codec.
                    self._state, self.complete = "done", True
                    return
                fhd = head[4]
                single = fhd >> 5 & 1
                self._checksum = bool(fhd >> 2 & 1)
                rest = ((0 if single else 1)               # window descriptor
                        + (0, 1, 2, 4)[fhd & 3]            # dictionary id
                        + ((1 if single else 0), 2, 4, 8)[fhd >> 6])  # content size
                self._skip_then(rest, "block")
            else:
                h = int.from_bytes(head, "little")
                size = 1 if (h >> 1 & 3) == 1 else h >> 3  # an RLE block stores one byte
                self._skip_then(size, "end" if h & 1 else "block")

    def _skip_then(self, count: int, state: str) -> None:
        if count:
            self._skip, self._then = count, state
        else:
            self._enter(state)

    def _enter(self, state: str) -> None:
        if state == "end":
            if self._checksum:
                self._checksum = False
                self._skip_then(4, "end")
                return
            self._state, self.complete = "done", True
            return
        self._state = state


class _ZstdTruncationCheck:
    """The decompressed side of :class:`_ZstdFrameTracker`: raises EOFError
    (one of ``_CORRUPT_ARCHIVE_ERRORS``, as GzipFile's own on a truncated
    .tar.gz) when the decompressed stream ends before its frame did."""

    def __init__(self, stream, frame: _ZstdFrameTracker) -> None:
        self._stream, self._frame = stream, frame

    def read(self, n: int = -1) -> bytes:
        data = self._stream.read(n)
        if not data and n != 0 and not self._frame.complete:
            raise EOFError("zstd archive is truncated: its data ends before its last block")
        return data


@contextlib.contextmanager
def _open_archive_reader(path: str, expected_sha256: Optional[str] = None):
    """Open archive for reading, transparently handling zst / gz.

    Yields a ``tarfile.TarFile``. The raw file handle (and, for zstd, the
    decompressor stream) is owned here and closed on exit: ``tarfile`` does
    not own an externally supplied ``fileobj``, so returning the bare TarFile
    would leak the underlying FD and zstd codec on every restore / preflight
    / stage.

    ``expected_sha256``, when given, is checked against the raw file bytes
    after the caller's block exits normally (the rest of the file is read to
    hash it); a mismatch raises ``RuntimeError``.

    Every header is read through :class:`_BoundedTarInfo` — the first member
    is already read inside ``tarfile.open``.
    """
    fmt = _archive_format(path)
    if fmt == "tar.zst" and not _HAS_ZSTD:
        raise RuntimeError(
            "Archive uses zstd compression but the zstandard library is "
            "not installed. Install python3-zstandard."
        )
    raw = open(path, "rb")
    try:
        src = _HashingReader(raw) if expected_sha256 else raw
        if fmt == "tar.zst":
            dctx = _zstd.ZstdDecompressor()
            frame = _ZstdFrameTracker(src)
            stream = dctx.stream_reader(frame, closefd=False)
            try:
                with tarfile.open(
                    fileobj=_ZstdTruncationCheck(stream, frame), mode="r|",
                    tarinfo=_BoundedTarInfo,
                ) as tar:
                    yield tar
            finally:
                stream.close()
        elif fmt == "tar.gz":
            # r:gz (GzipFile) rather than r|gz: tarfile's own stream reader
            # never checks the gzip trailer, GzipFile does once read to EOF.
            with tarfile.open(fileobj=src, mode="r:gz", tarinfo=_BoundedTarInfo) as tar:
                yield tar
        else:
            with tarfile.open(fileobj=src, mode="r:*", tarinfo=_BoundedTarInfo) as tar:
                yield tar
        if expected_sha256:
            src.verify(expected_sha256, path)
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Public API: create_backup
# ---------------------------------------------------------------------------

# The two names create_backup writes that outlive a killed run: the partial
# archive in backup_dir and the snapshot tmpdir under .staging. Slide exports
# stage as .staging/export-*.tar.*.tmp, so neither pattern can match theirs.
_PARTIAL_ARCHIVE_SUFFIXES = (".tar.zst.tmp", ".tar.gz.tmp")
_SNAPSHOT_TMPDIR_PREFIX = "wsi_backup_"


def _reclaim_abandoned_backup_files() -> int:
    """Remove what an earlier, killed backup left behind; return the count.

    A backup that dies without running its cleanup (SIGKILL, OOM, power loss,
    a worker restarted mid-run) leaves a partial ``<id>.tar.*.tmp`` — as large
    as the archive had got, terabytes for a full scope — and its
    ``.staging/wsi_backup_*`` snapshot dir. ``sweep_stale_staging`` reclaims
    both, but only once they are 24 h old, and only at startup or around a
    restore; the dashboard cannot remove them (the listing shows published
    archives only), and the next backup's free-space preflight counts the
    space they hold and refuses to start. Without this, the operator's only
    way out would be to wait a day or delete files by hand.

    Removed here regardless of age because of who calls it: create_backup
    runs only inside the single cross-worker job slot (the router claims it
    with ``_claim_job_slot`` before starting the thread; a slide export or
    import holds the same slot), so while this runs no other backup is in
    flight on any worker, and every file of this shape belongs to one that
    is no longer running. The TTL the sweep needs — it runs outside the slot,
    beside a backup that may be live — has nothing to protect here.
    """
    removed = 0
    backup_dir = settings.backup_dir
    try:
        names = os.listdir(backup_dir)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(_PARTIAL_ARCHIVE_SUFFIXES):
            continue
        path = os.path.join(backup_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            continue  # not something create_backup writes
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _logger.warning("backup: could not remove partial archive %s: %s", name, exc)
            continue
        removed += 1
        _logger.warning("backup: removed partial archive %s left by an interrupted backup", name)

    staging_root = os.path.join(backup_dir, ".staging")
    try:
        names = os.listdir(staging_root)
    except OSError:
        names = []
    for name in names:
        if not name.startswith(_SNAPSHOT_TMPDIR_PREFIX):
            continue
        path = os.path.join(staging_root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _logger.warning("backup: could not remove snapshot dir %s: %s", name, exc)
            continue
        removed += 1
        _logger.warning("backup: removed snapshot dir %s left by an interrupted backup", name)
    return removed


def _is_safe_wsi_filename(name: str) -> bool:
    """Whether a ``slides.wsi_filename`` is one restore accepts: a bare name,
    no path separator and no leading dot. The one rule both ends apply —
    :func:`validate_archive_integrity` refuses an archive whose DB holds
    any other, so :func:`create_backup` refuses to write one (a backup that
    reports success but can never be restored is worse than no backup)."""
    return "/" not in name and not name.startswith(".")


# Reasons a WSI the snapshot lists cannot go into a restorable archive, as the
# predicate of "<name> ...". Only _WSI_NOT_ON_DISK is compared against.
_WSI_NOT_ON_DISK = "is not on disk"
_WSI_UNSAFE_NAME = (
    "has a name restore refuses (a path separator or a leading dot)"
)


def _wsi_unarchivable_reason(
    wsi_root: str, wsi_root_real: str, fname: str, scope: str,
) -> Optional[str]:
    """Why ``fname`` (a snapshot ``wsi_filename``) cannot be archived in a
    form restore accepts, or None when nothing rules it out up front.

    Run over every listed WSI before the archive is opened, so a cause that
    no retry changes fails the backup in seconds with its real reason,
    instead of after the whole archive has been written (hours, for a full
    scope) with a "concurrent delete — retry" message. A metadata-scope
    archive carries no WSI bytes, so only the name rule applies to it.
    ``lstat`` / ``stat`` only: nothing is opened (a FIFO would block)."""
    if scope != SCOPE_FULL:
        return None if _is_safe_wsi_filename(fname) else _WSI_UNSAFE_NAME
    path = os.path.join(wsi_root, fname)
    if os.path.islink(path):
        return "is a symlink (restore does not support links)"
    if not _under_root(wsi_root_real, path):
        return "escapes the wsi root"
    if not _is_safe_wsi_filename(fname):
        return _WSI_UNSAFE_NAME
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _WSI_NOT_ON_DISK
    except OSError:
        return None  # let the archive loop raise it with its own errno
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file (restore accepts only regular files)"
    return None


def _unarchivable_wsi_error(reasons: dict, scope: str) -> RuntimeError:
    """The error for WSI files the database lists that cannot be archived,
    ``reasons`` mapping each name to its ``_wsi_unarchivable_reason``."""
    names = sorted(reasons)
    if all(reasons[n] == _WSI_NOT_ON_DISK for n in names):
        # Not a race: the rows exist but the files do not (typically a
        # metadata-only restore whose WSI files have not been re-uploaded
        # yet). Retrying cannot help, so say what does.
        return RuntimeError(
            "backup: full-scope archive missing "
            f"{len(names)} WSI file(s) that the database lists but "
            f"that are not on disk — first few: {names[:5]!r}. "
            "Re-upload or delete those slides, or take a backup "
            "without WSI files."
        )
    sample = "; ".join(f"{n!r} {reasons[n]}" for n in names[:5])
    fix = "fix or delete those slides"
    # A name restore refuses fails a metadata-scope archive too, so only
    # offer that way out when every name is one restore accepts.
    if scope == SCOPE_FULL and all(_is_safe_wsi_filename(n) for n in names):
        fix += ", or take a backup without WSI files"
    return RuntimeError(
        f"backup: {len(names)} WSI file(s) the database lists cannot be put "
        f"in an archive that restore accepts — first few: {sample}. Retrying "
        f"will not help: replace each with the regular file under a plain "
        f"name in wsi/, {fix}."
    )


def _wsi_changed_since_snapshot(
    names: list[str], generations: dict[int, tuple], wsi_root: str,
) -> set[str]:
    """Of ``names`` (WSI files the pre-scan found absent), the ones whose
    slide no longer describes the snapshot's file generation in the live DB
    — deleted or replaced after the snapshot, which a retry fixes — as
    opposed to rows whose file is simply not there.

    Each is asked through :func:`_open_snapshot_generation`, the archive
    loop's own check, so it runs under the slide's mutation lock: a delete
    that has unlinked the file but not yet committed the row cannot read as
    "row unchanged, file missing". A file that has meanwhile reappeared for
    an unchanged row is not "changed" either (the open is closed at once and
    the name keeps its pre-scan reason; that window is too narrow to be
    worth a second stat). Failing to ask (no live DB, an OSError other
    than the expected ENOENT) leaves the name to its permanent "not on
    disk" error."""
    if not names:
        return set()
    by_name: dict[str, list[int]] = {}
    for sid, gen in generations.items():
        if gen[1]:
            by_name.setdefault(gen[1], []).append(sid)
    changed: set[str] = set()
    try:
        live_conn = sqlite3.connect(_live_db_path())
    except sqlite3.Error as exc:
        _logger.warning("backup: could not re-check absent WSI files: %s", exc)
        return changed
    try:
        for fname in names:
            for sid in by_name.get(fname, ()):
                try:
                    f = _open_snapshot_generation(
                        live_conn, sid, generations[sid],
                        os.path.join(wsi_root, fname), _WSI_GENERATION,
                    )
                except _GenerationChanged:
                    changed.add(fname)
                    break
                except (OSError, sqlite3.Error):
                    continue
                if f is not None:
                    f.close()
    finally:
        live_conn.close()
    return changed


# Warnings one backup run reports on its job result; the server log has all.
_MAX_BACKUP_WARNINGS = 50


class _BackupWarnings:
    """What a backup left out of the archive it still published — collected
    for the job result, capped so a pathological install cannot grow the
    job record without bound. Logging stays with the call sites.

    Each warning is kept twice: the English sentence (``as_list``, the job
    result's ``warnings`` — what the log says, and the fallback for a code
    the dashboard does not know) and a ``{code, params}`` item
    (``as_items``, the result's ``warning_items``, index-aligned with
    ``warnings``) that the dashboard renders through its i18n bundle as
    ``adminBackup.warning.<code>``. Without the codes, a non-English
    dashboard could only show the English sentences raw. The codes, one per
    kind of leftover: ``invalidTourId``, ``tourDeletedAfterSnapshot``,
    ``tourAssetsMissing`` (_MissingTourDirCheck), ``assetLeftOut``,
    ``assetRemoved``, ``assetNotRegular`` (_add_tour_asset_files),
    ``thumbnailUnsafePath``, ``thumbnailMissing``, ``thumbnailChanged``,
    ``thumbnailNotRegular``, ``sidecarWriteFailed``, and ``more`` for the
    overflow line. ``params`` hold only names and ids (no prose), so a
    client that does not know a code falls back to the sentence at the same
    index."""

    def __init__(self, cap: int = _MAX_BACKUP_WARNINGS) -> None:
        self._cap = cap
        self._items: list[str] = []
        self._coded: list[dict] = []
        self._dropped = 0

    def add(self, message: str, code: Optional[str] = None, **params) -> None:
        if len(self._items) < self._cap:
            self._items.append(message)
            self._coded.append({"code": code or "unknown", "params": params})
        else:
            self._dropped += 1

    def as_list(self) -> list[str]:
        if not self._dropped:
            return list(self._items)
        return self._items + [
            f"…and {self._dropped} more (see the server log)"
        ]

    def as_items(self) -> list[dict]:
        items = [{"code": i["code"], "params": dict(i["params"])} for i in self._coded]
        if self._dropped:
            items.append({"code": "more", "params": {"count": self._dropped}})
        return items


def create_backup(
    scope: str,
    note: str = "",
    progress_cb: Optional[Callable[[dict], None]] = None,
    include_analytics: bool = True,
    include_history: bool = True,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> BackupRecord:
    """Snapshot the current install into ``settings.backup_dir`` and return a record.

    progress_cb is called with ``{"phase": ..., "bytes_written": int}``
    periodically. Phases: ``"snapshot_db"``, ``"writing_assets"``,
    ``"writing_wsi"``, ``"finalizing"``.

    ``should_cancel``, when given, is polled for an operator's cancel request
    (the router passes the job store's cross-worker marker check): at every
    phase boundary, before each tour / asset / WSI / thumbnail, and — time-
    throttled (``_CANCEL_CHECK_INTERVAL_SEC``) — while one file's bytes
    stream. A True answer raises :class:`BackupCancelled` through the same
    cleanup as any failure: the partial ``.tmp`` archive and the snapshot dir
    are removed and nothing is published. The last check sits right before
    the rename that publishes the archive; a request arriving after it is
    too late, and the backup completes normally. So a raised
    BackupCancelled always means "no archive", and a returned record always
    means "published" — never one with the other's outcome.

    ``include_analytics`` / ``include_history`` govern the ancillary DB table
    groups (access statistics and the admin audit trail). When False, those
    tables' rows are emptied in the snapshot copy before it is archived; the
    schema is preserved so a restore brings them back as empty tables. WSI /
    thumbnail presence is governed by ``scope`` (full vs metadata).

    The returned record's ``warnings`` lists what the archive left out
    without failing the backup (see :class:`BackupRecord`).

    The caller must hold the cross-worker job slot (``_claim_job_slot`` in
    the router, the only caller): the run starts by deleting every partial
    archive and snapshot dir in backup_dir as abandoned, which is only true
    while no other backup can be running.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    cancel = _CancelCheck(should_cancel)
    _ensure_backup_dirs()
    # Before the preflight, which would otherwise count a killed run's
    # partial archive against this one (see the helper).
    _reclaim_abandoned_backup_files()
    warnings = _BackupWarnings()
    cancel.check()

    # Cheap pre-flight: refuse to start a backup that obviously can't fit on
    # the backup volume even at a generous compression ratio. The archive
    # writer otherwise discovers ENOSPC mid-write, after which the tmp-file
    # cleanup races the close-stack and can leak partial archives. Mirrors
    # the equivalent check in stage_restore().
    _preflight_backup_free_space(scope)

    backup_id = _utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    archive_path = _archive_path(backup_id)
    tmp_archive = archive_path + ".tmp"

    # Holds the snapshot tmpdir once the TemporaryDirectory below is open, so
    # ``emit`` can keep its mtime fresh for the duration of the backup.
    snapshot_tmpdir: Optional[str] = None

    def emit(phase: str, written: int, **extra) -> None:
        if progress_cb is not None:
            progress_cb({"phase": phase, "bytes_written": written, **extra})
        # Keep the snapshot tmpdir's mtime fresh while the backup runs. The
        # lifespan-startup ``sweep_stale_staging()`` reclaims ``wsi_backup_*``
        # dirs by mtime, and this dir's mtime would otherwise freeze at
        # snapshot_db-write time. A full backup that streams a huge wsi/ corpus
        # for >24h could then have its snapshot tmpdir swept out from under it
        # by a sibling worker's startup sweep (which runs outside the job slot).
        # ``emit`` fires every <=2s / 25 files during the long wsi phase, so an
        # ``os.utime`` here keeps the dir well inside the TTL. Best-effort: a
        # failed touch just leaves the sweep window above unmitigated.
        if snapshot_tmpdir is not None:
            try:
                os.utime(snapshot_tmpdir)
            except OSError:
                pass

    # Snapshot the DB into BACKUP_DIR/.staging rather than the system temp
    # dir. Under the service's ``PrivateTmp=true`` sandbox the default temp
    # lives on a separate (often tmpfs / RAM-backed) filesystem that
    # ``_preflight_backup_free_space`` does not measure, so a large DB could
    # ENOSPC there even when the backup volume has ample room. Staging on the
    # backup volume keeps the snapshot on the same filesystem the preflight
    # checked — and matches where ``stage_restore`` / ``create_reset_stage``
    # already write their intermediate state.
    staging_root = os.path.join(settings.backup_dir, ".staging")
    with tempfile.TemporaryDirectory(prefix=_SNAPSHOT_TMPDIR_PREFIX, dir=staging_root) as tmpdir:
        snapshot_tmpdir = tmpdir  # arm emit()'s mtime refresh
        snapshot_db = os.path.join(tmpdir, "wsi.db")
        emit("snapshot_db", 0)
        _snapshot_sqlite(_live_db_path(), snapshot_db)
        # The archive's point in time is when the DB was copied, not when the
        # manifest is written: the strip, compaction and stamps below can take
        # minutes on a large DB, and a restore brings back the state of now.
        snapshot_at = _utc_iso_z()
        # Cancel checks from here on raise inside this ``with``, so the
        # TemporaryDirectory removes the snapshot on the way out.
        cancel.check()
        # The file generation each snapshot row describes, so every WSI /
        # thumbnail below is archived only if it is still that generation —
        # and, straight away, before the WAL switch / strip / compaction /
        # stamps below (minutes on a large DB), each WSI held at that
        # generation by a hard link (see _pin_snapshot_wsi_files), so a slide
        # replaced or deleted from here on no longer fails the backup.
        wsi_root = os.path.join(settings.data_dir, "wsi")
        generations: dict[int, tuple] = {}
        wsi_pins: dict[int, str] = {}
        wsi_bytes_total = 0
        if scope == SCOPE_FULL:
            _snap = sqlite3.connect(snapshot_db)
            try:
                generations = _read_file_generations(_snap)
            finally:
                _snap.close()
            wsi_pins, wsi_bytes_total = _pin_snapshot_wsi_files(
                generations, wsi_root, os.path.join(tmpdir, "wsi-pins"), cancel,
            )
            # The unpinned ones (no links possible) still count toward the
            # total the dashboard shows progress against.
            for sid, gen in generations.items():
                if gen[1] and sid not in wsi_pins:
                    try:
                        wsi_bytes_total += os.path.getsize(os.path.join(wsi_root, gen[1]))
                    except OSError:
                        pass
        # Out of WAL mode before anything below writes to the copy, so the
        # strip and the stamps are in wsi.db itself (see the helper).
        _use_rollback_journal(snapshot_db)
        # Drop opted-out ancillary table groups from the snapshot copy before
        # it is archived. Done on the frozen copy (never the live DB) so live
        # traffic is unaffected; the tables themselves remain, just emptied.
        strip_tables: list[str] = []
        if not include_analytics:
            strip_tables.extend(_ANALYTICS_TABLES)
        if not include_history:
            strip_tables.extend(_HISTORY_TABLES)
        if strip_tables:
            _strip_tables_in_place(snapshot_db, strip_tables)
            cancel.check()
        # Stamp the snapshot's app_version so downstream consumers don't have
        # to know how to read APP_VERSION at backup-time. Do the same for the
        # integer schema_version so restore can make a schema-precise decision.
        app_version = _version_to_stamp(_app_meta.sqlite_read_app_version(snapshot_db))
        _app_meta.sqlite_set_app_version(snapshot_db, app_version)
        _app_meta.sqlite_set_schema_version(snapshot_db, CURRENT_SCHEMA_VERSION)
        slide_count, tour_count = _read_db_counts(snapshot_db)
        # Snapshot the disk-state enumeration from the *frozen* DB. Walking
        # the live directories below would race concurrent slide / tour
        # mutations and either include files orphaned by deletes or skip
        # files added after the DB snapshot.
        wsi_filenames, tour_ids = _enumerate_snapshot(snapshot_db)

        # Fail fast on every listed WSI that cannot go into a restorable
        # archive for a reason no retry changes — absent, a symlink, not a
        # regular file, outside wsi/, a name restore refuses — before the
        # archive is opened, not after it has been written in full. The
        # per-file checks in the archive loop below stay: they catch what
        # changes between here and there.
        wsi_root = os.path.join(settings.data_dir, "wsi")
        wsi_root_real = os.path.realpath(wsi_root)
        unarchivable: dict[str, str] = {}
        # A pinned file is archived from its link, which holds the snapshot's
        # own regular file: what its name in wsi/ has become since (replaced,
        # deleted, swapped for a link) does not matter to this archive.
        pinned_names = {generations[sid][1] for sid in wsi_pins}
        for fname in sorted({n for n in wsi_filenames if n} - pinned_names):
            reason = _wsi_unarchivable_reason(wsi_root, wsi_root_real, fname, scope)
            if reason is not None:
                unarchivable[fname] = reason
        # "Not on disk" has two causes the stat above cannot tell apart: a
        # row whose file never came back (a metadata-only restore that has
        # not been re-uploaded — permanent, a retry fails the same way), and
        # a slide deleted or replaced *after* the snapshot, during the
        # rollback-journal switch / strip VACUUM / stamps above, which can
        # take a while on a large DB. The second is the race the archive
        # loop's _GenerationChanged reports as "retry"; telling that
        # operator to re-upload or delete a slide that is already gone (or
        # already new) is wrong. Ask the live DB, under the slide's mutation
        # lock like the loop does, whether the row still describes the
        # snapshot's file generation.
        changed_after_snapshot = _wsi_changed_since_snapshot(
            [n for n, r in unarchivable.items() if r == _WSI_NOT_ON_DISK],
            generations, wsi_root,
        )
        for fname in changed_after_snapshot:
            unarchivable.pop(fname, None)
        if changed_after_snapshot and not unarchivable:
            # Only the race: the same retry message the loop gives.
            _logger.warning(
                "backup: wsi file(s) %r changed after the DB snapshot",
                sorted(changed_after_snapshot)[:20],
            )
            raise RuntimeError(
                f"backup: {len(changed_after_snapshot)} slide(s)' WSI file was "
                "replaced or deleted after the DB snapshot was taken, so the "
                "archive would pair the snapshot's metadata with a different "
                "image. Retry the backup once the slides are no longer being "
                "changed."
            )
        if unarchivable:
            for fname, reason in sorted(unarchivable.items())[:20]:
                _logger.warning("backup: wsi file %r %s; not archived", fname, reason)
            raise _unarchivable_wsi_error(unarchivable, scope)

        manifest = Manifest(
            scope=scope,
            app_version=app_version,
            created_at=snapshot_at,
            slide_count=slide_count,
            tour_count=tour_count,
            note=note,
            archive_format=_archive_extension(),
            includes_analytics=include_analytics,
            includes_history=include_history,
            schema_version=CURRENT_SCHEMA_VERSION,
            python_version=platform.python_version(),
            python_packages=_installed_python_packages(),
        )
        manifest_bytes = json.dumps(manifest.to_dict(), indent=2).encode("utf-8")
        # Last check before the .tmp archive exists.
        cancel.check()

        # Track which WSI files actually made it into the archive so the
        # post-write integrity check can compare against db_wsi_set.
        wsi_files_written: set[str] = set()
        # ...and, for one left out, why — so the failure below names the real
        # cause (a symlink swapped in mid-run) instead of guessing at a race.
        wsi_skip_reasons: dict[str, str] = {}

        raw, tar, counter, compressor = _open_archive_writer(tmp_archive)
        write_failed = False
        cancelled_mid_write = False
        # Live-DB connection for the per-file generation checks (full only).
        live_conn: Optional[sqlite3.Connection] = None
        missing_tour_dirs = _MissingTourDirCheck(snapshot_db)
        try:
            # 1. manifest first so list-from-tail tools can read it cheaply.
            #    Run through ``_archive_filter`` to keep the invariant
            #    "every member in the archive goes through the filter"
            #    explicit, even though a bare TarInfo() already has the
            #    uid/gid/uname/gname defaults the filter would produce.
            mi = tarfile.TarInfo(MANIFEST_NAME)
            mi.size = len(manifest_bytes)
            mi.mtime = int(_utc_now().timestamp())
            mi_filtered = _archive_filter(mi)
            if mi_filtered is None:
                raise RuntimeError("archive filter rejected the manifest entry")
            tar.addfile(mi_filtered, io.BytesIO(manifest_bytes))

            # 2. DB snapshot. Pass the filter so the recorded ownership
            #    matches the rest of the archive (reproducibility across
            #    machines that run the service under different UIDs).
            #    Only wsi.db is archived, so a WAL beside it would hold
            #    writes the archive silently lacks; _use_rollback_journal
            #    means there is none, and this keeps it that way.
            if os.path.lexists(snapshot_db + "-wal"):
                raise RuntimeError(
                    "backup: the DB snapshot still has a write-ahead log; "
                    "refusing to archive it without the WAL's writes"
                )
            tar.add(snapshot_db, arcname=DB_ENTRY, filter=_archive_filter)
            emit("snapshot_db", counter.written)
            cancel.check()

            # 3. Tour assets. Iterate tour ids from the snapshot DB rather
            # than walking ``assets/tours/`` so a tour created or deleted
            # between snapshot and disk-walk does not desync the archive
            # from the DB rows it ships with.
            assets_root = os.path.join(settings.data_dir, "assets")
            tours_root = os.path.join(assets_root, "tours")
            if os.path.isdir(assets_root) and tour_ids:
                emit("writing_assets", counter.written)
                # Emit the parent dirs explicitly so empty-asset tours still
                # restore into the right hierarchy.
                _add_dir_entry(tar, assets_root, "assets")
                _add_dir_entry(tar, tours_root, "assets/tours")
                for tid in tour_ids:
                    cancel.check()
                    if not valid_public_id(tid):
                        # The token becomes a path segment below. A crafted
                        # value (e.g. "../.." from a restored DB) would make
                        # this tar.add walk DATA_DIR itself — earlier archives,
                        # the raw live wsi.db/-wal with the analytics tables
                        # the snapshot excluded — into the archive.
                        _logger.warning(
                            "backup: tour public_id %r is not a valid token; "
                            "its assets are not archived", tid,
                        )
                        warnings.add(
                            f"tour public_id {tid!r} is not a valid token; "
                            "its assets are not archived",
                            "invalidTourId", tour=str(tid),
                        )
                        continue
                    tour_dir = os.path.join(tours_root, str(tid))
                    if os.path.islink(tour_dir):
                        # A symlink was never archived (the filter drops
                        # links); don't start listing whatever it points at.
                        continue
                    if not os.path.isdir(tour_dir):
                        # No assets uploaded for this tour, or the tour was
                        # deleted after the snapshot — neither is fatal, but
                        # the second loses attachments the archived row had
                        # (see _MissingTourDirCheck).
                        found = missing_tour_dirs.warning_for(str(tid))
                        if found:
                            code, reason = found
                            _logger.warning("backup: %s", reason)
                            warnings.add(reason, code, tour=str(tid))
                        continue
                    # The tour's own entry first, as a recursive add would
                    # write it, so an empty tour dir still round-trips; then
                    # its files one by one (see _add_tour_asset_files).
                    _add_dir_entry(tar, tour_dir, f"assets/tours/{tid}")
                    added = _add_tour_asset_files(
                        tar, tour_dir, tid, "backup", warn=warnings.add,
                        cancel=cancel,
                    )
                    if not added and not os.path.isdir(tour_dir):
                        # The dir went between the check above and its scan
                        # (a tour delete removes both row and dir): the same
                        # question as a dir that was never there.
                        found = missing_tour_dirs.warning_for(str(tid))
                        if found:
                            code, reason = found
                            _logger.warning("backup: %s", reason)
                            warnings.add(reason, code, tour=str(tid))

            # 4. WSI files (only when full). Same rationale as assets:
            # enumerate from the snapshot, tolerate per-file disappearance —
            # and archive each file only while it is still the generation the
            # snapshot describes: from the link taken at snapshot time (see
            # _pin_snapshot_wsi_files), else checked now (see
            # _open_snapshot_generation).
            if scope == SCOPE_FULL:
                live_conn = sqlite3.connect(_live_db_path())
                wsi_root = os.path.join(settings.data_dir, "wsi")
                wsi_entries = [(sid, gen[1]) for sid, gen in generations.items() if gen[1]]
                if os.path.isdir(wsi_root) and wsi_entries:
                    emit("writing_wsi", counter.written, wsi_bytes_read=0, wsi_bytes_total=wsi_bytes_total)
                    _add_dir_entry(tar, wsi_root, "wsi")
                    # Emit progress *inside* the loop (throttled) so a full
                    # backup of many/large WSI files doesn't sit frozen on the
                    # single pre-loop emit for ~99% of its runtime — the
                    # dashboard couldn't tell a healthy multi-hour archive from a
                    # hang. counter.written advances as each file streams through
                    # the compressor, so re-emitting it shows real movement.
                    # Throttled to every 25 files or ~2s so the per-emit job
                    # write (a JSON file + os.replace) stays cheap.
                    # Also while one file streams (_ProgressReader), with the
                    # input bytes read against the snapshot's total, so the
                    # dashboard can show how far along the phase is: the
                    # archive bytes alone (compressed, unknown total) would
                    # stand still for as long as one multi-GB slide takes.
                    _wsi_progress = {"last": time.monotonic(), "read": 0}

                    def _emit_wsi(now: float) -> None:
                        _wsi_progress["last"] = now
                        emit(
                            "writing_wsi", counter.written,
                            wsi_bytes_read=_wsi_progress["read"],
                            wsi_bytes_total=wsi_bytes_total,
                        )

                    def _wsi_read(n: int) -> None:
                        _wsi_progress["read"] += n
                        now = time.monotonic()
                        if now - _wsi_progress["last"] >= 2.0:
                            _emit_wsi(now)

                    wsi_root_real = os.path.realpath(wsi_root)
                    for _idx, (slide_id, fname) in enumerate(wsi_entries):
                        cancel.check()
                        path = os.path.join(wsi_root, fname)
                        pinned = wsi_pins.get(slide_id)
                        if pinned is not None:
                            # Linked into the snapshot dir right after the
                            # snapshot, under the slide's lock and after its
                            # generation check (_pin_snapshot_wsi_files): the
                            # snapshot's own bytes, whatever happened to the
                            # live name since.
                            f = _open_pinned(pinned)
                        else:
                            # The pre-scan above refused all of these up front;
                            # these re-checks catch a change since (a symlink
                            # swapped in, a file removed out of band). Each skip
                            # records why and leaves the name out of
                            # wsi_files_written, so the cross-check below fails
                            # the backup with that reason rather than ship an
                            # archive whose DB references a file it lacks.
                            if os.path.islink(path):
                                # _archive_filter drops symlinks (restore refuses
                                # them), so a symlinked WSI's bytes would NOT be in
                                # the archive.
                                _logger.warning(
                                    "backup: wsi file %s is a symlink; not archived "
                                    "(restore does not support links)", fname,
                                )
                                wsi_skip_reasons[fname] = (
                                    "is a symlink (restore does not support links)"
                                )
                                continue
                            if not _under_root(wsi_root_real, path):
                                _logger.warning(
                                    "backup: wsi filename %r escapes the wsi root; "
                                    "not archived", fname,
                                )
                                wsi_skip_reasons[fname] = "escapes the wsi root"
                                continue
                            try:
                                f = _open_snapshot_generation(
                                    live_conn, slide_id, generations[slide_id], path,
                                    _WSI_GENERATION,
                                )
                            except FileNotFoundError as exc:
                                _logger.warning(
                                    "backup: wsi file %s missing at archive time: %s",
                                    fname, exc,
                                )
                                continue
                            except OSError as exc:
                                if exc.errno != errno.ELOOP:
                                    raise
                                # O_NOFOLLOW met a symlink swapped in after the
                                # islink check above.
                                _logger.warning(
                                    "backup: wsi file %s became a symlink; not "
                                    "archived", fname,
                                )
                                wsi_skip_reasons[fname] = (
                                    "is a symlink (restore does not support links)"
                                )
                                continue
                            except _GenerationChanged:
                                # Fail now rather than at the presence check below:
                                # the rest of a multi-hour archive would be wasted
                                # work on a backup that cannot be published.
                                raise RuntimeError(
                                    f"backup: slide {slide_id}'s WSI file was replaced "
                                    "or deleted after the DB snapshot was taken, so the "
                                    "archive would pair the snapshot's metadata with a "
                                    "different image. Retry the backup once the slide "
                                    "is no longer being changed."
                                ) from None
                        if f is None:
                            # A FIFO / device / directory at the path (opened
                            # non-blocking, so a FIFO cannot hang the run).
                            _logger.warning(
                                "backup: wsi file %s is not a regular file; "
                                "not archived", fname,
                            )
                            wsi_skip_reasons[fname] = (
                                "is not a regular file (restore accepts only "
                                "regular files)"
                            )
                            continue
                        with f:
                            if not _add_pinned_file(
                                tar,
                                _cancellable(_ProgressReader(f, _wsi_read), cancel),
                                f"wsi/{fname}",
                            ):
                                _logger.warning(
                                    "backup: wsi file %s is not a regular file; "
                                    "not archived", fname,
                                )
                                wsi_skip_reasons[fname] = (
                                    "is not a regular file (restore accepts "
                                    "only regular files)"
                                )
                                continue
                        wsi_files_written.add(fname)
                        now = time.monotonic()
                        if (_idx + 1) % 25 == 0 or (now - _wsi_progress["last"]) >= 2.0:
                            _emit_wsi(now)

            # 5. Thumbnails (only when full). Bundled alongside wsi/ because
            # thumbnails are produced only at upload / replace time and are
            # NOT regenerated on demand; a full-scope restore onto a fresh
            # host would otherwise show a wall of broken slide cards until a
            # manual rebuild. Enumerated from the snapshot DB (not a thumbs/
            # walk) for the same DB-consistency reason as wsi/. A thumbnail
            # missing on disk — or no longer the snapshot's generation — is
            # non-fatal and simply left out: the post-restore rebuild action
            # regenerates it from the WSI file, which beats restoring a
            # thumbnail of a different image.
            if scope == SCOPE_FULL:
                thumbs_root = os.path.join(settings.data_dir, "thumbs")
                thumb_entries = [(sid, gen[0]) for sid, gen in generations.items() if gen[0]]
                if os.path.isdir(thumbs_root) and thumb_entries:
                    _add_dir_entry(tar, thumbs_root, "thumbs")
                    thumbs_root_real = os.path.realpath(thumbs_root)
                    for slide_id, tname in thumb_entries:
                        cancel.check()
                        tpath = os.path.join(thumbs_root, tname)
                        if os.path.islink(tpath) or not _under_root(
                            thumbs_root_real, tpath
                        ):
                            # A thumbnail is regenerable, so an unusable name is
                            # only skipped (no integrity cross-check to trip).
                            _logger.warning(
                                "backup: thumbnail %r is a link or escapes the "
                                "thumbs root; not archived", tname,
                            )
                            warnings.add(
                                f"thumbnail {tname!r} is a link or escapes the "
                                "thumbs root; not archived",
                                "thumbnailUnsafePath", name=str(tname),
                                slide=slide_id,
                            )
                            continue
                        # Each skip below is non-fatal but also reported on
                        # the job: the archive restores without these
                        # thumbnails until someone runs the rebuild.
                        try:
                            f = _open_snapshot_generation(
                                live_conn, slide_id, generations[slide_id], tpath,
                                _THUMB_GENERATION,
                            )
                        except FileNotFoundError as exc:
                            _logger.warning(
                                "backup: thumbnail %s missing at archive time: %s",
                                tname, exc,
                            )
                            warnings.add(
                                f"thumbnail {tname} is missing on disk; not "
                                "archived (rebuild thumbnails after a restore)",
                                "thumbnailMissing", name=str(tname),
                                slide=slide_id,
                            )
                            continue
                        except OSError as exc:
                            if exc.errno != errno.ELOOP:
                                raise
                            f = None
                        except _GenerationChanged:
                            _logger.warning(
                                "backup: slide %s changed after the DB snapshot; "
                                "thumbnail %s not archived (rebuild it after a "
                                "restore)", slide_id, tname,
                            )
                            warnings.add(
                                f"slide {slide_id} changed after the DB snapshot; "
                                f"thumbnail {tname} not archived (rebuild it "
                                "after a restore)",
                                "thumbnailChanged", name=str(tname),
                                slide=slide_id,
                            )
                            continue
                        archived = False
                        if f is not None:
                            with f:
                                archived = _add_pinned_file(
                                    tar, _cancellable(f, cancel), f"thumbs/{tname}",
                                )
                        if not archived:
                            _logger.warning(
                                "backup: thumbnail %s is not a regular file; "
                                "not archived", tname,
                            )
                            warnings.add(
                                f"thumbnail {tname} is not a regular file; "
                                "not archived",
                                "thumbnailNotRegular", name=str(tname),
                                slide=slide_id,
                            )
        except BaseException as exc:
            # A source file failed to read as something other than
            # FileNotFoundError (PermissionError, OSError), _add_dir_entry
            # raised, etc. Flag so the finally reclaims the partial .tmp after
            # the close stack has run — otherwise a multi-GB partial archive
            # leaks until the next backup or the 24 h stale-staging sweep.
            write_failed = True
            cancelled_mid_write = isinstance(exc, BackupCancelled)
            raise
        finally:
            # Close every layer of the archive stack — tar, compressor,
            # raw file — and collect their failures rather than letting one
            # short-circuit the others. ``tar.close()`` flushes the trailer
            # block, ``compressor.close()`` flushes the codec trailer that
            # makes the archive readable at all, and ``raw.close()`` flushes
            # the page cache; any of them raising would otherwise either
            # leak file descriptors or publish a silently-truncated archive
            # as a "successful" backup. If anything failed, remove the tmp
            # so a broken archive is never renamed into the published path.
            if live_conn is not None:
                live_conn.close()
            missing_tour_dirs.close()
            close_errors: list[tuple[str, BaseException]] = []
            for name, closer in (
                ("tar", tar.close),
                ("compressor", compressor.close),
                ("raw", raw.close),
            ):
                try:
                    closer()
                except BaseException as exc:
                    close_errors.append((name, exc))
            if close_errors or write_failed:
                try:
                    os.remove(tmp_archive)
                except FileNotFoundError:
                    pass
            # A cancel can stop the tar stream mid-member, and closing a
            # stream cut there may itself complain. The archive is being
            # discarded either way; let the BackupCancelled propagate rather
            # than be replaced by a close error that would record the job as
            # failed.
            if close_errors and not cancelled_mid_write:
                detail = ", ".join(f"{n}={exc!r}" for n, exc in close_errors)
                raise RuntimeError(
                    f"backup archive close failed: {detail}"
                ) from close_errors[0][1]

    # Integrity sanity-check: full-scope archives must carry every WSI
    # file their DB snapshot references. Without this check, a slide
    # deleted (or its file replaced) between snapshot and tar.add would
    # produce an apparently-successful archive whose DB has a wsi_filename
    # column pointing at bytes that aren't in the archive — only detected
    # at restore time by validate_archive_integrity, which is too late to
    # fix without re-running the backup.
    if scope == SCOPE_FULL:
        db_wsi_set = {n for n in wsi_filenames if n}
        missing = sorted(db_wsi_set - wsi_files_written)
        if missing:
            try:
                os.remove(tmp_archive)
            except FileNotFoundError:
                pass
            # A file left out for a reason the loop saw (a symlink or FIFO
            # that appeared mid-run) is reported as that, not as a race a
            # retry would fix.
            known = {n: wsi_skip_reasons[n] for n in missing if n in wsi_skip_reasons}
            if known:
                raise _unarchivable_wsi_error(known, scope)
            wsi_dir = os.path.join(settings.data_dir, "wsi")
            absent = [n for n in missing if not os.path.lexists(os.path.join(wsi_dir, n))]
            if len(absent) == len(missing):
                raise _unarchivable_wsi_error(
                    {n: _WSI_NOT_ON_DISK for n in missing}, scope,
                )
            raise RuntimeError(
                "backup: full-scope archive missing "
                f"{len(missing)} WSI file(s) referenced by the DB snapshot — "
                f"first few: {missing[:5]!r}. "
                "Likely cause: a concurrent slide delete or replace ran "
                "during the backup. Retry the backup once the live state "
                "stops mutating."
            )

    # Finalize: fsync, write the sidecar, then atomically publish. Any failure
    # here (ENOSPC on fsync, an os.replace error, a cancel) must reclaim the
    # .tmp — and a sidecar already written for it — rather than leave the
    # multi-GB partial behind. os.replace removes tmp_archive on success, so
    # the cleanup is a harmless no-op once the archive is published.
    sidecar = archive_path + ".meta.json"
    sidecar_written = False
    published = False
    try:
        cancel.check()
        # fsync the archive bytes to disk *before* the rename so a crash between
        # rename and the next page-cache flush cannot leave a zero-byte file
        # under the published name. O_RDONLY is used for the fsync so the
        # original write fd doesn't have to be kept around through the try/finally.
        fd = os.open(tmp_archive, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

        emit("finalizing", os.path.getsize(tmp_archive))
        # The CountingWriter has been hashing every byte that hit the file as
        # it was written. Using its digest avoids a full second pass over what
        # may be a multi-GB archive — re-hashing the file would re-read all of
        # it from disk just to recompute the same hash.
        sha = counter.sha256_hex
        record = BackupRecord(
            id=backup_id,
            path=archive_path,
            size_bytes=os.path.getsize(tmp_archive),
            created_at=manifest.created_at,
            scope=manifest.scope,
            app_version=manifest.app_version,
            sha256=sha,
            note=manifest.note,
            archive_format=manifest.archive_format,
            includes_analytics=manifest.includes_analytics,
            includes_history=manifest.includes_history,
            schema_version=manifest.schema_version,
            warnings=warnings.as_list(),
            warning_items=warnings.as_items(),
        )
        # Sidecar JSON so listing entries doesn't require cracking open the
        # tar, and the only record of the archive's sha256. Written BEFORE the
        # archive is published: written after it, a crash in between left a
        # published archive whose listing rebuilds the sidecar from the
        # manifest without the sha256 — and every later restore from it then
        # went unverified. Before the rename, a crash leaves at worst a
        # sidecar no archive answers to, which the listing never shows and the
        # stale-staging sweep reclaims. Atomic write so list_backups() never
        # sees a half-written sidecar.
        #
        # Best-effort, matching slide_transfer's sidecar: the archive itself is
        # complete and restorable without it. Letting a sidecar write error
        # propagate would fail a backup whose archive is good. Degrade with a
        # warning instead; list_backups / find_backup rebuild a missing
        # sidecar from the archive's manifest (without the sha256, which only
        # this run knew).
        sidecar_error: Optional[OSError] = None
        try:
            _atomic_write_json(sidecar, record.to_dict())
        except OSError as exc:
            sidecar_error = exc
        # The write can fail after its rename, in the directory fsync: the
        # sidecar is then there, sha256 and all — not a failed write. (The
        # backup id is fresh, so nothing else can have put one there.)
        sidecar_written = os.path.isfile(sidecar)
        if not sidecar_written:
            _logger.warning(
                "backup: the sidecar of archive %s could not be written (%s); "
                "the listing rebuilds it from the archive's manifest, without "
                "the sha256", archive_path, sidecar_error,
            )
            warnings.add(
                "the archive's metadata file could not be written, so its sha256 "
                "is not recorded and a restore from it is checked by the "
                "archive's own checksums only",
                "sidecarWriteFailed",
            )
            record.warnings = warnings.as_list()
            record.warning_items = warnings.as_items()
        # The point of no return for a cancel: checked after the (possibly
        # long) fsync and immediately before the rename. A request that lands
        # later is ignored and the backup completes — the caller then records
        # "succeeded" for a published archive, never "cancelled" for one.
        cancel.check()
        os.replace(tmp_archive, archive_path)
        published = True
        # The bytes are durable (the file was fsync'd above); the dirent that
        # names the file is not until the parent directory itself is flushed.
        _fsync_parent_dir(archive_path)
    except BaseException:
        try:
            os.remove(tmp_archive)
        except OSError:
            pass
        # Not once the archive is published: the sidecar is then the record
        # of its sha256 (only the parent-dir fsync can fail after that).
        if sidecar_written and not published:
            try:
                os.remove(sidecar)
            except OSError:
                pass
        raise
    return record


def _under_root(root_real: str, path: str) -> bool:
    """True iff ``path`` resolves to something strictly inside ``root_real``.

    ``wsi_filename`` / ``thumb_filename`` are read from the snapshot DB, and a
    DB row is mutable: a restored or hand-edited value could carry a ``..``
    traversal. Those names are joined and handed straight to ``tar.add``, so
    without this guard a crafted row would pull an arbitrary host-readable file
    (the SQLite DB with its password hashes, say) into an archive an admin can
    download. Mirrors ``slide_transfer._slide_wsi_path`` and ``dzi._slide_path``,
    which guard the same DB-sourced names on their own paths to disk.
    """
    return os.path.realpath(path).startswith(root_real + os.sep)


def _add_dir_entry(tar: tarfile.TarFile, src_path: str, arcname: str) -> None:
    """Add a directory entry (no contents) so the parent path exists in the
    archive even when its children are added individually rather than via a
    recursive ``tar.add``. Tolerates the directory having vanished — the
    callers below skip child entries in that case anyway."""
    try:
        info = tar.gettarinfo(src_path, arcname=arcname)
    except FileNotFoundError:
        return
    if info is None:
        return
    info = _archive_filter(info)
    if info is None:
        return
    tar.addfile(info)


def _archive_filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    """Strip ownership info so archives are reproducible across machines, and
    drop entry types that ``_safe_extract`` would refuse on restore.

    Without the type-filter, a backup that happens to include a symlink in
    ``assets/`` or ``wsi/`` (an admin pointed at an external path, or some
    other tool produced the link) would be created successfully but fail to
    restore — the symptom only appears when the operator finally tries to
    restore. Dropping the unsupported entries here surfaces the issue in
    the backup logs (via the warning) instead.
    """
    if tarinfo.issym() or tarinfo.islnk():
        _logger.warning(
            "_archive_filter: skipping symlink/hardlink %r (links not "
            "supported by restore)", tarinfo.name,
        )
        return None
    if tarinfo.isdev():
        _logger.warning(
            "_archive_filter: skipping device file %r", tarinfo.name,
        )
        return None
    if tarinfo.isfifo():
        _logger.warning(
            "_archive_filter: skipping FIFO %r", tarinfo.name,
        )
        return None
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    return tarinfo


# ---------------------------------------------------------------------------
# Public API: list / delete
# ---------------------------------------------------------------------------

def _schema_version_of(data: dict) -> int:
    """Tolerant read of a manifest/sidecar ``schema_version`` cell.

    Returns 0 for absent / blank / non-numeric. This is the *listing* path,
    where tolerance is the point: a corrupt sidecar must show up as one bad
    row rather than 500 the whole backup list, and every other field these
    helpers read is likewise tolerant. The restore path reads the same cell
    strictly — see :func:`stage_restore`, which rejects an archive whose
    manifest omits it.
    """
    try:
        return int(data.get("schema_version", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


# Bytes of manifest.json a listing reads to rebuild a lost sidecar; every
# manifest this app writes is a few KB (the python_packages map included).
_MAX_REBUILD_MANIFEST_BYTES = 1024 * 1024


def _manifest_of_archive(path: str) -> Optional[dict]:
    """``manifest.json`` of the archive at ``path``, or None.

    Reads the first member only — create_backup writes the manifest first
    for exactly this — so it costs a header and a few KB of decompression
    whatever the archive's size; nothing past it is read, and in particular
    the file is not hashed (that would mean reading terabytes per listing).
    Tolerant like the rest of the listing path: anything unexpected is None.
    """
    try:
        with _open_archive_reader(path) as tar:
            member = tar.next()
            if (
                member is None
                or member.name != MANIFEST_NAME
                or not member.isreg()
                or member.size > _MAX_REBUILD_MANIFEST_BYTES
            ):
                return None
            f = tar.extractfile(member)
            if f is None:
                return None
            data = json.loads(f.read(member.size))
    except _CORRUPT_ARCHIVE_ERRORS + (RuntimeError,):
        return None
    return data if isinstance(data, dict) else None


def _persist_rebuilt_sidecar(sidecar: str, data: dict, *, replace: bool) -> None:
    """Best-effort write of a sidecar rebuilt from the archive's manifest.

    A sidecar that was *missing* is only created, never replaced (a hard link
    of a fully written temp file, which fails if the name exists): the
    listing that noticed it missing may be racing the create_backup that is
    about to write the real one, sha256 included, and must not overwrite it
    with this sha-less copy. A *corrupt* one is replaced — create_backup
    writes sidecars atomically, so a corrupt one is never a write in flight.
    """
    tmp = f"{sidecar}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(tmp, sidecar)
        else:
            try:
                os.link(tmp, sidecar)
            except FileExistsError:
                return  # the real sidecar landed first; keep it
        _fsync_parent_dir(sidecar)
        _logger.info("backup: rebuilt sidecar %s from the archive manifest", sidecar)
    except OSError as exc:
        _logger.warning("backup: could not persist rebuilt sidecar %s: %s", sidecar, exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _load_backup_record(backup_id: str, path: str, ext: str) -> Optional[BackupRecord]:
    """The listing row for the archive at ``path`` (``<backup_id>.<ext>``), or
    None if it vanished meanwhile.

    The id is always the one the filename carries — never the sidecar's
    ``id`` field. Download / delete / restore-from-snapshot all find the file
    by that id, so a sidecar copied beside another archive or hand-edited
    to disagree with the name would produce a row whose every action
    answers 404.

    A sidecar that is missing or unreadable is rebuilt from the archive's
    manifest and persisted (best effort), so an archive whose sidecar write
    failed — create_backup publishes the archive first and tolerates that —
    lists its real scope / versions / note instead of a "?" stub forever. The
    rebuilt row has no sha256 (only the run that wrote the archive hashed
    it), so a restore from it relies on the codec checksums, as for an
    uploaded archive. Only an archive whose manifest cannot be read either
    becomes the stub, which keeps it visible and deletable.
    """
    sidecar = path + ".meta.json"
    data: Optional[dict] = None
    sidecar_missing = False
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Valid JSON of the wrong shape (a list/string after partial
        # corruption) is corrupt too, rather than an AttributeError on .get
        # that breaks the WHOLE listing with a 500.
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        sidecar_missing = True
    except (OSError, ValueError):
        pass
    if data is None:
        manifest = _manifest_of_archive(path)
        if manifest is not None:
            try:
                size_bytes = os.path.getsize(path)
            except OSError:
                return None
            data = {
                "id": backup_id,
                "size_bytes": size_bytes,
                "created_at": str(manifest.get("created_at", "")),
                "scope": str(manifest.get("scope", "?")),
                "app_version": str(manifest.get("app_version", "?")),
                "sha256": "",
                "note": str(manifest.get("note", "")),
                "archive_format": ext,
                "includes_analytics": bool(manifest.get("includes_analytics", True)),
                "includes_history": bool(manifest.get("includes_history", True)),
                "schema_version": _schema_version_of(manifest),
            }
            _persist_rebuilt_sidecar(sidecar, data, replace=not sidecar_missing)
        else:
            data = {
                "scope": "?",
                "app_version": "?",
                "created_at": "",
                "sha256": "",
                "note": "",
                "archive_format": ext,
            }
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        # Archive deleted between the caller's isfile() check and here; skip
        # the row rather than 500 the whole listing.
        return None
    return BackupRecord(
        id=backup_id,
        path=path,
        size_bytes=size_bytes,
        created_at=data.get("created_at", ""),
        scope=data.get("scope", "?"),
        app_version=data.get("app_version", "?"),
        sha256=data.get("sha256", ""),
        note=data.get("note", ""),
        archive_format=data.get("archive_format", ext),
        # Tolerant like every other field on this listing path: a sidecar
        # too damaged to state these still deserves a row. stage_restore
        # requires them before anything is actually restored.
        includes_analytics=bool(data.get("includes_analytics", True)),
        includes_history=bool(data.get("includes_history", True)),
        schema_version=_schema_version_of(data),
    )


_ARCHIVE_EXTENSIONS = ("tar.zst", "tar.gz")


def list_backups() -> list[BackupRecord]:
    _ensure_backup_dirs()
    out: list[BackupRecord] = []
    for entry in sorted(os.listdir(settings.backup_dir), reverse=True):
        full = os.path.join(settings.backup_dir, entry)
        if not os.path.isfile(full):
            continue
        ext = next((e for e in _ARCHIVE_EXTENSIONS if entry.endswith("." + e)), None)
        if ext is None:
            continue
        rec = _load_backup_record(entry[: -len(ext) - 1], full, ext)
        if rec is not None:
            out.append(rec)
    return out


def find_backup(backup_id: str) -> Optional[BackupRecord]:
    """Look up a single backup by id without scanning the whole directory."""
    # Block path-traversal: the URL-supplied id flows straight into a path.
    if (
        not backup_id
        or "/" in backup_id
        or "\\" in backup_id
        or backup_id.startswith(".")
    ):
        return None
    _ensure_backup_dirs()
    for ext in _ARCHIVE_EXTENSIONS:
        path = os.path.join(settings.backup_dir, f"{backup_id}.{ext}")
        if not os.path.isfile(path):
            continue
        rec = _load_backup_record(backup_id, path, ext)
        if rec is not None:
            return rec
        # Deleted between isfile() and the size read: try the next ext,
        # then report not found rather than 500.
    return None


def delete_backup(backup_id: str) -> bool:
    """Remove a backup's archive and sidecar.

    Sidecar first: a crash between the two removals then leaves an archive
    without a sidecar, which the listing still shows (rebuilding the
    sidecar) so the operator sees the delete did not happen and can repeat
    it — the other order left an orphan ``.meta.json`` nothing lists. The
    sidecar is removed again after the archive, for a listing that rebuilt
    it in between; a crash in that window leaves an orphan the stale-staging
    sweep reclaims.
    """
    rec = find_backup(backup_id)
    if rec is None:
        return False
    sidecar = rec.path + ".meta.json"
    for path in (sidecar, rec.path, sidecar):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return True

# ---------------------------------------------------------------------------
# Public API: restore staging
# ---------------------------------------------------------------------------

def verify_backup(
    backup_id: str,
    progress_cb: Optional[Callable[[dict], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Read a saved backup end to end and check it, without restoring it.

    Listing never opens an archive, so a truncated or bit-rotted file looks
    healthy until someone tried to restore from it — when it is needed. This
    reads every member through the codec to its trailer (gzip CRC / zstd
    checksums, see :func:`_drain_tar_stream`) and checks the raw bytes against
    the sha256 the sidecar recorded at creation, when there is one. It checks
    that the archive is intact, not that it would restore (version stamps,
    schema), which the restore preflight reports.

    ``should_cancel`` is polled as for :func:`create_backup` (a check of a
    multi-TB archive holds the job slot for hours); a True answer raises
    :class:`BackupCancelled`.

    Returns ``{"backup_id", "sha256_checked", "members", "bytes"}``. Raises
    FileNotFoundError for an unknown backup, :class:`BackupDamaged` for a bad
    archive, and RuntimeError when it cannot be read (I/O, permissions, no
    zstd library) — which says nothing about the archive itself.
    """
    rec = find_backup(backup_id)
    if rec is None:
        raise FileNotFoundError(backup_id)
    expected = _sidecar_sha256(rec.path)
    cancel = _CancelCheck(should_cancel)
    members = 0
    content = 0
    have_manifest = False
    last = time.monotonic()

    def report(force: bool = False) -> None:
        nonlocal last
        now = time.monotonic()
        if progress_cb is None or (not force and now - last < 2.0):
            return
        last = now
        # Bytes of member content read (uncompressed): the archive's own
        # size is compressed, so the two are not shown against each other.
        progress_cb({
            "phase": "verifying", "backup_id": backup_id,
            "members": members, "bytes_read": content,
        })

    damaged: tuple = (
        tarfile.TarError, EOFError, ValueError, TypeError, zlib.error, gzip.BadGzipFile,
    )
    if _HAS_ZSTD:
        damaged = damaged + (_zstd.ZstdError,)
    try:
        with _open_archive_reader(rec.path, expected_sha256=expected) as tar:
            for member in tar:
                tar.members.clear()  # as in _safe_extract: not every TarInfo kept
                cancel.check()
                members += 1
                if member.name == MANIFEST_NAME:
                    have_manifest = True
                if member.isreg():
                    f = tar.extractfile(member)
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        content += len(chunk)
                        cancel.maybe_check(len(chunk))
                        report()
            _drain_tar_stream(tar)
    except (BackupCancelled, BackupDamaged):
        raise
    except ArchiveChecksumMismatch as exc:
        raise BackupDamaged(str(exc)) from exc
    except damaged as exc:
        raise BackupDamaged(f"the archive is damaged or truncated ({exc})") from exc
    except OSError as exc:
        raise RuntimeError(
            f"the archive could not be read ({exc}); this does not say whether "
            "it is intact — check the backup volume and try again"
        ) from exc
    if not have_manifest:
        raise BackupDamaged("the archive has no manifest, so it is not a backup this app made")
    report(force=True)
    return {
        "backup_id": backup_id,
        "sha256_checked": bool(expected),
        "members": members,
        "bytes": content,
    }


def _fsync_parent_dir(path: str) -> None:
    """fsync the directory containing ``path``, so a rename into it survives a
    crash: the dirent that names a file lives in its parent directory."""
    _restore_apply.fsync_dir(os.path.dirname(os.path.abspath(path)))


# JSON via tmp-file + fsync + os.replace + parent-dir fsync: a concurrent reader
# never sees a half-written file, and the rename survives a host crash.
_atomic_write_json = _restore_apply.write_json_durable


def _is_within(parent: str, child: str) -> bool:
    parent = os.path.realpath(parent)
    child = os.path.realpath(child)
    return os.path.commonpath([parent, child]) == parent


# Members (files and directories) an archive may hold. A backup carries two per
# slide (wsi/ + thumbs/) plus each tour's directory and attachments (at most
# TOUR_ASSET_MAX_FILES, default 200, per tour), so a real one stays far below
# this; tar's headers are so small that, uncapped, a few hundred KB of
# compressed empty entries could create millions of files on the data volume.
_MAX_ARCHIVE_MEMBERS = 1_000_000

# Written into a stage directory last, atomically, by stage_restore and
# create_reset_stage once every check on it has passed (the archive's sha256,
# the codec trailer, the manifest, the DB's magic / quick_check / version
# stamps). A staging job killed mid-extract (a worker restart, an OOM kill, a
# reboot) leaves a directory holding a partial tree that passed none of them,
# yet looks like any other stage; the apply and preflight endpoints and both
# swaps (request_apply, inprocess_apply) refuse a stage without this file.
# _safe_extract refuses an archive member by this name, so an archive cannot
# bring its own. Never promoted: plan_journal moves only wsi.db, assets/, wsi/
# and thumbs/ out of a stage, and close_journal drops the rest with it.
STAGE_COMPLETE_MARKER = ".staged-ok"


# Longest archive entry name accepted: PATH_MAX. Nothing this app writes comes
# near it (wsi/<id>_<200-byte name>, assets/tours/<token>/<name>).
_MAX_MEMBER_NAME_BYTES = 4096


def _safe_extract(
    tar: tarfile.TarFile,
    dest: str,
    max_total_bytes: Optional[int] = None,
    free_bytes: Optional[int] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    max_members: int = _MAX_ARCHIVE_MEMBERS,
) -> None:
    """Extract tar into dest, refusing path traversal / absolute / link entries.

    ``max_total_bytes``, when supplied, caps the aggregate of declared member
    sizes. The cap is enforced *before* extraction so a high-ratio zstd bomb
    can't fill the staging volume even if every individual member is small.

    ``free_bytes``, when supplied, is the free space measured on ``dest``'s
    volume before extraction (``disk_space.free_bytes``; None = unknown, no
    check). The same running total is checked against it with the shared
    reserve (``disk_space.reserve_for``: 10 %, at least 256 MiB) left over —
    the staging volume is by default the data volume the live DB writes to —
    so an archive whose *decompressed* contents don't fit is refused before
    the member that would overflow is written, instead of filling the volume
    to ENOSPC. Measured once rather than per member: one statvfs per member
    of a million-member archive is not free, and what the extraction itself
    wrote is exactly the running total.

    More than ``max_members`` entries is refused as a corrupt / unsupported
    archive. ``on_progress``, when supplied, is called after each member with
    the bytes extracted so far.

    After the last member the rest of the compressed stream is read to EOF
    (see :func:`_drain_tar_stream`) so the codec's trailing checksum is
    actually verified.
    """
    os.makedirs(dest, exist_ok=True)
    total = 0
    count = 0
    # Digests rather than the names themselves, and the TarInfo list tarfile
    # keeps in stream mode cleared after each member: at up to max_members
    # entries with names of up to _MAX_MEMBER_NAME_BYTES, both would grow to
    # several GB in the worker for a hostile archive. A 16-byte BLAKE2 digest
    # per name keeps the duplicate check at tens of MB.
    seen: set[bytes] = set()
    for member in tar:
        tar.members.clear()
        count += 1
        if count > max_members:
            raise RuntimeError(
                f"archive has more than {max_members} entries; it is corrupt or "
                "not a supported format"
            )
        if len(member.name.encode("utf-8", "surrogateescape")) > _MAX_MEMBER_NAME_BYTES:
            raise RuntimeError(
                f"archive entry name longer than {_MAX_MEMBER_NAME_BYTES} bytes; "
                "it is corrupt or not a supported format"
            )
        # Reject anything that could escape ``dest`` (CVE-2007-4559 family).
        target = os.path.join(dest, member.name)
        if not _is_within(dest, target):
            raise RuntimeError(f"unsafe path in archive: {member.name!r}")
        # ``_is_within`` only checks where a name lands, so "assets/tours/../../
        # wsi.db" (what a backup of a "../.." tour public_id produces) passes
        # it and silently replaces the real wsi.db member. The writer never
        # emits ".." segments or a name twice, so refuse both outright.
        if ".." in member.name.replace("\\", "/").split("/"):
            raise RuntimeError(f"unsafe path in archive: {member.name!r}")
        norm = os.path.normpath(member.name)
        key = hashlib.blake2b(norm.encode("utf-8", "surrogateescape"), digest_size=16).digest()
        if key in seen:
            raise RuntimeError(f"duplicate entry in archive: {member.name!r}")
        seen.add(key)
        # Extracted before the checks it vouches for, an archive's own marker
        # would make a stage killed mid-extract read as complete.
        if norm == STAGE_COMPLETE_MARKER:
            raise RuntimeError(f"reserved name in archive: {member.name!r}")
        if member.issym() or member.islnk():
            raise RuntimeError(f"links not allowed in archive: {member.name!r}")
        if member.isdev():
            raise RuntimeError(f"device files not allowed: {member.name!r}")
        if member.isreg():
            total += max(0, member.size)
            if max_total_bytes is not None and total > max_total_bytes:
                raise RuntimeError(
                    f"archive decompresses to more than the configured cap "
                    f"({max_total_bytes} bytes); refusing"
                )
            short = _disk_space.shortfall_given(total, free_bytes, dest)
            if short is not None:
                # "at least": the members after this one are not counted yet.
                raise RuntimeError(
                    short.message("staging volume")
                    + f" (the archive expands to at least {total} bytes)"
                )
        # ``filter='data'`` is Python 3.12+'s vetted extraction policy: it
        # refuses absolute / parent-traversal paths, devices, and unsafe
        # permission bits. Equivalent (stricter) checks have already run
        # above; passing the kwarg here is what makes the code forward-
        # compatible with Python 3.14, which raises when ``filter`` is omitted.
        tar.extract(member, dest, filter="data")
        if on_progress is not None:
            on_progress(total)
    _drain_tar_stream(tar)


def _drain_tar_stream(tar: tarfile.TarFile) -> None:
    """Read the decompressed stream behind ``tar`` to EOF.

    tarfile stops at the end-of-archive marker, before the codec trailer, so
    gzip's CRC32/ISIZE (and a zstd frame's content checksum) would go
    unchecked: a flipped byte inside stored WSI data would extract "successfully".
    Reaching EOF makes GzipFile raise BadGzipFile (an OSError) / zstd raise
    ZstdError on a mismatch, and EOFError on a truncated stream — all in
    ``_CORRUPT_ARCHIVE_ERRORS``. Costs only the tar's zero padding.
    """
    fobj = getattr(tar, "fileobj", None)
    if fobj is None:
        return
    while fobj.read(1 << 20):
        pass


def new_stage_id() -> str:
    """A fresh restore / reset stage id (the shape the routers' stage-id
    pattern accepts)."""
    return "stage-" + uuid.uuid4().hex[:12]


# Minimum seconds between two "extracting" progress reports of stage_restore:
# each one rewrites the job record that the dashboard polls.
_STAGE_PROGRESS_INTERVAL = 2.0


def stage_restore(
    archive_path: str,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> tuple[str, Manifest]:
    """Extract an archive into a fresh staging directory and return ``(stage_id, manifest)``.

    Caller is responsible for calling :func:`apply_migrations_to_stage` and
    then either :func:`request_apply` (production) or
    :func:`inprocess_apply` (dev).

    ``progress_cb`` is called with ``{"phase": "extracting", "bytes_extracted":
    int}`` while the archive is extracted (throttled), then with ``{"phase":
    "verifying"}`` once the database checks start.
    """
    _ensure_backup_dirs()
    # What a killed staging job — or a killed backup: its snapshot dir holds
    # hard links to every slide's WSI, which keep even deleted slides' blocks
    # allocated — left, at any age, before the preflight below counts the
    # space it holds (see the helpers; this runs inside the job slot).
    try:
        _reclaim_abandoned_restore_files(keep=archive_path)
        _reclaim_abandoned_backup_files()
    except Exception:
        _logger.exception("reclaiming abandoned restore stages failed; continuing")
    # Opportunistic sweep: a fresh restore attempt is the natural moment to
    # reclaim space from prior stages the operator never applied.
    try:
        sweep_stale_staging()
    except Exception:
        _logger.exception("sweep_stale_staging failed; continuing")
    stage_id = new_stage_id()
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=False)

    # Cheap pre-flight: refuse to extract if the staging volume cannot
    # plausibly hold the archive even at 1:1 compression. The size cap
    # inside ``_safe_extract`` still catches malicious-bomb cases, but it
    # only fires *after* members start landing on disk — by which time a
    # legitimate-but-too-big archive has already burned hundreds of GB of
    # free space that the next ``shutil.rmtree(ignore_errors=True)`` cannot
    # fully reclaim if the FS is wedged from being full.
    #
    # With the shared reserve left over (disk_space), like the extraction
    # itself: were this check to ask only archive_size <= free, an archive
    # that passed it with less than the reserve to spare would be sure to be
    # refused by _safe_extract anyway — its running total reaches about the
    # compressed size (the WSI bulk barely compresses) — but only after that
    # many bytes had been written. Asking the same question here refuses it
    # before the first one. Not a stricter rule, the same rule earlier. An
    # unknown size (0) or free space (None) skips both checks.
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
        raise RuntimeError(short.message("staging volume"))

    last_report = [0.0]

    def _extracted(total: int) -> None:
        now = time.monotonic()
        if now - last_report[0] < _STAGE_PROGRESS_INTERVAL:
            return
        last_report[0] = now
        if progress_cb is not None:
            progress_cb({"phase": "extracting", "bytes_extracted": total})
        # Keep the stage's mtime fresh while it is being written, as a backup
        # does for its snapshot dir: sweep_stale_staging (a sibling worker's
        # startup, outside the job slot) removes stage-* entries by their
        # top-level mtime, which extraction into subdirectories leaves at
        # creation time — an extraction running past the TTL would be swept
        # from under itself and then reported as a corrupt archive.
        try:
            os.utime(stage_dir)
        except OSError:
            pass

    try:
        # Extraction + manifest parse are the corruption-prone steps: a
        # truncated/garbage tar, a bad zstd frame, or a malformed manifest.json
        # raise TarError / ZstdError / OSError / ValueError / TypeError. Convert
        # those to a RuntimeError (which the router maps to a 400) instead of
        # letting them escape as a 500. Intentional RuntimeErrors below (missing
        # manifest, per-slide bundle, ...) already carry clear messages and pass
        # through unchanged.
        try:
            # Archives from backup_dir are checked against the sha256 their
            # sidecar recorded at creation (hashed during this same read).
            with _open_archive_reader(
                archive_path, expected_sha256=_sidecar_sha256(archive_path)
            ) as tar:
                _safe_extract(
                    tar,
                    stage_dir,
                    max_total_bytes=settings.backup_max_extract_bytes,
                    free_bytes=free_bytes,
                    on_progress=_extracted,
                )
            if progress_cb is not None:
                progress_cb({"phase": "verifying"})

            manifest_path = os.path.join(stage_dir, MANIFEST_NAME)
            if not os.path.isfile(manifest_path):
                raise RuntimeError("archive missing manifest.json")
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                # A JSON scalar/array manifest would AttributeError on data.get
                # below; treat it as corrupt up front.
                raise RuntimeError("archive is corrupt or not a supported format")
            # A per-slide export bundle (from Slide Import/Export) carries a
            # ``bundle_format`` marker and no ``wsi.db``; steer the operator to the
            # right tool instead of failing later with a cryptic "missing wsi.db".
            if str(data.get("bundle_format", "")).startswith("tessera-slide-export"):
                raise RuntimeError(
                    "This is a per-slide export bundle. Use Slide Import (not Restore) "
                    "to add these slides to the current database."
                )
            manifest = Manifest.from_dict(data)
        except RuntimeError:
            raise
        except _CORRUPT_ARCHIVE_ERRORS as exc:
            # A full / over-quota staging volume is an OSError too, but not
            # the archive's fault; say so instead of blaming the archive.
            if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise RuntimeError(
                    "insufficient free space on staging volume while extracting "
                    "the archive"
                ) from exc
            raise RuntimeError("archive is corrupt or not a supported format") from exc

        if manifest.scope not in VALID_SCOPES:
            raise RuntimeError(f"manifest has invalid scope: {manifest.scope!r}")

        db_path = os.path.join(stage_dir, DB_ENTRY)
        if not os.path.isfile(db_path):
            raise RuntimeError("archive missing wsi.db")
        # create_backup archives the snapshot alone, in rollback-journal mode,
        # so a wsi.db-wal / -shm / -journal member never comes from this app — yet the
        # first sqlite3.connect below would act on it: a -wal is replayed into
        # the staged DB and checkpointed into wsi.db (a hot -journal is rolled
        # back), so bytes outside the archived database, which no check below
        # sees separately, would become the restored one. Refuse before
        # anything opens the DB.
        for suffix in _restore_apply.DB_SIBLING_SUFFIXES:
            if os.path.lexists(db_path + suffix):
                raise RuntimeError(
                    f"archive carries {DB_ENTRY}{suffix} beside {DB_ENTRY}; backups "
                    "never include SQLite's own side files, so this archive is "
                    "not one this application wrote"
                )
        with open(db_path, "rb") as f:
            head = f.read(len(_restore_apply.SQLITE_MAGIC))
        if head != _restore_apply.SQLITE_MAGIC:
            raise RuntimeError("archive's wsi.db is not a SQLite database")
        # Extraction keeps the archived modes, and an archive made by hand or
        # by a dev run outside the systemd unit (no UMask=0027) records the
        # DB — and every asset, slide and thumbnail — as 0644 (directories
        # 0755). Both apply paths
        # move the staged tree into place as-is, so normalize all of it to
        # what the service creates (see _normalize_stage_modes); otherwise a
        # restore makes password hashes / sessions world-readable.
        _normalize_stage_modes(stage_dir)
        _check_sqlite_integrity(db_path)
        # An archive whose snapshot was taken in WAL
        # mode carries a WAL-mode header: the schema upgrade the apply runs
        # on the staged DB would then go to a wsi.db-wal the promote leaves
        # behind. Rollback-journal mode keeps every write in wsi.db.
        _use_rollback_journal(db_path)

        # Cross-check the manifest against the DB's own stamps. Both keys are
        # written by every boot, so a staged DB missing either is not one this
        # app wrote — reject rather than fall back to the manifest's word for it.
        db_version = _app_meta.sqlite_read_app_version(db_path)
        db_schema_version = _app_meta.sqlite_read_schema_version(db_path)
        if db_version is None or db_schema_version is None:
            raise RuntimeError(
                "archive's wsi.db carries no app_meta version stamps; it was "
                "not written by this application"
            )
        manifest_dirty = False
        if db_version != manifest.app_version:
            _logger.warning(
                "stage_restore: manifest app_version=%s but DB-stamped version=%s; "
                "trusting DB stamp",
                manifest.app_version, db_version,
            )
            manifest.app_version = db_version
            manifest_dirty = True
        if db_schema_version != manifest.schema_version:
            _logger.warning(
                "stage_restore: manifest schema_version=%s but DB-stamped=%s; "
                "trusting DB stamp",
                manifest.schema_version, db_schema_version,
            )
            manifest.schema_version = db_schema_version
            manifest_dirty = True
        if manifest_dirty:
            # Persist the correction so apply re-reads the same values
            # plan_restore was computed from. Without this rewrite the apply
            # endpoint would re-load the original (stale) manifest.json and
            # recompute a different plan than the one the operator confirmed.
            _atomic_write_json(manifest_path, manifest.to_dict())

        # The startup schema upgrade, tried on a copy now that the archive
        # passed its checks: here, in the staging job's thread, rather than
        # in the apply request (see trial_schema_reconcile). An archive the
        # plan refuses anyway is not tried. A failed upgrade is the
        # archive's verdict, not a staging failure: the stage is kept and
        # marked complete with it, and plan_staged_restore turns it into the
        # SCHEMA_UPGRADE_FAILED refusal the staging preview, the preflight
        # and the apply all report. A trial that could not even copy the
        # database (no space) raises and fails the staging, as every other
        # space shortfall above does.
        schema_trial: Optional[dict] = None
        if not plan_restore(manifest).blocking_reason:
            try:
                trial_schema_reconcile(stage_id)
                schema_trial = {"ok": True}
            except SchemaUpgradeFailed as exc:
                schema_trial = {"ok": False, "error": str(exc)}

        # Last, once everything above has passed (see STAGE_COMPLETE_MARKER).
        _mark_stage_complete(stage_dir, stage_id, schema_trial=schema_trial)
        return stage_id, manifest
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


# The modes the service creates files and directories with: wsi-app.service
# and wsi-sync.service run under UMask=0027 (0640 / 0750), and every runtime
# reader of DATA_DIR is the wsi user (nginx proxies /thumbs/ and /assets/
# through the app rather than reading the disk).
_STAGED_FILE_MODE = 0o640
_STAGED_DIR_MODE = 0o750


def _reclaim_abandoned_restore_files(keep: str = "") -> int:
    """Remove what an earlier, killed restore staging left behind; return the
    count.

    A staging job whose worker dies (a restart, OOM, SIGKILL) skips its own
    cleanup and leaves a partial ``.staging/stage-*`` tree — no
    :data:`STAGE_COMPLETE_MARKER`, up to the archive's full extracted size —
    and the ``.staging/upload-*`` copy of an uploaded archive. The dashboard
    never got a stage id for either, so nothing can cancel them, and
    ``sweep_stale_staging`` keeps them for 24 h: until then the next attempt's
    free-space preflight would count them and refuse the retry.

    Removed regardless of age for the same reason as
    :func:`_reclaim_abandoned_backup_files`: :func:`stage_restore` runs only
    inside the single cross-worker job slot, which every restore / reset /
    slide-import job holds while it writes these, so none of them is live
    now. Kept: complete stages (marked; they may be awaiting apply, or a
    pending request names one) and ``keep``, the upload this run stages from.
    """
    removed = 0
    staging_root = os.path.join(settings.backup_dir, ".staging")
    keep_real = os.path.realpath(keep) if keep else ""
    try:
        names = os.listdir(staging_root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(staging_root, name)
        if os.path.islink(path):
            continue
        if name.startswith("stage-") and os.path.isdir(path):
            if os.path.exists(os.path.join(path, STAGE_COMPLETE_MARKER)):
                continue
            what = "partial stage"
            remove = functools.partial(shutil.rmtree, path)
        elif name.startswith("upload-") and os.path.isfile(path):
            if keep_real and os.path.realpath(path) == keep_real:
                continue
            what = "archive upload copy"
            remove = functools.partial(os.remove, path)
        else:
            continue
        try:
            remove()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _logger.warning("restore: could not remove %s %s: %s", what, name, exc)
            continue
        removed += 1
        _logger.warning("restore: removed %s %s left by an interrupted staging", what, name)
    return removed


def reclaim_abandoned_job_files() -> int:
    """At startup: what killed backups / restore stagings left, removed at any
    age when no job holds the slot.

    The startup sweep (``sweep_stale_staging``) runs outside the slot beside
    jobs other workers may be running, so it waits 24 h; until then a killed
    backup's ``wsi_backup_*`` dir would keep a hard link to every slide's WSI
    — a deleted slide's or a deleted rollback tree's blocks would not be
    freed — and a killed staging's partial stage would hold its space. Checked under the slot
    lock: a job claims the slot under that lock, so with none running none
    can start while this runs. Complete stages are kept (see
    :func:`_reclaim_abandoned_restore_files`)."""
    from . import jobs as _jobs

    with _jobs.store.slot_guard():
        if _jobs.store.has_running():
            return 0
        return _reclaim_abandoned_backup_files() + _reclaim_abandoned_restore_files()


def _normalize_stage_modes(stage_dir: str) -> None:
    """chmod every regular file under ``stage_dir`` to 0640 and every
    directory (``stage_dir`` included) to 0750.

    An archive records the modes its files had when the backup was taken —
    0644 / 0755 from a hand-made or dev archive, or any host that does not
    run the service under UMask=0027 — and extraction
    keeps them (``filter="data"`` only clears group / other *write* bits).
    The swap renames the staged wsi.db, assets/, wsi/ and thumbs/ into
    DATA_DIR unchanged, so without this a restore re-publishes them to every
    local user. _safe_extract admits no links or device nodes, so the walk
    meets only files and directories; anything else is left alone rather
    than chmod'ed through.
    """
    os.chmod(stage_dir, _STAGED_DIR_MODE)
    for root, dirs, files in os.walk(stage_dir):
        for name in dirs:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, _STAGED_DIR_MODE)
        for name in files:
            path = os.path.join(root, name)
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.chmod(path, _STAGED_FILE_MODE)


def _sync_filesystem(path: str) -> None:
    """Flush the filesystem holding ``path`` to disk: ``syncfs(2)``, which
    waits for that filesystem only (not every mount, as ``sync`` does) and
    reports a writeback error not yet seen (EIO, ENOSPC on thin storage) as
    an OSError. ``os.sync()`` where syncfs is unavailable."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        syncfs = libc.syncfs
    except (OSError, AttributeError):
        os.sync()
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        if syncfs(fd) != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"syncfs failed: {os.strerror(err)}", path)
    finally:
        os.close(fd)


def _mark_stage_complete(
    stage_dir: str, stage_id: str, schema_trial: Optional[dict] = None,
) -> None:
    """Write :data:`STAGE_COMPLETE_MARKER` (atomically and durably): the stage
    passed every check and may be applied.

    A restore stage also records the outcome of its schema-upgrade trial
    (``schema_trial``: ``{"ok": true}`` or ``{"ok": false, "error": ...}``;
    None when the plan refused the archive before it was tried) and the
    binary that ran it (``app_version`` / ``schema_version``): a trial says
    whether THAT binary's upgrade succeeds, so another one's apply must not
    rely on it (:func:`plan_staged_restore`). The marker is a name the
    archive cannot bring along (_safe_extract refuses it), so neither can
    its verdict be.

    Everything staged is flushed to disk first. Extraction writes through
    the page cache and nothing else syncs the extracted WSI / thumbnail /
    asset files or their directory entries; an apply only renames them into
    place, so a power loss soon after a committed swap (writeback lagging far
    behind a multi-TB extraction) could bring them back empty or zero-filled
    while the journal is gone and the result says "ok". One ``syncfs`` of the
    staging filesystem here, before the stage may be applied, makes the
    marker mean "on disk" — and a writeback error it reports fails the
    staging instead of being marked complete."""
    _sync_filesystem(stage_dir)
    _atomic_write_json(
        os.path.join(stage_dir, STAGE_COMPLETE_MARKER),
        {
            "stage_id": stage_id,
            "staged_at": _utc_iso_z(),
            "app_version": APP_VERSION,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "schema_trial": schema_trial,
        },
    )


def stage_is_complete(stage_id: str) -> bool:
    """Whether the stage's :data:`STAGE_COMPLETE_MARKER` is there."""
    return os.path.isfile(os.path.join(
        settings.backup_dir, ".staging", stage_id, STAGE_COMPLETE_MARKER,
    ))


def _assert_stage_complete(stage_dir: str) -> None:
    if not os.path.isfile(os.path.join(stage_dir, STAGE_COMPLETE_MARKER)):
        raise RuntimeError(
            "the stage was not completely staged — its staging job was "
            "interrupted or failed before the archive passed its checks; "
            "stage the archive again"
        )


def plan_restore(manifest: Manifest) -> RestorePlan:
    """Compute the migration plan for a staged archive vs the running binary."""
    backup_v = manifest.app_version
    current_v = APP_VERSION
    backup_sv = manifest.schema_version
    current_sv = CURRENT_SCHEMA_VERSION

    def _plan(**kw) -> RestorePlan:
        return RestorePlan(
            backup_app_version=backup_v,
            current_app_version=current_v,
            backup_schema_version=backup_sv,
            current_schema_version=current_sv,
            **kw,
        )

    if backup_v == "?" or backup_v == "":
        return _plan(
            blocking_reason=(
                "Archive does not declare an app_version. A recorded version "
                "is required."
            ),
            blocking_code="NO_VERSION",
        )

    # Schema-precise downgrade guard: an archive carrying a higher
    # schema_version than this binary can produce would, once live, be
    # reconciled DOWN (DROP COLUMN) and lose the newer release's data. Block
    # regardless of the app_version string.
    if backup_sv > current_sv:
        return _plan(
            blocking_reason=(
                f"Archive is at schema_version {backup_sv}, newer than this "
                f"binary supports ({current_sv}). Downgrades are not supported."
            ),
            blocking_code="DOWNGRADE_NOT_SUPPORTED",
        )

    # No migration path exists below the baseline:
    # the reconcile would stamp it current without the transformations it
    # missed, so refuse it here, before any staging trial or swap.
    if backup_sv < BASELINE_SCHEMA_VERSION:
        return _plan(
            blocking_reason=(
                f"Archive is at schema_version {backup_sv}, older than the "
                f"oldest this binary supports ({BASELINE_SCHEMA_VERSION})."
            ),
            blocking_code="SCHEMA_TOO_OLD",
        )

    if backup_v == current_v:
        return _plan()

    if _app_meta.refuses_app_downgrade(backup_v, current_v, what="restore"):
        return _plan(
            blocking_reason=(
                f"Archive was created by app v{backup_v}, which is newer "
                f"than the running binary v{current_v}. Downgrades are not "
                f"supported."
            ),
            blocking_code="DOWNGRADE_NOT_SUPPORTED",
        )

    # Forward restore from an older (supported) archive: schema
    # reconciliation (``main._apply_column_migrations``, additive ALTERs) plus
    # any registered data migrations run on the live binary's next boot, so no
    # separately registered migration chain is needed here.
    return _plan()


def _read_stage_marker(stage_id: str) -> Optional[dict]:
    """The stage's :data:`STAGE_COMPLETE_MARKER` record, or None when it is
    missing or unreadable."""
    path = os.path.join(settings.backup_dir, ".staging", stage_id, STAGE_COMPLETE_MARKER)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def plan_staged_restore(stage_id: str, manifest: Manifest) -> RestorePlan:
    """:func:`plan_restore` for a staged archive, also refusing what its
    staging found: a schema-upgrade trial that failed
    (``SCHEMA_UPGRADE_FAILED``, with the trial's message — the same 409 a
    trial run by the apply itself would answer), or one that does not hold
    for this binary (``STAGE_STALE``).

    The staging job's result, the preflight and the apply all plan through
    this, so the three agree. A trial decides only for the binary that ran
    it — its migrations and its schema — so a stage the marker says another
    version staged (an upgrade between staging and apply), or whose marker
    records no trial, is refused as stale: staging it again runs the trial
    this binary's boot will actually face. Running it at apply time instead
    would bring back the slow synchronous copy the staging job exists to
    take out of the request. ``apply_migrations_to_stage``, the one change
    the apply still makes to the staged DB, only re-stamps the version the
    trial's copy was already stamped with, so it cannot change the verdict.
    """
    plan = plan_restore(manifest)
    if plan.blocking_reason:
        return plan
    marker = _read_stage_marker(stage_id) or {}
    trial = marker.get("schema_trial")
    if (
        not isinstance(trial, dict)
        or marker.get("app_version") != APP_VERSION
        or marker.get("schema_version") != CURRENT_SCHEMA_VERSION
    ):
        plan.blocking_code = "STAGE_STALE"
        plan.blocking_reason = (
            "This stage was staged by another version of the app (v"
            f"{marker.get('app_version') or '?'}), so whether this version can "
            "bring its database's schema up to date was never tried. Cancel it "
            "and stage the archive again."
        )
    elif trial.get("ok") is not True:
        plan.blocking_code = "SCHEMA_UPGRADE_FAILED"
        plan.blocking_reason = str(trial.get("error") or "the schema upgrade trial failed")
    return plan


def _version_to_stamp(recorded: Optional[str]) -> str:
    """The app_version to stamp on a database (a backup snapshot, a staged
    restore or reset) whose own stamp is ``recorded``: the running binary's
    APP_VERSION — except over a newer stamp while that version is the
    untrusted package.json fallback. Keep the newer stamp then, as
    ``bootstrap_app_version`` does for the live DB: overwriting it with the
    lower, untrusted value would disarm the downgrade guard for the next build
    that knows its version."""
    if (
        recorded is not None
        and not _version.is_authoritative(_version.APP_VERSION_SOURCE)
        and _app_meta.is_downgrade(recorded, APP_VERSION)
    ):
        return recorded
    return APP_VERSION


def apply_migrations_to_stage(stage_id: str, plan: RestorePlan) -> None:
    """Stamp the staged DB with the running binary's APP_VERSION before the
    swap (except over a newer stamp the running version cannot be trusted to
    outrank; see below). The next backend boot reconciles any schema differences via
    ``main._apply_column_migrations``, so this function only has to update
    the version metadata; no per-version migration chain runs here.
    """
    if plan.blocking_reason:
        raise RuntimeError(plan.blocking_reason)
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    db_path = os.path.join(stage_dir, DB_ENTRY)
    if not os.path.isfile(db_path):
        raise RuntimeError(f"stage {stage_id!r} has no wsi.db")

    # Defense in depth: confirm the staged DB still reports the version the
    # plan was built for. stage_restore() may have corrected the manifest
    # from the DB stamp, so a mismatch here would mean the staged file was
    # swapped or tampered with after staging.
    #
    # ``APP_VERSION`` is also accepted: this function stamps the staged DB to
    # the running binary's version at the end, but it does NOT rewrite the
    # stage's manifest.json. If a first apply stamped the DB and then a
    # *later* step failed (a cross-filesystem ``request_apply`` refusal, a
    # failed restart kick that cleared the pending request, …), the operator
    # can re-apply the same stage — but ``plan.backup_app_version`` would then
    # be recomputed from the still-original manifest.json while the DB already
    # reads APP_VERSION. Treating that as tampering would brick the stage
    # forever (re-staging the archive being the only escape). Since re-stamping
    # to APP_VERSION is idempotent, accept it explicitly and let the retry
    # proceed.
    staged_db_version = _app_meta.sqlite_read_app_version(db_path)
    if (
        staged_db_version is not None
        and staged_db_version != plan.backup_app_version
        and staged_db_version != APP_VERSION
    ):
        raise RuntimeError(
            f"staged DB reports app_version={staged_db_version!r} but the "
            f"plan was built for {plan.backup_app_version!r}; aborting"
        )

    # A newer archive gets here only when the running version is the
    # untrusted package.json fallback (plan_restore skipped the guard with a
    # warning); _version_to_stamp keeps the archive's stamp then.
    _app_meta.sqlite_set_app_version(db_path, _version_to_stamp(staged_db_version))


class SchemaUpgradeFailed(RuntimeError):
    """:func:`trial_schema_reconcile`: the startup schema upgrade fails on the
    staged database — the archive's verdict, which the stage records
    (:func:`stage_restore`) for the plan to refuse with
    ``SCHEMA_UPGRADE_FAILED``. Any other RuntimeError of the trial (the copy
    could not be made) is about the staging volume, not the archive, and
    fails the staging instead."""


def trial_schema_reconcile(stage_id: str) -> None:
    """Run the boot's schema reconciliation against a throwaway copy of the
    staged DB; raise :class:`SchemaUpgradeFailed`, saying why, when it fails,
    and a plain RuntimeError when the copy cannot be made.

    Run by :func:`stage_restore`, in the staging job's thread, once the
    archive has passed its checks. Inside ``POST /api/admin/restore/apply``
    it would be a whole second copy of the staged DB (seconds per GB) plus
    the upgrade itself, in a request nginx gives 60 s (``proxy_read_timeout``
    on ``/api/admin/``) and with no free-space check for the copy. The plan
    then refuses a failed trial (:func:`plan_staged_restore`), so the operator
    reads the refusal in the staging preview instead of only after confirming.

    A restored DB's schema is first brought up to date by the app's lifespan
    (``main.reconcile_schema``: create_all, the column ADD phase, the numbered
    migrations, the DROP phase, the indexes), and that runs only AFTER the
    swap has committed: by then the result file says ok, the old tree is
    parked, and a reconciliation that fails — a stale column SQLite cannot
    DROP in place, a data migration that trips over the archive's rows, a
    foreign-key violation a table rebuild uncovers — leaves wsi-app
    crash-looping on the restored data with no automatic way back. Running
    the very same steps first, on a copy, turns that into a refusal while the
    live data is still in place.

    The copy is the staged DB alone (never wsi/ or assets/), taken with the
    SQLite backup API into a scratch directory inside the stage and removed
    whatever happens; its engine opens it with the pragmas the boot's engine
    sets that bear on DDL (foreign_keys, busy_timeout) but not WAL, which
    would only leave sidecar files to clean up. The app-version stamp
    (``bootstrap_app_version``) is tried too: it is the other boot step that
    raises on a database it will not serve. The rest of the boot only seeds
    rows (the bootstrap admin, the tile geometry, the session floor) and
    cannot refuse one.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    from . import main as _main

    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    staged_db = os.path.join(stage_dir, DB_ENTRY)
    if not os.path.isfile(staged_db):
        raise RuntimeError(f"stage {stage_id!r} has no wsi.db")
    # The copy is a second wsi.db on the staging volume: checked by the rule
    # every large write is (disk_space), before a byte of it is written. The
    # upgrade may grow it further (an index build, a table rebuild); the
    # reserve covers that, and its own ENOSPC still fails the trial below.
    short = _disk_space.check(stage_dir, os.path.getsize(staged_db))
    if short is not None:
        raise RuntimeError(
            short.message("staging volume")
            + " (for the copy of the staged database its schema upgrade is tried on)"
        )
    trial_dir = tempfile.mkdtemp(prefix=".schema-trial-", dir=stage_dir)
    try:
        trial_db = os.path.join(trial_dir, DB_ENTRY)
        try:
            _snapshot_sqlite(staged_db, trial_db)
            # Stamped as apply_migrations_to_stage will stamp the staged DB
            # before the swap: bootstrap_app_version below then judges the
            # very stamp the boot will. The stamp is all that function
            # changes, so the trial's verdict holds at apply time.
            _app_meta.sqlite_set_app_version(
                trial_db, _version_to_stamp(_app_meta.sqlite_read_app_version(trial_db)),
            )
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(
                f"could not copy the staged database for a trial of its schema "
                f"upgrade ({exc}); free space on the staging volume and retry"
            ) from exc
        eng = create_engine(
            f"sqlite:///{trial_db}", future=True, hide_parameters=True,
            poolclass=NullPool,
        )

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_connection, _record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _logger.info(
            "restore of stage %s: trying the startup schema upgrade on a copy "
            "of the staged database (its schema-migration log lines below are "
            "about that copy)", stage_id,
        )
        try:
            _main.reconcile_schema(eng)
            _app_meta.bootstrap_app_version(eng)
        except Exception as exc:
            _logger.warning(
                "restore of stage %s refused: the startup schema upgrade fails "
                "on the staged database: %s", stage_id, exc, exc_info=True,
            )
            raise SchemaUpgradeFailed(
                f"this version cannot bring the archived database's schema up to "
                f"date — the startup schema upgrade fails on a copy of it "
                f"({exc}). Restoring it would leave the service unable to start; "
                f"the live data was not touched."
            ) from exc
        finally:
            eng.dispose()
    finally:
        shutil.rmtree(trial_dir, ignore_errors=True)


def cleanup_stage(stage_id: str) -> None:
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    shutil.rmtree(stage_dir, ignore_errors=True)


_STAGE_TTL_SECONDS = 24 * 3600


def sweep_stale_staging(ttl_seconds: int = _STAGE_TTL_SECONDS) -> int:
    """Remove abandoned backup/restore intermediates older than the TTL.

    Covers two leak sources:
      * ``.staging/`` entries (``stage-*`` / ``upload-*`` / ``wsi_backup_*``) —
        a user who stages a restore but never applies (closed the tab, network
        blip, abandoned the flow) leaves the whole staged tree on disk.
        ``wsi_backup_*`` are the snapshot tmpdirs ``create_backup`` uses;
        ``TemporaryDirectory`` removes them on the happy path, but a hard crash
        mid-backup can leak one. An in-progress backup keeps its snapshot
        tmpdir's mtime fresh from its progress callback (``os.utime`` on every
        tick), so the TTL protects a still-running backup even when this sweep
        fires from a sibling worker's lifespan startup, outside the job slot.
      * ``backup_dir/*.tmp`` partial archives — ``create_backup`` writes the
        archive to ``<id>.tar.*.tmp`` and renames on success. It reclaims
        the partial on any failure, but a hard crash (SIGKILL / power loss)
        mid-write still leaks the (possibly multi-GB) ``.tmp``
        (``list_backups`` skips non-``.tar.*`` names and each backup uses a
        fresh UUID); the next backup removes one at any age, inside the job
        slot (``_reclaim_abandoned_backup_files``), and this sweep catches it
        otherwise. An in-progress backup writes continuously so its ``.tmp``
        mtime stays fresh and is protected by the TTL.
      * orphan ``<id>.tar.*.meta.json`` sidecars whose archive is gone (an
        interrupted ``delete_backup``).

    Returns the number of entries removed. Errors are logged and the
    sweep continues with the next entry; a failed unlink shouldn't block
    a stage_restore call from going forward.
    """
    cutoff = time.time() - ttl_seconds
    cleaned = 0

    def _remove(path: str, name: str) -> None:
        nonlocal cleaned
        try:
            stat_result = os.stat(path)
        except OSError:
            return
        if stat_result.st_mtime > cutoff:
            return
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            cleaned += 1
            _logger.info("sweep_stale_staging: removed %s", name)
        except OSError as exc:
            _logger.warning("sweep_stale_staging: %s remove failed: %s", name, exc)

    staging_root = os.path.join(settings.backup_dir, ".staging")
    if os.path.isdir(staging_root):
        try:
            entries = os.listdir(staging_root)
        except OSError:
            entries = []
        for name in entries:
            # Only sweep entries produced by stage_restore /
            # receive_held_upload / create_backup's snapshot tmpdir, plus
            # the slide-transfer intermediates (``export-*`` generated bundles
            # awaiting download, ``import-*`` extracted stages awaiting apply);
            # leave any operator-placed files alone.
            if not (
                name.startswith("stage-")
                or name.startswith("upload-")
                or name.startswith("wsi_backup_")
                or name.startswith("export-")
                or name.startswith("import-")
            ):
                continue
            _remove(os.path.join(staging_root, name), name)

    # Stale partial archives sit in backup_dir itself, not under .staging/.
    backup_dir = settings.backup_dir
    if os.path.isdir(backup_dir):
        try:
            entries = os.listdir(backup_dir)
        except OSError:
            entries = []
        for name in entries:
            if name.endswith(".tmp"):
                _remove(os.path.join(backup_dir, name), name)
            elif name.endswith(".meta.json") and name[: -len(".meta.json")].endswith(
                tuple("." + e for e in _ARCHIVE_EXTENSIONS)
            ) and not os.path.lexists(os.path.join(backup_dir, name[: -len(".meta.json")])):
                # A sidecar whose archive is gone: what a delete_backup that
                # was interrupted (or raced a listing's sidecar rebuild)
                # leaves. Nothing lists it, so nothing else would remove it.
                _remove(os.path.join(backup_dir, name), name)

    return cleaned


# ---------------------------------------------------------------------------
# Public API: pre-restore rollback directories
# ---------------------------------------------------------------------------
#
# Every restore / reset apply parks the live DB + assets/ + wsi/ + thumbs/
# under ``data_dir/.pre-restore-<UTC>[-<uuid8>]/`` so a failed swap can roll
# back and a successful one still leaves a manual-recovery artefact. These are
# *intentionally* never auto-deleted (the operator decides when they're safe to
# drop), but for a full-scope apply each one is a complete copy of the WSI
# corpus, so without a way to see and reclaim them they silently pile up. The
# helpers below back an admin endpoint that lists their size and removes them
# on request.

_ROLLBACK_PREFIX = ".pre-restore-"
# Mints: production ``.pre-restore-<ts>``; dev in-process ``.pre-restore-<ts>-<uuid8>``
# where ts == strftime("%Y%m%dT%H%M%SZ"). Pinning the shape keeps a
# URL-supplied name from naming arbitrary directories under data_dir.
_ROLLBACK_NAME_RE = re.compile(r"^\.pre-restore-\d{8}T\d{6}Z(?:-[0-9a-f]{8})?\Z")


@dataclass
class RollbackDir:
    """A parked pre-restore tree under ``data_dir`` an operator may reclaim."""
    name: str
    created_at: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
        }


def _rollback_created_at(name: str) -> str:
    """Derive an ISO-8601 ``Z`` timestamp from a ``.pre-restore-<ts>`` name.

    Returns "" when the timestamp portion can't be parsed (shouldn't happen
    for names that pass ``_ROLLBACK_NAME_RE``, but keeps the listing from
    raising)."""
    core = name[len(_ROLLBACK_PREFIX):]
    ts = core.split("-", 1)[0]  # strip any "-<uuid8>" suffix
    try:
        dt = datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return ""
    return dt.isoformat() + "Z"


def list_rollback_dirs() -> list[RollbackDir]:
    """List ``.pre-restore-*`` rollback trees parked under ``data_dir``.

    Newest first. Best-effort: an unreadable data_dir yields an empty list and
    a directory whose size can't be fully walked still reports its partial sum
    (see :func:`_dir_total_bytes`). An empty one is left out: it is what a
    rolled-back apply leaves when its own clean-up could not remove it, and
    holds nothing to restore or reclaim."""
    data_dir = settings.data_dir
    out: list[RollbackDir] = []
    try:
        entries = os.listdir(data_dir)
    except OSError:
        return out
    for name in entries:
        if not _ROLLBACK_NAME_RE.match(name):
            continue
        full = os.path.join(data_dir, name)
        # Only real directories — never follow a symlink that happens to match
        # the pattern (deleting through it could reach outside data_dir).
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        try:
            if not os.listdir(full):
                continue
        except OSError:
            pass
        out.append(RollbackDir(
            name=name,
            created_at=_rollback_created_at(name),
            size_bytes=_dir_total_bytes(full),
        ))
    out.sort(key=lambda r: r.name, reverse=True)
    return out


# A rollback tree being deleted: renamed out of the listing first (one rename,
# instant), then removed. A kill mid-removal leaves one, which
# sweep_rollback_trash finishes at the next boot.
_ROLLBACK_TRASH_PREFIX = ".rollback-trash-"
_ROLLBACK_TRASH_RE = re.compile(r"^\.rollback-trash-[0-9a-f]{12}\Z")


def delete_rollback_dir(name: str, *, background: bool = False) -> bool:
    """Remove one ``.pre-restore-*`` rollback tree by name.

    Returns True when a directory was removed, False when it was already gone.
    Raises ``ValueError`` on a name that doesn't match the mint pattern or
    resolves outside ``data_dir`` — the endpoint maps that to a 400 so a
    traversal attempt can never reach :func:`shutil.rmtree`.

    The tree is first renamed to a ``.rollback-trash-*`` name, which takes it
    out of the listing at once. With ``background`` the (for a full-scope
    apply, WSI-corpus-sized) removal then runs on a thread, so the request does
    not outlast the proxy's timeout and report a failure for a delete that is
    in fact under way."""
    if not _ROLLBACK_NAME_RE.match(name):
        raise ValueError(f"invalid rollback dir name: {name!r}")
    full = os.path.join(settings.data_dir, name)
    # Defense in depth: the resolved path must be a direct child of data_dir.
    if os.path.dirname(os.path.realpath(full)) != os.path.realpath(settings.data_dir):
        raise ValueError(f"rollback dir escapes data_dir: {name!r}")
    if os.path.islink(full) or not os.path.isdir(full):
        return False
    trash = os.path.join(settings.data_dir, _ROLLBACK_TRASH_PREFIX + uuid.uuid4().hex[:12])
    try:
        os.rename(full, trash)
    except FileNotFoundError:
        return False  # a concurrent delete took it
    try:
        _fsync_parent_dir(trash)
    except OSError:
        # The rename stands either way; at worst a crash brings back the old
        # name, and the operator deletes it again.
        _logger.warning("could not fsync %s after renaming %s", settings.data_dir, name, exc_info=True)
    if background:
        threading.Thread(
            target=_rmtree_logged, args=(trash, name),
            name="rollback-delete", daemon=True,
        ).start()
    else:
        shutil.rmtree(trash)
    return True


def _rmtree_logged(path: str, what: str) -> bool:
    """``shutil.rmtree`` that carries on past an entry it cannot remove and
    logs each one (a root-owned or immutable file), so a removal no request
    waits for does not fail silently. Returns whether the tree is gone."""
    failures: list[str] = []

    def _record(_func, failed_path, exc_info):
        exc = exc_info if isinstance(exc_info, BaseException) else exc_info[1]
        failures.append(f"{failed_path}: {exc}")

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_record)
    else:  # pragma: no cover - 3.11 has only onerror
        shutil.rmtree(path, onerror=_record)
    if failures:
        _logger.warning(
            "could not fully remove rollback tree %s (%s; %d entr%s left, first: %s); "
            "remove %s by hand", what, path, len(failures),
            "y" if len(failures) == 1 else "ies", failures[0], path,
        )
        return False
    _logger.info("removed rollback tree %s", what)
    return True


def sweep_rollback_trash() -> int:
    """Finish removing rollback trees whose delete was interrupted (see
    :func:`delete_rollback_dir`). Returns how many were removed."""
    removed = 0
    try:
        entries = os.listdir(settings.data_dir)
    except OSError:
        return 0
    for name in entries:
        if not _ROLLBACK_TRASH_RE.match(name):
            continue
        full = os.path.join(settings.data_dir, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        if _rmtree_logged(full, name):
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Public API: hand-off to start-wsi-app.sh OR in-process apply (dev)
# ---------------------------------------------------------------------------

def _assert_stage_on_data_volume(stage_dir: str) -> None:
    """Refuse an apply whose stage and ``data_dir`` sit on different filesystems.

    Both swaps (start-wsi-app.sh and :func:`inprocess_apply`) promote the
    staged DB, assets/ and wsi/ with one ``rename(2)`` per journaled step —
    that atomicity is what lets a crash mid-swap be rolled back from the
    journal. Across filesystems a rename fails with EXDEV (and ``shutil.move``
    would instead degrade to a copy+delete that, for a multi-TB SCOPE_FULL
    archive, stalls the restart for hours and that a crash leaves half done).
    Refuse up front so the operator sees the misconfiguration before service
    downtime instead of as a mid-swap rollback.

    Tested with a real cross-directory link(2) rather than only by comparing
    ``st_dev``: under systemd's ``ProtectSystem=strict`` every
    ``ReadWritePaths=`` entry is its own bind mount, and the kernel refuses a
    rename across mount points (EXDEV) even when both sit on one filesystem —
    the same ``st_dev``. link(2) is held to the same rule, without moving
    anything. The API runs in the same mount namespace as the start script
    that performs the swap, so the probe sees exactly what the swap will. A
    filesystem without hard links can't answer it that way; the ``st_dev``
    comparison then stands in.

    The live database is a third place. DATABASE_URL may put the SQLite file
    outside DATA_DIR (config.py allows any ``sqlite:///`` path), yet the swap
    parks it — and its -wal / -shm / -journal — under
    ``DATA_DIR/.pre-restore-*`` and promotes the staged wsi.db onto its path.
    On another mount every such apply would fail at boot with EXDEV, roll
    back and drop the stage, the same way on every retry. So the directory
    holding the database is probed against both the stage and DATA_DIR too.
    """
    data_dir = settings.data_dir
    if not _renames_can_cross(stage_dir, data_dir, "the staging directory"):
        raise RuntimeError(
            "restore stage directory and data directory are on different "
            "filesystems or mounts; the swap's atomic renames cannot cross them. "
            f"Put BACKUP_DIR on the same mount as DATA_DIR ({data_dir}) "
            "— e.g. inside it, or under one ReadWritePaths= entry — and retry."
        )
    db_dir = os.path.dirname(os.path.abspath(_live_db_path()))
    if os.path.realpath(db_dir) == os.path.realpath(data_dir):
        return  # the default layout: DATA_DIR/wsi.db, covered above
    # The promote moves stage -> db_dir; the parks (and their rollback) move
    # db_dir <-> DATA_DIR.
    if not (
        _renames_can_cross(stage_dir, db_dir, "the staging directory")
        and _renames_can_cross(db_dir, data_dir, "the database's directory")
    ):
        raise RuntimeError(
            f"the live database ({_live_db_path()}, from DATABASE_URL) is on a "
            f"different filesystem or mount from DATA_DIR ({data_dir}) or "
            "BACKUP_DIR; a restore parks it under DATA_DIR/.pre-restore-* and "
            "moves the staged database onto its path with atomic renames, which "
            "cannot cross mounts. Keep the SQLite file on the same mount as "
            "DATA_DIR and BACKUP_DIR (by default it is DATA_DIR/wsi.db) and retry."
        )


def _renames_can_cross(src_dir: str, dst_dir: str, src_label: str) -> bool:
    """Whether a rename(2) from ``src_dir`` into ``dst_dir`` can succeed, by
    hard-linking a probe file across (see :func:`_assert_stage_on_data_volume`);
    ``st_dev`` decides where hard links are unsupported. Raises RuntimeError
    when ``src_dir`` cannot be written or either directory stat'ed."""
    probe = os.path.join(src_dir, f".rename-probe-{secrets.token_hex(6)}")
    target = os.path.join(dst_dir, os.path.basename(probe))
    try:
        with open(probe, "wb"):
            pass
    except OSError as exc:
        raise RuntimeError(f"unable to write to {src_label}: {exc}")
    try:
        os.link(probe, target)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return False
        try:
            return os.stat(src_dir).st_dev == os.stat(dst_dir).st_dev
        except OSError as stat_exc:
            raise RuntimeError(f"unable to stat {src_dir!r} or {dst_dir!r}: {stat_exc}")
    else:
        _remove_quietly(target)
        return True
    finally:
        _remove_quietly(probe)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# The swap itself — plan, journal, rollback, crash recovery — lives in
# restore_apply, the one implementation both start-wsi-app.sh (production, as
# ``python -m wsi_app.restore_apply``) and the dev path below run, so the two
# cannot drift into different crash semantics. It is standard-library only so
# the start script can run it without initialising this app; the wrappers
# here only bind it to ``settings``.


def restore_journal_path() -> str:
    """Where an apply journals its moves: ``RESTORE_JOURNAL_PATH`` or
    ``{data_dir}/.restore-apply.journal`` — the same default start-wsi-app.sh
    derives from ``DATA_DIR``. On the data volume, never /run, which a reboot
    wipes along with exactly the state the journal exists to recover."""
    return settings.restore_journal_path or os.path.join(
        settings.data_dir, _restore_apply.JOURNAL_NAME
    )


def recover_interrupted_apply() -> Optional[str]:
    """Reconcile a journal left by an apply that died mid-swap.

    The dev counterpart of start-wsi-app.sh's boot-time reconciliation (the
    same restore_apply code, minus the result file), run by the app's lifespan
    when ``BACKUP_DEV_INPROCESS_APPLY`` is on — a SIGKILL or power loss during
    :func:`inprocess_apply` bypasses its own rollback. Returns None when there
    is no journal, ``"rolled_back"`` or ``"completed"`` otherwise; raises
    ``restore_apply.NotReconciled`` (a ``RuntimeError``) — keeping the journal
    — when the tree cannot be brought back to a consistent state.
    """
    path = restore_journal_path()
    if not os.path.lexists(path):
        return None
    journal = _restore_apply.load_journal(path)
    outcome = _restore_apply.settle_journal(journal, path)
    if outcome == "completed":
        _revoke_sessions_after_apply(journal)
    _restore_apply.close_journal(journal, path)
    _logger.warning(
        "reconciled an interrupted restore apply of stage %s: %s",
        journal.get("stage_id"), outcome,
    )
    return outcome


def request_apply(stage_id: str, scope: str, requested_by: str) -> None:
    """Write ``settings.restore_request_path`` so start-wsi-app.sh applies the
    swap on the next service start. Caller should kick a service restart
    afterward.

    ``scope`` is one of :data:`VALID_APPLY_SCOPES` — same semantics as
    :func:`inprocess_apply` (all scopes wipe wsi/; only ``full`` promotes a
    staged wsi/ back into place).
    """
    if scope not in VALID_APPLY_SCOPES:
        raise ValueError(f"unknown apply scope {scope!r}")
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    # Both before the request exists: the boot that acts on it trusts the
    # stage to be whole and the renames to stay on one mount.
    _assert_stage_complete(stage_dir)
    _assert_stage_on_data_volume(stage_dir)
    # The boot promotes wsi.db alone; a -wal / -journal beside it would hold
    # writes (the schema upgrade, a reset's wipe) that never reached wsi.db
    # and are deleted with the stage.
    staged_db = os.path.join(stage_dir, DB_ENTRY)
    for suffix in _restore_apply.DB_SIBLING_SUFFIXES:
        if os.path.lexists(staged_db + suffix):
            raise RuntimeError(
                f"the staged {DB_ENTRY} has a {DB_ENTRY}{suffix} beside it, so "
                "writes made to it may not be in the file the apply would "
                "promote; stage the archive again"
            )
    payload = {
        "stage_id": stage_id,
        "stage_dir": stage_dir,
        "scope": scope,
        "data_dir": settings.data_dir,
        "db_path": _live_db_path(),
        "app_version": APP_VERSION,
        "requested_at": _utc_iso_z(),
        "requested_by": requested_by,
        # What the boot and the API age the request by, so a wall-clock step
        # before the restart neither expires it early nor keeps it alive
        # (see restore_apply.REQUEST_MAX_AGE_SECONDS).
        **_restore_apply.request_clock_stamp(),
    }
    req_path = settings.restore_request_path
    os.makedirs(os.path.dirname(req_path), exist_ok=True)
    # Drop the previous apply's report first. The dashboard waits for a result
    # naming this stage, and one could already be there: re-applying a stage
    # whose earlier request expired (the API wrote that "expired" report for
    # the same stage_id while claiming the slot for this one) would read as
    # this apply having failed — the stage dropped from the screen — while
    # the restart then applies it. From here on only the boot that acts on this
    # request writes a result.
    _remove_quietly(settings.restore_result_path)
    # _atomic_write_json's tmp + os.replace makes concurrent readers see
    # either the previous file or the complete new one, never a half-written
    # one. ``/run/wsi`` is a tmpfs, so a host crash before the restart loses
    # the request; nothing has moved by then, so the live data is intact and
    # the operator applies again. The path itself is fixed: the restart unit
    # hard-codes it (see docs/operations.md, "Configuration").
    _atomic_write_json(req_path, payload)


def read_apply_request() -> Optional[tuple[dict, bool]]:
    """The pending restore / reset request and whether it has expired — no
    restart picked it up within ``restore_apply.REQUEST_MAX_AGE_SECONDS``,
    so the boot will refuse it — or None when there is none. One that cannot
    be parsed reads as ``{}``, aged by its file's mtime."""
    path = settings.restore_request_path
    try:
        with open(path, "r", encoding="utf-8") as f:
            req = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        req = {}
    if not isinstance(req, dict):
        req = {}
    age = _restore_apply.request_age_seconds(req, path)
    return req, _restore_apply.request_expired(age)


def read_apply_result() -> Optional[dict]:
    """Read ``settings.restore_result_path`` (written by the start script).

    The file is left in place so multiple dashboard tabs / reloads can all
    observe the same outcome; clearing it is an explicit operation via the
    ``DELETE /api/admin/restore/result`` endpoint.
    """
    p = settings.restore_result_path
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def inprocess_apply(stage_id: str, scope: str, engine) -> None:
    """Dev-only: swap files in the running process. Production uses
    request_apply + service restart instead.

    ``scope`` is one of :data:`VALID_APPLY_SCOPES`. All scopes wipe the live
    ``wsi.db``, ``assets/``, ``wsi/``, and ``thumbs/`` by parking them under
    ``.pre-restore-<UTC>/``; only ``full`` promotes a staged ``wsi/`` +
    ``thumbs/`` back into place. ``metadata`` and ``reset`` therefore leave
    both empty by design — operators re-upload WSI files (which regenerates
    thumbnails) or run the "Rebuild thumbnails" action after a bulk copy.

    The plan, journal and rollback are restore_apply's — the very code
    start-wsi-app.sh runs — so an exception (even KeyboardInterrupt) rolls
    back here, and a kill that skips this frame entirely leaves the journal for
    :func:`recover_interrupted_apply` on the next start.
    """
    if scope not in VALID_APPLY_SCOPES:
        raise ValueError(f"unknown apply scope {scope!r}")
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    live_db = _live_db_path()
    journal_path = restore_journal_path()
    if os.path.lexists(journal_path):
        # Never stack a second swap onto a tree an earlier one may have left
        # half-applied; the next start reconciles it first.
        raise RuntimeError(
            f"an interrupted restore apply is still recorded at {journal_path!r}; "
            "restart the app to reconcile it before applying another"
        )
    # Nor onto a database whose last restore could not sign its sessions out
    # (restore_apply.SESSION_REVOCATION_SUFFIX; its post-apply hook's retry
    # failed too, and the dev process kept serving after that 500). The
    # marker names THAT restore's pre-restore database — the only record of
    # the counters it must be signed out past — and a second swap would park
    # the database the marker sits beside and either drop the marker on its
    # own successful sign-out or overwrite it with its own on a failed one:
    # every credential the first restore should have revoked would then
    # stay honoured. The boot never meets this (it refuses to serve until
    # the retry succeeds); here, restarting is what runs the retry.
    revocation_marker = live_db + _restore_apply.SESSION_REVOCATION_SUFFIX
    if os.path.lexists(revocation_marker):
        raise RuntimeError(
            f"a previous restore or reset could not sign out the sessions "
            f"issued before it, and that sign-out is still pending "
            f"({revocation_marker!r}); restart the app — its startup retries "
            f"the sign-out — before applying another restore or reset"
        )
    _assert_stage_complete(stage_dir)
    _assert_stage_on_data_volume(stage_dir)

    # 1. Hold back every new connection of this process for the swap
    #    (db.db_swap_gate): a request thread connecting while the live DB path
    #    is missing — between parking the live DB and its siblings and
    #    promoting the staged one — would create an empty wsi.db / -wal / -shm
    #    there, which the promote refuses to land beside
    #    (restore_apply._refuse_occupied_db_promote, still the backstop should
    #    the gate's bounded wait run out), and the swap's rollback then fails
    #    closed on. Such a request waits instead, and opens the restored database.
    #    The sign-out runs inside the gate too, so no request is served by the
    #    restored database's counters before they are moved on. Then stop
    #    SQLAlchemy from holding the file open.
    from .db import db_swap_gate

    with db_swap_gate():
        engine.dispose()

        # 2. Plan, journal and run the moves. A short uuid suffix keeps two applies
        #    that land in the same wall-clock second (the timestamp's resolution)
        #    from sharing one rollback dir — otherwise the second apply's park of
        #    ``assets`` would collide with the first apply's already-parked
        #    ``assets/``. The production swap can't collide (one apply per service
        #    boot), but the dev in-process path runs many per process. The
        #    ``.pre-restore-*`` glob the docs/recovery steps use still matches, and
        #    ``ls -dt`` still orders by mtime.
        journal = _restore_apply.plan_journal(
            stage_id=stage_id, scope=scope, stage_dir=stage_dir,
            data_dir=settings.data_dir, db_path=live_db, app_version=APP_VERSION,
            rollback_suffix=f"-{uuid.uuid4().hex[:8]}",
        )
        try:
            _restore_apply.write_json_durable(journal_path, journal)
            _restore_apply.apply_journal(journal, journal_path)
        except _restore_apply.NotReconciled as exc:
            # The rollback stopped at a step it could not undo rather than guess;
            # the journal is kept, so the next start refuses to serve (or, in dev,
            # retries the reconciliation) instead of running on a mixed tree.
            _logger.critical(
                "inprocess_apply rollback stopped (%s); journal %s kept for "
                "reconciliation on the next start", exc, journal_path,
            )
            raise
        except BaseException as exc:
            # Undone (or nothing had moved yet): the tree is pre-restore again.
            try:
                _restore_apply.remove_durable(journal_path)
            except OSError:
                _logger.exception("inprocess_apply: could not remove journal %s", journal_path)
            # Clean up the staged tree even on failure. The undo moved the
            # promoted entries back into it, but the operator must re-stage a
            # fresh archive to retry anyway, so it is just dead disk usage.
            cleanup_stage(stage_id)
            _write_apply_result({
                "ok": False, "stage_id": stage_id, "scope": scope, "stage": "swap",
                "error": str(exc) or type(exc).__name__,
            })
            raise

        result = _restore_apply.committed_result(journal)
    if result.get("session_revocation_error"):
        _logger.error(
            "restore of stage %s: could not sign out the sessions issued before "
            "it: %s", stage_id, result["session_revocation_error"],
        )
    _write_apply_result(result)
    _restore_apply.close_journal(journal, journal_path)


def _write_apply_result(payload: dict) -> None:
    """Report a dev in-process apply in ``settings.restore_result_path``, as
    the boot-time run reports a production one. Signing everyone out — the
    applying admin included — leaves the dashboard nothing else to read the
    outcome from: after signing in again it matches this file's stage_id.
    Best effort; the apply stands (or was undone) either way."""
    payload.setdefault("applied_at", _restore_apply.utc_now_z())
    path = settings.restore_result_path
    try:
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        _restore_apply.write_json_durable(path, payload)
    except OSError:
        _logger.warning("could not write the restore result %s", path, exc_info=True)


def record_post_apply_error(
    stage_id: str, scope: Optional[str], stage: str, error: str,
) -> None:
    """Amend a dev in-process apply's result with a post-apply hook failure.

    :func:`inprocess_apply` writes ``ok: true`` the moment the swap commits;
    the hooks that bring the running process up to the new DB (schema
    reconcile, bootstrap, the sign-out retry) run after it. When one of them
    fails the endpoint answers 500 and the job fails, but the apply has
    signed everyone out, so after signing in again the dashboard reads only
    this file — and an unamended ``ok: true`` would show a clean success. ``ok``
    stays true (the swap committed and is not rolled back, and the
    stage-id-matched success flow keys on it); ``post_apply_error`` carries
    the same message the endpoint and job report, ``post_apply_stage`` the
    step. The swap's own fields (rollback_root, wsi_uploads_pending,
    session_revocation_error, applied_at) are kept; if its report is missing
    or belongs to another stage (its write failed), a minimal ``ok: true``
    one is written instead, since the swap did commit. Durable and best
    effort, as :func:`_write_apply_result`.
    """
    result = read_apply_result()
    if (
        not isinstance(result, dict)
        or result.get("stage_id") != stage_id
        or result.get("ok") is not True
    ):
        result = {"ok": True, "stage_id": stage_id, "scope": scope}
    result["post_apply_error"] = error
    result["post_apply_stage"] = stage
    _write_apply_result(result)


def retry_pending_session_revocation() -> None:
    """Finish the sign-out a committed restore / reset could not do, or refuse
    to let the app serve.

    restore_apply leaves ``restore_apply.SESSION_REVOCATION_SUFFIX`` beside
    the live database when its sign-out failed after the swap committed.
    Served as it is, that database honours every staff token and app cookie
    revoked since the backup was taken (a password change, a disabled
    account, a "sign everyone out"): the result file's
    ``session_revocation_error`` alone failed open. So the boot (and the dev
    in-process apply's hooks) runs this once the schema is current — which by
    then gives the sign-out the users / auth_settings shape it needs — and
    before anything authenticates a request: the sign-out is retried against
    the parked pre-restore database the marker names, and the marker removed
    once it succeeded. Run before :func:`apply_session_generation_floor`,
    which records a floor the retry may leave.

    A parked database the marker names that is gone (an operator tidied
    ``.pre-restore-*`` before restarting) or unreadable (the damaged
    database the restore replaced) does not refuse the start:
    ``restore_apply.invalidate_sessions`` then moves every counter past a
    floor no value of that database can have reached
    (``restore_apply.unknown_previous_floor``). Refusing there would make the
    service unstartable after exactly the disaster a restore recovers from,
    until someone deleted the marker by hand — which signs nobody out.

    Raises RuntimeError — keeping the marker, so the next start retries —
    when the marker cannot be read, or the sign-out fails again: the live
    database itself cannot be read or updated, which no floor stands in for.
    Failing closed costs a service that refuses to start with the reason in
    its log; failing open, revoked credentials that work.
    """
    db_path = _live_db_path()
    marker = db_path + _restore_apply.SESSION_REVOCATION_SUFFIX
    remedy = (
        f"Fix the cause and restart; or, to serve this data accepting that "
        f"credentials revoked before the restore may work again until each "
        f"user's password is changed, delete {marker!r}."
    )
    try:
        with open(marker, "r", encoding="utf-8") as f:
            pending = json.load(f)
        if not isinstance(pending, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"a restore could not sign out the sessions issued before it, and "
            f"its retry marker {marker!r} is unreadable ({exc}). {remedy}"
        ) from exc
    previous = pending.get("previous_db")
    if previous is not None and not isinstance(previous, str):
        # A database was parked (the key is set) but the marker cannot say
        # where: as unknown as a deleted one, so signed out past the floor.
        previous = ""
    try:
        _restore_apply.invalidate_sessions(db_path, previous)
    except Exception as exc:
        raise RuntimeError(
            f"a restore could not sign out the sessions issued before it "
            f"(first failure: {pending.get('error')}), and retrying it at "
            f"startup failed too ({exc}); refusing to serve credentials it may "
            f"have revoked. {remedy}"
        ) from exc
    _restore_apply.remove_durable(marker)
    _logger.warning(
        "signed out the sessions issued before the restore of stage %s at "
        "startup (the restore itself could not: %s)",
        pending.get("stage_id"), pending.get("error"),
    )


def apply_session_generation_floor() -> None:
    """Raise ``auth_settings.session_generation`` to the floor a restore left
    beside the live database (``restore_apply.SESSION_FLOOR_SUFFIX``), then
    drop it.

    The swapped-in database had no auth_settings row (or column) to record
    the generation the restore moved to. A missing row is created here — as
    the dashboard would create it, with the model's defaults; a missing column
    was added at its default by the schema reconcile — and the generation is
    raised to the floor. Run by the boot
    (and after a dev in-process apply) once the schema is current. A floor
    that cannot be read is logged and left in place.
    """
    from .admin_helpers import get_or_create_singleton
    from .db import SessionLocal
    from .models import AuthSettings

    path = _live_db_path() + _restore_apply.SESSION_FLOOR_SUFFIX
    try:
        with open(path, "r", encoding="utf-8") as f:
            floor = int(json.load(f)["session_generation"])
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _logger.error("could not read the session generation floor %s: %s", path, exc)
        return
    db = SessionLocal()
    try:
        row = get_or_create_singleton(db, AuthSettings)
        if int(row.session_generation or 0) < floor:
            row.session_generation = floor
        db.commit()
    finally:
        db.close()
    _restore_apply.remove_durable(path)


def _revoke_sessions_after_apply(journal: dict) -> None:
    """Sign out every session issued before a committed dev in-process apply,
    as the boot-time run does (``restore_apply.invalidate_sessions``). The
    caller reloads the app's cached session generation."""
    err = _restore_apply.invalidate_sessions_after(journal)
    if err:
        _logger.error(
            "restore of stage %s: could not sign out the sessions issued before "
            "it: %s", journal.get("stage_id"), err,
        )


# ---------------------------------------------------------------------------
# Preflight: archive integrity + live-state / archive-state summaries
# ---------------------------------------------------------------------------

@dataclass
class IntegrityReport:
    """Result of :func:`validate_archive_integrity` for a staged archive.

    ``ok`` is True when the staged archive's DB references resolve cleanly
    against the on-disk archive contents. ``errors`` lists specific failures
    so the apply endpoint can refuse with a precise message. ``warnings`` lists
    findings that do not block the restore (``ok`` ignores them); they are
    also logged.
    """
    ok: bool
    errors: list[str]
    missing_wsi_files: list[str]
    unexpected_wsi_files: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "missing_wsi_files": self.missing_wsi_files,
            "unexpected_wsi_files": self.unexpected_wsi_files,
        }


def validate_archive_integrity(stage_id: str, manifest: Manifest) -> IntegrityReport:
    """Cross-check the staged DB's references against the staged file tree.

    Catches archives that are internally inconsistent (DB rows referencing
    files that didn't make it into the archive) *before* the swap, so a
    metadata-scope restore can't promote a DB whose wsi_filename column
    points at files the operator no longer has, and a full-scope restore
    can't leave the live wsi/ in a state where every freshly-promoted
    slide returns 404 for tile requests.

    For ``full`` scope this also flags wsi files present in the archive
    but unreferenced by the staged DB — those would just be dead weight
    after the swap.
    """
    errors: list[str] = []
    warnings: list[str] = []
    missing_wsi: list[str] = []
    unexpected_wsi: list[str] = []

    if manifest.scope not in VALID_SCOPES:
        errors.append(f"manifest has invalid scope: {manifest.scope!r}")
        return IntegrityReport(
            ok=False, errors=errors,
            missing_wsi_files=missing_wsi,
            unexpected_wsi_files=unexpected_wsi,
        )

    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    db_path = os.path.join(stage_dir, DB_ENTRY)
    if not os.path.isfile(db_path):
        errors.append("staged archive has no wsi.db")
        return IntegrityReport(
            ok=False, errors=errors,
            missing_wsi_files=missing_wsi,
            unexpected_wsi_files=unexpected_wsi,
        )

    try:
        db_wsi_filenames, db_tour_ids = _enumerate_snapshot(db_path)
    except sqlite3.Error as exc:
        # An unreadable archive DB means WSI coverage cannot be verified; report
        # it as an integrity error (ok=False) instead of silently skipping the
        # check, which an empty db_wsi_set below would have done.
        errors.append(f"archive database is unreadable: {exc}")
        db_wsi_filenames, db_tour_ids = [], []
    db_wsi_set = {n for n in db_wsi_filenames if n}

    # Defense in depth: a wsi_filename is only ever a bare basename minted by
    # the uploader. Reject any row whose filename contains a path separator or
    # starts with "." so a crafted whole-DB archive can't smuggle a path-
    # traversal value (e.g. "../../etc/x" or an absolute path) that a later
    # consumer joins onto the wsi/ root — the restore swap and tile path both
    # trust these names as basenames. Reported as a plain integrity error so
    # the apply endpoint refuses the archive with a clear message.
    # create_backup refuses to write such an archive with the same predicate.
    unsafe_wsi = sorted(
        n for n in db_wsi_set if not _is_safe_wsi_filename(n)
    )
    if unsafe_wsi:
        sample = ", ".join(unsafe_wsi[:3]) + ("…" if len(unsafe_wsi) > 3 else "")
        errors.append(
            f"archive database has {len(unsafe_wsi)} slide row(s) with an unsafe "
            f"wsi_filename (path separator or leading dot): {sample}"
        )

    # Tour public_ids are a WARNING, not an error — unlike wsi_filename above.
    # create_backup archives a live DB verbatim and only *skips the assets* of
    # a tour whose token is invalid (with a warning), so refusing such rows
    # here would make a backup that reported success impossible to restore. Keeping
    # the row is safe because nothing turns a stored tour token into a path
    # without validating it first: the restore itself never uses DB ids
    # (_safe_extract works on member names and refuses ".." segments and
    # duplicates; the swap moves the assets/ tree wholesale), create_backup
    # and the slide export skip invalid tokens, and every other filesystem
    # consumer goes through admin_helpers.tour_asset_dir, which raises on a
    # malformed token (best-effort cleanup sites check valid_public_id first).
    # So a "../.." row restored here can no longer make the next backup walk
    # DATA_DIR; such a tour just has no reachable assets.
    unsafe_tours = sorted(t for t in db_tour_ids if not valid_public_id(t))
    if unsafe_tours:
        sample = ", ".join(repr(t) for t in unsafe_tours[:3]) + (
            "…" if len(unsafe_tours) > 3 else ""
        )
        warnings.append(
            f"archive database has {len(unsafe_tours)} tour row(s) with an "
            f"invalid public_id (restored as-is; their assets are not "
            f"reachable and are never archived): {sample}"
        )

    # Tour-asset presence is deliberately not checked: a tour with no
    # markdown attachments legitimately has no assets/tours/{id}/ dir,
    # per-tour expected file counts are not carried in the archive, and a
    # missing attachment dir only means the tour shows no images — the tour
    # rows themselves are preserved by the DB swap.

    if manifest.scope == SCOPE_FULL:
        # full archive should carry every wsi file the DB references.
        archive_wsi_dir = os.path.join(stage_dir, "wsi")
        if db_wsi_set:
            archive_wsi_set: set[str] = set()
            if os.path.isdir(archive_wsi_dir):
                try:
                    archive_wsi_set = {
                        n for n in os.listdir(archive_wsi_dir)
                        if os.path.isfile(os.path.join(archive_wsi_dir, n))
                    }
                except OSError as exc:
                    errors.append(f"archive wsi/ unreadable: {exc}")
            missing_wsi = sorted(db_wsi_set - archive_wsi_set)
            unexpected_wsi = sorted(archive_wsi_set - db_wsi_set)
            if missing_wsi:
                errors.append(
                    f"full-scope archive is missing {len(missing_wsi)} wsi file(s) "
                    f"referenced by its database"
                )
    elif manifest.scope == SCOPE_METADATA:
        # metadata archive should NOT carry a wsi/ directory at all — the
        # swap leaves wsi/ empty by design.
        archive_wsi_dir = os.path.join(stage_dir, "wsi")
        if os.path.isdir(archive_wsi_dir):
            errors.append(
                "metadata-scope archive unexpectedly contains a wsi/ directory; "
                "manifest may be lying about its scope"
            )
        # Likewise no thumbs/ — keeping the invariant that a thumbnail never
        # exists without its backing WSI file. A metadata restore wipes
        # thumbs/, so an archive that shipped them would reintroduce
        # thumbnails for slides whose WSI bodies are gone.
        archive_thumbs_dir = os.path.join(stage_dir, "thumbs")
        if os.path.isdir(archive_thumbs_dir):
            errors.append(
                "metadata-scope archive unexpectedly contains a thumbs/ directory; "
                "manifest may be lying about its scope"
            )

    for w in warnings:
        _logger.warning("restore integrity (stage %s): %s", stage_id, w)
    return IntegrityReport(
        ok=not errors,
        errors=errors,
        missing_wsi_files=missing_wsi,
        unexpected_wsi_files=unexpected_wsi,
        warnings=warnings,
    )


def _dir_total_bytes(path: str) -> int:
    """Sum the on-disk size of every regular file under ``path``. Best-effort:
    permission errors / disappeared files are skipped silently."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        pass
    return total


def get_live_state_summary() -> dict:
    """Snapshot the live DB + filesystem so the UI can render an honest
    "this much data will be deleted" warning before an operator confirms
    a restore or reset.

    Numbers are read once and may be slightly stale by the time the apply
    actually runs — this is fine because the apply endpoint re-validates
    the staged archive (and the rollback root captures the *actual* state
    that was wiped, not these numbers).
    """
    db_path = _live_db_path()
    slide_count = 0
    tour_count = 0
    wsi_filenames_in_db: list[str] = []
    if os.path.isfile(db_path):
        try:
            slide_count, tour_count = _read_db_counts(db_path)
        except Exception:
            pass
        try:
            wsi_filenames_in_db, _ = _enumerate_snapshot(db_path)
        except Exception:
            pass

    data_dir = settings.data_dir
    wsi_root = os.path.join(data_dir, "wsi")
    assets_root = os.path.join(data_dir, "assets")

    wsi_files_on_disk: list[str] = []
    if os.path.isdir(wsi_root):
        try:
            wsi_files_on_disk = [
                n for n in os.listdir(wsi_root)
                if os.path.isfile(os.path.join(wsi_root, n))
            ]
        except OSError:
            pass

    return {
        "slide_count": slide_count,
        "tour_count": tour_count,
        "wsi_file_count": len(wsi_files_on_disk),
        "wsi_files_referenced_by_db": len([n for n in wsi_filenames_in_db if n]),
        "wsi_total_bytes": _dir_total_bytes(wsi_root) if os.path.isdir(wsi_root) else 0,
        "assets_total_bytes": _dir_total_bytes(assets_root) if os.path.isdir(assets_root) else 0,
    }


def get_archive_state_summary(stage_id: str, manifest: Manifest) -> dict:
    """Summarise what a staged archive will install."""
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    db_path = os.path.join(stage_dir, DB_ENTRY)
    slide_count = manifest.slide_count
    tour_count = manifest.tour_count
    wsi_filenames: list[str] = []
    if os.path.isfile(db_path):
        try:
            slide_count_db, tour_count_db = _read_db_counts(db_path)
            slide_count, tour_count = slide_count_db, tour_count_db
        except Exception:
            pass
        try:
            wsi_filenames, _ = _enumerate_snapshot(db_path)
        except Exception:
            pass
    wsi_dir = os.path.join(stage_dir, "wsi")
    wsi_file_count = 0
    wsi_total_bytes = 0
    if os.path.isdir(wsi_dir):
        try:
            wsi_file_count = sum(
                1 for n in os.listdir(wsi_dir)
                if os.path.isfile(os.path.join(wsi_dir, n))
            )
        except OSError:
            pass
        wsi_total_bytes = _dir_total_bytes(wsi_dir)
    return {
        "scope": manifest.scope,
        "slide_count": slide_count,
        "tour_count": tour_count,
        "wsi_file_count": wsi_file_count,
        "wsi_files_referenced_by_db": len([n for n in wsi_filenames if n]),
        "wsi_total_bytes": wsi_total_bytes,
        # Number of slides that will need a WSI re-upload after the apply.
        # For full-scope this is 0 (the archive carries them); for
        # metadata-scope this is every slide the staged DB references,
        # since wsi/ is wiped and not replaced.
        "wsi_uploads_pending": (
            0 if manifest.scope == SCOPE_FULL
            else len([n for n in wsi_filenames if n])
        ),
    }


# ---------------------------------------------------------------------------
# Reset stage: build a "wiped" DB image for the apply machinery to swap in
# ---------------------------------------------------------------------------

def _preflight_reset_free_space(stage_dir: str) -> None:
    """Refuse a reset whose DB snapshot cannot fit on the staging volume with
    the shared reserve (disk_space) left free.

    The reset stage is a full copy of the live DB (the WAL checkpointed into
    it) written under BACKUP_DIR/.staging — by default the data volume the
    live DB itself writes to — before the wipe shrinks it. Without this
    check, a reset started on a nearly-full disk (precisely when an operator
    reaches for it) would stream the snapshot into ENOSPC, failing the live
    DB's own writes meanwhile, which every other large write (backup, restore,
    import, upload) refuses up front. The wipe's rollback journal and the
    VACUUM's temporary copy are not counted: pages freed by the DELETEs are
    not journaled, and the VACUUM copy holds only the preserved admin /
    config rows. Best-effort like every check here: an unreadable size or
    unknown free space skips it."""
    try:
        db_path = _live_db_path()
        nbytes = os.path.getsize(db_path)
    except (OSError, RuntimeError):
        return
    try:
        nbytes += os.path.getsize(db_path + "-wal")
    except OSError:
        pass
    short = _disk_space.check(os.path.dirname(stage_dir), nbytes)
    if short is not None:
        raise RuntimeError(short.message("staging volume"))


def create_reset_stage(stage_id: Optional[str] = None) -> str:
    """Build a staging directory containing a DB image with only the admin /
    config rows preserved, ready for :func:`request_apply` / :func:`inprocess_apply`.

    The reset uses the same swap-then-rollback machinery as a restore so
    operators get the same atomicity guarantees: live DB + assets/ + wsi/
    are parked under ``.pre-restore-<UTC>/`` and only removed by the
    operator once they're satisfied with the result.

    ``stage_id`` (from :func:`new_stage_id`) lets the caller hand the id out
    before the stage is built; a fresh one is minted otherwise. Returns the
    ``stage_id``.
    """
    _ensure_backup_dirs()
    # Opportunistic sweep mirrors the same in stage_restore() — a fresh apply
    # attempt is a good moment to reclaim stale stage trees.
    try:
        sweep_stale_staging()
    except Exception:
        _logger.exception("sweep_stale_staging failed during create_reset_stage; continuing")

    stage_id = stage_id or new_stage_id()
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=False)

    try:
        _preflight_reset_free_space(stage_dir)
        staged_db = os.path.join(stage_dir, DB_ENTRY)
        # Snapshot the live DB so the staged file has the exact same schema
        # (incl. any future migrations the binary has already run) and so
        # the preserved User rows are the *current* admin set, not some
        # stale copy. Using the online backup API keeps it safe under
        # concurrent traffic; the snapshot is wiped below before any reader
        # ever sees it.
        _snapshot_sqlite(_live_db_path(), staged_db)
        # Out of WAL mode before the wipe and the stamps write to it, so they
        # land in wsi.db itself — the one file an apply promotes — rather than
        # in a wsi.db-wal that reaches it only through the checkpoint on the
        # last close, and is otherwise deleted with the stage (as for a backup
        # snapshot; see the helper).
        _use_rollback_journal(staged_db)
        # Read before the wipe, which may empty app_meta.
        app_version = _version_to_stamp(_app_meta.sqlite_read_app_version(staged_db))
        _wipe_db_in_place(staged_db, preserve=_RESET_PRESERVE_TABLES)
        # Stamp the version so the post-swap engine open doesn't refuse it.
        # The reset image is a current-shape snapshot, so schema_version is
        # CURRENT (mirrors the app_version stamp; keeps the two consistent
        # whether or not app_meta was in the preserve set).
        _app_meta.sqlite_set_app_version(staged_db, app_version)
        _app_meta.sqlite_set_schema_version(staged_db, CURRENT_SCHEMA_VERSION)
        # Last, as stage_restore does: a reset job killed mid-wipe must not
        # leave a stage request_apply accepts (see STAGE_COMPLETE_MARKER).
        _mark_stage_complete(stage_dir, stage_id)
        return stage_id
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _wipe_db_in_place(db_path: str, preserve: tuple[str, ...]) -> None:
    """Delete every row from every user table in ``db_path`` except those
    listed in ``preserve``. Runs inside a single transaction so a crash
    can't leave the file partially-wiped, then VACUUMs to reclaim the
    space (otherwise the file stays large enough to hold the deleted
    rows, which defeats the point of resetting on a small disk).

    Uses ``PRAGMA foreign_keys=OFF`` for the duration so a delete-cascade
    can't follow a foreign key back into a preserved row (e.g. some
    future schema where a preserved table referenced a wiped one).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        all_tables = [r[0] for r in rows]
        wipe = [t for t in all_tables if t not in preserve]
        conn.execute("BEGIN")
        for t in wipe:
            # Quoting the identifier defends against the (currently impossible
            # but cheap-to-prevent) case where a future migration introduces
            # a table name with special characters.
            conn.execute(f'DELETE FROM "{t}"')
        # Reset autoincrement counters so post-reset slide/tour ids start
        # at 1 instead of resuming from the previous max. SQLite stores
        # these in the ``sqlite_sequence`` table (managed implicitly by
        # ``AUTOINCREMENT``); deleting from it is safe even when the table
        # is absent on schemas that never used AUTOINCREMENT.
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name NOT IN ("
                + ",".join(["?"] * len(preserve))
                + ")",
                preserve,
            )
        except sqlite3.OperationalError:
            # sqlite_sequence didn't exist (no AUTOINCREMENT in the schema).
            pass
        conn.commit()
        # VACUUM must run outside an open transaction.
        conn.execute("VACUUM")
    finally:
        conn.close()


def _strip_tables_in_place(db_path: str, tables) -> None:
    """Empty the named tables in ``db_path`` and reclaim the freed pages.

    The inverse selection of :func:`_wipe_db_in_place`: instead of deleting
    everything *except* a preserve-list, it deletes the rows of just the
    ``tables`` handed in (the ancillary analytics / audit groups an operator
    opted out of). Tables that don't exist in this DB are skipped, so the
    caller can pass a static group list without first reflecting the schema.
    Runs in one transaction, then compacts so the archived snapshot actually
    shrinks rather than carrying the deleted rows' pages as free space.

    The compaction is ``VACUUM INTO`` a sibling file swapped in afterwards,
    not a plain ``VACUUM``: that one builds a temp copy in SQLite's temp dir
    (TMPDIR — the data volume under systemd, which the backup preflight never
    measures) and then rewrites the file through a rollback journal beside
    it, so it would peak at three DB-sized files on the backup volume
    (snapshot, journal, archive) plus one on the data volume, past what the
    preflight allows for. ``VACUUM INTO`` writes the compacted copy once, next to the
    snapshot, so the peak stays at the two copies the preflight counts.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        present = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        targets = [t for t in tables if t in present]
        conn.execute("BEGIN")
        for t in targets:
            # Quote the identifier defensively (mirrors _wipe_db_in_place).
            conn.execute(f'DELETE FROM "{t}"')
        # Reset the autoincrement counters for the emptied tables so a later
        # restore + fresh inserts start ids at 1 rather than resuming from the
        # stripped rows' max. Safe no-op when sqlite_sequence is absent.
        if targets:
            try:
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN ("
                    + ",".join(["?"] * len(targets))
                    + ")",
                    targets,
                )
            except sqlite3.OperationalError:
                pass
        conn.commit()
        compacted = db_path + ".compact"
        _remove_quietly(compacted)
        try:
            # Outside a transaction, like VACUUM itself.
            conn.execute("VACUUM INTO ?", (compacted,))
        except BaseException:
            _remove_quietly(compacted)
            raise
    finally:
        conn.close()
    try:
        os.replace(compacted, db_path)
    except BaseException:
        _remove_quietly(compacted)
        raise


def _table_group_bytes(conn: sqlite3.Connection, tables) -> int:
    """Approximate the on-disk bytes occupied by ``tables`` (and their
    indexes) in an open SQLite connection.

    Prefers the ``dbstat`` virtual table (``SUM(pgsize)`` over the table's
    pages), which is compiled into CPython's bundled SQLite by default. When
    ``dbstat`` is unavailable, falls back to ``row_count * heuristic_bytes``
    so callers still get a usable order-of-magnitude estimate rather than 0.
    """
    present = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    targets = [t for t in tables if t in present]
    if not targets:
        return 0
    placeholders = ",".join(["?"] * len(targets))
    try:
        # dbstat.name carries the table OR index name; an index's `tbl`-style
        # linkage isn't exposed, so sum by the page owner directly. Indexes on
        # the target tables are named separately and would be missed here, but
        # for analytics/audit tables the row data dominates, so this is a
        # close-enough estimate.
        row = conn.execute(
            f"SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name IN ({placeholders})",
            targets,
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        # No dbstat vtab in this build — estimate from row counts. ~256 bytes
        # per row is a deliberately rough average across the analytics/audit
        # tables (page_views rows carry UA strings; audit_logs carry JSON).
        total_rows = 0
        for t in targets:
            try:
                total_rows += int(
                    conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                )
            except sqlite3.Error:
                continue
        return total_rows * 256


# estimate_backup_components walks wsi/ and reads every page of the analytics
# and history tables (dbstat) — GBs on a busy install — and the Backup tab
# asked for it on every open. It is a size hint, so one answer serves for a
# few minutes (per worker).
_ESTIMATE_TTL_SECONDS = 300.0
_estimate_cache: dict = {}
_estimate_lock = threading.Lock()


def estimate_backup_components(*, max_age: float = _ESTIMATE_TTL_SECONDS) -> dict:
    """:func:`_estimate_backup_components`, reused for ``max_age`` seconds.

    Keyed by the paths it measures, so a changed ``data_dir`` / DB (tests,
    a restore that swapped the DB) is measured afresh."""
    try:
        db_path = _live_db_path()
    except RuntimeError:
        db_path = ""
    key = (settings.data_dir, db_path)
    now = time.monotonic()
    with _estimate_lock:
        hit = _estimate_cache.get(key)
        if hit is not None and now - hit[0] < max_age:
            return dict(hit[1])
    out = _estimate_backup_components()
    with _estimate_lock:
        _estimate_cache.clear()
        _estimate_cache[key] = (now, dict(out))
    return out


def _estimate_backup_components() -> dict:
    """Approximate the uncompressed byte size each selectable backup component
    contributes, so the admin UI can show a size hint next to each toggle.

    Returns a dict with keys ``db_core``, ``analytics``, ``history``, ``wsi``,
    ``thumbs`` (all ints, bytes). Numbers are best-effort and read live, so
    they may be slightly stale by the time a backup actually runs — they exist
    to give the operator a sense of scale, not an exact archive size (the
    archive is additionally compressed). ``db_core`` is the whole DB file minus
    the two ancillary groups, clamped at 0.
    """
    data_dir = settings.data_dir
    wsi_root = os.path.join(data_dir, "wsi")
    thumbs_root = os.path.join(data_dir, "thumbs")
    out = {
        "db_core": 0,
        "analytics": 0,
        "history": 0,
        "wsi": _dir_total_bytes(wsi_root) if os.path.isdir(wsi_root) else 0,
        "thumbs": _dir_total_bytes(thumbs_root) if os.path.isdir(thumbs_root) else 0,
    }
    try:
        db_path = _live_db_path()
    except RuntimeError:
        return out
    if not os.path.isfile(db_path):
        return out
    db_total = 0
    try:
        db_total = os.path.getsize(db_path)
    except OSError:
        pass
    conn = sqlite3.connect(db_path)
    try:
        analytics = _table_group_bytes(conn, _ANALYTICS_TABLES)
        history = _table_group_bytes(conn, _HISTORY_TABLES)
    finally:
        conn.close()
    out["analytics"] = analytics
    out["history"] = history
    out["db_core"] = max(0, db_total - analytics - history)
    return out
