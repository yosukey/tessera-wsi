"""Plumbing shared by the admin report routers.

The analytics dashboard, the audit history and the error-log listing all
read calendar dates in one zone — the *analytics timezone*
(``WSI_ANALYTICS_TIMEZONE``, with the host's zone as the fallback) — and the
history and log listings filter and serialise their rows the same way. That
common code lives here so the three routers cannot drift apart.
"""
from __future__ import annotations

import functools
import logging
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from typing import Callable, Iterator, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .admin_helpers import api_error
from .config import settings
from .db import SessionLocal, utc_ts_bound

_logger = logging.getLogger("wsi_app.analytics")

# Invalid WSI_ANALYTICS_TIMEZONE values already reported, so a typo is logged
# once per process rather than on every request.
_tz_warned: set[str] = set()


def _zone_name_or_none(name: str) -> Optional[str]:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


@functools.lru_cache(maxsize=1)
def _host_tz_name() -> Optional[str]:
    """IANA name of the host's zone from ``TZ`` / ``/etc/localtime`` /
    ``/etc/timezone``, or None when none of them names one.

    Cached for the life of the process: the host zone cannot change under a
    running worker (a changed ``/etc/localtime`` reaches it only on restart,
    like every other process on the box), and without the cache every
    dashboard request re-read those files several times.
    """
    tz_env = _zone_name_or_none(os.environ.get("TZ", "").strip().lstrip(":"))
    if tz_env:
        return tz_env
    try:
        link = os.readlink("/etc/localtime")
        for prefix in ("/usr/share/zoneinfo/", "/usr/lib/zoneinfo/", "/usr/share/lib/zoneinfo/"):
            if prefix in link:
                cand = _zone_name_or_none(link.split(prefix, 1)[1])
                if cand:
                    return cand
    except OSError:
        pass
    try:
        with open("/etc/timezone") as f:
            cand = _zone_name_or_none(f.read().strip())
            if cand:
                return cand
    except OSError:
        pass
    return None


def analytics_tz_name() -> Optional[str]:
    """The IANA name driving analytics bucketing, if one is known.

    Operators pin the zone with ``WSI_ANALYTICS_TIMEZONE`` (e.g.
    ``Europe/Berlin``); the host's OS zone is not necessarily what the operator
    wants for "yesterday's hits". When that setting is unset — or names no
    zone — the host's IANA name is used. None when neither is recoverable, in
    which case callers fall back to the host's current UTC offset.
    """
    raw = settings.analytics_timezone.strip()
    configured = _zone_name_or_none(raw)
    if configured:
        return configured
    if raw and raw not in _tz_warned:
        # A typo'd name (e.g. "Erope/Berlin") would otherwise fall back to the
        # host zone silently.
        _logger.warning(
            "WSI_ANALYTICS_TIMEZONE=%r is not a valid IANA timezone name; "
            "falling back to host TZ for analytics bucketing",
            raw,
        )
        _tz_warned.add(raw)
    return _host_tz_name()


def analytics_tz() -> tzinfo:
    """The tzinfo that anchors daily boundaries.

    Used for window math (``?days=N``, ``start_date=YYYY-MM-DD``), for the
    SQL day bucketing and for the history / log date filters — they all have
    to agree, or a chart or a filter drops a day at the boundary.
    """
    name = analytics_tz_name()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def server_tz_info() -> dict:
    """The analytics zone as the admin UI shows it.

    The JSON field carrying this is called ``server_tz`` for API stability,
    but its semantics are the *analytics* zone. The frontend renders the
    report timestamps and the daily-chart caption in it so they line up with
    the bucket and filter boundaries.
    """
    tz = analytics_tz()
    name = analytics_tz_name()
    abbr: str | None = None
    utc_offset: str | None = None
    try:
        now = datetime.now(tz)
        abbr = now.tzname() or None
        raw = now.utcoffset()
        if raw is not None:
            total = int(raw.total_seconds())
            sign = "+" if total >= 0 else "-"
            h, rem = divmod(abs(total), 3600)
            utc_offset = f"{sign}{h:02d}:{rem // 60:02d}"
    except Exception:
        pass
    return {"abbr": abbr, "tz_name": name, "utc_offset": utc_offset}


def format_in_analytics_tz(dt: Optional[datetime], tz: tzinfo) -> str:
    """ISO 8601 (second precision) of a stored naive-UTC timestamp in ``tz``;
    "" for None."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Audit-history / error-log listings
# ---------------------------------------------------------------------------
def parse_day(value: str, error_code: str) -> date:
    """A ``YYYY-MM-DD`` filter value; 400 with ``error_code`` otherwise."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise api_error(400, error_code, value=value)


def day_range_filters(column, since: Optional[str], until: Optional[str], error_code: str) -> list:
    """Clauses keeping ``column`` within the inclusive ``since`` .. ``until``
    calendar days of the analytics zone.

    The columns store naive UTC, so each local-midnight boundary is converted
    to UTC before comparison. Reading the days in the analytics zone (not raw
    UTC) gives an operator filtering "since 2026-01-01" their local day,
    matching the dashboard's buckets — and puts an audit event and the error
    it caused on the same "day" in both listings.
    """
    tz = analytics_tz()
    clauses = []
    if since:
        start = datetime.combine(parse_day(since, error_code), dt_time.min, tzinfo=tz)
        clauses.append(column >= utc_ts_bound(start))
    if until:
        # Inclusive day: compare against the start of the following local day.
        end = datetime.combine(
            parse_day(until, error_code) + timedelta(days=1), dt_time.min, tzinfo=tz
        )
        clauses.append(column < utc_ts_bound(end))
    return clauses


def iter_newest_first(model, filters: list, limit: int, to_row: Callable, *,
                      batch_size: int = 1000) -> Iterator[list]:
    """Rows of ``model`` matching ``filters``, newest id first, up to ``limit``,
    converted with ``to_row`` — read in id-keyset batches, each in its own
    short session.

    A CSV export streams for as long as the client takes to download it. One
    cursor held across the whole response would keep a pooled connection
    checked out and a read transaction open all that time: SQLite cannot
    checkpoint the WAL past an open reader, so the -wal file grows for the
    duration, and the connection is held outside the thread-token accounting
    that keeps the pool from running dry. Batching by ``id < last`` releases
    both between batches and still returns one consistent ordering: rows
    written during the export have higher ids and are never picked up.
    """
    remaining = limit
    before: Optional[int] = None
    while remaining > 0:
        n = min(batch_size, remaining)
        stmt = select(model).where(*filters)
        if before is not None:
            stmt = stmt.where(model.id < before)
        stmt = stmt.order_by(model.id.desc()).limit(n)
        db = SessionLocal()
        try:
            rows = db.execute(stmt).scalars().all()
            out = [to_row(r) for r in rows]
            last_id = rows[-1].id if rows else None
        finally:
            db.close()
        yield from out
        if len(out) < n:
            return
        remaining -= len(out)
        before = last_id


def escape_like(term: str) -> str:
    """Escape LIKE wildcards in ``term``; use with ``escape="\\\\"``."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def occurred_at_iso(dt: datetime) -> str:
    """A stored ``occurred_at`` with its UTC offset stamped on.

    SQLite stores ``func.now()`` as a naive UTC string; the explicit offset
    lets the frontend render it in whichever zone it wants.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
