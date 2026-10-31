"""Admin error-log listing.

Read-only view over the ``error_logs`` table written by the DB logging
handler (``error_log.DBLogHandler``), plus a "clear" for rows already
triaged. Hard-gated to the admin role: log messages and tracebacks can embed
request paths, identifiers, and other operational detail, so the surface is
not opened to managers/instructors (same rationale as the audit-history
router).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..admin_helpers import audit
from ..admin_reports import (
    day_range_filters,
    escape_like,
    iter_newest_first,
    occurred_at_iso,
    server_tz_info,
)
from ..auth import require_admin
from ..config import settings
from ..csv_export import (
    claim_export_slot,
    resolve_export_limit,
    slot_csv_response,
)
from ..db import get_db
from ..models import ErrorLog, User

router = APIRouter()

# Ids are SQLite rowids; bound like the listing's offset so an oversized value
# is a 422 here rather than an OverflowError at bind time.
_MAX_ID = 2**62

# The logger name of the row a clear leaves behind (see admin_logs_clear).
_CLEAR_MARKER_LOGGER = "wsi_app.error_log.clear"


def _build_filters(
    level: Optional[str],
    logger: Optional[str],
    search: Optional[str],
    since: Optional[str],
    until: Optional[str],
) -> list:
    """Translate the shared query knobs into SQLAlchemy filter clauses.

    Used by the JSON listing, the level counts and the CSV export so the
    surfaces can never drift on what a given filter means.
    """
    filters = []
    if level:
        filters.append(ErrorLog.level == level.upper())
    if logger:
        # Exact logger name, or a prefix ending in "." (e.g. "wsi_app.routers.").
        if logger.endswith("."):
            filters.append(
                ErrorLog.logger.like(f"{escape_like(logger)}%", escape="\\")
            )
        else:
            filters.append(ErrorLog.logger == logger)
    if search:
        # The message, the traceback (an exception class or a frame's file
        # name is often the best handle) and the request path.
        pattern = f"%{escape_like(search)}%"
        filters.append(or_(
            ErrorLog.message.like(pattern, escape="\\"),
            ErrorLog.traceback.like(pattern, escape="\\"),
            ErrorLog.request_path.like(pattern, escape="\\"),
        ))
    # Days are read in the analytics timezone, exactly as the audit-history
    # filter reads them, so an audit event and the error it caused land on the
    # same "day" in both listings.
    filters += day_range_filters(
        ErrorLog.occurred_at, since, until, "adminLogs.invalidDate"
    )
    return filters


def _item(r, traceback: Optional[str] = None) -> dict:
    out = {
        "id": r.id,
        "occurred_at": occurred_at_iso(r.occurred_at),
        "level": r.level,
        "logger": r.logger,
        "message": r.message,
        "module": r.module,
        "func": r.func,
        "line": r.line,
        "has_traceback": bool(r.has_traceback) if traceback is None else bool(traceback),
        "request_path": r.request_path,
        "request_method": r.request_method,
    }
    if traceback is not None:
        out["traceback"] = traceback
    return out


@router.get("/api/admin/logs")
def admin_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10_000_000),
    # Exact level name (WARNING/ERROR/CRITICAL); column width 16.
    level: Optional[str] = Query(None, max_length=16),
    # Exact logger name, or a prefix ending in "."; column width 255.
    logger: Optional[str] = Query(None, max_length=255),
    # Substring match on the message, traceback and request path (SQL LIKE —
    # ASCII case-insensitive).
    search: Optional[str] = Query(None, max_length=255),
    # Inclusive ISO dates interpreted in the analytics timezone
    # (WSI_ANALYTICS_TIMEZONE, host TZ fallback) — consistent with the audit
    # history filter. E.g. since=2026-01-01&until=2026-01-31.
    since: Optional[str] = Query(None, max_length=10),
    until: Optional[str] = Query(None, max_length=10),
    # The snapshot the pages are read from: the ``snapshot_id`` the first page
    # returned. Without it, rows written while the operator pages would shift
    # every later page by that many rows (repeating some, skipping others).
    max_id: Optional[int] = Query(None, ge=0, le=_MAX_ID),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    filters = _build_filters(level, logger, search, since, until)
    snapshot_id = max_id
    if snapshot_id is None:
        snapshot_id = db.execute(select(func.max(ErrorLog.id))).scalar_one() or 0
    in_snapshot = [*filters, ErrorLog.id <= snapshot_id]

    total = db.execute(
        select(func.count()).select_from(ErrorLog).where(*in_snapshot)
    ).scalar_one()
    # Rows matching the filters written after the snapshot, so the UI can
    # offer to reload instead of silently showing a stale first page.
    newer = db.execute(
        select(func.count()).select_from(ErrorLog)
        .where(*filters, ErrorLog.id > snapshot_id)
    ).scalar_one()
    # Every filter but the level, so the counts say what each level choice
    # would show.
    level_filters = [
        *_build_filters(None, logger, search, since, until), ErrorLog.id <= snapshot_id,
    ]
    level_counts = {
        lvl: n for lvl, n in db.execute(
            select(ErrorLog.level, func.count())
            .where(*level_filters)
            .group_by(ErrorLog.level)
        ).all()
    }
    # The traceback (up to 16 KB a row) stays out of the listing: it is shown
    # on demand, from /api/admin/logs/{id}.
    rows = db.execute(
        select(
            ErrorLog.id, ErrorLog.occurred_at, ErrorLog.level, ErrorLog.logger,
            ErrorLog.message, ErrorLog.module, ErrorLog.func, ErrorLog.line,
            ErrorLog.request_path, ErrorLog.request_method,
            (func.coalesce(func.length(ErrorLog.traceback), 0) > 0).label("has_traceback"),
        )
        .where(*in_snapshot)
        .order_by(ErrorLog.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "total": total,
        "snapshot_id": snapshot_id,
        "newer": newer,
        "level_counts": level_counts,
        # The zone the since/until filters are read in, so the UI can render
        # occurred_at in the same one.
        "server_tz": server_tz_info(),
        "items": [_item(r) for r in rows],
    }


@router.delete("/api/admin/logs")
def admin_logs_clear(
    # Required: only the rows the operator has seen (the listing's
    # snapshot_id and below) are cleared, never one logged meanwhile.
    max_id: int = Query(..., ge=0, le=_MAX_ID),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete the stored error-log rows up to ``max_id``.

    Only the queryable copy goes: journald keeps every line, and the clear
    itself is audited.

    A marker row recording the clear is written first, in the same
    transaction. Besides telling the next reader why the list is short, it
    keeps the table from going empty: ids are a plain INTEGER PRIMARY KEY,
    which SQLite reissues from max(id)+1, so after deleting every row the next
    error would get id 1 — below every snapshot an open tab still holds, so
    its "newer" count would miss it and a stale clear from that tab
    (``max_id`` = its old snapshot) would delete it unseen.
    """
    # Only earlier clear markers up to max_id: nothing to clear. Writing a new
    # marker (and an audit row) for each such click would only pile them up.
    has_rows = db.execute(
        select(ErrorLog.id)
        .where(ErrorLog.id <= max_id, ErrorLog.logger != _CLEAR_MARKER_LOGGER)
        .limit(1)
    ).first() is not None
    if not has_rows:
        return {"deleted": 0}
    marker = ErrorLog(
        level="INFO",
        logger=_CLEAR_MARKER_LOGGER,
        message=(
            f"Error log cleared by {admin.username}: the rows up to id {max_id} "
            "were deleted from this table; journald keeps every line."
        ),
        traceback="",
    )
    db.add(marker)
    db.flush()
    # Below the marker whatever max_id says, so the marker always survives.
    deleted = db.execute(
        delete(ErrorLog).where(ErrorLog.id <= max_id, ErrorLog.id < marker.id)
    ).rowcount or 0
    db.commit()
    audit(admin, "system.error_logs.clear", target=str(max_id), deleted=deleted)
    return {"deleted": deleted}


