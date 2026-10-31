"""Admin audit-history listing.

Read-only view over the ``audit_logs`` table written by
``admin_helpers.audit()``. Lives in its own router (not
``admin_system.py``) because that router is gated by
``require_server_access``, which managers/instructors can be granted;
the history surface includes login events, user management, and settings
changes, so it is hard-gated to the admin role.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..admin_reports import (
    day_range_filters,
    escape_like,
    iter_newest_first,
    occurred_at_iso,
    server_tz_info,
)
from ..audit_log import AUTH_FAILURE_ACTION_PREFIXES
from ..auth import require_admin
from ..config import settings
from ..csv_export import (
    claim_export_slot,
    resolve_export_limit,
    slot_csv_response,
)
from ..db import get_db
from ..models import AuditLog, User

router = APIRouter()
_logger = logging.getLogger("wsi_app.admin_history")

# See admin_logs._MAX_ID.
_MAX_ID = 2**62


def _details_obj(raw: str) -> dict:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _build_filters(
    action: Optional[str],
    actor: Optional[str],
    since: Optional[str],
    until: Optional[str],
    target: Optional[str] = None,
    system: bool = False,
) -> list:
    """Translate the shared query knobs into SQLAlchemy filter clauses.

    Used by both the JSON listing and the CSV export so the two surfaces
    can never drift on what a given filter means.
    """
    filters = []
    if action:
        if action.endswith("."):
            filters.append(
                AuditLog.action.like(f"{escape_like(action)}%", escape="\\")
            )
        else:
            filters.append(AuditLog.action == action)
    if actor:
        # Substring (ASCII case-insensitive), so a partial name finds the
        # user. A failed login has no actor — the name tried is its target —
        # so those rows match on the target: filtering by a user then shows
        # the attempts against that account too.
        pattern = f"%{escape_like(actor)}%"
        is_auth_failure = or_(*(
            AuditLog.action.like(f"{prefix}%") for prefix in AUTH_FAILURE_ACTION_PREFIXES
        ))
        filters.append(or_(
            AuditLog.actor_username.like(pattern, escape="\\"),
            and_(is_auth_failure, AuditLog.target.like(pattern, escape="\\")),
        ))
    if target:
        filters.append(AuditLog.target.like(f"%{escape_like(target)}%", escape="\\"))
    if system:
        # Rows with no actor: what the UI labels "System" (boot-time records,
        # background jobs, and unauthenticated login failures).
        filters.append(AuditLog.actor_id.is_(None))
        filters.append(AuditLog.actor_username == "")
    # Days are read in the analytics timezone, like the dashboard's buckets.
    filters += day_range_filters(
        AuditLog.occurred_at, since, until, "adminHistory.invalidDate"
    )
    return filters


@router.get("/api/admin/history")
def admin_history(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10_000_000),
    # Exact action name, or a category prefix ending in "." (e.g. "slide.").
    # max_length matches the DB column width in models.AuditLog.
    action: Optional[str] = Query(None, max_length=64),
    # Substring of actor_username (column width 255); for a failed login,
    # of the username tried.
    actor: Optional[str] = Query(None, max_length=255),
    # Substring of the target (column width 512).
    target: Optional[str] = Query(None, max_length=512),
    # Only rows with no actor ("System" in the UI).
    system: bool = Query(False),
    # Inclusive ISO dates interpreted in the analytics timezone
    # (WSI_ANALYTICS_TIMEZONE, host TZ fallback) — consistent with the
    # dashboard's daily buckets. E.g. since=2026-01-01&until=2026-01-31.
    since: Optional[str] = Query(None, max_length=10),
    until: Optional[str] = Query(None, max_length=10),
    # The snapshot the pages are read from (the first page's snapshot_id), so
    # events recorded while the operator pages do not shift later pages.
    max_id: Optional[int] = Query(None, ge=0, le=_MAX_ID),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    filters = _build_filters(action, actor, since, until, target, system)
    snapshot_id = max_id
    if snapshot_id is None:
        snapshot_id = db.execute(select(func.max(AuditLog.id))).scalar_one() or 0

    total = db.execute(
        select(func.count()).select_from(AuditLog)
        .where(*filters, AuditLog.id <= snapshot_id)
    ).scalar_one()
    newer = db.execute(
        select(func.count()).select_from(AuditLog)
        .where(*filters, AuditLog.id > snapshot_id)
    ).scalar_one()
    rows = db.execute(
        select(AuditLog)
        .where(*filters, AuditLog.id <= snapshot_id)
        .order_by(AuditLog.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return {
        "total": total,
        "snapshot_id": snapshot_id,
        # Matching events recorded after the snapshot (the UI offers a reload).
        "newer": newer,
        # The zone the since/until filters are read in, so the UI can render
        # occurred_at in the same one.
        "server_tz": server_tz_info(),
        "items": [
            {
                "id": r.id,
                "occurred_at": occurred_at_iso(r.occurred_at),
                "actor_id": r.actor_id,
                "actor_username": r.actor_username,
                "actor_role": r.actor_role,
                "action": r.action,
                "target": r.target,
                "details": _details_obj(r.details),
            }
            for r in rows
        ],
    }


# Column order mirrors the JSON listing (plus the raw id) so an admin who
# knows the on-screen table can read the spreadsheet without a legend.
_CSV_HEADER = [
    "occurred_at",
    "id",
    "actor_id",
    "actor_username",
    "actor_role",
    "action",
    "target",
    "details",
]


@router.get("/api/admin/history/export.csv")
def admin_history_export_csv(
    # No default/`le` on the param: the effective ceiling is derived from the
    # retention row cap (WSI_AUDIT_LOG_MAX_ROWS) and an absolute safety bound in
    # resolve_export_limit() below, both of which are dynamic. An explicit
    # ?limit only narrows the result further.
    limit: Optional[int] = Query(None, ge=1),
    action: Optional[str] = Query(None, max_length=64),
    actor: Optional[str] = Query(None, max_length=255),
    target: Optional[str] = Query(None, max_length=512),
    system: bool = Query(False),
    since: Optional[str] = Query(None, max_length=10),
    until: Optional[str] = Query(None, max_length=10),
    _: User = Depends(require_admin),
    # The request session the auth dependency read the user through (cached
    # per request), handed over to be closed before the body streams.
    db: Session = Depends(get_db),
):
    # Follow the retention row cap, always bounded by an absolute ceiling so a
    # disabled cap (WSI_AUDIT_LOG_MAX_ROWS=0 ⇒ unbounded table) can't stream the
    # whole table into memory. Shared with the error-log export via
    # resolve_export_limit so the two never drift.
    effective_limit = resolve_export_limit(limit, settings.audit_log_max_rows)

    # Validates the filters (a bad date is a 400) before a slot is taken.
    filters = _build_filters(action, actor, since, until, target, system)

    def _to_row(r) -> list:
        return [
            occurred_at_iso(r.occurred_at),
            r.id,
            r.actor_id if r.actor_id is not None else "",
            r.actor_username,
            r.actor_role or "",
            r.action,
            r.target or "",
            # Raw JSON string in a single cell; csv_safe() defangs any
            # leading formula character before the spreadsheet sees it.
            r.details or "",
        ]

    # File name pattern mirrors the analytics export:
    # audit-history-<start>_<end>.csv, or audit-history-all.csv when no
    # window was supplied.
    if since or until:
        span = f"{since or 'start'}_{until or 'end'}"
    else:
        span = "all"
    fname = f"audit-history-{span}.csv"
    claim_export_slot()
    # Short keyset batches (see iter_newest_first), streamed: neither the rows
    # nor the CSV are ever all held, and no read transaction spans the download.
    return slot_csv_response(
        fname, _CSV_HEADER, iter_newest_first(AuditLog, filters, effective_limit, _to_row),
        auth_db=db,
    )
