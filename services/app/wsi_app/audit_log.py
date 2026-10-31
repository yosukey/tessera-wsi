"""Best-effort persistence for admin audit events.

``admin_helpers.audit()`` emits a JSON line to the ``wsi_app.audit``
logger (journald) and then calls :func:`persist_audit_event` so the same
event lands in the ``audit_logs`` table for the admin history UI. The
journald line is the primary, tamper-evident record; the DB row is the
queryable convenience copy. Persistence must therefore never raise into
an endpoint and never block on more than SQLite's busy_timeout.

This module imports only ``db``/``models``/``config`` to stay free of
import cycles with ``admin_helpers`` (which every router imports).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import retention
from .config import settings
from .db import SessionLocal
from .models import AuditLog

_logger = logging.getLogger("wsi_app.audit_log")

# An over-long details payload (e.g. a bulk delete embedding thousands of
# slide ids) is truncated rather than rejected: the action/target columns
# still carry the essentials and the journald line keeps the full payload.
_DETAILS_MAX_BYTES = 8 * 1024

_prune_throttle = retention.PruneThrottle()


def _encode_details(extra: Optional[dict]) -> str:
    if not extra:
        return ""
    try:
        encoded = json.dumps(extra, default=str, ensure_ascii=False)
    except Exception:
        return ""
    if len(encoded.encode("utf-8")) > _DETAILS_MAX_BYTES:
        try:
            encoded = json.dumps(
                {"truncated": True, "preview": encoded[:1024]}, ensure_ascii=False
            )
        except Exception:
            return ""
    return encoded


def persist_audit_event(
    actor_id: Optional[int],
    actor_username: Optional[str],
    actor_role: Optional[str],
    action: str,
    target: Optional[str],
    extra: Optional[dict],
) -> None:
    """INSERT one audit row in a short-lived session. Never raises.

    Every ``audit()`` call site fires after its endpoint's ``db.commit()``,
    so the request session never holds the SQLite write lock here; the
    worst case is one ``busy_timeout`` wait against another worker.
    """
    try:
        db = SessionLocal()
        try:
            db.add(AuditLog(
                actor_id=actor_id,
                actor_username=actor_username or "",
                actor_role=actor_role,
                action=action[:64],
                target=target[:512] if target is not None else None,
                details=_encode_details(extra),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        _logger.warning(
            "audit persist failed; the journald audit line is the only "
            "record — action=%s target=%s", action, target, exc_info=True,
        )
    _maybe_prune()


# Failed-login rows are unauthenticated and attacker-generated: a spray from
# many source IPs (each under the per-IP throttle) would otherwise fill the
# shared row cap and push real admin actions out of the history. They get their
# own, smaller cap on top of the global one. The two-factor sign-in's
# pre-sign-in rows belong here too: written with no actor (the account named
# as their target, which is what the history's actor filter matches them by),
# and repeatable by anyone holding the password.
AUTH_FAILURE_ACTION_PREFIXES = (
    "admin.login.failure",
    "admin.login.mfa_failure",
    "admin.login.mfa_required",
    "app.login.failure",
)
AUTH_FAILURE_MAX_ROWS = 10_000


def prune_audit_logs(
    db: Session,
    max_rows: Optional[int] = None,
    max_age_days: Optional[int] = None,
    max_auth_failure_rows: Optional[int] = None,
) -> int:
    """Apply the row/age caps to audit_logs. Returns rows deleted.

    Caller commits. Parameters default to the configured caps; tests pass
    explicit values. A cap of 0 disables that dimension.
    ``max_auth_failure_rows`` additionally keeps only the newest N failed-login
    rows (see :data:`AUTH_FAILURE_ACTION_PREFIXES`), independent of ``max_rows``.
    """
    if max_auth_failure_rows is None:
        max_auth_failure_rows = AUTH_FAILURE_MAX_ROWS
    if max_rows is None:
        max_rows = settings.audit_log_max_rows
    if max_age_days is None:
        max_age_days = settings.audit_log_max_age_days
    deleted = 0
    if max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        # Never the newest row, so ids keep growing (see
        # error_log.prune_error_logs): the history's snapshots rely on it.
        result = db.execute(delete(AuditLog).where(
            AuditLog.occurred_at < cutoff,
            AuditLog.id < select(func.max(AuditLog.id)).scalar_subquery(),
        ))
        deleted += result.rowcount or 0
    # Applied BEFORE the global cap: a burst of failed-login rows that alone
    # exceeds max_rows would otherwise make the global cap delete the oldest
    # rows first, which are the real admin actions this cap exists to protect.
    if max_auth_failure_rows > 0:
        is_auth_failure = or_(
            *(AuditLog.action.like(f"{prefix}%") for prefix in AUTH_FAILURE_ACTION_PREFIXES)
        )
        threshold = db.execute(
            select(AuditLog.id)
            .where(is_auth_failure)
            .order_by(AuditLog.id.desc())
            .offset(max_auth_failure_rows)
            .limit(1)
        ).scalar_one_or_none()
        if threshold is not None:
            result = db.execute(
                delete(AuditLog).where(is_auth_failure, AuditLog.id <= threshold)
            )
            deleted += result.rowcount or 0
    if max_rows > 0:
        # Newest max_rows survive; id order matches insertion order.
        threshold = db.execute(
            select(AuditLog.id).order_by(AuditLog.id.desc()).offset(max_rows).limit(1)
        ).scalar_one_or_none()
        if threshold is not None:
            result = db.execute(delete(AuditLog).where(AuditLog.id <= threshold))
            deleted += result.rowcount or 0
    return deleted


def prune_at_boot() -> None:
    """Boot-time prune; best-effort (called from the lifespan janitor block)."""
    retention.prune_at_boot(prune_audit_logs, _logger, "audit_logs")


def _maybe_prune() -> None:
    """Opportunistic prune, throttled per worker (see ``retention``)."""
    retention.maybe_prune(_prune_throttle, prune_audit_logs, _logger, "audit_logs")