# Column order mirrors the JSON listing (plus the raw id) so an admin who
# knows the on-screen table can read the spreadsheet without a legend.
_CSV_HEADER = [
    "occurred_at",
    "id",
    "level",
    "logger",
    "message",
    "module",
    "func",
    "line",
    "request_method",
    "request_path",
    "traceback",
]


@router.get("/api/admin/logs/export.csv")
def admin_logs_export_csv(
    # No default/`le` on the param: the effective ceiling follows the retention
    # row cap (WSI_ERROR_LOG_MAX_ROWS) and an absolute safety bound, both
    # applied by resolve_export_limit() below — so raising the retention cap
    # never silently truncates the download the way a fixed 100k `le` would, and
    # a disabled cap (0) can't stream an unbounded table. Mirrors the audit-history export.
    limit: Optional[int] = Query(None, ge=1),
    level: Optional[str] = Query(None, max_length=16),
    logger: Optional[str] = Query(None, max_length=255),
    search: Optional[str] = Query(None, max_length=255),
    since: Optional[str] = Query(None, max_length=10),
    until: Optional[str] = Query(None, max_length=10),
    # Tracebacks are most of the file's size (up to 16 KB a row); without them
    # the column stays, empty, so the layout does not change.
    include_traceback: bool = Query(True),
    _: User = Depends(require_admin),
    # The request session the auth dependency read the user through (cached
    # per request), handed over to be closed before the body streams.
    db: Session = Depends(get_db),
):
    effective_limit = resolve_export_limit(limit, settings.error_log_max_rows)
    # Validates the filters (a bad date is a 400) before a slot is taken.
    filters = _build_filters(level, logger, search, since, until)

    def _to_row(r) -> list:
        return [
            occurred_at_iso(r.occurred_at),
            r.id,
            r.level,
            r.logger,
            r.message or "",
            r.module or "",
            r.func or "",
            r.line if r.line is not None else "",
            r.request_method or "",
            r.request_path or "",
            (r.traceback or "") if include_traceback else "",
        ]

    if since or until:
        span = f"{since or 'start'}_{until or 'end'}"
    else:
        span = "all"
    fname = f"error-logs-{span}.csv"
    claim_export_slot()
    # Read in short keyset batches (see iter_newest_first) and stream, so
    # neither the rows nor the rendered CSV are ever all held at once, and no
    # read transaction stays open for the length of the download.
    return slot_csv_response(
        fname, _CSV_HEADER, iter_newest_first(ErrorLog, filters, effective_limit, _to_row),
        auth_db=db,
    )


@router.get("/api/admin/logs/{log_id:int}")
def admin_log_detail(
    log_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """One row, traceback included (the listing leaves it out)."""
    if log_id > _MAX_ID:
        raise HTTPException(status_code=404, detail="Not found")
    row = db.get(ErrorLog, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _item(row, traceback=row.traceback or "")
