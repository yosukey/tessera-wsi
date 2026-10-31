"""Self-hosted access-analytics endpoints.

* `POST /api/analytics/pageview` is the public beacon — every SPA route
  change for `/`, `/slides/:id`, `/slides/:id/tours/:tourId` triggers one.
  The endpoint never returns 4xx for "uninteresting" inputs (unknown
  slide id, malformed body); it silently drops the row so a misbehaving
  client cannot use the response code to enumerate IDs or DOS the
  request log.
* `GET /api/admin/analytics/dashboard` returns summary + all five
  rankings in a single response. The dashboard UI calls this instead of
  hitting each per-section endpoint individually so a "refresh" click
  consumes 1 nginx rate-limit token, not 6.
* The per-section endpoints (`summary`, `top-slides`, `top-tours`,
  `referrers`, `ua-breakdown`, `top-users`, `recent`) serve
  programmatic callers and tests; admin auth is required on all of them.
  Aggregate endpoints return *counts*, never raw IPs/emails; only the
  recent-activity endpoint surfaces individual rows.
* `GET /api/admin/analytics/export.csv` renders the same datasets to
  a CSV download for spreadsheet analysis. The endpoint accepts the
  same window/filter knobs as the JSON endpoints.
* Admin-only maintenance: `DELETE /api/admin/analytics/data` (a date range,
  or everything with `all=1`), `DELETE /api/admin/analytics/user` (one
  identity; `POST .../user/preview` lists what it would match first) and
  `POST /api/admin/analytics/pseudonymize` (plaintext rows -> hashed form).
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from sqlalchemy import Integer, String, bindparam, case, func, or_, select, union, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .. import analytics as _an
from .. import analytics_hash as _an_hash
from .. import app_auth as _app_auth
from .. import csv_export
from .. import network as _net
from ..admin_helpers import api_error, audit, coerce_aware
from ..admin_reports import analytics_tz, format_in_analytics_tz, server_tz_info
from ..auth import (
    STAFF_ROLES,
    bearer_token,
    require_admin,
    require_analytics_access,
    resolve_staff_user,
)
from ..config import settings
from ..db import get_db, utc_ts_bound
from ..models import (
    PageView,
    Slide,
    SlideDwellEvent,
    Tour,
    TourSession,
    User,
)

logger = logging.getLogger("wsi_app.analytics")

router = APIRouter()


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------
_VALID_KIND = ("top", "viewer", "tour")
_BOT_FILTER = PageView.ua_class == "bot"
# Hard caps on the period a single query may span. The JSON endpoints
# already bounded `days` at 365; the date-range path uses the same
# upper bound so an operator cannot accidentally run an unbounded scan
# (e.g. `start=1970-01-01`) that locks the table.
_MAX_WINDOW_DAYS = 365

def _parse_iso_date(s: str) -> datetime:
    """Parse a YYYY-MM-DD string as midnight in the analytics TZ.

    Raises HTTPException(400) on malformed input so the caller sees
    "the date is wrong" rather than silently falling back to `days`,
    which would hide typos in the picker.
    """
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=analytics_tz())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {s!r} (expected YYYY-MM-DD)") from exc


def _resolve_window(
    days: int,
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[datetime, datetime, int]:
    """Return (since, until, span_days) for the requested period.

    `start_date` / `end_date` are inclusive YYYY-MM-DD interpreted in
    the analytics TZ (`WSI_ANALYTICS_TIMEZONE` or host fallback);
    `until` is exclusive (end_date + 1 day) so a same-day request
    returns the full 24h. When both are omitted the window falls back
    to the `days` preset (last N days), which the bundled dashboard
    uses for its quick-select buttons.
    """
    now = datetime.now(analytics_tz())
    if start_date or end_date:
        s_dt = _parse_iso_date(start_date) if start_date else None
        e_dt = _parse_iso_date(end_date) if end_date else None
        if s_dt is not None and e_dt is not None and s_dt > e_dt:
            # Compared as dates, before end_date becomes the exclusive bound
            # (the next midnight): a start exactly one day after the end
            # equals that bound, would slip past the `s_dt > until` check
            # below and come back as an empty window reported as "1 day".
            raise HTTPException(
                status_code=400,
                detail="start_date must be on or before end_date",
            )
        if s_dt is None:
            # Open-start window: clamp to the upper bound so the scan
            # never reaches beyond the documented limit.
            s_dt = (e_dt or now) - timedelta(days=_MAX_WINDOW_DAYS - 1)
        until = (e_dt + timedelta(days=1)) if e_dt is not None else now
        if s_dt > until:
            # Refuse to silently re-interpret an inverted range. `_parse_iso_date`
            # already 400s on malformed input; doing anything other than 400 here
            # would mean malformed→400 / inverted→200-with-rewritten-window,
            # which is the worst kind of API surprise for scripted callers.
            if e_dt is None:
                # Only `start_date` was supplied and it's after "now". The
                # generic "start_date must be on or before end_date" message
                # is misleading here because the caller didn't pass end_date.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"start_date {s_dt.date().isoformat()} is in the future "
                        f"(no end_date supplied; defaulted to today)"
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail="start_date must be on or before end_date",
            )
        # Calendar days the window touches, both ends inclusive — the number
        # of daily buckets `_day_bounds` lists for it. The elapsed time's
        # `.days` would round a start-only window (until = now, part-way
        # through today) down: start 365 days ago would pass the cap while
        # covering 366 days, and "days" under-report every such window by one.
        last_day = (until - timedelta(microseconds=1)).astimezone(analytics_tz()).date()
        span = (last_day - s_dt.date()).days + 1
        if span > _MAX_WINDOW_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"date range too large (max {_MAX_WINDOW_DAYS} days)",
            )
        return s_dt, until, span
    # Preset "last N days": today and the N-1 calendar days before it, from
    # local midnight (analytics TZ). `now - N days` would start part-way through a
    # day, so the daily chart would show N+1 buckets, the first one partial, and
    # the window would not match a custom range of the same N days.
    d = max(1, min(int(days or 1), _MAX_WINDOW_DAYS))
    first_day = now.date() - timedelta(days=d - 1)
    # combine() with the zone resolves that midnight under the rules in force
    # on that day (a DST change inside the window does not shift it).
    return datetime.combine(first_day, dt_time.min, tzinfo=now.tzinfo), now, d


def _apply_default_filters(stmt, include_bots: bool, include_staff: bool = False):
    """Drop bot UA rows and staff-attributed rows from aggregate queries by default.

    Bot traffic is excluded by default so a misconfigured robots.txt
    doesn't drown out the actual human counts; the operator can flip
    ``include_bots=1`` to see the full picture when investigating.

    Staff (admin / manager / instructor) traffic is also excluded by
    default: when a logged-in staff member opens a viewer page, the
    beacon forwards their JWT and ``record_pageview`` stamps
    ``PageView.actor_role`` with the staff role. The default aggregation
    is meant to reflect *educational / learner* access — QA traversals
    by staff would otherwise pollute the per-slide / per-user rankings
    every time a manager re-opens a slide they just uploaded. Operators
    can flip ``include_staff=true`` from the dashboard to lift the
    filter when they specifically want to see staff activity.
    """
    if not include_bots:
        stmt = stmt.where(~_BOT_FILTER)
    if not include_staff:
        stmt = stmt.where(PageView.actor_role.is_(None))
    return stmt


# Report queries give up after this long and answer 503 (narrow the window):
# nginx stops waiting at 60 s, and a query that outlives the request still
# holds its read transaction (stalling WAL checkpoints) while a retry stacks
# another one behind it. A 365-day window at the default 5M-row cap takes a
# fraction of this.
_QUERY_TIME_BUDGET_S = 50.0
# SQLite VM instructions between deadline checks (a check is one clock read).
_QUERY_BUDGET_CHECK_STEPS = 100_000


@contextmanager
def _query_budget(db: Session):
    """Interrupt the statements run inside once ``_QUERY_TIME_BUDGET_S`` has
    passed, turning the interruption into a 503 the dashboard can explain."""
    raw = db.connection().connection.driver_connection
    deadline = time.monotonic() + _QUERY_TIME_BUDGET_S
    raw.set_progress_handler(lambda: time.monotonic() > deadline, _QUERY_BUDGET_CHECK_STEPS)
    timed_out = None
    try:
        yield
    except OperationalError as exc:
        if "interrupted" not in str(exc.orig):
            raise
        timed_out = exc
    finally:
        # Off while this request still holds the connection. Once it is back
        # in the pool, `raw` must not be touched again: another request may
        # be mid-statement on it (with its own handler), and the call would
        # wait on SQLite's connection mutex while holding the GIL. Left on,
        # the expired deadline would interrupt that request's statements.
        raw.set_progress_handler(None, 0)
    if timed_out is not None:
        # Hands the connection back to the pool.
        db.rollback()
        logger.warning(
            "analytics report query stopped after %.0fs; asked for a shorter window",
            _QUERY_TIME_BUDGET_S,
        )
        raise api_error(503, "adminDash.apiErr.analyticsQueryTimeout") from timed_out


# ---------------------------------------------------------------------------
# Aggregate computations (private helpers used by both the per-section
# endpoints and the bundled /dashboard endpoint).
#
# Each helper accepts a (since, until) window so callers can request the
# preset "last N days" range *or* an explicit start/end date span. The
# window bounds are half-open [since, until) — a same-day picker (e.g.
# 2025-01-01 to 2025-01-01) covers the full 24h of that day.
# ---------------------------------------------------------------------------
def _window_filter(stmt, since: datetime, until: datetime):
    # Normalize to UTC before binding. SQLAlchemy's `DateTime(timezone=True)`
    # on SQLite stores values as naive strings derived from the bind's
    # *local-time* fields (not its absolute instant), so feeding a non-UTC-aware
    # datetime would compare against the naive UTC strings written by
    # `func.now()` and miss the row by the local-UTC offset.
    # utc_ts_bound also drops a zero fraction so a row stamped at exactly the
    # boundary second falls in the window it starts, not the one before.
    return (
        stmt.where(PageView.occurred_at >= utc_ts_bound(since))
        .where(PageView.occurred_at < utc_ts_bound(until))
    )


# No time zone changes its UTC offset twice within this span, so sampling the
# offset at this stride finds every transition in a window (see below).
_OFFSET_SCAN_STRIDE_S = 6 * 3600


def _utc_offset_periods(since: datetime, until: datetime) -> list:
    """``[(utc_start_epoch, offset_seconds), ...]`` covering [since, until).

    The analytics TZ's UTC offset as it was over the window, split at each
    transition (DST start/end, a zone's rule change). Transitions are located
    to the exact second by bisection, which is what lets an event be dated
    with the offset in force *when it happened*.
    """
    tz = analytics_tz()

    def offset_at(epoch: int) -> int:
        return int(datetime.fromtimestamp(epoch, tz).utcoffset().total_seconds())

    start = int(since.timestamp())
    end = int(until.timestamp())
    periods = [(start, offset_at(start))]
    t = start
    while t < end:
        nxt = min(t + _OFFSET_SCAN_STRIDE_S, end)
        if offset_at(nxt) != offset_at(t):
            lo, hi = t, nxt  # offset_at(lo) != offset_at(hi)
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if offset_at(mid) == offset_at(lo):
                    lo = mid
                else:
                    hi = mid
            periods.append((hi, offset_at(hi)))
        t = nxt
    return periods


def _local_day_expr(since: datetime, until: datetime):
    """SQL expression for the analytics-TZ calendar day of `occurred_at`
    (used for the top users' distinct-day count).

    SQLite-only build: the column stores naive UTC strings
    (server_default=func.now()), so each row is shifted by a UTC offset
    before the date is extracted with `strftime` — the offset in force at
    that row's instant, not today's. A single "now" offset would date a
    winter evening viewed from summer (or the reverse) into the
    neighbouring day on every day of the other season, disagreeing with the
    local-midnight bounds the daily chart (``_day_bounds``) uses. The window
    holds at most a couple of transitions per year, so the CASE stays short.
    """
    periods = _utc_offset_periods(since, until)

    def modifier(offset: int) -> str:
        # SQLite accepts `'+N seconds'` / `'-N seconds'` as a datetime modifier.
        return f"{offset:+d} seconds"

    if len(periods) == 1:
        mod = modifier(periods[0][1])
    else:
        # A row before the next period's start belongs to the current one.
        mod = case(
            *[
                (
                    PageView.occurred_at
                    < utc_ts_bound(datetime.fromtimestamp(nxt_start, timezone.utc)),
                    modifier(offset),
                )
                for (_start, offset), (nxt_start, _next) in zip(periods, periods[1:])
            ],
            else_=modifier(periods[-1][1]),
        )
    return func.strftime("%Y-%m-%d", PageView.occurred_at, mod)


# User-Agent buckets classify_user_agent writes besides "other"; anything
# else (including an empty value) is counted as "other".
_UA_CLASSES = ("desktop", "mobile", "tablet", "bot")


def _day_bounds(since: datetime, until: datetime) -> list:
    """``(YYYY-MM-DD, start, end)`` for every analytics-TZ calendar day that
    [since, until) touches, each clipped to the window.

    Midnights are resolved with the zone rules in force on that day, so a day
    across a DST change is 23 or 25 hours long and every event falls on the
    local day it happened on. Every day in the window is listed, even
    zero-traffic ones, so the chart shows calendar gaps rather than
    collapsing them; a same-day window yields one day.
    """
    tz = analytics_tz()
    day: date = since.astimezone(tz).date()
    last: date = (until - timedelta(microseconds=1)).astimezone(tz).date()
    out = []
    while day <= last:
        start = datetime.combine(day, dt_time.min, tzinfo=tz)
        end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=tz)
        out.append((day.isoformat(), max(start, since), min(end, until)))
        day += timedelta(days=1)
    return out


def _day_counts(
    db: Session, since: datetime, until: datetime, inc_bots: bool, inc_staff: bool,
) -> tuple[list, dict]:
    """Per-day view counts by kind, plus window-wide counts by UA class.

    One query per analytics-TZ day, each an index range scan over that day
    that only sums (no GROUP BY): grouping a year of rows by a computed day
    would make SQLite sort every row of the window, which, with separate
    kind-total and UA passes, would dominate the cost of a long-window
    dashboard. Returns ``([{day, top, viewer, tour}, ...],
    {ua_class: views})``.
    """
    lo = bindparam("lo", type_=String)
    hi = bindparam("hi", type_=String)
    stmt = _apply_default_filters(
        select(
            func.count(),
            *[func.sum(case((PageView.kind == k, 1), else_=0)) for k in _VALID_KIND],
            *[func.sum(case((PageView.ua_class == c, 1), else_=0)) for c in _UA_CLASSES],
        ).where(PageView.occurred_at >= lo, PageView.occurred_at < hi),
        inc_bots,
        inc_staff,
    )
    daily: list = []
    ua = dict.fromkeys(_UA_CLASSES + ("other",), 0)
    for day, start, end in _day_bounds(since, until):
        # utc_ts_bound's text form: see _window_filter (boundary-second rows).
        row = db.execute(
            stmt, {"lo": utc_ts_bound(start).value, "hi": utc_ts_bound(end).value}
        ).one()
        total, *rest = (int(v or 0) for v in row)
        by_kind = dict(zip(_VALID_KIND, rest[:len(_VALID_KIND)]))
        daily.append({"day": day, **by_kind})
        by_ua = dict(zip(_UA_CLASSES, rest[len(_VALID_KIND):]))
        for cls, n in by_ua.items():
            ua[cls] += n
        ua["other"] += total - sum(by_ua.values())
    return daily, ua


def _ua_rows(ua: dict) -> list:
    return [
        {"ua_class": cls, "views": n}
        for cls, n in sorted(ua.items(), key=lambda kv: (-kv[1], kv[0]))
        if n
    ]


def _compute_daily(
    db: Session, since: datetime, until: datetime, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    """Return `[{day, top, viewer, tour}, ...]` sorted by day (analytics TZ),
    every day of the window included."""
    return _day_counts(db, since, until, inc_bots, inc_staff)[0]


def _compute_summary(
    db: Session, since: datetime, until: datetime, span_days: int, inc_bots: bool,
    inc_staff: bool = False,
    *,
    day_counts: Optional[tuple] = None,
    unique_users: Optional[int] = None,
) -> dict:
    """Window totals, distinct visitors and the daily series.

    ``day_counts`` (a ``_day_counts`` result) and ``unique_users`` let the
    dashboard pass values its other sections already computed instead of
    scanning the window again for them.
    """
    daily, _ua = day_counts or _day_counts(db, since, until, inc_bots, inc_staff)
    totals = {k: sum(d[k] for d in daily) for k in _VALID_KIND}

    def distinct(col, *where) -> int:
        # Count the groups of a GROUP BY rather than COUNT(DISTINCT col):
        # SQLite answers the latter by inserting every row into a temporary
        # b-tree, several times slower over a long window than sorting.
        groups = _apply_default_filters(
            _window_filter(select(col), since, until).where(*where),
            inc_bots,
            inc_staff,
        ).group_by(col).subquery()
        return int(db.execute(select(func.count()).select_from(groups)).scalar_one() or 0)

    # "" is "no address recorded" (hashed mode with the salt gone, a beacon
    # with no peer), not one more visitor. Across a plain -> hashed switch one
    # address is counted twice, raw and hashed, until the old rows are
    # converted with POST /api/admin/analytics/pseudonymize.
    unique_ips = distinct(PageView.client_ip, PageView.client_ip != "")
    if unique_users is None:
        unique_users = distinct(PageView.user_email, PageView.user_email != "")
    return {
        "days": span_days,
        "start_date": since.date().isoformat(),
        # `until` is exclusive, so the inclusive end date is the day that
        # contains the last covered instant. Subtract a single tick rather
        # than a whole day: for an explicit date range (until = end+1day)
        # this still yields the requested end date, while for the preset
        # "last N days" window (until = now) it correctly reports today —
        # matching the final bucket enumerated by _compute_daily — instead
        # of mislabelling the range as ending yesterday.
        "end_date": (until - timedelta(microseconds=1)).date().isoformat(),
        "totals": totals,
        "unique_ips": unique_ips,
        "unique_users": int(unique_users),
        "daily": daily,
    }


def _slide_labels(db: Session, slide_ids, *cols) -> dict:
    """``{slide_id: (col, ...)}`` for the ranked ids, NULL read as "".

    The one title lookup every ranked table makes. It selects just the label
    columns, never full Slide rows: the CSV export ranks up to 10 000 ids, and
    a ``select(Slide)`` there would build that many ORM objects — and, but
    for the image blobs being deferred on the model, with each slide's macro
    and label JPEGs attached (300 slides -> 82 MB for one top-slides pass)."""
    if not slide_ids:
        return {}
    return {
        row[0]: tuple(v or "" for v in row[1:])
        for row in db.execute(select(Slide.id, *cols).where(Slide.id.in_(slide_ids))).all()
    }


def _distinct_ips():
    """COUNT(DISTINCT client_ip) leaving out "" (see the summary's unique_ips)."""
    return func.count(func.distinct(func.nullif(PageView.client_ip, "")))


# Every ranking orders by its count and then by its group key, so rows tied on
# the count come back in one fixed order: without the tie-breaker SQLite
# returned ties in whatever order its temporary b-tree produced, and the
# dashboard (limit 20) and a CSV export (limit 1000) could disagree on which
# tied rows made the cut and in what order.
def _compute_top_slides(
    db: Session, since: datetime, until: datetime, limit: int, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    stmt = _apply_default_filters(
        _window_filter(
            select(
                PageView.slide_id,
                func.count().label("views"),
                _distinct_ips().label("uniq_ips"),
            ),
            since,
            until,
        )
        .where(PageView.kind.in_(("viewer", "tour")))
        .where(PageView.slide_id.is_not(None)),
        inc_bots,
        inc_staff,
    ).group_by(PageView.slide_id).order_by(func.count().desc(), PageView.slide_id).limit(limit)

    rows = db.execute(stmt).all()
    if not rows:
        return []
    slide_ids = [r[0] for r in rows]
    title_map = _slide_labels(db, slide_ids, Slide.title, Slide.catalog_id, Slide.public_id)
    return [
        {
            "slide_id": sid,
            # public_id lets the dashboard link to the public viewer, which is
            # keyed by the token, not the numeric id.
            "public_id": title_map.get(sid, ("", "", ""))[2],
            "title": title_map.get(sid, ("", "", ""))[0],
            "catalog_id": title_map.get(sid, ("", "", ""))[1],
            "views": int(views),
            "unique_ips": int(uniq),
        }
        for sid, views, uniq in rows
    ]


def _compute_top_tours(
    db: Session, since: datetime, until: datetime, limit: int, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    stmt = _apply_default_filters(
        _window_filter(
            select(
                PageView.tour_id,
                func.count().label("views"),
                _distinct_ips().label("uniq_ips"),
                # The slide the views were recorded under (the beacon checks
                # the tour belongs to it), for a tour deleted since.
                func.max(PageView.slide_id).label("slide_id"),
            ),
            since,
            until,
        )
        .where(PageView.kind == "tour")
        .where(PageView.tour_id.is_not(None)),
        inc_bots,
        inc_staff,
    ).group_by(PageView.tour_id).order_by(func.count().desc(), PageView.tour_id).limit(limit)

    rows = db.execute(stmt).all()
    if not rows:
        return []
    tour_ids = [r[0] for r in rows]
    tours = db.execute(select(Tour).where(Tour.id.in_(tour_ids))).scalars().all()
    tour_map = {t.id: t for t in tours}
    slide_ids = list({t.slide_id for t in tours} | {r[3] for r in rows if r[3] is not None})
    slide_info = _slide_labels(db, slide_ids, Slide.title, Slide.public_id)
    out = []
    for tid, views, uniq, pv_slide_id in rows:
        # A tour deleted since keeps its rank with blank labels, as a deleted
        # slide does in top-slides. Skipping it after the LIMIT would return fewer
        # rows than asked for (17 of 20), and fewer than the CSV export of
        # the same window ranks.
        t = tour_map.get(tid)
        slide_id = t.slide_id if t is not None else pv_slide_id
        s_title, s_public_id = slide_info.get(slide_id, ("", ""))
        out.append({
            "tour_id": tid,
            # Both tokens so the dashboard can link to /slides/{slide}/tours/{tour}.
            "public_id": (t.public_id or "") if t is not None else "",
            "slide_public_id": s_public_id,
            "tour_title": (t.title or "") if t is not None else "",
            "slide_id": slide_id,
            "slide_title": s_title,
            "views": int(views),
            "unique_ips": int(uniq),
        })
    return out


def _compute_referrers(
    db: Session, since: datetime, until: datetime, limit: int, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    stmt = _apply_default_filters(
        _window_filter(
            select(PageView.referrer_host, func.count().label("views")),
            since,
            until,
        ),
        inc_bots,
        inc_staff,
    ).group_by(PageView.referrer_host).order_by(
        func.count().desc(), PageView.referrer_host
    ).limit(limit)
    return [
        {"referrer_host": host or "", "views": int(views)}
        for host, views in db.execute(stmt).all()
    ]


def _compute_ua_breakdown(
    db: Session, since: datetime, until: datetime, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    return _ua_rows(_day_counts(db, since, until, inc_bots, inc_staff)[1])


def _compute_top_users(
    db: Session, since: datetime, until: datetime, limit: int, inc_bots: bool,
    inc_staff: bool = False,
) -> list:
    return _top_users_and_count(db, since, until, limit, inc_bots, inc_staff)[0]


def _top_users_and_count(
    db: Session, since: datetime, until: datetime, limit: int, inc_bots: bool,
    inc_staff: bool = False,
) -> tuple[list, int]:
    """The top users, and how many distinct users the window has in all (the
    summary's ``unique_users``, read off the same grouping)."""
    # Group by user_email so basic-auth/ldap subjects (no users.id) and
    # admins (with users.id) bucket consistently. Anonymous traffic
    # (empty email) is excluded — otherwise a public deployment with
    # many unauthenticated viewers always shows "(anonymous)" at rank 1
    # and pushes the actual named users out of the limit-bounded window.
    # The `unique_users` field on the summary uses the same filter, so
    # this keeps the two views consistent.
    day_expr = _local_day_expr(since, until)
    stmt = _apply_default_filters(
        _window_filter(
            select(
                PageView.user_email,
                func.max(PageView.user_id).label("user_id"),
                func.count().label("views"),
                func.count(func.distinct(day_expr)).label("uniq_days"),
                _distinct_ips().label("uniq_ips"),
                # Evaluated over the grouped rows before LIMIT: the number of
                # users in the window.
                func.count().over().label("users_total"),
            ),
            since,
            until,
        ),
        inc_bots,
        inc_staff,
    ).where(PageView.user_email != "").group_by(PageView.user_email).order_by(
        func.count().desc(), PageView.user_email
    ).limit(limit)
    rows = db.execute(stmt).all()
    return [
        {
            "user_email": email or "",
            "user_id": int(uid) if uid is not None else None,
            "views": int(views),
            "unique_days": int(udays),
            "unique_ips": int(uips),
        }
        for email, uid, views, udays, uips, _total in rows
    ], (int(rows[0][-1]) if rows else 0)


# ---------------------------------------------------------------------------
# Public beacon
# ---------------------------------------------------------------------------
# SQLite binds integers as signed 64-bit; a value outside this range raises
# OverflowError at bind time. The beacons promise never to surface a 5xx for
# malformed input (see the module docstring), so an out-of-range id is coerced
# to None — i.e. "no association" — rather than passed through to db.get().
_MIN_DB_INT = -(2**63)
_MAX_DB_INT = 2**63 - 1


def _coerce_optional_int(v) -> Optional[int]:
    """Return an int within SQLite's 64-bit bind range, or None; never raise."""
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: JSON "1e999" parses to float('inf'), which int()
        # refuses with OverflowError (NaN raises ValueError). Uncaught, it
        # would reach the app-wide handler as a 422 plus an error_logs row — on an
        # unauthenticated endpoint, and only once the slide token resolved.
        return None
    return n if _MIN_DB_INT <= n <= _MAX_DB_INT else None


def _resolve_slide(db, token) -> Optional[tuple[int, bool]]:
    """Map a public_id token to ``(slide PK, is_published)``, or None.

    The beacons are URL-driven and only ever see the public_id token (never the
    numeric PK). Analytics rows keep storing the PK, so the token is resolved
    here on write. An unknown token resolves to None → the caller returns 204,
    preserving the silent-drop / no-probe contract.
    """
    if not isinstance(token, str) or not token:
        return None
    row = db.execute(
        select(Slide.id, Slide.is_published).where(Slide.public_id == token)
    ).first()
    return (row[0], bool(row[1])) if row is not None else None


# Unpublished slides and tours are recorded only for staff browsers (the rows
# carry actor_role, so the default aggregations leave them out — but an
# operator who ticks "include staff" sees their draft previews). Anyone else
# cannot open a draft at all — the public API answers 404 — so such a beacon is
# forged or stale (the content was unpublished after the page loaded) and is
# dropped.
def _slide_published(db, slide_id: int) -> bool:
    return bool(db.execute(
        select(Slide.is_published).where(Slide.id == slide_id)
    ).scalar_one_or_none())


def _resolve_tour_by_public_id(db, token) -> Optional[Tour]:
    """Map a public_id token to the Tour row (for its PK + parent slide_id), or None."""
    if not isinstance(token, str) or not token:
        return None
    return db.execute(
        select(Tour).where(Tour.public_id == token)
    ).scalar_one_or_none()


def _resolve_beacon_staff_user(db, auth_header: str) -> Optional[User]:
    """Resolve a forwarded staff bearer to a live ``User`` row, once per beacon.

    Learner / anonymous beacons carry no admin Bearer, so the result is
    ``None`` and the row is treated as educational traffic. A valid staff JWT
    yields the row. The single resolved row feeds BOTH identity attribution
    (threaded into ``resolve_identity`` as its ``staff_user`` hint) and the
    ``actor_role`` stamp, so a staff direct-API beacon does one ``users``
    fetch instead of two.

    resolve_staff_user enforces signature/expiry/aud AND the token_version
    (tv) check + is_active liveness, so a password change / forced logout
    that bumped token_version stops attributing the staff row immediately —
    parity with every other staff auth path.
    """
    token = bearer_token(auth_header)
    if not token:
        return None
    return resolve_staff_user(token, db)


def _staff_actor_role(staff_user: Optional[User], auth_header: str = "") -> Optional[str]:
    """Classify a resolved beacon user into a staff ``actor_role``, or None.

    A staff-role (admin/manager/instructor) user yields the role so QA
    traversals are excluded from the default aggregations (which filter
    ``actor_role IS NULL``); anything else — including ``None`` for
    learner / anonymous beacons — stays NULL. Shared by the pageview,
    slide-dwell and tour-event beacons so all three stamp identically.

    A forwarded staff bearer that no longer resolves (expired / revoked)
    still marks a staff browser: fall back to its signed role claim so the
    row is not counted as a learner (tagging only; it authorises nothing).
    """
    if staff_user is not None and staff_user.role in STAFF_ROLES:
        return staff_user.role
    return _an.signed_staff_role(auth_header)


@router.post("/api/analytics/pageview", status_code=204)
async def record_pageview(
    request: Request,
    db: Session = Depends(get_db),
):
    # The body is parsed by hand (rather than via a Pydantic Body model) so
    # that a malformed payload still resolves to a 204: this endpoint is the
    # in-app beacon, must not surface 4xx to the SPA, and must not let a
    # caller distinguish "bad body" from "valid body, ignored row" by status
    # code. Any parse/validation failure is silently dropped.
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)
    if not isinstance(payload, dict):
        return Response(status_code=204)
    # The DB work below (validation get()s, identity resolution, INSERT +
    # commit) is synchronous SQLite I/O. SQLite is configured with
    # busy_timeout=5000, so a write contending with the WAL writer can block
    # inside the C extension for seconds; running it inline on the event loop
    # would freeze the whole worker for every other in-flight request. This
    # is the highest-frequency public endpoint, so push the blocking work to
    # the threadpool (where the plain `def` read endpoints already run).
    headers = request.headers
    cookies = request.cookies
    client_host = request.client.host if request.client else "127.0.0.1"
    resp = await run_in_threadpool(
        _persist_pageview, db, payload, headers, cookies, client_host,
        getattr(request.state, "verified_basic_user", None),
    )
    return _with_prune(resp)


def _with_prune(resp: Response) -> Response:
    """Attach the opportunistic, throttled retention prune (mirrors audit /
    error logs) to a beacon response as a background task.

    Starlette runs it after the response has been sent, and — being a sync
    callable — in the threadpool, so neither the event loop nor the beacon
    waits on the DELETEs: awaiting it inline held the response for however
    long the prune took (a backlog of aged-out rows is minutes of work). The
    prune bounds itself in time and runs one at a time per worker (see
    ``analytics.maybe_prune_analytics``); it never raises."""
    resp.background = BackgroundTask(_an.maybe_prune_analytics)
    return resp


def _persist_pageview(
    db, payload, headers, cookies, client_host, verified_basic_user=None,
) -> Response:
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _VALID_KIND:
        return Response(status_code=204)
    # Mode first: with analytics off (the default) nothing is recorded, so a
    # beacon must cost one settings read and none of the token / identity
    # work below. The 204 keeps the silent-drop contract.
    mode = _an.current_analytics_mode(db)
    if mode == _an.ANALYTICS_OFF:
        return Response(status_code=204)
    # Beacons carry the unguessable public_id token (never the numeric PK);
    # resolve to the internal PK for storage. Unknown token → 204 (silent drop).
    slide_id = None
    tour_id = None
    published = True
    if kind != "top":  # the top-page beacon never carries slide/tour context
        slide = _resolve_slide(db, payload.get("slide_public_id"))
        if slide is None:
            return Response(status_code=204)
        slide_id, published = slide
    if kind == "tour":
        tour = _resolve_tour_by_public_id(db, payload.get("tour_public_id"))
        if tour is None:
            return Response(status_code=204)
        # Tour must belong to the asserted slide; otherwise the row would let a
        # caller probe for `(slide, tour)` combinations by watching for distinct
        # accept/drop behaviour.
        if tour.slide_id != slide_id:
            return Response(status_code=204)
        tour_id = tour.id
        published = published and bool(tour.is_published)

    client_ip = _net.get_client_ip(headers, client_host)
    user_agent = (headers.get("user-agent") or "")[:512]
    ua_class = _an.classify_user_agent(user_agent)
    # The genuine external referrer is only available to the browser as
    # `document.referrer` at page-load time; the beacon is a fetch() issued
    # *from* the already-loaded SPA, so its own `Referer` HTTP header always
    # points back at the app's own host and would be dropped as a self-referral.
    # The frontend therefore forwards `document.referrer` in the payload.
    referer = payload.get("referrer")
    if not isinstance(referer, str):
        referer = ""
    host_header = (headers.get("host") or "").split(":", 1)[0]
    referrer_host = _an.extract_referrer_host(referer, host_header)

    session_cookie = cookies.get(_app_auth.SESSION_COOKIE)
    auth_header = headers.get("authorization", "")
    # Resolve the forwarded staff bearer to a live User row *once*: the same
    # row feeds identity attribution (threaded into resolve_identity) and the
    # actor_role stamp below, so a staff direct-API beacon does a single
    # `users` fetch rather than one per lookup.
    staff_user = _resolve_beacon_staff_user(db, auth_header)
    # Stamp the row with the staff role when the beacon forwarded a valid
    # staff JWT (admin/manager/instructor login). The frontend's beacon
    # attaches the admin Bearer when sessionStorage carries one — learner
    # / anonymous traffic does not, so actor_role stays NULL. Default
    # analytics aggregations filter on actor_role IS NULL so QA traversals
    # by staff don't pollute the educational metrics.
    actor_role = _staff_actor_role(staff_user, auth_header)
    if not published and actor_role is None:
        return Response(status_code=204)
    user_id, user_identifier, mode = _an.resolve_identity(
        session_cookie, headers, client_host, db,
        staff_user=staff_user,
        verified_basic_user=verified_basic_user,
        mode=mode,
    )

    # Hashed mode promises a DB-only leak cannot name the viewer, so nothing
    # stored next to the hashed identity may re-identify it: no users.id link
    # (a join on users would give the username back), a keyed hash in place
    # of the raw IP, and no raw User-Agent (ua_class, derived above, is kept).
    if mode == _an.ANALYTICS_HASHED:
        user_id = None
        client_ip = _an.pseudonymise_client_ip(client_ip)
        user_agent = ""

    client_ip = client_ip[:64]
    row = PageView(
        kind=kind,
        slide_id=slide_id,
        tour_id=tour_id,
        client_ip=client_ip,
        user_agent=user_agent,
        ua_class=ua_class,
        referrer_host=referrer_host,
        user_id=user_id,
        user_email=user_identifier,
        actor_role=actor_role,
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        # Analytics inserts must never break the user's navigation. If
        # the write fails (DB locked, disk full, etc.), the beacon is
        # explicitly best-effort — drop the row rather than 5xx-ing the
        # SPA. Log so an operator can detect a persistent outage; without
        # this the analytics table simply stops growing with no signal.
        db.rollback()
        logger.warning(
            "analytics pageview insert failed (kind=%s slide_id=%s tour_id=%s)",
            kind, slide_id, tour_id,
            exc_info=True,
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Public beacons: slide dwell time + tour session progress
# ---------------------------------------------------------------------------
# UUID-shaped session id used by the dwell / tour beacons. 32 hex with
# optional dashes — accept v4 with or without separators since the frontend
# uses crypto.randomUUID(). Always applied with fullmatch(): a `$` anchor
# would also accept a trailing newline, storing a distinct id for the session.
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")


def _valid_session_id(raw) -> Optional[str]:
    return raw if isinstance(raw, str) and _SESSION_ID_RE.fullmatch(raw) else None


# Maximum drift between client-supplied timestamps and server "now".
# A clock-skewed client (or a misbehaving one trying to plant rows in 1900
# or 9999) should not be able to push events outside a reasonable window;
# beyond this drift the beacon silently drops the value.
_CLIENT_TS_MAX_DRIFT = timedelta(days=7)


def _utcnow() -> datetime:
    """The server's clock as the beacons read it (one seam for tests that
    simulate a session spread over time)."""
    return datetime.now(timezone.utc)


# How far a timestamp the client stamps *as it sends the beacon* (a slide
# dwell's ended_at, a tour's started_at on "start", completed_at, sent_at) may
# be from the server's receipt and still be taken as given. A client clock
# within this of the server's is trusted, so its spans are measured on one
# clock from end to end (see analyticsTime.ts); beyond it the clock is wrong —
# or the stamp forged — and the stamp is pulled in to the allowance's edge.
_CLIENT_CLOCK_SKEW = timedelta(minutes=5)


def _receipt_offset(stamp: datetime, received: datetime) -> timedelta:
    """How far to move a send-time ``stamp`` (and the session's other stamps
    from the same clock) to bring it within ``_CLIENT_CLOCK_SKEW`` of the
    server's receipt time: zero within the skew, else only the excess beyond
    it — the stamp is clamped to ``received ± skew``.

    This is what bounds a session's dwell by the server's clock. Without it
    every span would come from client stamps that only have to lie within
    ``_CLIENT_TS_MAX_DRIFT`` (a week) of now, so a tour "started" four hours
    ago a second ago, or a slide dwell ending days in the future, would earn
    a ceiling-sized dwell at once. Anchored, a stored session's span can grow
    only as fast as the server's clock runs (plus this skew at each end) —
    however many beacons a client sends.

    The correction has to be continuous in the offset. Shifting a stamp past
    the skew by the *whole* offset would mean a client clock sitting near
    the edge (say 4:59.5 fast) has one beacon taken as given and the next —
    pushed over by a few hundred ms of network latency — moved back the full
    five minutes: a completion then lands before the start it followed (and
    is dropped as clock nonsense) and a dwell session's end freezes for ~5 min.
    Clamped, latency jitter moves a stamp only by the jitter itself, and a
    clock far off still lands within the skew of the receipt — the same
    receipt ± skew envelope the dwell bounds rely on.
    """
    offset = stamp - received
    if offset > _CLIENT_CLOCK_SKEW:
        return offset - _CLIENT_CLOCK_SKEW
    if offset < -_CLIENT_CLOCK_SKEW:
        return offset + _CLIENT_CLOCK_SKEW
    return timedelta(0)


def _parse_client_ts(raw) -> Optional[datetime]:
    """Parse a client-supplied ISO 8601 timestamp into a UTC datetime.

    The frontend sends timezone-offset-bearing ISO strings (e.g.
    ``2026-05-20T10:30:00+09:00``) so the server can normalise to UTC for
    storage without trusting the client's local clock. Returns None on
    any parse failure or when the parsed value drifts more than
    ``_CLIENT_TS_MAX_DRIFT`` from "now" (sanity bound — a bad value would
    otherwise sit in the table at e.g. year 9999, harmless on queries but
    visible as garbage in CSV exports).
    """
    if not isinstance(raw, str):
        return None
    try:
        # Python 3.11+ accepts the "Z" suffix natively; pre-3.11 needs the
        # explicit "+00:00" rewrite.
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive — caller forgot the offset. Treat as UTC rather than as
        # local (the beacon is a public endpoint; "server local" depends
        # on deployment).
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt_utc = dt.astimezone(timezone.utc)
    except OverflowError:
        # e.g. "0001-01-01T00:00:00+23:59": the UTC instant falls outside
        # datetime's range. Drop it like any other unparseable value.
        return None
    now_utc = _utcnow()
    if abs(dt_utc - now_utc) > _CLIENT_TS_MAX_DRIFT:
        return None
    return dt_utc


# Allowance on top of a session's own span when capping a reported dwell. The
# client measures dwell on a monotonic clock but stamps the span with
# second-precision wall-clock times, so an honest dwell can exceed the stamped
# span by up to a second or so.
_DWELL_SPAN_SLACK = timedelta(seconds=5)

# Most dwell the FIRST beacon of a slide-dwell session may claim. The viewer
# flushes on a 120 s heartbeat (frontend useSlideDwell.ts HEARTBEAT_MS) and on
# every hide / pagehide, and its clock is paused while hidden, so an honest
# first beacon carries at most ~2 min. Ten minutes (5x) leaves room for a late
# heartbeat — a main thread busy decoding tiles, low-power timer clamping, a
# device that slept while visible (a monotonic clock that keeps counting
# through sleep reports the nap as foreground time; capping it is right) —
# and for cached SPA builds without the heartbeat, whose first flush comes
# only on hide: such a tab loses whatever of its first uninterrupted stretch
# exceeds ten minutes (later stretches still count), until it reloads the
# current build. A forging client gets this much per new session instead of
# the 4 h dwell ceiling.
_FIRST_BEACON_MAX_DWELL_MS = 10 * 60 * 1000


def _cap_dwell_to_span(ms: int, start: datetime, end: datetime) -> int:
    """``ms`` no larger than the time between ``start`` and ``end`` (plus
    ``_DWELL_SPAN_SLACK``): foreground time cannot exceed the session's
    duration, so anything above is a bad or forged report."""
    span_ms = int((end - start + _DWELL_SPAN_SLACK).total_seconds() * 1000)
    return min(ms, max(0, span_ms))


def _clamp_dwell_ms(ms: int) -> int:
    """Clamp a reported dwell to [0, settings.analytics_max_dwell_ms].

    Negative values become 0; absurdly large values (idle tabs left open
    overnight, misbehaving client clocks) are capped at the configured
    upper bound so a single bad row cannot dominate the aggregate.
    """
    if ms < 0:
        return 0
    if ms > settings.analytics_max_dwell_ms:
        return settings.analytics_max_dwell_ms
    return int(ms)


@router.post("/api/analytics/slide-dwell", status_code=204)
async def record_slide_dwell(
    request: Request,
    db: Session = Depends(get_db),
):
    """Per-session dwell-time beacon for the viewer page.

    Same silent-drop contract as the pageview beacon: a malformed body,
    unknown slide id, anonymous viewer, or analytics-off state all
    resolve to 204 without persisting anything. The frontend fires this
    on viewer unmount (and on ``visibilitychange`` when the tab goes
    hidden) with ``navigator.sendBeacon`` so a tab-close still delivers
    the timing.
    """
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)
    if not isinstance(payload, dict):
        return Response(status_code=204)
    # Offload the synchronous SQLite work to the threadpool so a contended
    # write (busy_timeout up to 5s) can't stall the event loop — see
    # record_pageview.
    headers = request.headers
    cookies = request.cookies
    client_host = request.client.host if request.client else "127.0.0.1"
    resp = await run_in_threadpool(
        _persist_slide_dwell, db, payload, headers, cookies, client_host,
        getattr(request.state, "verified_basic_user", None),
    )
    # Opportunistic, throttled retention prune — see _with_prune.
    return _with_prune(resp)


def _persist_slide_dwell(
    db, payload, headers, cookies, client_host, verified_basic_user=None,
) -> Response:
    # Mode first, as in the pageview beacon: analytics off costs one read.
    mode = _an.current_analytics_mode(db)
    if mode == _an.ANALYTICS_OFF:
        return Response(status_code=204)
    # Beacon carries the public_id token; resolve to the internal PK for storage.
    slide = _resolve_slide(db, payload.get("slide_public_id"))
    if slide is None:
        return Response(status_code=204)
    slide_id, published = slide

    # Drop bot traffic before identity resolution. The dwell/tour tables
    # don't carry a ua_class column, so there is no post-hoc "exclude
    # bots" toggle on the dashboard — once a bot row lands it inflates
    # avg_dwell_ms forever. The bot share should be near zero in practice
    # (crawlers rarely linger on the viewer page and never fire the
    # unmount/pagehide flush), but a synthetic monitor with cached
    # credentials would otherwise dominate the average.
    if _an.classify_user_agent(headers.get("user-agent") or "") == "bot":
        return Response(status_code=204)

    started_at = _parse_client_ts(payload.get("started_at"))
    ended_at = _parse_client_ts(payload.get("ended_at"))
    if not started_at or not ended_at:
        return Response(status_code=204)
    if ended_at < started_at:
        return Response(status_code=204)
    # ended_at is stamped as the beacon goes out, so it is anchored to the
    # receipt time; started_at (stamped at mount, on the same clock) moves
    # with it, keeping the session's own span. See _receipt_offset.
    offset = _receipt_offset(ended_at, _utcnow())
    started_at -= offset
    ended_at -= offset

    dwell_ms = _coerce_optional_int(payload.get("dwell_ms"))
    if dwell_ms is None:
        dwell_ms = int((ended_at - started_at).total_seconds() * 1000)
    dwell_ms = _clamp_dwell_ms(dwell_ms)

    # Optional client-minted session id. When present the beacon is an
    # incremental update for one viewer-page session (fired on
    # visibilitychange-hidden / pagehide / unmount); reconcile into the
    # existing row via max(dwell_ms) so the full foreground dwell is kept
    # even when the user backgrounded and returned to the tab. Absent /
    # malformed → one-shot insert, which is what a non-JS or scripted caller
    # posting a single summary beacon produces.
    session_id = _valid_session_id(payload.get("session_id"))

    session_cookie = cookies.get(_app_auth.SESSION_COOKIE)
    auth_header = headers.get("authorization", "")
    # Single staff-bearer resolution shared by identity + actor_role — see
    # _persist_pageview.
    staff_user = _resolve_beacon_staff_user(db, auth_header)
    actor_role = _staff_actor_role(staff_user, auth_header)
    if not published and actor_role is None:
        return Response(status_code=204)
    _uid, user_identifier, mode = _an.resolve_identity(
        session_cookie, headers, client_host, db,
        staff_user=staff_user,
        verified_basic_user=verified_basic_user,
        mode=mode,
    )
    if not user_identifier:
        # No row for anonymous viewers — see SlideDwellEvent docstring.
        return Response(status_code=204)

    def _existing() -> Optional[SlideDwellEvent]:
        if session_id is None:
            return None
        return db.execute(
            select(SlideDwellEvent).where(SlideDwellEvent.session_id == session_id)
        ).scalar_one_or_none()

    try:
        existing = _existing()
        if existing is not None:
            _merge_dwell_beacon(db, existing, user_identifier, slide_id, dwell_ms, ended_at)
            return Response(status_code=204)

        # A new session row. Its first beacon's dwell is capped at
        # _FIRST_BEACON_MAX_DWELL whatever span it claims, and the stored
        # start is pulled up to just before that dwell: from here on the
        # session's span — and with it every later dwell (see
        # _merge_dwell_beacon) — can only grow as the clock runs. This, not a
        # count of sessions per identity, is what bounds a forging client:
        # a shared sign-in (a class on one Basic account) opens hundreds of
        # honest sessions, and each one can claim at most this much.
        first_dwell = min(_cap_dwell_to_span(dwell_ms, started_at, ended_at), _FIRST_BEACON_MAX_DWELL_MS)
        started_at = max(
            started_at,
            ended_at - timedelta(milliseconds=first_dwell) - _DWELL_SPAN_SLACK,
        )
        row = SlideDwellEvent(
            slide_id=slide_id,
            session_id=session_id,
            user_identifier=user_identifier,
            started_at=started_at,
            ended_at=ended_at,
            dwell_ms=first_dwell,
            actor_role=actor_role,
        )
        db.add(row)
        db.commit()
    except IntegrityError:
        # Two beacons for the same session raced past the read above and both
        # tried to insert; the UNIQUE constraint kept exactly one. The loser's
        # report is not a duplicate, though — the first two beacons of a
        # session (visibilitychange-hidden racing pagehide) carry different
        # dwells, and dropping the later one would undercount it. So merge it into
        # the winner's row, once, exactly as if it had arrived second. Benign
        # and self-correcting, so it stays out of the WARNING stream that feeds
        # the admin error log — an ordinary page unload would otherwise look
        # like a defect there.
        db.rollback()
        logger.debug(
            "analytics slide-dwell insert lost an insert race (slide_id=%s); merging",
            slide_id,
        )
        try:
            existing = _existing()
            if existing is not None:
                _merge_dwell_beacon(db, existing, user_identifier, slide_id, dwell_ms, ended_at)
        except Exception:
            db.rollback()
            logger.warning(
                "analytics slide-dwell merge after an insert race failed (slide_id=%s)",
                slide_id, exc_info=True,
            )
    except Exception:
        db.rollback()
        logger.warning(
            "analytics slide-dwell insert failed (slide_id=%s)",
            slide_id, exc_info=True,
        )
    return Response(status_code=204)


def _merge_dwell_beacon(
    db, existing: SlideDwellEvent, user_identifier: str, slide_id: int,
    dwell_ms: int, ended_at: datetime,
) -> None:
    """Fold a later beacon of a stored dwell session into its row and commit.

    Guards first: a beacon from another identity or for another slide never
    touches the row (it is dropped without trace).
    """
    if existing.user_identifier != user_identifier:
        # Hijack guard: a different authenticated user writing to
        # someone else's session — drop without trace (mirrors the
        # tour-event handler).
        return
    if existing.slide_id != slide_id:
        # A session_id is minted per viewer-page session (a single
        # slide). A beacon that reuses an existing session_id but
        # points at a DIFFERENT slide would otherwise inflate this
        # slide's dwell from another slide's page — drop it, matching
        # the user_identifier hijack guard above.
        return
    session_id = existing.session_id
    # Monotonic dwell; never let a later, smaller report (clock skew,
    # out-of-order in-flight beacons) walk the value back.
    #
    # Ratchet each field with its own conditional UPDATE rather than
    # computing max() in Python and assigning. The read above and the
    # write are separate transactions with no row lock, so two
    # overlapping beacons for the same session (visibilitychange racing
    # pagehide) could both read dwell=100, compute 200 and 150, and let
    # the 150 land last — silently undercounting. A statement that only
    # ever raises the stored value cannot lose an update regardless of
    # interleaving.
    #
    # ``synchronize_session=False`` on both: the default "evaluate"
    # strategy re-runs the WHERE clause in Python against the
    # identity-mapped row, where SQLite has handed back a *naive* UTC
    # ``ended_at`` — comparing that to the aware bound value raises
    # TypeError. Nothing downstream reads ``existing`` again (the
    # handler returns immediately), so skipping the in-session sync is
    # free. The bound value is converted to naive UTC to match how the
    # column is stored, keeping the SQL comparison well-defined.
    # The session runs from the stored started_at (whatever this beacon
    # claims) to the latest end reported so far, which may be an
    # earlier beacon's if this one arrived out of order. The stored start was
    # pulled up to just before the first beacon's (capped) dwell when the row
    # was inserted, and every end is receipt-anchored (see _receipt_offset),
    # so the dwell can never exceed: first dwell + the server-clock time
    # elapsed since the row was stored + the receipt skew at each end (+ the
    # span slack) — however many beacons, with whatever claims, arrive.
    session_start = coerce_aware(existing.started_at)
    session_end = coerce_aware(existing.ended_at)
    clamped = _clamp_dwell_ms(max(
        int(existing.dwell_ms or 0),
        _cap_dwell_to_span(dwell_ms, session_start, max(session_end, ended_at)),
    ))
    db.execute(
        update(SlideDwellEvent)
        .where(
            SlideDwellEvent.session_id == session_id,
            SlideDwellEvent.dwell_ms < clamped,
        )
        .values(dwell_ms=clamped)
        .execution_options(synchronize_session=False)
    )
    ended_at_naive = ended_at.astimezone(timezone.utc).replace(tzinfo=None)
    db.execute(
        update(SlideDwellEvent)
        .where(
            SlideDwellEvent.session_id == session_id,
            SlideDwellEvent.ended_at < ended_at_naive,
        )
        .values(ended_at=ended_at_naive)
        .execution_options(synchronize_session=False)
    )
    db.commit()


@router.post("/api/analytics/tour-event", status_code=204)
async def record_tour_event(
    request: Request,
    db: Session = Depends(get_db),
):
    """Tour-session progress beacon.

    Accepts three event shapes, all keyed by a client-minted
    ``session_id`` (UUID v4):

    * ``event="start"``  — create the row with started_at + total_steps.
    * ``event="progress"`` — bump last_step_index + total_dwell_ms (``sent_at``
      stamps when the dwell was read, on the clock that stamped started_at).
    * ``event="complete"`` — set completed_at + completed=True.

    The session_id lets a single user's tour be tracked across multiple
    HTTP calls without server-side session state, and lets the frontend
    deliver a final "complete" event via ``navigator.sendBeacon`` on
    page unload without waiting for a response.

    All three shapes are idempotent: repeating "start" is a no-op,
    "progress" only moves last_step_index forward, and "complete" only
    fires once per session row.
    """
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)
    if not isinstance(payload, dict):
        return Response(status_code=204)
    # Offload the synchronous SQLite work to the threadpool so a contended
    # write (busy_timeout up to 5s) can't stall the event loop — see
    # record_pageview.
    headers = request.headers
    cookies = request.cookies
    client_host = request.client.host if request.client else "127.0.0.1"
    resp = await run_in_threadpool(
        _persist_tour_event, db, payload, headers, cookies, client_host,
        getattr(request.state, "verified_basic_user", None),
    )
    # Opportunistic, throttled retention prune — see _with_prune.
    return _with_prune(resp)


def _persist_tour_event(
    db, payload, headers, cookies, client_host, verified_basic_user=None,
) -> Response:
    event = payload.get("event")
    if event not in ("start", "progress", "complete"):
        return Response(status_code=204)

    session_id = _valid_session_id(payload.get("session_id"))
    if session_id is None:
        return Response(status_code=204)

    # Mode first, as in the pageview beacon: analytics off costs one read.
    mode = _an.current_analytics_mode(db)
    if mode == _an.ANALYTICS_OFF:
        return Response(status_code=204)

    # Beacon carries the public_id token; resolve to the internal PK for storage.
    tour = _resolve_tour_by_public_id(db, payload.get("tour_public_id"))
    if tour is None:
        return Response(status_code=204)
    tour_id = tour.id

    # See record_slide_dwell: tour_sessions has no ua_class column either,
    # so a bot row would skew completion-rate / avg-dwell aggregates with
    # no way to filter it out after the fact.
    if _an.classify_user_agent(headers.get("user-agent") or "") == "bot":
        return Response(status_code=204)

    session_cookie = cookies.get(_app_auth.SESSION_COOKIE)
    auth_header = headers.get("authorization", "")
    # Single staff-bearer resolution shared by identity + actor_role — see
    # _persist_pageview.
    staff_user = _resolve_beacon_staff_user(db, auth_header)
    actor_role = _staff_actor_role(staff_user, auth_header)
    if actor_role is None and not (
        tour.is_published and _slide_published(db, tour.slide_id)
    ):
        return Response(status_code=204)
    _uid, user_identifier, mode = _an.resolve_identity(
        session_cookie, headers, client_host, db,
        staff_user=staff_user,
        verified_basic_user=verified_basic_user,
        mode=mode,
    )
    if not user_identifier:
        return Response(status_code=204)

    try:
        existing = db.execute(
            select(TourSession).where(TourSession.session_id == session_id)
        ).scalar_one_or_none()

        if event == "start":
            if existing is not None:
                return Response(status_code=204)
            started_at = _parse_client_ts(payload.get("started_at"))
            if not started_at:
                return Response(status_code=204)
            # "start" goes out as the tour opens, so its started_at is a
            # send-time stamp: anchored to the receipt, every later span of the
            # session (to sent_at / completed_at, anchored the same way) is
            # bounded by the server's elapsed time since this row was first
            # seen. A start claiming to be hours old does not pre-pay the
            # dwell a progress beacon may report right after it (beyond the
            # skew allowance it is pulled in to receipt − skew).
            received = _utcnow()
            started_at -= _receipt_offset(started_at, received)
            # No dwell rides on "start" (the row begins at 0), and with both
            # ends of every later span receipt-anchored, a tour's dwell and
            # its completion time are bounded by the server's elapsed time
            # since this start was received (plus the skew at each end) — so
            # new tour sessions need no per-identity count either.
            total_steps = _coerce_optional_int(payload.get("total_steps")) or 0
            # Clamp step_index to non-negative — `_coerce_optional_int` happily
            # returns -5 (truthy in Python), and `or 0` would not rescue it.
            start_step = max(0, _coerce_optional_int(payload.get("step_index")) or 0)
            row = TourSession(
                session_id=session_id,
                tour_id=tour_id,
                user_identifier=user_identifier,
                started_at=started_at,
                last_step_index=start_step,
                total_steps=max(0, total_steps),
                completed=False,
                total_dwell_ms=0,
                actor_role=actor_role,
            )
            db.add(row)
            db.commit()
            return Response(status_code=204)

        if existing is None:
            # progress/complete with no prior "start" — silently drop.
            # The frontend always fires start first, so reaching this
            # branch means the start was lost (network blip, off→on mode
            # toggle mid-tour, etc.). Resurrecting a row from a partial
            # progress event would silently invent started_at.
            return Response(status_code=204)

        if existing.user_identifier != user_identifier:
            # Hijack guard: a different authenticated user trying to
            # write to someone else's session. Drop without trace.
            return Response(status_code=204)
        if existing.tour_id != tour_id:
            # A session_id is minted per tour session; a beacon naming another
            # tour would move this tour's progress / completion numbers.
            return Response(status_code=204)

        if event == "progress":
            # Ratcheting UPDATEs rather than read-modify-write assignment, for
            # the same reason as the slide-dwell handler: the SELECT above and
            # this write are separate transactions with no row lock, so two
            # overlapping progress beacons could otherwise lose the higher of
            # the two values.
            step_idx = _coerce_optional_int(payload.get("step_index"))
            if step_idx is not None and step_idx > existing.last_step_index:
                db.execute(
                    update(TourSession)
                    .where(
                        TourSession.session_id == session_id,
                        TourSession.last_step_index < int(step_idx),
                    )
                    .values(last_step_index=int(step_idx))
                    .execution_options(synchronize_session=False)
                )
            dwell = _coerce_optional_int(payload.get("total_dwell_ms"))
            if dwell is not None:
                # Time spent in the tour cannot exceed the time since it
                # started — up to when the dwell was read, even once
                # completed: "complete" is sent on reaching the last step, the
                # reader lingers there, and a trailing progress beacon records
                # that time. The span is measured on the client's clock, from
                # started_at to this beacon's sent_at: measured to the
                # server's now, a client clock running S ahead of the server
                # would lose S from every dwell. A beacon without a usable
                # sent_at (missing, malformed or out of range) is capped at
                # the server's now, which is exact for a client clock that
                # agrees with the server's. sent_at is anchored to the receipt
                # like the start was (_receipt_offset): a clock within the
                # skew keeps measuring on its own time, one further off — or a
                # sent_at forged days ahead — is pulled in to the receipt
                # time ± the skew, as its start was.
                received = _utcnow()
                span_end = _parse_client_ts(payload.get("sent_at")) or received
                span_end -= _receipt_offset(span_end, received)
                capped = _cap_dwell_to_span(dwell, coerce_aware(existing.started_at), span_end)
                # Monotonic: never let a smaller value (client clock skew,
                # out-of-order in-flight beacons) walk total_dwell_ms back.
                clamped = _clamp_dwell_ms(max(int(existing.total_dwell_ms or 0), capped))
                db.execute(
                    update(TourSession)
                    .where(
                        TourSession.session_id == session_id,
                        TourSession.total_dwell_ms < clamped,
                    )
                    .values(total_dwell_ms=clamped)
                    .execution_options(synchronize_session=False)
                )
            db.commit()
            return Response(status_code=204)

        if event == "complete":
            if existing.completed:
                return Response(status_code=204)
            completed_at = _parse_client_ts(payload.get("completed_at"))
            if not completed_at:
                return Response(status_code=204)
            # Stamped as the beacon goes out: receipt-anchored like sent_at.
            completed_at -= _receipt_offset(completed_at, _utcnow())
            # SQLite hands DateTime(timezone=True) back naive; it was stored
            # as UTC by _parse_client_ts.
            started_at = existing.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if completed_at < started_at:
                # Clock nonsense — same rule as the slide-dwell beacon's
                # ended_at < started_at. A negative completion time would drag
                # avg_completion_ms down (or below zero).
                return Response(status_code=204)
            # Cap the completion time at the dwell ceiling, as total_dwell_ms
            # is: a tour left open for days before its last step would
            # otherwise dominate the tour's avg_completion_ms.
            max_span = timedelta(milliseconds=settings.analytics_max_dwell_ms)
            if completed_at - started_at > max_span:
                completed_at = started_at + max_span
            existing.completed = True
            existing.completed_at = completed_at
            # Same ratcheting rationale as the "progress" branch above: a
            # completion beacon overlapping a final progress beacon must not
            # walk the other one's higher value back.
            step_idx = _coerce_optional_int(payload.get("step_index"))
            if step_idx is not None and step_idx > existing.last_step_index:
                db.execute(
                    update(TourSession)
                    .where(
                        TourSession.session_id == session_id,
                        TourSession.last_step_index < int(step_idx),
                    )
                    .values(last_step_index=int(step_idx))
                    .execution_options(synchronize_session=False)
                )
            dwell = _coerce_optional_int(payload.get("total_dwell_ms"))
            if dwell is not None:
                # Time spent in the tour cannot exceed start → completion.
                clamped = _clamp_dwell_ms(max(
                    int(existing.total_dwell_ms or 0),
                    _cap_dwell_to_span(dwell, started_at, completed_at),
                ))
                db.execute(
                    update(TourSession)
                    .where(
                        TourSession.session_id == session_id,
                        TourSession.total_dwell_ms < clamped,
                    )
                    .values(total_dwell_ms=clamped)
                    .execution_options(synchronize_session=False)
                )
            db.commit()
            return Response(status_code=204)
    except IntegrityError:
        # See the slide-dwell handler: a concurrent duplicate beacon is the
        # UNIQUE constraint doing its job, not a fault worth an error-log row.
        db.rollback()
        logger.debug(
            "analytics tour-event lost an insert race (event=%s session=%s)",
            event, session_id,
        )
    except Exception:
        db.rollback()
        logger.warning(
            "analytics tour-event insert failed (event=%s session=%s)",
            event, session_id, exc_info=True,
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Admin: bundled dashboard endpoint
#
# The frontend renders summary + all five ranking tables from a single
# fetch. Returning everything in one response keeps a "refresh" click
# under one nginx-rate-limit token instead of six, avoiding the 429 errors
# six separate requests trigger when the tab refreshes alongside other admin
# requests already in flight.
# ---------------------------------------------------------------------------
@router.get("/api/admin/analytics/dashboard")
def admin_dashboard(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD; overrides `days` when set"),
    end_date: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD"),
    limit: int = Query(20, ge=1, le=100),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    inc_bots = bool(include_bots)
    inc_staff = bool(include_staff)
    since, until, span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        # Sections share passes over the window where they can: the per-day
        # counts feed the summary totals, the daily chart and the UA
        # breakdown, and the top-users grouping also yields the distinct-user
        # count.
        day_counts = _day_counts(db, since, until, inc_bots, inc_staff)
        top_users, users_total = _top_users_and_count(
            db, since, until, limit, inc_bots, inc_staff
        )
        return {
            "summary": _compute_summary(
                db, since, until, span, inc_bots, inc_staff,
                day_counts=day_counts, unique_users=users_total,
            ),
            "top_slides": _compute_top_slides(db, since, until, limit, inc_bots, inc_staff),
            "top_tours": _compute_top_tours(db, since, until, limit, inc_bots, inc_staff),
            "referrers": _compute_referrers(db, since, until, limit, inc_bots, inc_staff),
            "ua_breakdown": _ua_rows(day_counts[1]),
            "top_users": top_users,
            "server_tz": server_tz_info(),
        }


# ---------------------------------------------------------------------------
# Admin: per-section endpoints (kept for programmatic callers / tests)
# ---------------------------------------------------------------------------
@router.get("/api/admin/analytics/summary")
def admin_summary(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_summary(db, since, until, span, bool(include_bots), bool(include_staff))


@router.get("/api/admin/analytics/top-slides")
def admin_top_slides(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_top_slides(db, since, until, limit, bool(include_bots), bool(include_staff))


@router.get("/api/admin/analytics/top-tours")
def admin_top_tours(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_top_tours(db, since, until, limit, bool(include_bots), bool(include_staff))


@router.get("/api/admin/analytics/referrers")
def admin_referrers(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_referrers(db, since, until, limit, bool(include_bots), bool(include_staff))


@router.get("/api/admin/analytics/ua-breakdown")
def admin_ua_breakdown(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_ua_breakdown(db, since, until, bool(include_bots), bool(include_staff))


@router.get("/api/admin/analytics/top-users")
def admin_top_users(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_top_users(db, since, until, limit, bool(include_bots), bool(include_staff))


# ---------------------------------------------------------------------------
# Admin: recent activity tail
# ---------------------------------------------------------------------------
_ACTOR_ROLE_FILTER_VALUES = ("admin", "manager", "instructor", "none")


@router.get("/api/admin/analytics/recent")
def admin_recent(
    limit: int = Query(200, ge=1, le=1000),
    # Bound to the users-table PK range so an out-of-range id is a 422 rather
    # than a 500: an unbounded user_id >= 2**63 raises OverflowError at SQLite
    # bind time on the `PageView.user_id == user_id` filter.
    user_id: Optional[int] = Query(None, ge=1, le=_MAX_DB_INT),
    # max_length matches the DB column widths in models.PageView
    # (user_email=String(255), client_ip=String(64)). Anything longer than
    # the column width can't possibly match an existing row anyway, and
    # bounding the parameter at validation time avoids passing arbitrarily
    # large strings into SQLAlchemy bind-parameter handling.
    user_email: Optional[str] = Query(None, max_length=255),
    client_ip: Optional[str] = Query(None, max_length=64),
    kind: Optional[str] = Query(None, max_length=32),
    days: Optional[int] = Query(None, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    actor_role: Optional[str] = Query(
        None,
        description=(
            "Filter to a specific actor_role. One of admin / manager / "
            "instructor matches rows stamped with that role; `none` "
            "matches rows where actor_role IS NULL (learner / anonymous)."
        ),
    ),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since = until = None
    if days is not None or start_date or end_date:
        # `days` alone resolves to a `now - N days` window via
        # _resolve_window; start/end take precedence inside the helper.
        since, until, _span = _resolve_window(days or 0, start_date, end_date)
    # Budgeted like the other report endpoints: a filter on an unindexed
    # combination (or none at all) can walk the whole retained history.
    with _query_budget(db):
        return _recent_rows(
            db, limit=limit, since=since, until=until, user_id=user_id,
            user_email=user_email, client_ip=client_ip, kind=kind,
            include_bots=bool(include_bots), include_staff=bool(include_staff),
            actor_role=actor_role,
        )


def _recent_rows(
    db: Session,
    *,
    limit: int,
    since: Optional[datetime],
    until: Optional[datetime],
    user_id: Optional[int],
    user_email: Optional[str],
    client_ip: Optional[str],
    kind: Optional[str],
    include_bots: bool,
    include_staff: bool,
    actor_role: Optional[str],
) -> list:
    """The newest page-view rows matching the recent-log filters, as the JSON
    ``/recent`` endpoint returns them (the CSV ``recent`` dataset renders the
    same rows). ``since`` / ``until`` None means no window: the newest rows
    across the whole retained history.
    """
    stmt = select(PageView).order_by(PageView.occurred_at.desc(), PageView.id.desc())
    if since is not None and until is not None:
        stmt = _window_filter(stmt, since, until)
    if user_id is not None:
        stmt = stmt.where(PageView.user_id == user_id)
    if user_email:
        stmt = stmt.where(PageView.user_email == user_email)
    if client_ip:
        stmt = stmt.where(PageView.client_ip == client_ip)
    if kind:
        # Reject unknown kinds outright rather than silently dropping the
        # filter — otherwise a typo (e.g. `kind=Tour`) returns the global
        # latest rows and looks like the filter "matched everything".
        if kind not in _VALID_KIND:
            raise HTTPException(
                status_code=400,
                detail=f"invalid kind: {kind!r} (expected one of {list(_VALID_KIND)})",
            )
        stmt = stmt.where(PageView.kind == kind)
    # actor_role filter: when explicitly requested, override the default
    # IS-NULL clause that _apply_default_filters would otherwise inject.
    # "none" matches learner / anonymous traffic (actor_role IS NULL); any
    # of the role names matches that specific staff role. Passing
    # actor_role= implies include_staff=1 so the explicit filter isn't
    # silently AND-ed away by the staff-exclusion clause.
    if actor_role is not None:
        if actor_role not in _ACTOR_ROLE_FILTER_VALUES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid actor_role: {actor_role!r} "
                    f"(expected one of {list(_ACTOR_ROLE_FILTER_VALUES)})"
                ),
            )
        include_staff = True
        if actor_role == "none":
            stmt = stmt.where(PageView.actor_role.is_(None))
        else:
            stmt = stmt.where(PageView.actor_role == actor_role)
    stmt = _apply_default_filters(stmt, include_bots, include_staff).limit(limit)
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return []

    # Resolve titles in one round-trip.
    slide_ids = list({r.slide_id for r in rows if r.slide_id})
    tour_ids = list({r.tour_id for r in rows if r.tour_id})
    slide_titles = _slide_labels(db, slide_ids, Slide.title, Slide.public_id)
    tour_titles = {
        t.id: (t.title or "", t.public_id or "")
        for t in (db.execute(select(Tour).where(Tour.id.in_(tour_ids))).scalars().all() if tour_ids else [])
    }
    tz = analytics_tz()
    out = []
    for r in rows:
        s_title, s_public_id = slide_titles.get(r.slide_id, ("", "")) if r.slide_id else ("", "")
        t_title, t_public_id = tour_titles.get(r.tour_id, ("", "")) if r.tour_id else ("", "")
        out.append({
            "id": r.id,
            "occurred_at": format_in_analytics_tz(r.occurred_at, tz),
            "kind": r.kind,
            "slide_id": r.slide_id,
            "slide_public_id": s_public_id,
            "slide_title": s_title,
            "tour_id": r.tour_id,
            "tour_public_id": t_public_id,
            "tour_title": t_title,
            "user_id": r.user_id,
            "user_email": r.user_email or "",
            "client_ip": r.client_ip or "",
            "ua_class": r.ua_class or "other",
            "user_agent": r.user_agent or "",
            "referrer_host": r.referrer_host or "",
            "actor_role": r.actor_role,
        })
    return out


# ---------------------------------------------------------------------------
# Admin: CSV export
#
# Spreadsheet-friendly download for the same datasets the dashboard
# renders. A single endpoint keyed by `dataset` keeps the surface tight
# (one place to add auth, one URL to bookmark) at the cost of slightly
# branchy header logic — preferable to seven near-duplicate endpoints.
# ---------------------------------------------------------------------------
_CSV_DATASETS = (
    "summary",
    "daily",
    "top-slides",
    "top-tours",
    "top-users",
    "referrers",
    "ua",
    "recent",
)


# CSV rendering (UTF-8 BOM + formula-injection defence) is shared with the
# audit-history export; see wsi_app.csv_export.
_csv_response = csv_export.csv_response


@router.get("/api/admin/analytics/export.csv")
def admin_export_csv(
    dataset: str = Query("summary", description="|".join(_CSV_DATASETS)),
    # `days` defaults to None — see the `recent` branch below for the
    # reason. Aggregate datasets that always need a finite window
    # substitute the conventional 30-day fallback when nothing is set.
    days: Optional[int] = Query(None, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
    # Bound to the users-table PK range (see admin_recent) so an out-of-range
    # id yields a 422 instead of an OverflowError 500 at SQLite bind time.
    user_id: Optional[int] = Query(None, ge=1, le=_MAX_DB_INT),
    user_email: Optional[str] = Query(None, max_length=255),
    client_ip: Optional[str] = Query(None, max_length=64),
    kind: Optional[str] = Query(None, max_length=32),
    include_bots: int = Query(0),
    include_staff: int = Query(0),
    # Mirrors the JSON /recent endpoint: only meaningful for dataset=recent.
    # Supplying it implies include_staff so the explicit role filter is not
    # AND-ed away by the default staff-exclusion clause.
    actor_role: Optional[str] = Query(None),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    if dataset not in _CSV_DATASETS:
        raise HTTPException(status_code=400, detail=f"unknown dataset: {dataset!r}")
    # actor_role only filters the per-row `recent` dataset. The aggregate
    # datasets never apply it, so honouring it silently would hand a scripted
    # caller staff-excluded numbers that contradict the role they asked for.
    # Reject explicitly (matching _resolve_window's "400 over silent
    # reinterpretation" stance) rather than returning misleading data. The
    # dashboard already omits actor_role for non-recent exports, so the UI is
    # unaffected.
    if actor_role is not None and dataset != "recent":
        raise HTTPException(
            status_code=400,
            detail="actor_role filter is only supported for dataset=recent",
        )
    # The other row filters likewise: a `top-slides` export "for one user"
    # would come back as everyone's ranking under a file the caller took for
    # that user's. A value that is present but empty is still a filter the caller
    # asked for, so it is refused too.
    if dataset != "recent":
        row_filters = {
            "user_id": user_id, "user_email": user_email,
            "client_ip": client_ip, "kind": kind,
        }
        given = [name for name, value in row_filters.items() if value is not None]
        if given:
            raise HTTPException(
                status_code=400,
                detail=f"{', '.join(given)} filter is only supported for dataset=recent",
            )
    inc_bots = bool(include_bots)
    inc_staff = bool(include_staff)
    # The `recent` dataset mirrors the JSON /api/admin/analytics/recent
    # contract: with no window parameters supplied, return the most
    # recent rows up to `limit` across the entire retained history (no
    # implicit date filter). Aggregate datasets cannot be windowless —
    # an unbounded summary would scan the whole table — so they fall
    # back to the conventional 30-day default.
    window_supplied = days is not None or start_date or end_date
    if dataset == "recent" and not window_supplied:
        since = until = None
        span = 0
        span_label = "all"
    else:
        since, until, span = _resolve_window(days or 30, start_date, end_date)
        # Derive the label's end date from the last covered *instant*, not
        # `until - 1 day`. `until` is exclusive: for an explicit date range
        # (until = end+1day) subtracting a tick still lands on the requested
        # end date, while for the preset `?days=N` window (until = now)
        # subtracting a whole day would mislabel the file as ending *yesterday*
        # even though the content — and _compute_summary's end_date — runs
        # through today. Match the content by subtracting a single tick.
        span_label = f"{since.date().isoformat()}_{(until - timedelta(microseconds=1)).date().isoformat()}"
    # File name pattern: analytics-<dataset>-<start>_<end>.csv. Ending
    # in the date span makes side-by-side comparisons of multiple
    # downloads obvious in the user's downloads folder.
    fname = f"analytics-{dataset}-{span_label}.csv"
    with _query_budget(db):
        return _export_csv_dataset(
            db, dataset, fname, since, until, span, limit, inc_bots, inc_staff,
            user_id=user_id, user_email=user_email, client_ip=client_ip,
            kind=kind, actor_role=actor_role,
        )


def _export_csv_dataset(
    db: Session, dataset: str, fname: str,
    since: Optional[datetime], until: Optional[datetime], span: int,
    limit: int, inc_bots: bool, inc_staff: bool,
    *, user_id, user_email, client_ip, kind, actor_role,
):
    if dataset == "summary":
        s = _compute_summary(db, since, until, span, inc_bots, inc_staff)
        header = ["metric", "value"]
        rows = [
            ["start_date", s["start_date"]],
            ["end_date", s["end_date"]],
            ["days", s["days"]],
            ["total_views", s["totals"]["top"] + s["totals"]["viewer"] + s["totals"]["tour"]],
            ["views_top", s["totals"]["top"]],
            ["views_viewer", s["totals"]["viewer"]],
            ["views_tour", s["totals"]["tour"]],
            ["unique_ips", s["unique_ips"]],
            ["unique_users", s["unique_users"]],
        ]
        return _csv_response(fname, header, rows)

    if dataset == "daily":
        daily = _compute_daily(db, since, until, inc_bots, inc_staff)
        header = ["day", "top", "viewer", "tour", "total"]
        rows = [
            [r["day"], r["top"], r["viewer"], r["tour"], r["top"] + r["viewer"] + r["tour"]]
            for r in daily
        ]
        return _csv_response(fname, header, rows)

    if dataset == "top-slides":
        data = _compute_top_slides(db, since, until, limit, inc_bots, inc_staff)
        header = ["rank", "slide_id", "catalog_id", "title", "views", "unique_ips"]
        rows = [
            [i + 1, r["slide_id"], r["catalog_id"], r["title"], r["views"], r["unique_ips"]]
            for i, r in enumerate(data)
        ]
        return _csv_response(fname, header, rows)

    if dataset == "top-tours":
        data = _compute_top_tours(db, since, until, limit, inc_bots, inc_staff)
        header = ["rank", "tour_id", "tour_title", "slide_id", "slide_title", "views", "unique_ips"]
        rows = [
            [
                i + 1,
                r["tour_id"],
                r["tour_title"],
                r["slide_id"],
                r["slide_title"],
                r["views"],
                r["unique_ips"],
            ]
            for i, r in enumerate(data)
        ]
        return _csv_response(fname, header, rows)

    if dataset == "top-users":
        data = _compute_top_users(db, since, until, limit, inc_bots, inc_staff)
        # `user_identifier` rather than `user_email`: in hashed mode the
        # column holds an HMAC hex digest, and even in plain mode it may
        # hold a bare username (basic-auth / LDAP subject) rather than an
        # email. The JSON endpoint names the field `user_email`, but
        # the CSV is a downstream-consumer artefact
        # where a more accurate header avoids confusion. The recent CSV
        # keeps `user_email` because it mirrors the raw DB column name.
        header = ["rank", "user_identifier", "user_id", "views", "unique_days", "unique_ips"]
        rows = [
            [
                i + 1,
                r["user_email"],
                r["user_id"] if r["user_id"] is not None else "",
                r["views"],
                r["unique_days"],
                r["unique_ips"],
            ]
            for i, r in enumerate(data)
        ]
        return _csv_response(fname, header, rows)

    if dataset == "referrers":
        data = _compute_referrers(db, since, until, limit, inc_bots, inc_staff)
        header = ["rank", "referrer_host", "views"]
        rows = [
            [i + 1, r["referrer_host"], r["views"]]
            for i, r in enumerate(data)
        ]
        return _csv_response(fname, header, rows)

    if dataset == "ua":
        data = _compute_ua_breakdown(db, since, until, inc_bots, inc_staff)
        header = ["ua_class", "views"]
        rows = [[r["ua_class"], r["views"]] for r in data]
        return _csv_response(fname, header, rows)

    # dataset == "recent": raw page-view rows — the same rows, filters
    # (actor_role included) and formatting as the JSON /recent endpoint, so a
    # CSV export of the on-screen recent log matches what the admin is
    # looking at. With no window parameter `since`/`until` are None and the
    # newest `limit` rows of the whole retained history are returned.
    rows_out = _recent_rows(
        db, limit=limit, since=since, until=until, user_id=user_id,
        user_email=user_email, client_ip=client_ip, kind=kind,
        include_bots=inc_bots, include_staff=inc_staff, actor_role=actor_role,
    )
    header = [
        "occurred_at",
        "kind",
        "slide_id",
        "slide_title",
        "tour_id",
        "tour_title",
        "user_email",
        "user_id",
        "actor_role",
        "client_ip",
        "ua_class",
        "user_agent",
        "referrer_host",
    ]
    out_rows = [
        [
            r["occurred_at"],
            r["kind"],
            r["slide_id"] or "",
            r["slide_title"],
            r["tour_id"] or "",
            r["tour_title"],
            r["user_email"],
            r["user_id"] if r["user_id"] is not None else "",
            r["actor_role"] or "",
            r["client_ip"],
            r["ua_class"],
            r["user_agent"],
            r["referrer_host"],
        ]
        for r in rows_out
    ]
    return _csv_response(fname, header, out_rows)


# ---------------------------------------------------------------------------
# Admin: tour-completion and slide-dwell aggregates
# ---------------------------------------------------------------------------
def _compute_tour_completion(
    db: Session, since: datetime, until: datetime, limit: int,
    inc_staff: bool = False,
) -> list:
    """Return per-tour completion stats inside the window.

    Each row covers one ``tour_id``. The tour-event beacon records only
    identified viewers, and only while analytics is on, so there are no
    anonymous rows to filter; staff sessions (``actor_role`` set) are left
    out unless ``inc_staff``. ``avg_completion_ms`` averages only the completed
    sessions; ``avg_dwell_ms`` averages every session including the
    abandoned ones, so a tour with many drop-offs surfaces visibly.
    """
    s = since.astimezone(timezone.utc) if since.tzinfo else since
    u = until.astimezone(timezone.utc) if until.tzinfo else until
    stmt = (
        select(
            TourSession.tour_id,
            func.count().label("sessions_total"),
            func.sum(
                func.cast(TourSession.completed, Integer)
            ).label("sessions_completed"),
            func.avg(TourSession.total_dwell_ms).label("avg_dwell_ms"),
        )
        .where(TourSession.started_at >= s)
        .where(TourSession.started_at < u)
    )
    # Exclude staff QA traversals by default — parity with pageview / dwell.
    if not inc_staff:
        stmt = stmt.where(TourSession.actor_role.is_(None))
    stmt = (
        stmt.group_by(TourSession.tour_id)
        # Group key as tie-breaker — see _compute_top_slides.
        .order_by(func.count().desc(), TourSession.tour_id)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    if not rows:
        return []

    # Compute completion-time average per tour in a second pass — keeping the
    # "average of (completed_at - started_at) over completed sessions" out of
    # the main GROUP BY keeps that aggregate clean. SQLite-only build, so
    # julianday() (days; *86_400_000 -> ms) is the natural fit.
    tour_ids = [r[0] for r in rows]
    avg_complete_stmt = (
        select(
            TourSession.tour_id,
            func.avg(
                (func.julianday(TourSession.completed_at)
                 - func.julianday(TourSession.started_at)) * 86400000.0
            ).label("avg_ms"),
        )
        .where(TourSession.tour_id.in_(tour_ids))
        .where(TourSession.started_at >= s)
        .where(TourSession.started_at < u)
        .where(TourSession.completed.is_(True))
    )
    if not inc_staff:
        avg_complete_stmt = avg_complete_stmt.where(TourSession.actor_role.is_(None))
    avg_complete_stmt = avg_complete_stmt.group_by(TourSession.tour_id)
    avg_complete = {tid: float(ms or 0) for tid, ms in db.execute(avg_complete_stmt).all()}

    tours = db.execute(select(Tour).where(Tour.id.in_(tour_ids))).scalars().all()
    tour_map = {t.id: t for t in tours}
    slide_ids = list({t.slide_id for t in tours})
    slide_info = _slide_labels(db, slide_ids, Slide.title, Slide.public_id)
    out: list = []
    for tid, total, completed, avg_dwell in rows:
        t = tour_map.get(tid)
        s_title, s_public_id = slide_info.get(t.slide_id, ("", "")) if t else ("", "")
        out.append({
            "tour_id": tid,
            "public_id": (t.public_id or "") if t else "",
            "slide_public_id": s_public_id,
            "tour_title": (t.title or "") if t else "",
            "slide_id": t.slide_id if t else None,
            "slide_title": s_title,
            "sessions_total": int(total or 0),
            "sessions_completed": int(completed or 0),
            "completion_rate": (float(completed or 0) / float(total)) if total else 0.0,
            "avg_completion_ms": int(avg_complete.get(tid, 0)),
            "avg_dwell_ms": int(avg_dwell or 0),
        })
    return out


def _compute_slide_dwell(
    db: Session, since: datetime, until: datetime, limit: int,
    inc_staff: bool = False,
) -> list:
    """Per-slide average dwell time inside the window.

    One row per ``slide_id`` ordered by ``SUM(dwell_ms) DESC`` — the
    slides that consumed the most cumulative viewer attention come
    first, regardless of session count. This intentionally differs
    from _compute_top_slides (which ranks by view count); the
    dashboard puts the two tables side-by-side so the operator can
    spot slides that are viewed often but skimmed quickly, vs. slides
    viewed less often but studied at length. The page-view beacon and
    the slide-dwell beacon are independent, so a slide can show up in
    top-slides without a dwell number when a viewer left fast enough
    that no flush path (unmount / pagehide / visibilitychange) fired.
    """
    s = since.astimezone(timezone.utc) if since.tzinfo else since
    u = until.astimezone(timezone.utc) if until.tzinfo else until
    stmt = (
        select(
            SlideDwellEvent.slide_id,
            func.count().label("sessions"),
            func.avg(SlideDwellEvent.dwell_ms).label("avg_dwell_ms"),
            func.sum(SlideDwellEvent.dwell_ms).label("total_dwell_ms"),
            func.count(func.distinct(SlideDwellEvent.user_identifier)).label("unique_users"),
        )
        .where(SlideDwellEvent.started_at >= s)
        .where(SlideDwellEvent.started_at < u)
    )
    # Exclude staff QA traversals (actor_role IS NOT NULL) by default so the
    # dwell averages reflect learner access — parity with the pageview
    # aggregations' _apply_default_filters staff clause.
    if not inc_staff:
        stmt = stmt.where(SlideDwellEvent.actor_role.is_(None))
    stmt = (
        stmt.group_by(SlideDwellEvent.slide_id)
        .order_by(func.sum(SlideDwellEvent.dwell_ms).desc(), SlideDwellEvent.slide_id)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    if not rows:
        return []
    slide_ids = [r[0] for r in rows]
    slide_map = _slide_labels(db, slide_ids, Slide.title, Slide.catalog_id, Slide.public_id)
    return [
        {
            "slide_id": sid,
            "public_id": slide_map.get(sid, ("", "", ""))[2],
            "title": slide_map.get(sid, ("", "", ""))[0],
            "catalog_id": slide_map.get(sid, ("", "", ""))[1],
            "sessions": int(sessions or 0),
            "avg_dwell_ms": int(avg or 0),
            "total_dwell_ms": int(total or 0),
            "unique_users": int(uu or 0),
        }
        for sid, sessions, avg, total, uu in rows
    ]


@router.get("/api/admin/analytics/tour-completion")
def admin_tour_completion(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_tour_completion(db, since, until, limit, bool(include_staff))


@router.get("/api/admin/analytics/slide-dwell")
def admin_slide_dwell(
    days: int = Query(30, ge=1, le=_MAX_WINDOW_DAYS),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    include_staff: int = Query(0),
    _: User = Depends(require_analytics_access),
    db: Session = Depends(get_db),
):
    since, until, _span = _resolve_window(days, start_date, end_date)
    with _query_budget(db):
        return _compute_slide_dwell(db, since, until, limit, bool(include_staff))


# ---------------------------------------------------------------------------
# Admin: clear collected analytics data
# ---------------------------------------------------------------------------
@router.delete("/api/admin/analytics/data")
def admin_clear_analytics(
    from_date: Optional[str] = Query(
        None,
        description=(
            "Inclusive YYYY-MM-DD lower bound (in analytics TZ). When set, "
            "only rows on or after midnight-of-from_date are deleted."
        ),
    ),
    to_date: Optional[str] = Query(
        None,
        description=(
            "Inclusive YYYY-MM-DD upper bound (in analytics TZ). When set, "
            "only rows strictly before midnight-of-(to_date+1) are deleted."
        ),
    ),
    all_: bool = Query(
        False,
        alias="all",
        description=(
            "Required (all=1) to delete EVERY analytics row; not combinable "
            "with from_date / to_date."
        ),
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Wipe analytics rows across all three event tables.

    Irreversible. ``all=1`` empties every table (the "nuke" semantics) with
    one unqualified ``DELETE`` per table, which SQLite runs as a truncate —
    seconds even for millions of rows. ``from_date`` and ``to_date`` are
    independent inclusive YYYY-MM-DD bounds in the analytics TZ — passing
    only one leaves the other end open. The dwell / tour tables are wiped
    alongside ``page_views`` to keep the three event streams in lockstep.

    The full wipe has to be asked for by name: were it what a request with
    no range meant, a range lost on the way (a misspelt parameter, a date
    picker that sent ``from_date=``) would silently erase the whole history.
    No scope at all, ``all`` together with a date, or a date parameter that
    is present but empty answers 400 ``analyticsClearScopeRequired`` and
    deletes nothing.

    A ranged wipe has to delete row by row (index maintenance for every
    row), which can take minutes on a large table, so each call works for at
    most ``_CLEAR_TIME_BUDGET_S`` and answers ``"complete": false`` when rows
    may remain; the caller repeats the identical request until it answers
    ``"complete": true`` (the dashboard does this). Every call is audited
    with what it deleted.
    """
    # Presence, not truthiness: `from_date=` is a range the caller meant to
    # send, and must not fall through to "no range".
    if any(v is not None and not v.strip() for v in (from_date, to_date)):
        raise api_error(400, "adminDash.apiErr.analyticsClearScopeRequired")
    ranged = from_date is not None or to_date is not None
    if ranged == bool(all_):
        # Neither a range nor all=1, or both at once.
        raise api_error(400, "adminDash.apiErr.analyticsClearScopeRequired")
    from_dt: Optional[datetime] = _parse_iso_date(from_date) if from_date else None
    to_dt: Optional[datetime] = _parse_iso_date(to_date) if to_date else None
    if from_dt is not None and to_dt is not None and from_dt > to_dt:
        raise HTTPException(
            status_code=400,
            detail="from_date must be on or before to_date",
        )

    if all_:
        # Each table commits on its own, so a failure part-way leaves the
        # earlier tables empty; _audit_partial_clear records that.
        try:
            pv_deleted = _an.delete_all(db, PageView)
            sde_deleted = _an.delete_all(db, SlideDwellEvent)
            ts_deleted = _an.delete_all(db, TourSession)
        except Exception:
            db.rollback()
            logger.exception("analytics clear-all commit failed")
            _audit_partial_clear(admin, "analytics.clear", scope="all", from_date=None, to_date=None)
            raise api_error(500, "adminDash.apiErr.analyticsDeleteFailed")
        logger.warning(
            "admin cleared all analytics "
            "(page_views=%d, slide_dwell_events=%d, tour_sessions=%d)",
            pv_deleted, sde_deleted, ts_deleted,
        )
        audit(
            admin, "analytics.clear",
            from_date=None, to_date=None, scope="all",
            deleted_page_views=int(pv_deleted),
            deleted_slide_dwell_events=int(sde_deleted),
            deleted_tour_sessions=int(ts_deleted),
        )
        return _clear_result(pv_deleted, sde_deleted, ts_deleted, complete=True)

    # Build inclusive-on-both-ends predicates. The upper cutoff is
    # midnight of (to_date + 1) so the last second of to_date is still
    # eligible for deletion.
    lower_utc = from_dt.astimezone(timezone.utc) if from_dt is not None else None
    upper_dt = (to_dt + timedelta(days=1)) if to_dt is not None else None
    upper_utc = upper_dt.astimezone(timezone.utc) if upper_dt is not None else None

    def _bounds(col):
        # utc_ts_bound: see _window_filter (boundary-second rows).
        clauses = []
        if lower_utc is not None:
            clauses.append(col >= utc_ts_bound(lower_utc))
        if upper_utc is not None:
            clauses.append(col < utc_ts_bound(upper_utc))
        return clauses

    # Chunked deletes commit as they go (see analytics.delete_until), so a
    # failure part-way leaves earlier slices deleted; the call is re-runnable.
    try:
        (pv_deleted, sde_deleted, ts_deleted), complete = _delete_within_budget(db, (
            (PageView, _bounds(PageView.occurred_at)),
            (SlideDwellEvent, _bounds(SlideDwellEvent.started_at)),
            (TourSession, _bounds(TourSession.started_at)),
        ))
    except Exception:
        db.rollback()
        logger.exception("analytics range delete failed")
        _audit_partial_clear(
            admin, "analytics.clear", from_date=from_date or None, to_date=to_date or None
        )
        raise api_error(500, "adminDash.apiErr.analyticsDeleteFailed")
    logger.warning(
        "admin pruned analytics rows in [%s, %s] "
        "(page_views=%d, slide_dwell_events=%d, tour_sessions=%d, complete=%s)",
        from_date or "-inf", to_date or "+inf",
        pv_deleted, sde_deleted, ts_deleted, complete,
    )
    # Destructive + irreversible: record WHO cleared WHAT range and WHEN in the
    # structured audit trail, alongside every other destructive admin action.
    audit(
        admin, "analytics.clear",
        from_date=from_date or None, to_date=to_date or None,
        deleted_page_views=int(pv_deleted),
        deleted_slide_dwell_events=int(sde_deleted),
        deleted_tour_sessions=int(ts_deleted),
        complete=complete,
    )
    return _clear_result(pv_deleted, sde_deleted, ts_deleted, complete=complete)


# How long one ranged clear / user purge request keeps deleting before it
# answers "complete": false for the caller to repeat it. Well inside nginx's
# 60 s proxy timeout: a chunk that has to wait out another writer can take
# SQLite's 5 s busy_timeout on top of its own work. A module constant so tests
# can shrink it.
_CLEAR_TIME_BUDGET_S = 20.0


def _delete_within_budget(db: Session, targets) -> tuple:
    """Chunk-delete ``(model, where-clauses)`` targets in order within
    ``_CLEAR_TIME_BUDGET_S``. Returns ``([rows deleted per target], complete)``;
    a target the budget did not reach counts 0."""
    deadline = time.monotonic() + _CLEAR_TIME_BUDGET_S
    counts = []
    complete = True
    for model, where in targets:
        if not complete:
            counts.append(0)
            continue
        n, complete = _an.delete_until(db, model, *where, deadline=deadline)
        counts.append(n)
    return counts, complete


def _clear_result(pv: int, sde: int, ts: int, *, complete: bool) -> dict:
    return {
        "ok": True,
        "complete": complete,
        "deleted": int(pv),
        "deleted_page_views": int(pv),
        "deleted_slide_dwell_events": int(sde),
        "deleted_tour_sessions": int(ts),
    }


def _audit_partial_clear(admin, action: str, **details) -> None:
    """Record a destructive analytics delete that failed part-way.

    The deletes commit chunk by chunk (see ``analytics.delete_until``), so a
    failure can leave rows already gone. The 500 alone would leave no audit
    trail of that, so say so before it is raised. Never raises itself: it runs
    inside an error path and must not replace the structured 500 with a bare one."""
    try:
        audit(admin, action, partial_failure=True, **details)
    except Exception:
        logger.exception("could not record the partial analytics delete in the audit trail")


# compute_user_hash output: 64 lowercase hex chars (shared with the
# pseudonymize conversion, which must recognise the same digests).
_DIGEST_RE = _an.DIGEST_RE

# Session-subject prefixes of the app-access sign-in modes (routers/app_auth).
_APP_AUTH_MODES = ("basic", "ldap", "google")


# The identifier travels in a form body, not the URL: nginx's access log and
# uvicorn's (into the journal) write the full request line, so a raw email in
# the query string would be kept there in plaintext — the opposite of what an
# erasure is for. Neither the purge nor its preview reads the query string.
_IDENTIFIER_HELP = (
    "User identifier. In pseudonymous mode this is the HMAC hex "
    "digest shown in the top-users table; in plaintext mode it is the "
    "email/username. The server matches it verbatim against "
    "PageView.user_email and the dwell/tour tables' user_identifier "
    "(also with each app-access sign-in prefix, basic: / ldap: / "
    "google:, for a bare name) and, when a hash salt is configured, "
    "also against its digest under the current salt — so a raw "
    "email/username reaches both plaintext and hashed rows."
)


def _identifier_form():
    return Form(..., max_length=255, description=_IDENTIFIER_HELP)


def _identifier_matcher(identifier: str):
    """``(ident, digest, match)`` for a purge / preview ``identifier``.

    ``match(col)`` is the WHERE clause selecting the rows a purge of this
    identifier deletes, ``ident`` the stripped input and ``digest`` its
    hashed-mode form ("" when it was not hashed). The purge and its preview
    share it so the preview lists exactly what the purge would delete.
    """
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=422, detail="identifier must not be empty")
    # App-access sign-ins (basic / ldap / google) are recorded under the session
    # subject "<mode>:<username>", so a bare email or username must also be
    # tried with each mode prefix or an erasure request would report success
    # while deleting nothing. Lower-cased forms cover the case-insensitive
    # identity the hashed mode already uses. (This is also why a bare name
    # reaches several people's rows — "alice" the LDAP user and "alice" the
    # basic-auth user — which the preview endpoint lets the admin see first.)
    candidates = {ident, ident.lower()}
    if not any(ident.lower().startswith(f"{m}:") for m in _APP_AUTH_MODES):
        for m in _APP_AUTH_MODES:
            candidates.add(f"{m}:{ident}")
            candidates.add(f"{m}:{ident.lower()}")
    targets = set(candidates)
    digest = ""
    # An input that already is a digest (copied from the dashboard) is matched
    # as-is; hashing it again could never match a stored row.
    if not _DIGEST_RE.match(ident) and _an_hash.salt_available():
        # The hashed-mode form of the same input, so a raw email/username
        # also reaches rows recorded while the mode was "hashed". (Rows hashed
        # under an earlier salt are only reachable by their stored digest.)
        digest = _an_hash.compute_user_hash(ident)
        if digest:
            targets.add(digest)
        for cand in candidates:
            cand_digest = _an_hash.compute_user_hash(cand)
            if cand_digest:
                targets.add(cand_digest)
    # Plain-mode rows keep the identifier as the IdP/LDAP returned it, whose
    # case the admin typing the request cannot know; compare case-insensitively
    # (digests are lowercase hex, so this does not widen those matches). This
    # is a scan rather than an index lookup, which is fine for a rare erasure.
    lowered = {t.lower() for t in targets}

    def match(col):
        # Exact IN keeps the ASCII/Unicode forms the admin typed (SQLite's
        # lower() only folds ASCII, Python's .lower() folds Unicode, so the
        # lowered set alone would miss "Ünal@…"); the lower() arm adds the
        # case-insensitive ASCII match.
        return or_(col.in_(targets), func.lower(col).in_(lowered))

    return ident, digest, match


# Distinct stored identifiers the preview lists at most.
_PREVIEW_MAX_SUBJECTS = 50


@router.post("/api/admin/analytics/user/preview")
def admin_preview_analytics_user(
    identifier: str = _identifier_form(),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """What ``DELETE /api/admin/analytics/user`` would delete for
    ``identifier``, without deleting anything.

    ``subjects`` are the distinct stored identifiers the purge matches (up to
    ``_PREVIEW_MAX_SUBJECTS``, as stored: plaintext or digest), so an admin
    can see that a bare "alice" also reaches "ldap:alice" and "basic:alice"
    before erasing; the counts are the rows per table. Read-only and neither
    logged nor audited; a POST only so the identifier stays out of the
    access logs (see _IDENTIFIER_HELP).
    """
    _ident, _digest, match = _identifier_matcher(identifier)
    sources = (
        (PageView, PageView.user_email),
        (SlideDwellEvent, SlideDwellEvent.user_identifier),
        (TourSession, TourSession.user_identifier),
    )
    with _query_budget(db):
        # UNION (not UNION ALL) folds a subject recorded in several tables.
        subjects_q = union(*[
            select(col.label("subject")).where(match(col)) for _model, col in sources
        ]).subquery()
        subjects = db.execute(
            select(subjects_q.c.subject).order_by(subjects_q.c.subject)
            .limit(_PREVIEW_MAX_SUBJECTS)
        ).scalars().all()
        counts = [
            int(db.execute(
                select(func.count()).select_from(model).where(match(col))
            ).scalar_one() or 0)
            for model, col in sources
        ]
    return {
        "subjects": list(subjects),
        "page_views": counts[0],
        "slide_dwell_events": counts[1],
        "tour_sessions": counts[2],
    }


@router.delete("/api/admin/analytics/user")
def admin_clear_analytics_user(
    identifier: str = _identifier_form(),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete every analytics row attributed to a single user identifier.

    Intended use cases: GDPR Article 17 ("right to erasure") requests,
    and purging records for off-boarded users. The match is exact: the
    caller must supply the identifier in the form the rows store, which
    in hashed mode means the digest from the dashboard — or the raw
    email/username, which is also hashed under the current salt and matched
    in that form. Rows can carry either form (the mode may have been
    switched since they were written), so both are purged together. See
    ``_identifier_matcher`` for the full set of forms matched, and
    ``POST /api/admin/analytics/user/preview`` to list them first.

    The three event tables are purged in sequence, each in committed chunks
    (see ``analytics.delete_until``). That is NOT atomic: a failure part-way
    leaves earlier chunks gone and answers 500 with a ``partial_failure`` audit
    row; the request is idempotent, so re-running it finishes the job. Like a
    ranged clear it stops after ``_CLEAR_TIME_BUDGET_S`` with
    ``"complete": false`` when rows may remain, for the caller to repeat.
    """
    ident, digest, _match = _identifier_matcher(identifier)
    # What the audit row names: the digest in hashed mode (so a raw email typed
    # into the purge box never lands in the trail), else the identifier. Decided
    # up front - before any row is deleted - so the failure path can use it too.
    audit_target = (
        digest
        if digest and _an.current_analytics_mode(db) == _an.ANALYTICS_HASHED
        else ident
    )

    # Chunked like every other analytics delete: this scans (lower() defeats the
    # index), so one big DELETE would hold the SQLite write lock throughout.
    try:
        (pv_deleted, sde_deleted, ts_deleted), complete = _delete_within_budget(db, (
            (PageView, [_match(PageView.user_email)]),
            (SlideDwellEvent, [_match(SlideDwellEvent.user_identifier)]),
            (TourSession, [_match(TourSession.user_identifier)]),
        ))
    except Exception:
        db.rollback()
        logger.exception("analytics clear-user delete failed")
        _audit_partial_clear(admin, "analytics.purge_user", target=audit_target)
        raise api_error(500, "adminDash.apiErr.analyticsDeleteFailed")
    # The identifier stays out of this line: WARNING records are persisted to
    # error_logs (and so into backups), which would keep the erased user's
    # email around in plaintext mode. The audit event below records it.
    logger.warning(
        "admin purged analytics for one identifier "
        "(page_views=%d, slide_dwell_events=%d, tour_sessions=%d, complete=%s)",
        pv_deleted, sde_deleted, ts_deleted, complete,
    )
    # GDPR erasure: "who erased whose data and when" is exactly what the audit
    # trail must retain. ``target`` is the (already-pseudonymous in hashed mode)
    # identifier the admin supplied — or, in hashed mode, its digest, so a raw
    # email typed into the purge box doesn't land in the audit trail.
    audit(
        admin, "analytics.purge_user", target=audit_target,
        deleted_page_views=int(pv_deleted),
        deleted_slide_dwell_events=int(sde_deleted),
        deleted_tour_sessions=int(ts_deleted),
        complete=complete,
    )
    out = _clear_result(pv_deleted, sde_deleted, ts_deleted, complete=complete)
    out["deleted"] = int(pv_deleted + sde_deleted + ts_deleted)
    return out


# ---------------------------------------------------------------------------
# Admin: pseudonymise rows recorded in plaintext mode
# ---------------------------------------------------------------------------
@router.post("/api/admin/analytics/pseudonymize")
def admin_pseudonymize_analytics(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Convert the rows still holding plaintext identities into their hashed
    form under the current salt — what mode "hashed" would have stored had it
    been on when they were recorded (see ``analytics.pseudonymize_until`` for
    the columns). Switching the mode only changes rows recorded from then on;
    without this the plaintext history stayed readable (and every visitor of
    both periods counted twice in unique IPs / users, raw and hashed).

    Irreversible: the plaintext is gone afterwards. Idempotent, and works in
    committed slices for at most ``_CLEAR_TIME_BUDGET_S`` per call, answering
    ``"complete": false`` while rows may remain; the caller repeats it until
    ``"complete": true``. Without a usable salt it answers 409
    ``analyticsSaltUnavailable`` and changes nothing; likewise 409
    ``analyticsPseudonymizeNeedsHashed`` while the mode is "plain".
    """
    # Salt first: without one the mode cannot be switched to "hashed" either,
    # so that is the refusal the admin can act on.
    if not _an_hash.salt_available():
        raise api_error(409, "adminDash.apiErr.analyticsSaltUnavailable")
    # Not while the mode is "plain": every beacon still records plaintext —
    # new rows, but also the later beacons of a dwell / tour session already
    # open, whose identity would no longer match its converted row: the
    # hijack guards (_merge_dwell_beacon, the tour-event handler) then drop
    # them, silently freezing those sessions. Refused before any work, so the
    # caller knows nothing changed. "off" is fine: beacons are discarded at
    # the mode check before those guards, so an admin who stopped recording
    # can still convert what was kept without turning recording back on.
    if _an.current_analytics_mode(db) == _an.ANALYTICS_PLAIN:
        raise api_error(409, "adminDash.apiErr.analyticsPseudonymizeNeedsHashed")
    deadline = time.monotonic() + _CLEAR_TIME_BUDGET_S
    try:
        updated, complete = _an.pseudonymize_until(db, deadline=deadline)
    except RuntimeError as exc:
        db.rollback()
        raise api_error(409, "adminDash.apiErr.analyticsSaltUnavailable") from exc
    except Exception:
        # Slices commit as they go, so earlier ones stay converted; the call
        # is idempotent and can be re-run.
        db.rollback()
        logger.exception("analytics pseudonymize failed")
        _audit_partial_clear(admin, "analytics.pseudonymize")
        raise api_error(500, "adminDash.apiErr.analyticsPseudonymizeFailed")
    logger.warning(
        "admin pseudonymised plaintext analytics rows "
        "(page_views=%d, slide_dwell_events=%d, tour_sessions=%d, complete=%s)",
        updated["page_views"], updated["slide_dwell_events"], updated["tour_sessions"],
        complete,
    )
    audit(
        admin, "analytics.pseudonymize",
        updated_page_views=int(updated["page_views"]),
        updated_slide_dwell_events=int(updated["slide_dwell_events"]),
        updated_tour_sessions=int(updated["tour_sessions"]),
        complete=complete,
    )
    return {"updated": {k: int(v) for k, v in updated.items()}, "complete": complete}
