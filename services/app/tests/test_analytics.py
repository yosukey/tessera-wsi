"""Analytics beacon + admin endpoint tests.

Beacon endpoint is intentionally permissive (always 204) so behavior is
verified by inspecting the resulting `page_views` rows. Admin endpoints
are tested for both the default (admin/bot-excluded) and "show
everything" code paths.

The conftest test DB is session-scoped — fixtures from other tests can
leave `page_views` rows behind. Each test in this module wipes the
table up front so assertions about absolute counts stay stable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from wsi_app.analytics import (
    classify_user_agent,
    extract_referrer_host,
)
from wsi_app.models import PageView, Slide, Tour


@pytest.fixture(autouse=True)
def _clean_pageviews(db_session):
    db_session.execute(delete(PageView))
    db_session.commit()
    yield
    db_session.execute(delete(PageView))
    db_session.commit()


@pytest.fixture
def beacon_clock(monkeypatch):
    """The server time the beacons receive at: ``beacon_clock.at(t)`` makes
    the following beacons arrive at ``t`` (the real clock until set).

    The beacons anchor send-time stamps to their receipt, so a test simulating
    a session spread over minutes has to deliver each beacon at its moment
    rather than all of them now."""
    from wsi_app.routers import analytics as _ar_mod

    class _Clock:
        now = None

        def at(self, t):
            self.now = t

    clock = _Clock()
    real = _ar_mod._utcnow
    monkeypatch.setattr(_ar_mod, "_utcnow", lambda: clock.now or real())
    return clock


@pytest.fixture
def slide(db_session) -> Slide:
    """A *published* slide (the shared fixture's is a draft): beacons from
    non-staff viewers are recorded for published content only."""
    s = Slide(title="Test Slide", catalog_id="TEST-001", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_classify_user_agent_buckets():
    assert classify_user_agent("Googlebot/2.1 (+http://www.google.com/bot.html)") == "bot"
    assert classify_user_agent("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)") == "tablet"
    assert classify_user_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)") == "mobile"
    assert classify_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120") == "desktop"
    assert classify_user_agent("") == "other"
    assert classify_user_agent("python-requests/2.31") == "bot"


def test_extract_referrer_host_drops_self_and_strips_path():
    assert extract_referrer_host("https://example.com/path?q=1", "wsi.local") == "example.com"
    assert extract_referrer_host("https://wsi.local/foo", "wsi.local") == ""
    assert extract_referrer_host("", "wsi.local") == ""
    assert extract_referrer_host("not a url", "wsi.local") == ""


def test_beacon_top(client, db_session):
    r = client.post("/api/analytics/pageview", json={"kind": "top"})
    assert r.status_code == 204
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "top"
    assert rows[0].slide_id is None
    assert rows[0].tour_id is None
    assert rows[0].user_email == ""


def test_beacon_viewer_with_existing_slide(client, db_session, slide):
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers={"User-Agent": "Mozilla/5.0 (iPhone)"},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].slide_id == slide.id
    assert rows[0].ua_class == "mobile"


def test_beacon_records_payload_referrer(client, db_session):
    """The external referrer is forwarded in the payload (from
    `document.referrer`), not read from the request's own `Referer`
    header — that header always points back at the app's own origin."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "top", "referrer": "https://www.google.com/search?q=histology"},
    )
    assert r.status_code == 204
    row = db_session.execute(select(PageView)).scalar_one()
    # Only the host survives — query string / path are stripped.
    assert row.referrer_host == "www.google.com"


def test_beacon_self_referrer_recorded_as_direct(client, db_session):
    """A same-host referrer is dropped server-side, leaving the row as
    direct traffic."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "top", "referrer": "http://testserver/slides/3"},
        headers={"Host": "testserver"},
    )
    assert r.status_code == 204
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.referrer_host == ""


def test_beacon_ignores_referer_header(client, db_session):
    """The request's own `Referer` header is not consulted; only the
    payload referrer counts."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"Referer": "https://example.com/external"},
    )
    assert r.status_code == 204
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.referrer_host == ""


def test_beacon_unknown_slide_silently_drops(client, db_session):
    r = client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": "no-such-token"})
    assert r.status_code == 204
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 0


def test_beacon_tour_must_match_slide(client, db_session, slide):
    other_slide = Slide(title="Other")
    db_session.add(other_slide)
    db_session.commit()
    db_session.refresh(other_slide)
    tour = Tour(slide_id=slide.id, title="Tour", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    # Mismatched slide_id silently drops.
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "tour", "slide_public_id": other_slide.public_id, "tour_public_id": tour.public_id},
    )
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.execute(select(PageView)).scalars().all() == []

    # Correct match writes a row.
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "tour", "slide_public_id": slide.public_id, "tour_public_id": tour.public_id},
    )
    assert r.status_code == 204
    db_session.expire_all()
    rows = db_session.execute(select(PageView)).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "tour"


def test_beacon_invalid_kind_silently_dropped(client, db_session):
    r = client.post("/api/analytics/pageview", json={"kind": "garbage"})
    assert r.status_code == 204
    assert db_session.execute(select(PageView)).scalars().all() == []


def test_beacon_attaches_admin_identity(client, db_session, admin_user, admin_headers, slide):
    """When the request carries the admin Bearer (direct API call, not the
    SPA beacon), the row is attributed via user_id + user_email. There is
    no separate is_admin flag — the admin is just another named user."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers={**admin_headers, "User-Agent": "Mozilla/5.0 (X11; Linux)"},
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.user_id == admin_user.id
    assert row.user_email == admin_user.username


def test_admin_summary_excludes_bots_by_default(client, admin_headers, slide):
    """Bot UAs are filtered out of aggregates by default. Admin traffic is
    not a separate filter axis — admins show up as named users and
    can be excluded via the per-user filter on the recent log."""
    # 1 anonymous viewer hit
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    # 1 admin hit — counted like any other named user.
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    # 1 bot hit (excluded by default)
    client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={"User-Agent": "Googlebot/2.1"},
    )

    # Default summary excludes staff (actor_role IS NOT NULL) rows AND bots:
    # only the anonymous viewer hit remains.
    body = client.get("/api/admin/analytics/summary?days=30", headers=admin_headers).json()
    assert body["totals"]["viewer"] == 1
    assert body["totals"]["top"] == 0  # bot row filtered out

    # include_staff=1 brings the admin row back; include_bots=1 brings the bot row back.
    body = client.get(
        "/api/admin/analytics/summary?days=30&include_bots=1&include_staff=1",
        headers=admin_headers,
    ).json()
    assert body["totals"]["viewer"] == 2
    assert body["totals"]["top"] == 1


def test_admin_top_slides_resolves_titles(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    body = client.get("/api/admin/analytics/top-slides?days=30", headers=admin_headers).json()
    assert len(body) == 1
    assert body[0]["slide_id"] == slide.id
    assert body[0]["title"] == slide.title


def test_admin_dashboard_bundles_all_aggregates(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})

    body = client.get("/api/admin/analytics/dashboard?days=30", headers=admin_headers).json()
    assert set(body.keys()) == {
        "summary",
        "top_slides",
        "top_tours",
        "referrers",
        "ua_breakdown",
        "top_users",
        "server_tz",
    }
    assert body["summary"]["totals"]["top"] == 1
    assert body["summary"]["totals"]["viewer"] == 1
    assert any(row["slide_id"] == slide.id for row in body["top_slides"])


def test_admin_endpoints_require_auth(client):
    for path in (
        "/api/admin/analytics/dashboard",
        "/api/admin/analytics/summary",
        "/api/admin/analytics/top-slides",
        "/api/admin/analytics/top-tours",
        "/api/admin/analytics/referrers",
        "/api/admin/analytics/ua-breakdown",
        "/api/admin/analytics/top-users",
        "/api/admin/analytics/recent",
    ):
        assert client.get(path).status_code == 401


def test_admin_recent_filters(client, admin_headers, admin_user, slide):
    """Recent log leaves staff rows out by default, and the user_email filter
    narrows to a single named user."""
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )

    # Default recent excludes staff rows (actor_role IS NOT NULL); only the
    # anonymous "top" row comes back.
    rows = client.get("/api/admin/analytics/recent?limit=10", headers=admin_headers).json()
    assert any(r["kind"] == "top" for r in rows)
    assert not any(r["user_email"] == admin_user.username for r in rows)
    # Response shape: rows carry actor_role, not an is_admin field.
    assert "is_admin" not in rows[0]
    assert "actor_role" in rows[0]

    # include_staff=1 brings the admin-attributed row back.
    rows_all = client.get(
        "/api/admin/analytics/recent?limit=10&include_staff=1", headers=admin_headers
    ).json()
    assert any(r["user_email"] == admin_user.username for r in rows_all)

    # Filter by user_email narrows to that user's rows. Passing user_email
    # bypasses the staff exclusion implicitly is NOT the case (filter is
    # additive), so include_staff=1 is also needed here.
    rows = client.get(
        f"/api/admin/analytics/recent?limit=10&include_staff=1&user_email={admin_user.username}",
        headers=admin_headers,
    ).json()
    assert len(rows) == 1
    assert rows[0]["user_email"] == admin_user.username


def test_admin_clear_analytics_wipes_all_rows(client, admin_headers, db_session, slide):
    # Seed a mix of anonymous + admin rows to verify that the clear
    # endpoint truncates everything, not just one of the filtered subsets.
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    assert db_session.execute(select(PageView)).scalars().all()

    r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] >= 2

    # Subsequent reads see an empty table.
    db_session.expire_all()
    assert db_session.execute(select(PageView)).scalars().all() == []
    rows = client.get(
        "/api/admin/analytics/recent?limit=10", headers=admin_headers
    ).json()
    assert rows == []


def test_admin_clear_analytics_requires_auth(client):
    assert client.delete("/api/admin/analytics/data").status_code == 401


# ---------------------------------------------------------------------------
# Custom date-range support
# ---------------------------------------------------------------------------
def _backdate_pageview(db_session, occurred_at):
    """Seed one PageView at the given timestamp and return its id."""
    from datetime import timezone as _tz
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=_tz.utc)
    pv = PageView(
        kind="top",
        client_ip="10.0.0.1",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
        ua_class="desktop",
    )
    db_session.add(pv)
    db_session.commit()
    db_session.refresh(pv)
    pv.occurred_at = occurred_at
    db_session.add(pv)
    db_session.commit()
    return pv.id


def test_admin_summary_accepts_custom_date_range(client, admin_headers, db_session):
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    # 1 hit ~10 days ago, 1 hit ~40 days ago.
    _backdate_pageview(db_session, now - timedelta(days=10))
    _backdate_pageview(db_session, now - timedelta(days=40))

    # Window spanning the last 20 days picks up only the recent hit.
    start = (now - timedelta(days=20)).date().isoformat()
    end = now.date().isoformat()
    body = client.get(
        f"/api/admin/analytics/summary?start_date={start}&end_date={end}",
        headers=admin_headers,
    ).json()
    assert body["totals"]["top"] == 1
    assert body["start_date"] == start
    assert body["end_date"] == end

    # Wider window that includes both hits.
    start_wide = (now - timedelta(days=60)).date().isoformat()
    body_wide = client.get(
        f"/api/admin/analytics/summary?start_date={start_wide}&end_date={end}",
        headers=admin_headers,
    ).json()
    assert body_wide["totals"]["top"] == 2


def test_admin_summary_rejects_malformed_date(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/summary?start_date=not-a-date",
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_admin_summary_rejects_oversize_range(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/summary?start_date=2000-01-01&end_date=2030-12-31",
        headers=admin_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def test_admin_export_csv_summary(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})

    r = client.get(
        "/api/admin/analytics/export.csv?dataset=summary&days=30",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.text
    # BOM + header row + several metric rows.
    assert body.startswith("﻿") or body.startswith("﻿metric")
    assert "metric,value" in body
    assert "total_views,2" in body
    assert "views_top,1" in body
    assert "views_viewer,1" in body


def test_admin_export_csv_top_slides(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=top-slides&days=30",
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    assert "rank,slide_id,catalog_id,title,views,unique_ips" in body
    assert f",{slide.id}," in body


def test_admin_export_csv_recent(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&days=30",
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    assert "occurred_at,kind,slide_id" in body
    # The single seeded viewer hit appears in the body.
    assert "viewer," in body


def test_admin_export_csv_unknown_dataset_400(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=garbage",
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_admin_export_csv_requires_auth(client):
    assert client.get("/api/admin/analytics/export.csv?dataset=summary").status_code == 401


def test_admin_export_csv_respects_custom_range(client, admin_headers, db_session):
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    _backdate_pageview(db_session, now - timedelta(days=5))
    _backdate_pageview(db_session, now - timedelta(days=50))

    start = (now - timedelta(days=10)).date().isoformat()
    end = now.date().isoformat()
    r = client.get(
        f"/api/admin/analytics/export.csv?dataset=summary&start_date={start}&end_date={end}",
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    assert "total_views,1" in body


def test_csv_preset_window_filename_label_ends_today(client, admin_headers):
    # The preset `?days=N` window runs THROUGH today (until = now),
    # so the download filename's end date must be today. Labelling with
    # `until - 1 day` would mislabel the file as ending *yesterday* while the
    # CSV content (and _compute_summary's end_date) covers today.
    from datetime import datetime

    from wsi_app.admin_reports import analytics_tz

    today = datetime.now(analytics_tz()).date().isoformat()
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=summary&days=7",
        headers=admin_headers,
    )
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert f"_{today}.csv" in cd, cd


def test_csv_explicit_end_date_filename_label_matches_end(client, admin_headers):
    # The explicit start/end path must stay correct: until = end+1day, and the
    # single-microsecond backoff still lands on the requested end date.
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=summary"
        "&start_date=2026-01-01&end_date=2026-01-31",
        headers=admin_headers,
    )
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "analytics-summary-2026-01-01_2026-01-31.csv" in cd, cd


# ---------------------------------------------------------------------------
# Always-204 guarantee for the public beacon
# ---------------------------------------------------------------------------
def test_beacon_returns_204_for_malformed_bodies(client, db_session):
    # Empty body, missing kind, wrong type for kind, plain string, list:
    # none of these may leak a Pydantic 422 to the SPA. The beacon's
    # contract is "always 204"; verify it holds.
    for body in (b"", b"{}", b'{"kind": 123}', b'"top"', b"[]", b"not-json"):
        r = client.post(
            "/api/analytics/pageview",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 204, f"body={body!r} → {r.status_code}"
    assert db_session.execute(select(PageView)).scalars().all() == []


# ---------------------------------------------------------------------------
# Inverted date ranges error out instead of being silently rewritten
# ---------------------------------------------------------------------------
def test_admin_summary_rejects_inverted_range(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/summary?start_date=2025-05-10&end_date=2025-05-01",
        headers=admin_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# CSV formula-injection defang
# ---------------------------------------------------------------------------
def test_csv_export_defangs_formula_cells(client, admin_headers, db_session, slide):
    # Seed a row whose externally-controllable fields start with the
    # spreadsheet-formula trigger characters.
    pv = PageView(
        kind="viewer",
        slide_id=slide.id,
        client_ip="10.0.0.99",
        user_agent="=cmd|'/c calc'!A0",
        ua_class="desktop",
        referrer_host="=HYPERLINK(\"http://x\",\"x\")",
        user_email="+attacker@example.com",
    )
    db_session.add(pv)
    db_session.commit()

    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&days=30",
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    # Each tainted field is prefixed with a leading apostrophe so the
    # spreadsheet renders it as text rather than evaluating it.
    assert "'=cmd|'/c calc'!A0" in body
    assert "'=HYPERLINK" in body
    assert "'+attacker@example.com" in body
    # Sanity: a benign value passes through unchanged.
    assert "10.0.0.99" in body


# ---------------------------------------------------------------------------
# Daily aggregation fills zero-traffic days
# ---------------------------------------------------------------------------
def test_admin_summary_daily_fills_zero_days(client, admin_headers, db_session):
    """Days with no recorded views must appear in `daily` with zero counts.

    Without this, a sparse week (1 view on Mon, 0 the rest) collapses to
    a single row on the dashboard chart and the operator cannot tell
    "no traffic" from "no record".
    """
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    # Seed only two hits inside a 5-day window (today and the four calendar
    # days before it): 1.5 and 3.5 days ago, on day -1/-2 and -3/-4.
    _backdate_pageview(db_session, now - timedelta(days=1, hours=12))
    _backdate_pageview(db_session, now - timedelta(days=3, hours=12))

    body = client.get("/api/admin/analytics/summary?days=5", headers=admin_headers).json()
    daily = body["daily"]
    # Window is today and four days back → exactly 5 rows.
    assert len(daily) == 5
    totals = sum(r["top"] + r["viewer"] + r["tour"] for r in daily)
    assert totals == 2
    # At least one row is a zero-traffic day.
    assert any(r["top"] + r["viewer"] + r["tour"] == 0 for r in daily)
    # Rows are sorted ascending by day.
    days = [r["day"] for r in daily]
    assert days == sorted(days)


# ---------------------------------------------------------------------------
# Recent endpoint rejects invalid `kind` (instead of silently dropping it)
# ---------------------------------------------------------------------------
def test_admin_recent_rejects_invalid_kind(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/recent?kind=Tour",  # capitalised → invalid
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_admin_export_csv_recent_rejects_invalid_kind(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&kind=garbage",
        headers=admin_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Top users excludes the anonymous bucket
# ---------------------------------------------------------------------------
def test_admin_summary_future_start_without_end_gives_clear_error(client, admin_headers):
    """When only `start_date` is provided and it's in the future, the
    400 message should not mention `end_date` (which the caller never
    supplied). A generic "start_date must be on or before
    end_date" string would confuse scripted callers.
    """
    from datetime import date as _date, timedelta as _td
    future = (_date.today() + _td(days=30)).isoformat()
    r = client.get(
        f"/api/admin/analytics/summary?start_date={future}",
        headers=admin_headers,
    )
    assert r.status_code == 400
    detail = (r.json() or {}).get("detail") or ""
    assert "future" in detail.lower()
    assert future in detail


def test_admin_top_users_excludes_anonymous(client, admin_headers, admin_user, slide):
    # 3 anonymous hits + 1 named (admin-attributed) hit. The anonymous
    # bucket would dominate the ranking by view count if it weren't
    # filtered out by the empty-user_email guard inside _compute_top_users.
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )

    # Default excludes staff rows, so the admin-attributed hit is filtered out;
    # passing include_staff=1 brings it back.
    body = client.get(
        "/api/admin/analytics/top-users?days=30&include_staff=1",
        headers=admin_headers,
    ).json()
    assert all(row["user_email"] != "" for row in body)
    # The named admin row is the only remaining bucket.
    assert len(body) == 1
    assert body[0]["user_email"] == admin_user.username


# =====================================================================
# Analytics mode (off / hashed / plain) + dwell / tour-session beacons
# =====================================================================

import datetime as _dt
from wsi_app.db import SessionLocal as _SessionLocal
from wsi_app.models import NetworkSettings as _NetworkSettings, PageView as _PageView, SlideDwellEvent as _SlideDwellEvent, TourSession as _TourSession, Tour as _Tour


def _set_analytics_mode(mode: str):
    db = _SessionLocal()
    try:
        ns = db.get(_NetworkSettings, 1)
        if ns is None:
            ns = _NetworkSettings(id=1, analytics_mode=mode)
            db.add(ns)
        else:
            ns.analytics_mode = mode
        db.commit()
    finally:
        db.close()


def _count_page_views() -> int:
    db = _SessionLocal()
    try:
        return db.query(_PageView).count()
    finally:
        db.close()


def test_beacon_drops_rows_when_mode_off(client, slide):
    """analytics_mode='off' silently drops every beacon."""
    _set_analytics_mode("off")
    before = _count_page_views()
    r = client.post("/api/analytics/pageview", json={"kind": "top"})
    assert r.status_code == 204
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
    )
    assert r.status_code == 204
    assert _count_page_views() == before


def test_beacon_hashes_identifier_in_hashed_mode(client, admin_headers, admin_user, slide):
    """analytics_mode='hashed' stores the HMAC digest instead of the raw username."""
    _set_analytics_mode("hashed")
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        rows = db.query(_PageView).filter(_PageView.kind == "viewer").all()
        assert rows, "hashed-mode beacon should still write a row"
        ident = rows[-1].user_email
        # 64-hex sha256 digest, NOT the raw admin username.
        assert ident != admin_user.username
        assert len(ident) == 64
        assert all(c in "0123456789abcdef" for c in ident)
    finally:
        db.close()


def test_slide_dwell_beacon_writes_row(client, admin_headers, slide):
    """Slide-dwell beacon persists a SlideDwellEvent for authenticated users."""
    # Relative to "now" so the beacon's client-timestamp drift guard
    # (_CLIENT_TS_MAX_DRIFT = 7 days) doesn't reject the row when the suite
    # runs more than a week after a hardcoded calendar date.
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=5)).isoformat()
    r = client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": started,
            "ended_at": ended,
            "dwell_ms": 300_000,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        rows = db.query(_SlideDwellEvent).all()
        assert len(rows) == 1
        assert rows[0].slide_id == slide.id
        assert rows[0].dwell_ms == 300_000
        assert rows[0].user_identifier  # plain mode = admin username
    finally:
        db.close()


def test_slide_dwell_drops_anonymous(client, slide):
    """Anonymous viewers don't record dwell rows even in plain mode."""
    db = _SessionLocal()
    try:
        before = db.query(_SlideDwellEvent).count()
    finally:
        db.close()
    # Relative to "now" so the beacon's client-timestamp drift guard
    # (_CLIENT_TS_MAX_DRIFT = 7 days) doesn't reject the row when the suite
    # runs more than a week after a hardcoded calendar date.
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=5)).isoformat()
    r = client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": started,
            "ended_at": ended,
            "dwell_ms": 300_000,
        },
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        # Anonymous beacon must be a no-op.
        assert db.query(_SlideDwellEvent).count() == before
    finally:
        db.close()


def test_tour_event_lifecycle(client, admin_headers, db_session, slide, beacon_clock):
    """Start -> progress -> complete writes a single TourSession row with
    completed=True at the end."""
    tour = _Tour(slide_id=slide.id, title="T", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    session_id = "12345678-1234-4567-8901-123456789012"
    # Relative to "now" so the beacon's 7-day client-timestamp drift guard
    # doesn't reject the row as the suite ages past a hardcoded date.
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()

    beacon_clock.at(_now - _dt.timedelta(minutes=10))
    r = client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "started_at": started,
            "step_index": 0,
            "total_steps": 3,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204

    beacon_clock.at(_now - _dt.timedelta(minutes=9))
    r = client.post(
        "/api/analytics/tour-event",
        json={
            "event": "progress",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "step_index": 1,
            "total_dwell_ms": 30_000,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204

    completed = (_now - _dt.timedelta(minutes=5)).isoformat()
    beacon_clock.at(_now - _dt.timedelta(minutes=5))
    r = client.post(
        "/api/analytics/tour-event",
        json={
            "event": "complete",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "completed_at": completed,
            "step_index": 2,
            "total_dwell_ms": 300_000,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204

    db = _SessionLocal()
    try:
        rows = db.query(_TourSession).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == session_id
        assert row.completed is True
        assert row.last_step_index == 2
        assert row.total_dwell_ms == 300_000
    finally:
        db.close()


def test_tour_event_complete_validates_completion_time(
    client, admin_headers, db_session, slide, monkeypatch, beacon_clock,
):
    """A ``completed_at`` before ``started_at`` is dropped (like slide-dwell's
    ended < started), and an over-long completion time is capped at
    analytics_max_dwell_ms so one stale tab can't dominate avg_completion_ms."""
    import dataclasses
    from wsi_app.routers import analytics as _an_router

    cap_ms = 60 * 60 * 1000
    monkeypatch.setattr(
        _an_router, "settings",
        dataclasses.replace(_an_router.settings, analytics_max_dwell_ms=cap_ms),
    )
    tour = _Tour(slide_id=slide.id, title="T", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = _now - _dt.timedelta(days=2)

    def _post(sid, event, **extra):
        r = client.post(
            "/api/analytics/tour-event",
            json={"event": event, "session_id": sid,
                  "tour_public_id": tour.public_id, **extra},
            headers=admin_headers,
        )
        assert r.status_code == 204

    early = "33333333-4444-4555-8666-777777777777"
    late = "44444444-5555-4666-8777-888888888888"
    # Each beacon arrives when it was stamped (they are send-time stamps).
    beacon_clock.at(started)
    _post(early, "start", started_at=started.isoformat(), total_steps=2)
    _post(late, "start", started_at=started.isoformat(), total_steps=2)
    # A completion stamped before the start (a client clock set back within
    # the receipt skew) is clock nonsense.
    _post(early, "complete",
          completed_at=(started - _dt.timedelta(minutes=4)).isoformat())
    beacon_clock.at(_now)
    _post(late, "complete", completed_at=_now.isoformat())

    db = _SessionLocal()
    try:
        rows = {
            r.session_id: r
            for r in db.query(_TourSession).filter(
                _TourSession.session_id.in_([early, late])
            )
        }
        assert rows[early].completed is False
        assert rows[early].completed_at is None
        assert rows[late].completed is True
        span = (
            rows[late].completed_at.replace(tzinfo=None)
            - rows[late].started_at.replace(tzinfo=None)
        )
        assert span == _dt.timedelta(milliseconds=cap_ms)
    finally:
        db.close()


def test_tour_event_total_dwell_is_monotonic(client, admin_headers, db_session, slide, beacon_clock):
    """A smaller `total_dwell_ms` in a later progress event MUST NOT walk
    the persisted value backwards. Out-of-order in-flight beacons (the
    server has no ordering guarantee for separate POSTs) or a client-side
    clock jump could otherwise truncate the dwell.
    """
    tour = _Tour(slide_id=slide.id, title="t", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    sid = "11111111-2222-4333-8444-555555555555"
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()

    beacon_clock.at(_now - _dt.timedelta(minutes=10))
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
            "started_at": started, "step_index": 0, "total_steps": 3,
        },
        headers=admin_headers,
    )
    beacon_clock.at(_now)
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "progress", "session_id": sid, "tour_public_id": tour.public_id,
            "step_index": 1, "total_dwell_ms": 60_000,
        },
        headers=admin_headers,
    )
    # Late-arriving beacon with a SMALLER dwell. Must not regress.
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "progress", "session_id": sid, "tour_public_id": tour.public_id,
            "step_index": 1, "total_dwell_ms": 30_000,
        },
        headers=admin_headers,
    )
    db = _SessionLocal()
    try:
        row = db.query(_TourSession).filter(_TourSession.session_id == sid).one()
        assert row.total_dwell_ms == 60_000, (
            f"total_dwell_ms must not regress; got {row.total_dwell_ms}"
        )
    finally:
        db.close()


def test_tour_event_start_rejects_negative_step_index(client, admin_headers, db_session, slide):
    """Negative `step_index` on the start event must be clamped to 0
    rather than persisted verbatim (which would leak through downstream
    progress comparisons).
    """
    tour = _Tour(slide_id=slide.id, title="t", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    sid = "22222222-3333-4444-9555-666666666666"
    started = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
            "started_at": started, "step_index": -5, "total_steps": 3,
        },
        headers=admin_headers,
    )
    db = _SessionLocal()
    try:
        row = db.query(_TourSession).filter(_TourSession.session_id == sid).one()
        assert row.last_step_index == 0, (
            f"negative start step_index must be clamped to 0; got {row.last_step_index}"
        )
    finally:
        db.close()


def test_slide_dwell_drops_unreasonable_timestamps(client, admin_headers, slide):
    """A client clock skewed far into the past or future must not be
    able to plant rows with garbage timestamps in the table.
    """
    db = _SessionLocal()
    try:
        baseline = db.query(_SlideDwellEvent).count()
    finally:
        db.close()
    # 30 days in the future > _CLIENT_TS_MAX_DRIFT (7d).
    far_future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)).isoformat()
    r = client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": far_future,
            "ended_at": far_future,
            "dwell_ms": 1500,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        assert db.query(_SlideDwellEvent).count() == baseline, (
            "rows with timestamps outside the sanity window must be dropped"
        )
    finally:
        db.close()


def _seed_pageview_at(slide_id, occurred_at):
    db = _SessionLocal()
    try:
        row = _PageView(
            kind="viewer",
            slide_id=slide_id,
            client_ip="127.0.0.1",
            user_agent="",
            ua_class="desktop",
            referrer_host="",
            user_email="ancient@example.com",
            occurred_at=occurred_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def test_admin_clear_to_date_inclusive(client, admin_headers, slide):
    """`to_date` is inclusive of the named day's full 24h."""
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    # Old row on the boundary day — must be deleted because the bound is inclusive.
    _seed_pageview_at(
        slide.id,
        _dt.datetime(2020, 12, 31, 23, 30, tzinfo=_dt.timezone.utc),
    )
    db = _SessionLocal()
    try:
        total_before = db.query(_PageView).count()
    finally:
        db.close()

    r = client.delete(
        "/api/admin/analytics/data?to_date=2020-12-31",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    db = _SessionLocal()
    try:
        assert db.query(_PageView).count() == total_before - 1
    finally:
        db.close()


def test_admin_clear_range_keeps_outside_rows(client, admin_headers, slide):
    """A from/to window deletes only rows inside the inclusive range."""
    # Recent row stays.
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    # Row before the window stays.
    _seed_pageview_at(
        slide.id,
        _dt.datetime(2019, 6, 15, tzinfo=_dt.timezone.utc),
    )
    # Two rows inside the window — both go.
    _seed_pageview_at(
        slide.id,
        _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc),
    )
    _seed_pageview_at(
        slide.id,
        _dt.datetime(2020, 6, 30, 12, 0, tzinfo=_dt.timezone.utc),
    )
    db = _SessionLocal()
    try:
        total_before = db.query(_PageView).count()
    finally:
        db.close()

    r = client.delete(
        "/api/admin/analytics/data?from_date=2020-01-01&to_date=2020-12-31",
        headers=admin_headers,
    )
    assert r.status_code == 200
    db = _SessionLocal()
    try:
        assert db.query(_PageView).count() == total_before - 2
    finally:
        db.close()


def test_admin_clear_rejects_inverted_range(client, admin_headers):
    r = client.delete(
        "/api/admin/analytics/data?from_date=2024-02-01&to_date=2024-01-01",
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_admin_tour_completion_endpoint_executes(client, admin_headers, db_session, slide):
    """`/api/admin/analytics/tour-completion` must not 500.

    The handler casts via `Integer` inside SQLAlchemy expressions; the
    name that cast relies on must be imported, or every call raises
    NameError — which the frontend would swallow, rendering "empty" and
    masking the failure. This test exercises the endpoint with a real
    session row so such a failure surfaces here.
    """
    tour = _Tour(slide_id=slide.id, title="t", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    session_id = "abcdef01-2345-4678-9abc-def012345678"
    # Use a fixed past offset from "now" so the window includes the row
    # regardless of when the test runs.
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "started_at": started,
            "step_index": 0,
            "total_steps": 2,
        },
        headers=admin_headers,
    )
    completed = (_now - _dt.timedelta(minutes=7)).isoformat()
    client.post(
        "/api/analytics/tour-event",
        json={
            "event": "complete",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "completed_at": completed,
            "step_index": 1,
            "total_dwell_ms": 180_000,
        },
        headers=admin_headers,
    )

    # Confirm the beacon actually wrote a row before exercising the
    # aggregate endpoint. If this fixture-debug assertion ever fails,
    # the test environment (mode, auth, etc.) is the problem — not the
    # aggregate handler under test.
    db = _SessionLocal()
    try:
        ts_rows = db.query(_TourSession).filter(_TourSession.session_id == session_id).all()
        assert ts_rows and ts_rows[0].completed, (
            f"tour-event beacons should have written a completed row; got {ts_rows!r}"
        )
    finally:
        db.close()

    # Query a wide window so the row's `started_at` (a few minutes before
    # "now") falls inside regardless of the host TZ used to compute "now".
    # include_staff=1: the beacons above were written with the admin JWT, so
    # the rows carry actor_role="admin" and the default aggregation (which
    # filters actor_role IS NULL) would otherwise exclude them.
    r = client.get(
        "/api/admin/analytics/tour-completion?days=365&include_staff=1",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    matching = [row for row in body if row["tour_id"] == tour.id]
    assert matching, f"completed tour should appear in tour-completion; got {body!r}"
    row = matching[0]
    assert row["sessions_total"] >= 1
    assert row["sessions_completed"] >= 1
    assert row["completion_rate"] == pytest.approx(1.0)
    assert row["avg_completion_ms"] > 0


def test_admin_slide_dwell_endpoint_returns_aggregates(client, admin_headers, slide):
    """`/api/admin/analytics/slide-dwell` aggregates SlideDwellEvent rows."""
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=8)).isoformat()
    client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": started,
            "ended_at": ended,
            "dwell_ms": 120_000,
        },
        headers=admin_headers,
    )
    # include_staff=1: the dwell beacon was written with the admin JWT, so
    # the row carries actor_role="admin" and the default aggregation excludes
    # it (parity with the pageview staff-exclusion default).
    r = client.get(
        "/api/admin/analytics/slide-dwell?days=30&include_staff=1",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    matching = [row for row in body if row["slide_id"] == slide.id]
    assert matching
    assert matching[0]["sessions"] >= 1
    assert matching[0]["avg_dwell_ms"] >= 1


def test_admin_clear_user_purges_across_tables(client, admin_headers, admin_user, slide):
    """User-purge wipes the matching rows from all three event tables."""
    # Write a pageview AND a slide-dwell for the admin.
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=9)).isoformat()
    client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": started,
            "ended_at": ended,
            "dwell_ms": 60_000,
        },
        headers=admin_headers,
    )

    r = client.request(
        "DELETE", "/api/admin/analytics/user", data={"identifier": admin_user.username},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_page_views"] >= 1
    assert body["deleted_slide_dwell_events"] >= 1

    db = _SessionLocal()
    try:
        assert db.query(_PageView).filter(_PageView.user_email == admin_user.username).count() == 0
        assert db.query(_SlideDwellEvent).filter(_SlideDwellEvent.user_identifier == admin_user.username).count() == 0
    finally:
        db.close()


def test_admin_clear_user_matches_raw_and_hashed_forms(
    client, admin_headers, db_session, caplog,
):
    """A raw email purges rows stored in either form (rows stored as plaintext
    and hashed-mode digests), a pasted digest still purges its rows, and the
    WARNING line (persisted to error_logs) never carries the identifier."""
    import logging
    from wsi_app import analytics_hash as _ah

    email = "Erase.Me@example.com"
    digest = _ah.compute_user_hash(email)
    assert digest
    other_digest = _ah.compute_user_hash("keep@example.com")
    for ident in (email, digest, other_digest):
        db_session.add(PageView(kind="top", user_email=ident))
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger="wsi_app.analytics"):
        r = client.request(
            "DELETE", "/api/admin/analytics/user", data={"identifier": email},
            headers=admin_headers,
        )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 2
    left = db_session.execute(select(PageView.user_email)).scalars().all()
    assert left == [other_digest]
    purge_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "wsi_app.analytics" and "purged" in rec.getMessage()
    ]
    assert purge_lines
    assert not any(email in m or digest in m for m in purge_lines)

    # A digest copied from the dashboard is matched verbatim.
    r = client.request(
        "DELETE", "/api/admin/analytics/user", data={"identifier": other_digest},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 1


def test_admin_clear_user_matches_app_access_subjects(client, admin_headers, db_session):
    """App-access sign-ins are stored as "<mode>:<username>" (plain) or the hash
    of that. A bare email must reach both, or an erasure request reports success
    while deleting nothing."""
    from wsi_app import analytics_hash as _ah

    stored = "google:Alice@Example.com"
    other = "google:bob@example.com"
    rows = [stored, _ah.compute_user_hash(stored), other, _ah.compute_user_hash(other)]
    for ident in rows:
        db_session.add(PageView(kind="top", user_email=ident))
    db_session.commit()

    r = client.request(
        "DELETE", "/api/admin/analytics/user", data={"identifier": "alice@example.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 2
    left = set(db_session.execute(select(PageView.user_email)).scalars().all())
    assert left == {other, _ah.compute_user_hash(other)}


def test_admin_clear_user_matches_non_ascii_identifier(client, admin_headers, db_session):
    """SQLite's lower() folds ASCII only: a stored "Ünal@example.com" must still
    be erased by the identifier exactly as stored (the case-insensitive
    match alone would delete nothing yet report ok)."""
    for ident in ("Ünal@example.com", "keep@example.com"):
        db_session.add(PageView(kind="top", user_email=ident))
    db_session.commit()
    r = client.request(
        "DELETE", "/api/admin/analytics/user", data={"identifier": "Ünal@example.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 1
    left = set(db_session.execute(select(PageView.user_email)).scalars().all())
    assert "Ünal@example.com" not in left and "keep@example.com" in left


def test_partial_analytics_clear_failure_is_audited(client, admin_headers, db_session, monkeypatch):
    """The deletes commit chunk by chunk, so a failure can leave rows gone;
    that must leave an audit row, not just a 500."""
    from wsi_app import analytics as _an
    from wsi_app.models import AuditLog

    db_session.add(PageView(kind="top", user_email="x@example.com"))
    db_session.commit()

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_an, "delete_all", boom)
    r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    assert r.status_code == 500
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "analytics.clear")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None and '"partial_failure": true' in row.details


def test_failed_later_table_in_a_range_clear_is_a_structured_500_and_audited(
    client, admin_headers, db_session, slide
):
    """A range clear commits its page_views chunks before it moves on to the
    dwell table; a failure there must still be the structured 500 with a
    partial_failure audit row."""
    from sqlalchemy import text
    from wsi_app.models import AuditLog

    db_session.execute(text("DROP TRIGGER IF EXISTS boom_dwell"))
    now = _dt.datetime.now(_dt.timezone.utc)
    db_session.add(_SlideDwellEvent(
        slide_id=slide.id, user_identifier="range@example.com",
        started_at=now, ended_at=now, dwell_ms=1,
    ))
    db_session.add(PageView(kind="top", user_email="range@example.com"))
    db_session.commit()
    db_session.execute(text(
        "CREATE TRIGGER boom_dwell BEFORE DELETE ON slide_dwell_events "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    ))
    db_session.commit()
    try:
        before = db_session.query(AuditLog).filter(AuditLog.action == "analytics.clear").count()
        from fastapi.testclient import TestClient
        from wsi_app.main import app

        lenient = TestClient(app, raise_server_exceptions=False)
        r = lenient.delete(
            "/api/admin/analytics/data",
            params={"from_date": "2000-01-01", "to_date": "2100-01-01"},
            headers=admin_headers,
        )
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsDeleteFailed"
        db_session.expire_all()
        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.action == "analytics.clear")
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert len(rows) == before + 1 and '"partial_failure": true' in rows[0].details
        # The page_views chunk committed before the failure.
        assert db_session.query(PageView).count() == 0
    finally:
        db_session.execute(text("DROP TRIGGER IF EXISTS boom_dwell"))
        db_session.execute(delete(_SlideDwellEvent))
        db_session.commit()


def test_slide_dwell_drops_bot_user_agent(client, admin_headers, slide):
    """Bot UAs never persist a SlideDwellEvent. The dwell tables don't
    have a ua_class column, so a bot row would inflate avg_dwell_ms
    with no way to filter it out post-hoc."""
    db = _SessionLocal()
    try:
        before = db.query(_SlideDwellEvent).count()
    finally:
        db.close()
    # Relative to "now" so the beacon's client-timestamp drift guard
    # (_CLIENT_TS_MAX_DRIFT = 7 days) doesn't reject the row when the suite
    # runs more than a week after a hardcoded calendar date.
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=5)).isoformat()
    r = client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "started_at": started,
            "ended_at": ended,
            "dwell_ms": 300_000,
        },
        headers={**admin_headers, "User-Agent": "Googlebot/2.1"},
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        assert db.query(_SlideDwellEvent).count() == before
    finally:
        db.close()


def test_tour_event_drops_bot_user_agent(client, admin_headers, db_session, slide):
    """Bot UAs never create a TourSession row, for the same reason as
    slide-dwell — no ua_class column means no filter knob."""
    tour = _Tour(slide_id=slide.id, title="T", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    session_id = "12345678-1234-4567-8901-aaaaaaaaaaaa"
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    r = client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start",
            "session_id": session_id,
            "tour_public_id": tour.public_id,
            "started_at": started,
            "step_index": 0,
            "total_steps": 3,
        },
        headers={**admin_headers, "User-Agent": "Googlebot/2.1"},
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        assert db.query(_TourSession).filter(_TourSession.session_id == session_id).count() == 0
    finally:
        db.close()


def test_csv_recent_without_window_returns_all_rows(client, admin_headers, db_session, slide):
    """The `recent` CSV honors the same "no implicit window" contract as
    the JSON /api/admin/analytics/recent endpoint: when the caller omits
    every window parameter, rows older than 30 days still come back.
    Aggregate datasets keep the 30-day default since an unbounded summary
    would scan the entire table."""
    # Plant a row 100 days in the past — would be filtered out by a
    # days=30 default but should survive when no window is asked.
    old = _PageView(
        kind="top",
        client_ip="10.0.0.1",
        user_agent="Mozilla/5.0",
        ua_class="desktop",
        occurred_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=100),
    )
    db_session.add(old)
    db_session.commit()

    r = client.get("/api/admin/analytics/export.csv?dataset=recent", headers=admin_headers)
    assert r.status_code == 200
    body = r.text
    # Header + at least one data row containing the seeded IP.
    assert "occurred_at" in body
    assert "10.0.0.1" in body, "row outside the 30-day window must still appear"
    # Filename label reflects the "all" window so a side-by-side compare
    # of two downloads doesn't get confused with a date-bounded export.
    assert 'filename="analytics-recent-all.csv"' in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# actor_role recording + filtering
# ---------------------------------------------------------------------------
def test_pageview_records_admin_actor_role(client, db_session, admin_headers, slide):
    """A beacon carrying a staff JWT stamps the row with that role."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.actor_role == "admin"


def test_pageview_records_instructor_actor_role(client, db_session, instructor_headers, slide):
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=instructor_headers,
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.actor_role == "instructor"


def test_pageview_anonymous_has_null_actor_role(client, db_session, slide):
    """No Authorization header -> actor_role IS NULL."""
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.actor_role is None


def test_staff_beacon_resolves_user_once(
    client, db_session, admin_headers, admin_user, slide, monkeypatch
):
    """A staff direct-API beacon resolves the forwarded bearer to a
    User row exactly once — identity attribution reuses the row resolved for
    actor_role (the resolve_staff_user hint) instead of decoding the bearer
    and hitting `users` a second time."""
    import wsi_app.routers.analytics as _an_router

    calls = {"resolve": 0}
    real_resolve = _an_router.resolve_staff_user

    def _counting_resolve(token, db):
        calls["resolve"] += 1
        return real_resolve(token, db)

    monkeypatch.setattr(_an_router, "resolve_staff_user", _counting_resolve)

    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    # Both identity and role come from the single resolved staff user.
    assert row.actor_role == "admin"
    assert row.user_id == admin_user.id
    assert row.user_email == admin_user.username
    assert calls["resolve"] == 1


def test_revoked_staff_beacon_is_not_credited_to_the_reused_user_id(
    client, db_session, admin_headers, slide
):
    """A deleted user's still-unexpired token must not attribute a beacon to
    the account that now holds the same user id: identity comes only from a
    bearer that resolve_staff_user accepts (tv + liveness), never from the
    token's bare ``sub``."""
    from wsi_app.auth import create_token
    from wsi_app.models import User

    r = client.post(
        "/api/admin/users",
        json={"username": "beacon-gone", "password": "password123", "role": "instructor"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    gone_id = r.json()["id"]
    stale_token = create_token(db_session.get(User, gone_id))
    assert client.delete(f"/api/admin/users/{gone_id}", headers=admin_headers).status_code == 200
    r = client.post(
        "/api/admin/users",
        json={"username": "beacon-heir", "password": "password123", "role": "admin"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    # SQLite hands the freed max id to the next row; the scenario needs that.
    assert r.json()["id"] == gone_id

    db_session.execute(delete(PageView))
    db_session.commit()
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers={"Authorization": f"Bearer {stale_token}"},
    )
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.execute(select(PageView)).scalar_one()
    assert row.user_id is None
    assert row.user_email in (None, "")
    # Still tagged as a staff browser from the signed role claim, but as the
    # role the token was minted with, not the heir's.
    assert row.actor_role == "instructor"


def test_summary_default_excludes_staff_rows(client, admin_headers, slide):
    """Default ``include_staff=False`` filters out rows whose actor_role is
    set (i.e. staff QA traversals). Only learner / anonymous rows count."""
    # 1 anonymous + 1 admin viewer hit.
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    body = client.get("/api/admin/analytics/summary?days=30", headers=admin_headers).json()
    assert body["totals"]["viewer"] == 1


def test_summary_include_staff_brings_staff_rows_back(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    body = client.get(
        "/api/admin/analytics/summary?days=30&include_staff=1",
        headers=admin_headers,
    ).json()
    assert body["totals"]["viewer"] == 2


def test_recent_actor_role_filter_admin(client, admin_headers, instructor_headers, slide):
    """``actor_role=admin`` returns only admin-attributed rows. Per
    handler comments, supplying actor_role implies include_staff=1, so the
    caller doesn't have to AND it in."""
    # Mix of anonymous, admin and instructor rows.
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=instructor_headers,
    )

    rows = client.get(
        "/api/admin/analytics/recent?limit=20&actor_role=admin",
        headers=admin_headers,
    ).json()
    assert rows, "should return at least the admin-attributed row"
    assert all(r["actor_role"] == "admin" for r in rows)


def test_recent_actor_role_filter_none_returns_anonymous(client, admin_headers, slide):
    """``actor_role=none`` matches rows where actor_role IS NULL (anonymous
    / learner traffic)."""
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    rows = client.get(
        "/api/admin/analytics/recent?limit=20&actor_role=none",
        headers=admin_headers,
    ).json()
    assert rows
    assert all(r["actor_role"] is None for r in rows)


def test_recent_actor_role_rejects_invalid_value(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/recent?limit=10&actor_role=superuser",
        headers=admin_headers,
    )
    assert r.status_code == 400


def _csv_recent_rows(text):
    """Parse a recent-dataset CSV body into a list of dict rows."""
    import csv as _csv
    import io as _io

    # Strip the UTF-8 BOM the exporter prepends for Excel.
    reader = _csv.DictReader(_io.StringIO(text.lstrip("﻿")))
    return list(reader)


def test_csv_recent_actor_role_filter_matches_displayed_view(
    client, admin_headers, instructor_headers, slide
):
    """A CSV export of the recent log must honour the actor_role filter the
    same way the JSON /recent endpoint does. The CSV endpoint
    must declare actor_role, or the frontend's filter is silently dropped
    and — because the default staff-exclusion clause still applies —
    exporting an "instructor"-filtered view returns zero staff rows, the
    opposite of what the admin saw on screen."""
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=instructor_headers,
    )

    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&actor_role=instructor",
        headers=admin_headers,
    )
    assert r.status_code == 200
    rows = _csv_recent_rows(r.text)
    assert rows, "instructor-attributed row should be exported"
    assert all(row["actor_role"] == "instructor" for row in rows)


def test_csv_recent_actor_role_none_returns_anonymous(client, admin_headers, slide):
    client.post("/api/analytics/pageview", json={"kind": "top"})
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers=admin_headers,
    )
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&actor_role=none",
        headers=admin_headers,
    )
    assert r.status_code == 200
    rows = _csv_recent_rows(r.text)
    assert rows
    assert all(row["actor_role"] == "" for row in rows)


def test_csv_recent_actor_role_rejects_invalid_value(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&actor_role=superuser",
        headers=admin_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Slide-dwell session reconciliation + staff actor_role parity
# ---------------------------------------------------------------------------
def test_slide_dwell_session_reconciles_into_one_row(client, admin_headers, slide, beacon_clock):
    """Two beacons sharing a session_id collapse into a single row whose
    dwell_ms is the max of the reports (the visibility/pagehide/unmount
    flushes of one viewer-page session). Firing only once on first hidden
    would lose dwell after a tab switch."""
    sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()

    def _beacon(dwell_ms, ended_offset_min):
        # Delivered as it is stamped.
        beacon_clock.at(_now - _dt.timedelta(minutes=ended_offset_min))
        return client.post(
            "/api/analytics/slide-dwell",
            json={
                "slide_public_id": slide.public_id,
                "session_id": sid,
                "started_at": started,
                "ended_at": (_now - _dt.timedelta(minutes=ended_offset_min)).isoformat(),
                "dwell_ms": dwell_ms,
            },
            headers=admin_headers,
        )

    # First flush (e.g. visibilitychange-hidden) then a larger final flush
    # (unmount) — and a stale, smaller out-of-order report in between.
    assert _beacon(180_000, 7).status_code == 204
    assert _beacon(480_000, 2).status_code == 204
    assert _beacon(120_000, 1).status_code == 204  # smaller: must not regress

    db = _SessionLocal()
    try:
        rows = db.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).all()
        assert len(rows) == 1, f"session must collapse into one row; got {rows!r}"
        assert rows[0].dwell_ms == 480_000, "dwell_ms must hold the max report"
    finally:
        db.close()


def test_slide_dwell_records_staff_actor_role(client, admin_headers, slide):
    """A dwell beacon carrying a staff JWT stamps actor_role so the default
    aggregation can exclude staff QA traversals — parity with PageView."""
    sid = "11111111-2222-4333-8444-555555555555"
    _now = _dt.datetime.now(_dt.timezone.utc)
    r = client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": slide.public_id,
            "session_id": sid,
            "started_at": (_now - _dt.timedelta(minutes=10)).isoformat(),
            "ended_at": (_now - _dt.timedelta(minutes=5)).isoformat(),
            "dwell_ms": 300_000,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        row = db.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).one()
        assert row.actor_role == "admin"
    finally:
        db.close()


def test_slide_dwell_aggregate_excludes_staff_by_default(client, admin_headers, db_session):
    """An admin-attributed dwell row (actor_role set) is filtered out of the
    default slide-dwell aggregate and reappears with include_staff=1."""
    # Isolate on a fresh slide so other tests' rows can't mask the result.
    s = Slide(title="dwell-staff", catalog_id="DS-1", wsi_filename="x", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    _now = _dt.datetime.now(_dt.timezone.utc)
    client.post(
        "/api/analytics/slide-dwell",
        json={
            "slide_public_id": s.public_id,
            "session_id": "99999999-8888-4777-8666-555544443333",
            "started_at": (_now - _dt.timedelta(minutes=10)).isoformat(),
            "ended_at": (_now - _dt.timedelta(minutes=5)).isoformat(),
            "dwell_ms": 300_000,
        },
        headers=admin_headers,
    )

    default = client.get(
        "/api/admin/analytics/slide-dwell?days=30", headers=admin_headers
    ).json()
    assert not [row for row in default if row["slide_id"] == s.id], (
        "staff dwell row must be excluded by default"
    )
    with_staff = client.get(
        "/api/admin/analytics/slide-dwell?days=30&include_staff=1", headers=admin_headers
    ).json()
    assert [row for row in with_staff if row["slide_id"] == s.id], (
        "include_staff=1 must bring the staff dwell row back"
    )


def test_tour_session_records_staff_actor_role(client, admin_headers, db_session, slide):
    """The tour-event start beacon stamps actor_role from a staff JWT."""
    tour = _Tour(slide_id=slide.id, title="actor-role-tour", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    sid = "abcdabcd-1234-4abc-8def-abcabcabcabc"
    _now = _dt.datetime.now(_dt.timezone.utc)
    r = client.post(
        "/api/analytics/tour-event",
        json={
            "event": "start",
            "session_id": sid,
            "tour_public_id": tour.public_id,
            "started_at": (_now - _dt.timedelta(minutes=10)).isoformat(),
            "step_index": 0,
            "total_steps": 2,
        },
        headers=admin_headers,
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        row = db.query(_TourSession).filter(_TourSession.session_id == sid).one()
        assert row.actor_role == "admin"
    finally:
        db.close()


def test_clear_analytics_emits_audit(client, admin_headers, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="wsi_app.audit"):
        r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any("analytics.clear" in rec.getMessage() for rec in caplog.records), \
        "destructive analytics clear must be recorded in the audit trail"


def test_purge_analytics_user_emits_audit(client, admin_headers, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="wsi_app.audit"):
        r = client.request("DELETE", "/api/admin/analytics/user", data={"identifier": "someone"},
                           headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any("analytics.purge_user" in rec.getMessage() for rec in caplog.records), \
        "GDPR erasure must be recorded in the audit trail"


def test_csv_export_rejects_actor_role_on_aggregate(client, admin_headers):
    # An actor_role filter on an aggregate dataset would silently return
    # staff-excluded numbers, so it is refused rather than honoured.
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=top-users&actor_role=instructor",
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text


def test_csv_export_allows_actor_role_on_recent(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&actor_role=instructor",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


def test_preset_summary_end_date_is_today(client, admin_headers):
    r = client.get("/api/admin/analytics/summary?days=7", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # The preset window runs up to "now", so the inclusive end date the UI
    # shows must be today, matching the last daily bucket — not yesterday.
    assert body["end_date"] == body["daily"][-1]["day"]


# ---------------------------------------------------------------------------
# retention pruning across the three analytics event tables
# (prune_analytics) — mirrors test_error_logs.py / the audit prune tests.
# ---------------------------------------------------------------------------
from wsi_app import analytics as _analytics  # noqa: E402


def _wipe_analytics_tables(db):
    for _model in (_PageView, _SlideDwellEvent, _TourSession):
        db.execute(delete(_model))
    db.commit()


def _seed_dwell(db, *, ident: str, started_at) -> int:
    row = _SlideDwellEvent(
        slide_id=1,
        session_id=None,
        user_identifier=ident,
        started_at=started_at,
        ended_at=started_at,
        dwell_ms=1000,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _seed_tour(db, *, sid: str, started_at) -> int:
    row = _TourSession(
        session_id=sid,
        tour_id=1,
        user_identifier="u",
        started_at=started_at,
        last_step_index=0,
        total_steps=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def test_prune_analytics_row_cap_keeps_newest_per_table(db_session):
    """The row-cap keeps only the newest N rows of EACH of the three tables
    independently (id order = insertion order)."""
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    pv_ids = [_seed_pageview_at(1, now) for _ in range(5)]
    dw_ids = [_seed_dwell(db_session, ident=f"u{i}", started_at=now) for i in range(5)]
    ts_ids = [
        _seed_tour(db_session, sid=f"{i:08d}-1111-4222-8333-444455556666", started_at=now)
        for i in range(5)
    ]

    deleted = _analytics.prune_analytics(db_session, max_rows=2, max_age_days=0)
    db_session.commit()
    # 3 rows dropped from each of the three tables.
    assert deleted == 9

    surviving_pv = db_session.execute(select(_PageView.id)).scalars().all()
    surviving_dw = db_session.execute(select(_SlideDwellEvent.id)).scalars().all()
    surviving_ts = db_session.execute(select(_TourSession.id)).scalars().all()
    assert set(surviving_pv) == set(pv_ids[-2:])
    assert set(surviving_dw) == set(dw_ids[-2:])
    assert set(surviving_ts) == set(ts_ids[-2:])


def test_prune_analytics_age_cap_drops_old_rows_all_tables(db_session):
    """The age-cap drops rows older than N days from every table, keying on
    each table's event-time column (occurred_at / started_at)."""
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    stale = now - _dt.timedelta(days=400)
    fresh = now - _dt.timedelta(days=1)

    old_pv = _seed_pageview_at(1, stale)
    new_pv = _seed_pageview_at(1, fresh)
    old_dw = _seed_dwell(db_session, ident="old", started_at=stale)
    new_dw = _seed_dwell(db_session, ident="new", started_at=fresh)
    old_ts = _seed_tour(db_session, sid="aaaaaaaa-1111-4222-8333-444455556666", started_at=stale)
    new_ts = _seed_tour(db_session, sid="bbbbbbbb-1111-4222-8333-444455556666", started_at=fresh)

    deleted = _analytics.prune_analytics(db_session, max_rows=0, max_age_days=365)
    db_session.commit()
    assert deleted == 3  # one stale row per table

    assert db_session.execute(select(_PageView.id)).scalars().all() == [new_pv]
    assert db_session.execute(select(_SlideDwellEvent.id)).scalars().all() == [new_dw]
    assert db_session.execute(select(_TourSession.id)).scalars().all() == [new_ts]
    # Sanity: the stale ids really are gone.
    assert old_pv not in db_session.execute(select(_PageView.id)).scalars().all()
    assert old_dw not in db_session.execute(select(_SlideDwellEvent.id)).scalars().all()
    assert old_ts not in db_session.execute(select(_TourSession.id)).scalars().all()


def test_prune_analytics_zero_caps_disable(db_session):
    """max_rows=0 and max_age_days=0 disable both dimensions — nothing is
    deleted even for an ancient row."""
    _wipe_analytics_tables(db_session)
    _seed_pageview_at(1, _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))
    _seed_dwell(db_session, ident="u", started_at=_dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))
    _seed_tour(
        db_session,
        sid="cccccccc-1111-4222-8333-444455556666",
        started_at=_dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc),
    )
    deleted = _analytics.prune_analytics(db_session, max_rows=0, max_age_days=0)
    db_session.commit()
    assert deleted == 0
    assert db_session.execute(select(func.count()).select_from(_PageView)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(_SlideDwellEvent)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(_TourSession)).scalar_one() == 1


def test_prune_analytics_reads_configured_caps(db_session):
    """With no explicit args, prune_analytics reads settings.analytics_log_*.
    A tiny configured row cap prunes down to the newest N."""
    from wsi_app.config import settings as _settings
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    pv_ids = [_seed_pageview_at(1, now) for _ in range(4)]

    orig_rows = _settings.analytics_log_max_rows
    orig_age = _settings.analytics_log_max_age_days
    object.__setattr__(_settings, "analytics_log_max_rows", 1)
    object.__setattr__(_settings, "analytics_log_max_age_days", 0)
    try:
        deleted = _analytics.prune_analytics(db_session)
        db_session.commit()
    finally:
        object.__setattr__(_settings, "analytics_log_max_rows", orig_rows)
        object.__setattr__(_settings, "analytics_log_max_age_days", orig_age)
    assert deleted == 3
    assert db_session.execute(select(_PageView.id)).scalars().all() == [pv_ids[-1]]


def test_prune_at_boot_runs_without_error(db_session):
    """The boot-time entrypoint the lifespan janitor calls is safe to invoke
    and commits its own short-lived session."""
    _wipe_analytics_tables(db_session)
    _seed_pageview_at(1, _dt.datetime.now(_dt.timezone.utc))
    # Should not raise; with the default (large) caps it deletes nothing.
    _analytics.prune_at_boot()
    db_session.expire_all()
    assert db_session.execute(select(func.count()).select_from(_PageView)).scalar_one() == 1


# =====================================================================
# Beacon input hardening: non-finite numbers / out-of-range timestamps
# =====================================================================

def _post_raw(client, path: str, body: str, headers=None):
    # Raw body: json.dumps would emit "Infinity", but the attack is the
    # literal "1e999", which Python's json parses to float('inf').
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return client.post(path, content=body, headers=h)


def _unhandled_warnings(caplog):
    return [r for r in caplog.records if r.name == "wsi_app.unhandled"]


@pytest.mark.parametrize("mode", ["plain", "off"])
@pytest.mark.parametrize("value", ["1e999", "-1e999", "1e300"])
def test_slide_dwell_non_finite_dwell_is_a_quiet_204(client, slide, caplog, mode, value):
    """1e999 must not escape as OverflowError → 422 + an error_logs row, even
    with analytics off and no auth — and only for a slide token that exists,
    which would make the status double as an existence oracle."""
    _set_analytics_mode(mode)
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=5)).isoformat()
    statuses = []
    with caplog.at_level("WARNING"):
        for token in (slide.public_id, "no-such-token"):
            body = (
                f'{{"slide_public_id": "{token}", "started_at": "{started}", '
                f'"ended_at": "{ended}", "dwell_ms": {value}}}'
            )
            statuses.append(_post_raw(client, "/api/analytics/slide-dwell", body).status_code)
    assert statuses == [204, 204]
    assert _unhandled_warnings(caplog) == []


def test_slide_dwell_non_finite_dwell_falls_back_to_timestamps(client, admin_headers, slide):
    _now = _dt.datetime.now(_dt.timezone.utc)
    started = (_now - _dt.timedelta(minutes=10)).isoformat()
    ended = (_now - _dt.timedelta(minutes=5)).isoformat()
    body = (
        f'{{"slide_public_id": "{slide.public_id}", "started_at": "{started}", '
        f'"ended_at": "{ended}", "dwell_ms": 1e999}}'
    )
    r = _post_raw(client, "/api/analytics/slide-dwell", body, admin_headers)
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        rows = db.query(_SlideDwellEvent).filter(_SlideDwellEvent.slide_id == slide.id).all()
        assert [row.dwell_ms for row in rows] == [300_000]
    finally:
        db.query(_SlideDwellEvent).delete()
        db.commit()
        db.close()


@pytest.mark.parametrize("ts", [
    "0001-01-01T00:00:00+23:59",
    "9999-12-31T23:59:59-23:59",
])
def test_slide_dwell_out_of_range_timestamp_is_a_quiet_204(client, admin_headers, slide, caplog, ts):
    _now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with caplog.at_level("WARNING"):
        r = client.post(
            "/api/analytics/slide-dwell",
            json={"slide_public_id": slide.public_id, "started_at": ts, "ended_at": _now},
            headers=admin_headers,
        )
    assert r.status_code == 204
    assert _unhandled_warnings(caplog) == []


def test_tour_event_non_finite_numbers_are_a_quiet_204(client, admin_headers, db_session, slide, caplog):
    tour = _Tour(slide_id=slide.id, title="T-inf", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)
    sid = "abcdef01-1234-4567-8901-123456789012"
    started = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    with caplog.at_level("WARNING"):
        r1 = _post_raw(client, "/api/analytics/tour-event", (
            f'{{"event": "start", "session_id": "{sid}", "tour_public_id": "{tour.public_id}", '
            f'"started_at": "{started}", "step_index": 1e999, "total_steps": -1e999}}'
        ), admin_headers)
        r2 = _post_raw(client, "/api/analytics/tour-event", (
            f'{{"event": "progress", "session_id": "{sid}", "tour_public_id": "{tour.public_id}", '
            f'"step_index": 1e999, "total_dwell_ms": 1e999}}'
        ), admin_headers)
    assert (r1.status_code, r2.status_code) == (204, 204)
    assert _unhandled_warnings(caplog) == []
    db = _SessionLocal()
    try:
        row = db.query(_TourSession).filter(_TourSession.session_id == sid).one()
        assert (row.last_step_index, row.total_steps, row.total_dwell_ms) == (0, 0, 0)
    finally:
        db.query(_TourSession).filter(_TourSession.session_id == sid).delete()
        db.commit()
        db.close()


# =====================================================================
# Hashed mode stores nothing that re-identifies the hashed viewer
# =====================================================================

def test_hashed_mode_drops_user_id_raw_ip_and_ua(client, admin_headers, admin_user, slide):
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
    _set_analytics_mode("hashed")
    r = client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers={**admin_headers, "User-Agent": ua},
    )
    assert r.status_code == 204
    db = _SessionLocal()
    try:
        row = db.query(_PageView).filter(_PageView.kind == "viewer").one()
        assert row.user_id is None
        assert row.user_agent == ""
        assert row.ua_class == "mobile"  # the coarse class survives
        # Keyed hash, not the TestClient's raw address.
        assert row.client_ip != "testclient"
        assert len(row.client_ip) == 64
        assert all(c in "0123456789abcdef" for c in row.client_ip)
        assert row.client_ip != row.user_email
    finally:
        db.close()

    # The admin recent-activity view therefore returns none of them either.
    recent = client.get(
        "/api/admin/analytics/recent", params={"include_staff": 1}, headers=admin_headers
    )
    assert recent.status_code == 200, recent.text
    body = recent.json()
    items = body["items"] if isinstance(body, dict) else body
    assert items
    assert items[0]["user_id"] is None
    assert items[0]["user_agent"] == ""
    assert items[0]["client_ip"] == row.client_ip

    # Same address → same digest, so unique-IP counts still work.
    client.post(
        "/api/analytics/pageview",
        json={"kind": "top"},
        headers={**admin_headers, "User-Agent": ua},
    )
    db = _SessionLocal()
    try:
        ips = {r.client_ip for r in db.query(_PageView).all()}
        assert ips == {row.client_ip}
    finally:
        db.close()


def test_plain_mode_keeps_user_id_ip_and_ua(client, admin_headers, admin_user, slide):
    _set_analytics_mode("plain")
    client.post(
        "/api/analytics/pageview",
        json={"kind": "viewer", "slide_public_id": slide.public_id},
        headers={**admin_headers, "User-Agent": "Mozilla/5.0 (Windows NT 10.0)"},
    )
    db = _SessionLocal()
    try:
        row = db.query(_PageView).filter(_PageView.kind == "viewer").one()
        assert row.user_id == admin_user.id
        assert row.user_agent == "Mozilla/5.0 (Windows NT 10.0)"
        assert row.client_ip == "testclient"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# A staff browser whose token expired / was revoked is not a learner
# ---------------------------------------------------------------------------
def _staff_bearer(user, *, exp_offset: int, tv=None, secret=None, **extra) -> dict:
    import time as _time

    import jwt as _jwt

    from wsi_app.auth import JWT_ALG, JWT_SECRET

    now = int(_time.time())
    payload = {
        "sub": str(user.id), "username": user.username, "role": user.role,
        "tv": int(user.token_version or 0) if tv is None else tv,
        "iat": now - 90000, "exp": now + exp_offset, **extra,
    }
    token = _jwt.encode(payload, secret or JWT_SECRET, algorithm=JWT_ALG)
    return {"Authorization": f"Bearer {token}"}


def _pageview_role(client, db_session, headers):
    db_session.execute(delete(PageView))
    db_session.commit()
    r = client.post("/api/analytics/pageview", json={"kind": "top"}, headers=headers)
    assert r.status_code == 204
    db_session.expire_all()
    return db_session.execute(select(PageView)).scalar_one().actor_role


def test_expired_or_revoked_staff_token_still_tags_actor_role(client, db_session, admin_user):
    # Expired (the SPA keeps forwarding the sessionStorage token past its 24 h).
    assert _pageview_role(
        client, db_session, _staff_bearer(admin_user, exp_offset=-60)
    ) == "admin"
    # Revoked by a token_version bump (password change / forced logout).
    assert _pageview_role(
        client, db_session,
        _staff_bearer(admin_user, exp_offset=3600, tv=int(admin_user.token_version or 0) + 1),
    ) == "admin"


def test_unsigned_or_session_bearer_does_not_tag_actor_role(client, db_session, admin_user):
    # Wrong signing key: a forged role claim tags nothing.
    assert _pageview_role(
        client, db_session,
        _staff_bearer(admin_user, exp_offset=-60, secret="not-the-secret-" * 3),
    ) is None
    # An aud-bearing token is an app-access session token, never a staff one.
    assert _pageview_role(
        client, db_session, _staff_bearer(admin_user, exp_offset=-60, aud="app-access"),
    ) is None


def test_window_filter_puts_boundary_second_row_in_its_own_window(db_session):
    """``occurred_at`` is written by ``func.now()`` as fraction-less text, while
    a datetime bind carries ``.000000``; the window bound must compare equal to
    a row stamped at exactly the boundary second so it lands in the window it
    starts (>= since), not the one before it (< until)."""
    import datetime as _d
    from sqlalchemy import text
    from wsi_app.routers.analytics import _window_filter

    db_session.add(PageView(kind="top"))
    db_session.commit()
    # Pin the stamp as CURRENT_TIMESTAMP would write it: no fractional part.
    db_session.execute(text("UPDATE page_views SET occurred_at = '2026-01-01 00:00:00'"))
    db_session.commit()
    boundary = _d.datetime(2026, 1, 1, tzinfo=_d.timezone.utc)
    day = _d.timedelta(days=1)

    def _count(since, until):
        stmt = _window_filter(select(func.count()).select_from(PageView), since, until)
        return db_session.execute(stmt).scalar_one()

    assert _count(boundary, boundary + day) == 1
    assert _count(boundary - day, boundary) == 0


# ---------------------------------------------------------------------------
# Daily buckets use the UTC offset in force at each event,
# and an end date one day before the start is refused
# ---------------------------------------------------------------------------
@pytest.fixture
def new_york_tz():
    """WSI_ANALYTICS_TIMEZONE=America/New_York (a zone with DST) for one test."""
    from zoneinfo import ZoneInfo

    from wsi_app.config import settings

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "America/New_York")
    try:
        yield ZoneInfo("America/New_York")
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)


@pytest.mark.parametrize("utc_ts, local_day", [
    # 23:30 on the local day, in each season: whichever season "now" is in,
    # a single now-sampled offset would push one of these into the next
    # day, outside the requested window.
    ("2026-01-06T04:30:00", "2026-01-05"),  # EST, UTC-5
    ("2026-07-06T03:30:00", "2026-07-05"),  # EDT, UTC-4
])
def test_daily_bucket_uses_the_offset_in_force_at_the_event(
    client, admin_headers, db_session, new_york_tz, utc_ts, local_day,
):
    from datetime import datetime as _datetime

    db_session.execute(delete(PageView))
    db_session.commit()
    _backdate_pageview(db_session, _datetime.fromisoformat(utc_ts))
    body = client.get(
        f"/api/admin/analytics/summary?start_date={local_day}&end_date={local_day}",
        headers=admin_headers,
    ).json()
    assert body["daily"] == [{"day": local_day, "top": 1, "viewer": 0, "tour": 0}]


def test_utc_offset_periods_split_exactly_at_the_transition(new_york_tz):
    from datetime import datetime as _datetime

    from wsi_app.routers import analytics as _ar

    tz = new_york_tz
    periods = _ar._utc_offset_periods(
        _datetime(2026, 3, 1, tzinfo=tz), _datetime(2026, 3, 15, tzinfo=tz),
    )
    # DST began 2026-03-08 02:00 EST = 07:00 UTC.
    assert [(p[1]) for p in periods] == [-5 * 3600, -4 * 3600]
    assert _datetime.fromtimestamp(periods[1][0], _ar.timezone.utc).isoformat() == "2026-03-08T07:00:00+00:00"


def test_daily_rows_add_up_to_the_summary_across_a_transition(client, admin_headers, db_session, new_york_tz):
    from datetime import datetime as _datetime

    db_session.execute(delete(PageView))
    db_session.commit()
    # Late-evening views on both sides of the March transition.
    for ts in ("2026-03-07T04:30:00", "2026-03-08T04:30:00", "2026-03-09T03:30:00", "2026-03-10T03:30:00"):
        _backdate_pageview(db_session, _datetime.fromisoformat(ts))
    body = client.get(
        "/api/admin/analytics/summary?start_date=2026-03-06&end_date=2026-03-09",
        headers=admin_headers,
    ).json()
    by_day = {r["day"]: r["top"] for r in body["daily"]}
    assert by_day == {"2026-03-06": 1, "2026-03-07": 1, "2026-03-08": 1, "2026-03-09": 1}


def test_start_one_day_after_end_is_refused(client, admin_headers):
    """The end date becomes an exclusive next-midnight bound; checked against
    that, a start exactly one day later would equal it and be accepted as an
    empty window reported as one day."""
    r = client.get(
        "/api/admin/analytics/summary?start_date=2026-01-06&end_date=2026-01-05",
        headers=admin_headers,
    )
    assert r.status_code == 400
    r = client.get(
        "/api/admin/analytics/summary?start_date=2026-01-05&end_date=2026-01-05",
        headers=admin_headers,
    )
    assert r.status_code == 200


def test_cubot_handsets_are_not_classified_as_bots():
    from wsi_app.analytics import classify_user_agent

    for ua in (
        "Mozilla/5.0 (Linux; Android 9; CUBOT_X19) AppleWebKit/537.36 Chrome/91 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 11; Cubot P40) AppleWebKit/537.36 Chrome/91 Mobile Safari/537.36",
    ):
        assert classify_user_agent(ua) != "bot"
    assert classify_user_agent("Mozilla/5.0 (compatible; Googlebot/2.1)") == "bot"
    assert classify_user_agent("Mozilla/5.0 (compatible; bingbot/2.0)") == "bot"


# ---------------------------------------------------------------------------
# chunked deletes: the write lock is released between slices
# ---------------------------------------------------------------------------
@pytest.fixture
def commit_counter():
    from sqlalchemy import event

    from wsi_app.db import engine

    n = {"commits": 0}

    def _on_commit(conn):
        n["commits"] += 1

    event.listen(engine, "commit", _on_commit)
    yield n
    event.remove(engine, "commit", _on_commit)


def test_prune_analytics_chunked_counts_match(db_session, monkeypatch, commit_counter):
    """With a tiny chunk the row-cap and age-cap still delete exactly the same
    rows, and do so over several committed slices."""
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 3)
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    old = now - _dt.timedelta(days=400)
    old_ids = [_seed_pageview_at(1, old) for _ in range(7)]
    new_ids = [_seed_pageview_at(1, now) for _ in range(8)]
    dw = [_seed_dwell(db_session, ident=f"u{i}", started_at=now) for i in range(10)]

    commit_counter["commits"] = 0
    deleted = _analytics.prune_analytics(db_session, max_rows=5, max_age_days=365)
    # age: 7 old page_views; cap: page_views 8 new -> 5 (3 dropped), dwell 10 -> 5.
    assert deleted == 7 + 3 + 5
    assert commit_counter["commits"] >= 5  # several slices, not one big statement

    db_session.expire_all()
    assert set(db_session.execute(select(_PageView.id)).scalars()) == set(new_ids[-5:])
    assert not set(old_ids) & set(db_session.execute(select(_PageView.id)).scalars())
    assert set(db_session.execute(select(_SlideDwellEvent.id)).scalars()) == set(dw[-5:])


def test_prune_analytics_chunk_boundary_exact_multiple(db_session, monkeypatch):
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    for _ in range(6):
        _seed_pageview_at(1, now)
    assert _analytics.prune_analytics(db_session, max_rows=2, max_age_days=0) == 4
    assert db_session.execute(select(func.count()).select_from(_PageView)).scalar_one() == 2


def test_admin_clear_all_is_chunked_and_complete(
    client, admin_headers, db_session, monkeypatch, commit_counter
):
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    for _ in range(5):
        _seed_pageview_at(1, now)
    for i in range(3):
        _seed_dwell(db_session, ident=f"u{i}", started_at=now)

    commit_counter["commits"] = 0
    r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_page_views"] == 5
    assert body["deleted_slide_dwell_events"] == 3
    assert commit_counter["commits"] >= 4
    db_session.expire_all()
    for model in (_PageView, _SlideDwellEvent):
        assert db_session.execute(select(func.count()).select_from(model)).scalar_one() == 0


def test_admin_clear_range_is_chunked_and_keeps_outside_rows(
    client, admin_headers, db_session, monkeypatch
):
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    _wipe_analytics_tables(db_session)
    inside = _dt.datetime(2020, 3, 10, 12, tzinfo=_dt.timezone.utc)
    outside = _dt.datetime(2021, 6, 1, 12, tzinfo=_dt.timezone.utc)
    for _ in range(5):
        _seed_pageview_at(1, inside)
    keep = [_seed_pageview_at(1, outside) for _ in range(3)]

    r = client.delete(
        "/api/admin/analytics/data?from_date=2020-03-01&to_date=2020-03-31",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 5
    db_session.expire_all()
    assert set(db_session.execute(select(_PageView.id)).scalars()) == set(keep)


def test_recent_ties_on_occurred_at_break_by_id_desc(client, admin_headers, db_session):
    """Rows sharing a timestamp (second-resolution) must come back newest-id
    first so the limit cutoff is stable instead of an arbitrary subset. SQLite
    often returns ties in rowid order by accident, so the query text is pinned
    as well as the result."""
    import csv
    import io

    from sqlalchemy import event

    from wsi_app.db import engine

    _wipe_analytics_tables(db_session)
    when = _dt.datetime(2030, 1, 1, 12, tzinfo=_dt.timezone.utc)
    ids = []
    for i in range(6):
        row = _PageView(
            kind="viewer", slide_id=None, client_ip="127.0.0.1", user_agent=f"ua-{i}",
            ua_class="desktop", referrer_host="", user_email="", occurred_at=when,
        )
        db_session.add(row)
        db_session.commit()
        ids.append(row.id)

    sql: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        if "FROM page_views" in statement and "ORDER BY" in statement:
            sql.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        r = client.get("/api/admin/analytics/recent?limit=3", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert [x["id"] for x in r.json()] == ids[::-1][:3]

        r = client.get(
            "/api/admin/analytics/export.csv?dataset=recent&limit=3", headers=admin_headers
        )
        assert r.status_code == 200, r.text
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert [x["user_agent"] for x in rows] == ["ua-5", "ua-4", "ua-3"]
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert len(sql) >= 2
    for stmt in sql:
        assert "ORDER BY page_views.occurred_at DESC, page_views.id DESC" in stmt


# ---------------------------------------------------------------------------
# Clear-all truncates, ranged clears / purges stay inside
# a time budget, dashboard passes and query budget, beacon validation
# ---------------------------------------------------------------------------
from wsi_app.routers import analytics as _ar  # noqa: E402


def _capture_page_view_deletes():
    from sqlalchemy import event

    from wsi_app.db import engine

    seen: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        flat = " ".join(statement.split())
        if flat.startswith("DELETE FROM page_views"):
            seen.append(flat)

    event.listen(engine, "before_cursor_execute", _capture)
    return seen, lambda: event.remove(engine, "before_cursor_execute", _capture)


def test_clear_all_is_one_unqualified_delete_per_table(client, admin_headers, db_session, monkeypatch):
    """An unqualified DELETE gets SQLite's truncate optimisation; the chunked
    `WHERE id IN (...)` form maintained every index row by row (~30x slower)."""
    _wipe_analytics_tables(db_session)
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    for i in range(5):
        db_session.add(_PageView(kind="top", user_email=f"u{i}@example.com"))
    db_session.commit()
    seen, stop = _capture_page_view_deletes()
    try:
        r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    finally:
        stop()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complete"] is True and body["deleted_page_views"] == 5
    assert seen == ["DELETE FROM page_views"]
    assert db_session.query(_PageView).count() == 0


def test_ranged_clear_stops_at_the_time_budget_and_resumes(client, admin_headers, db_session, monkeypatch):
    from wsi_app.models import AuditLog

    _wipe_analytics_tables(db_session)
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    monkeypatch.setattr(_ar, "_CLEAR_TIME_BUDGET_S", 0.0)
    when = _dt.datetime(2026, 5, 1, 12, tzinfo=_dt.timezone.utc)
    for i in range(5):
        db_session.add(_PageView(kind="top", user_email=f"r{i}@example.com", occurred_at=when))
    db_session.add(_PageView(kind="top", user_email="outside@example.com",
                             occurred_at=when + _dt.timedelta(days=30)))
    db_session.commit()

    params = {"from_date": "2026-04-30", "to_date": "2026-05-02"}
    first = client.delete("/api/admin/analytics/data", params=params, headers=admin_headers)
    assert first.status_code == 200, first.text
    # One chunk per call once the budget has run out.
    assert first.json()["complete"] is False
    assert first.json()["deleted_page_views"] == 2

    total = 2
    for _ in range(5):
        r = client.delete("/api/admin/analytics/data", params=params, headers=admin_headers)
        assert r.status_code == 200
        total += r.json()["deleted_page_views"]
        if r.json()["complete"]:
            break
    assert r.json()["complete"] is True
    assert total == 5
    left = db_session.execute(select(_PageView.user_email)).scalars().all()
    assert left == ["outside@example.com"]
    audited = (
        db_session.query(AuditLog).filter(AuditLog.action == "analytics.clear")
        .order_by(AuditLog.id.desc()).limit(2).all()
    )
    assert '"complete": true' in audited[0].details
    assert '"complete": false' in audited[1].details


def test_user_purge_stops_at_the_time_budget_and_resumes(client, admin_headers, db_session, monkeypatch):
    _wipe_analytics_tables(db_session)
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    monkeypatch.setattr(_ar, "_CLEAR_TIME_BUDGET_S", 0.0)
    for _ in range(3):
        db_session.add(_PageView(kind="top", user_email="gone@example.com"))
    db_session.add(_PageView(kind="top", user_email="kept@example.com"))
    db_session.commit()

    params = {"identifier": "gone@example.com"}
    r = client.request("DELETE", "/api/admin/analytics/user", data=params, headers=admin_headers)
    assert r.status_code == 200 and r.json()["complete"] is False
    r = client.request("DELETE", "/api/admin/analytics/user", data=params, headers=admin_headers)
    assert r.status_code == 200 and r.json()["complete"] is True
    left = db_session.execute(select(_PageView.user_email)).scalars().all()
    assert left == ["kept@example.com"]


def test_dashboard_sections_agree_with_the_rows(client, admin_headers, db_session):
    """The folded passes (per-day counts feeding totals / daily / UA, the
    top-users grouping feeding unique_users, GROUP BY-based distinct counts)
    must still describe the same rows."""
    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = [
        ("top", "1.1.1.1", "a@x", "desktop"),
        ("top", "1.1.1.1", "a@x", "mobile"),
        ("viewer", "2.2.2.2", "b@x", ""),        # empty class counts as "other"
        ("viewer", "3.3.3.3", "", "tablet"),
        ("tour", "3.3.3.3", "b@x", "desktop"),
        ("top", "9.9.9.9", "bot@x", "bot"),      # excluded by default
    ]
    for kind, ip, email, ua in rows:
        db_session.add(_PageView(kind=kind, client_ip=ip, user_email=email, ua_class=ua,
                                 occurred_at=now - _dt.timedelta(hours=1)))
    db_session.commit()

    body = client.get("/api/admin/analytics/dashboard?days=7", headers=admin_headers).json()
    s = body["summary"]
    assert s["totals"] == {"top": 2, "viewer": 2, "tour": 1}
    assert {k: sum(d[k] for d in s["daily"]) for k in ("top", "viewer", "tour")} == s["totals"]
    assert s["unique_ips"] == 3
    assert s["unique_users"] == 2
    assert {r["ua_class"]: r["views"] for r in body["ua_breakdown"]} == {
        "desktop": 2, "mobile": 1, "tablet": 1, "other": 1,
    }
    assert {u["user_email"]: u["views"] for u in body["top_users"]} == {"a@x": 2, "b@x": 2}
    # The standalone endpoints compute the same numbers on their own.
    alone = client.get("/api/admin/analytics/summary?days=7", headers=admin_headers).json()
    assert alone == s
    ua = client.get("/api/admin/analytics/ua-breakdown?days=7&include_bots=1", headers=admin_headers).json()
    assert {r["ua_class"]: r["views"] for r in ua}["bot"] == 1


def test_report_queries_past_the_time_budget_answer_503(client, admin_headers, db_session, monkeypatch):
    _wipe_analytics_tables(db_session)
    db_session.add(_PageView(kind="top", client_ip="1.1.1.1"))
    db_session.commit()
    monkeypatch.setattr(_ar, "_QUERY_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(_ar, "_QUERY_BUDGET_CHECK_STEPS", 1)
    r = client.get("/api/admin/analytics/dashboard?days=7", headers=admin_headers)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsQueryTimeout"
    r = client.get("/api/admin/analytics/export.csv?dataset=summary&days=7", headers=admin_headers)
    assert r.status_code == 503
    # The handler does not stay on the pooled connection.
    monkeypatch.setattr(_ar, "_QUERY_TIME_BUDGET_S", 50.0)
    monkeypatch.setattr(_ar, "_QUERY_BUDGET_CHECK_STEPS", 100_000)
    r = client.get("/api/admin/analytics/dashboard?days=7", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["summary"]["totals"]["top"] == 1


def test_query_budget_clears_its_handler_before_the_connection_is_released(monkeypatch):
    """The timeout path's rollback hands the connection back to the pool.
    Any statement run on it from then on — another request's, the moment it
    checks the connection out — must not be interrupted by this request's
    expired deadline."""
    from fastapi import HTTPException
    from sqlalchemy import text

    heavy = text(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000)"
        " SELECT count(*) FROM c"
    )
    monkeypatch.setattr(_ar, "_QUERY_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(_ar, "_QUERY_BUDGET_CHECK_STEPS", 1000)
    db = _SessionLocal()
    raw = db.connection().connection.driver_connection
    after_release = {}
    real_rollback = db.rollback

    def rollback_then_reuse():
        real_rollback()
        # Whoever checks the connection out next runs on this same handle.
        after_release["count"] = raw.execute(str(heavy)).fetchone()[0]

    monkeypatch.setattr(db, "rollback", rollback_then_reuse)
    try:
        with pytest.raises(HTTPException) as caught:
            with _ar._query_budget(db):
                db.execute(heavy).scalar()
        assert caught.value.status_code == 503
    finally:
        db.close()
    assert after_release["count"] == 200000


def test_query_budget_never_touches_the_connection_after_releasing_it(monkeypatch):
    """Once the timeout path's rollback has checked the connection back in,
    another request may be mid-statement on it; set_progress_handler would
    then wait on SQLite's connection mutex while holding the GIL (stalling
    the worker, or deadlocking against that request's own handler)."""
    import types

    from fastapi import HTTPException
    from sqlalchemy import event, text

    from wsi_app.db import engine

    heavy = text(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000)"
        " SELECT count(*) FROM c"
    )
    monkeypatch.setattr(_ar, "_QUERY_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(_ar, "_QUERY_BUDGET_CHECK_STEPS", 1000)
    log = []

    def on_checkin(dbapi_conn, record):
        log.append("checkin")

    db = _SessionLocal()
    raw = db.connection().connection.driver_connection

    class RecordingHandle:
        def set_progress_handler(self, *args):
            log.append("set_progress_handler")
            return raw.set_progress_handler(*args)

    handle = types.SimpleNamespace(connection=types.SimpleNamespace(driver_connection=RecordingHandle()))
    monkeypatch.setattr(db, "connection", lambda *a, **k: handle)
    event.listen(engine, "checkin", on_checkin)
    try:
        with pytest.raises(HTTPException) as caught:
            with _ar._query_budget(db):
                db.execute(heavy).scalar()
        assert caught.value.status_code == 503
    finally:
        event.remove(engine, "checkin", on_checkin)
        db.close()
    assert "checkin" in log
    assert "set_progress_handler" not in log[log.index("checkin"):]
    assert log.count("set_progress_handler") == 2  # installed, then cleared


def test_beacons_skip_identity_work_when_analytics_is_off(client, admin_headers, slide, monkeypatch):
    calls = []
    monkeypatch.setattr(_analytics, "probe_identity", lambda *a, **k: calls.append("probe") or (None, ""))
    monkeypatch.setattr(_ar, "resolve_staff_user", lambda *a, **k: calls.append("staff"))
    monkeypatch.setattr(_ar, "_resolve_slide", lambda *a, **k: calls.append("slide"))
    _set_analytics_mode("off")
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id},
                    headers=admin_headers)
        client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id, "started_at": now.isoformat(),
            "ended_at": now.isoformat(), "dwell_ms": 1,
        }, headers=admin_headers)
        client.post("/api/analytics/tour-event", json={
            "event": "start", "session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "tour_public_id": "x", "started_at": now.isoformat(),
        }, headers=admin_headers)
    finally:
        _set_analytics_mode("plain")
    assert calls == []


def test_hashing_failure_warns_once_per_process(client, admin_headers, slide, monkeypatch, caplog):
    import logging

    from wsi_app import analytics_hash as _ah

    monkeypatch.setattr(_ah, "_configured_salt", None)
    monkeypatch.setattr(_ah, "_ephemeral_salt", None)
    _analytics._hash_failed_warning.reset()
    _set_analytics_mode("hashed")
    try:
        with caplog.at_level(logging.WARNING, logger="wsi_app.analytics"):
            for _ in range(4):
                client.post("/api/analytics/pageview",
                            json={"kind": "viewer", "slide_public_id": slide.public_id},
                            headers=admin_headers)
    finally:
        _set_analytics_mode("plain")
    assert sum("identifier hashing failed" in m for m in caplog.messages) == 1


def test_warning_limiter_reports_what_it_held_back(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(_analytics.time, "monotonic", lambda: clock[0])
    lim = _analytics._WarningLimiter(600)
    assert lim.allow() == 0
    assert lim.allow() is None and lim.allow() is None
    clock[0] += 601
    assert lim.allow() == 2
    once = _analytics._WarningLimiter(None)
    assert once.allow() == 0
    clock[0] += 10_000
    assert once.allow() is None


def test_invalid_cf_jwt_beacon_warning_is_rate_limited(caplog, db_session, monkeypatch):
    import logging

    monkeypatch.setattr(_analytics._cf_access, "cf_jwt_status", lambda *a, **k: ("invalid", None))
    _analytics._cf_invalid_warning.reset()
    with caplog.at_level(logging.WARNING, logger="wsi_app.analytics"):
        for _ in range(5):
            assert _analytics.probe_identity(None, {}, "203.0.113.9", db_session) == (None, "")
    assert sum("CF Access JWT verification failed" in m for m in caplog.messages) == 1


def test_tour_progress_and_complete_must_name_the_sessions_tour(client, admin_headers, db_session, slide):
    t1 = _Tour(slide_id=slide.id, title="A", is_published=True)
    t2 = _Tour(slide_id=slide.id, title="B", is_published=True)
    db_session.add_all([t1, t2])
    db_session.commit()
    sid = "abcdefab-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    now = _dt.datetime.now(_dt.timezone.utc)
    assert client.post("/api/analytics/tour-event", json={
        "event": "start", "session_id": sid, "tour_public_id": t1.public_id,
        "started_at": (now - _dt.timedelta(minutes=5)).isoformat(), "total_steps": 3,
    }, headers=admin_headers).status_code == 204
    client.post("/api/analytics/tour-event", json={
        "event": "progress", "session_id": sid, "tour_public_id": t2.public_id,
        "step_index": 2, "total_dwell_ms": 99_000,
    }, headers=admin_headers)
    client.post("/api/analytics/tour-event", json={
        "event": "complete", "session_id": sid, "tour_public_id": t2.public_id,
        "completed_at": now.isoformat(), "step_index": 2,
    }, headers=admin_headers)
    db_session.expire_all()
    row = db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()
    assert (row.tour_id, row.last_step_index, row.total_dwell_ms, row.completed) == (t1.id, 0, 0, False)
    # The session's own tour still updates it.
    client.post("/api/analytics/tour-event", json={
        "event": "progress", "session_id": sid, "tour_public_id": t1.public_id,
        "step_index": 1, "total_dwell_ms": 1_000,
    }, headers=admin_headers)
    db_session.expire_all()
    assert db_session.get(_TourSession, row.id).last_step_index == 1


def test_session_id_with_a_trailing_newline_is_not_a_session_id(client, admin_headers, db_session, slide):
    now = _dt.datetime.now(_dt.timezone.utc)
    sid = "0123456789abcdef0123456789abcdef"
    client.post("/api/analytics/slide-dwell", json={
        "slide_public_id": slide.public_id, "session_id": sid + "\n",
        "started_at": (now - _dt.timedelta(minutes=1)).isoformat(), "ended_at": now.isoformat(),
        "dwell_ms": 5_000,
    }, headers=admin_headers)
    db_session.expire_all()
    rows = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.slide_id == slide.id).all()
    assert [r.session_id for r in rows] == [None]  # recorded as a one-shot row
    r = client.post("/api/analytics/tour-event", json={
        "event": "start", "session_id": sid + "\n", "tour_public_id": "whatever",
        "started_at": now.isoformat(),
    }, headers=admin_headers)
    assert r.status_code == 204
    assert db_session.query(_TourSession).filter(_TourSession.session_id.like(sid + "%")).count() == 0


def test_dwell_is_capped_at_the_session_span(client, admin_headers, db_session, slide):
    now = _dt.datetime.now(_dt.timezone.utc)
    sid = "fedcba98-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    started = now - _dt.timedelta(seconds=60)
    client.post("/api/analytics/slide-dwell", json={
        "slide_public_id": slide.public_id, "session_id": sid,
        "started_at": started.isoformat(), "ended_at": now.isoformat(), "dwell_ms": 3_600_000,
    }, headers=admin_headers)
    db_session.expire_all()
    row = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).one()
    assert 60_000 <= row.dwell_ms <= 65_000
    # A later beacon cannot widen the span by claiming an earlier start.
    later = now + _dt.timedelta(seconds=10)
    client.post("/api/analytics/slide-dwell", json={
        "slide_public_id": slide.public_id, "session_id": sid,
        "started_at": (now - _dt.timedelta(hours=2)).isoformat(),
        "ended_at": later.isoformat(), "dwell_ms": 3_600_000,
    }, headers=admin_headers)
    db_session.expire_all()
    assert 70_000 <= db_session.get(_SlideDwellEvent, row.id).dwell_ms <= 75_000


def test_tour_completion_dwell_is_capped_at_start_to_completion(client, admin_headers, db_session, slide):
    tour = _Tour(slide_id=slide.id, title="cap", is_published=True)
    db_session.add(tour)
    db_session.commit()
    sid = "12345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    now = _dt.datetime.now(_dt.timezone.utc)
    client.post("/api/analytics/tour-event", json={
        "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
        "started_at": (now - _dt.timedelta(seconds=30)).isoformat(), "total_steps": 2,
    }, headers=admin_headers)
    client.post("/api/analytics/tour-event", json={
        "event": "complete", "session_id": sid, "tour_public_id": tour.public_id,
        "completed_at": now.isoformat(), "step_index": 1, "total_dwell_ms": 3_600_000,
    }, headers=admin_headers)
    db_session.expire_all()
    row = db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()
    assert row.completed and 30_000 <= row.total_dwell_ms <= 35_000


def test_tour_progress_dwell_is_capped_at_the_session_so_far(
    client, admin_headers, db_session, slide, beacon_clock,
):
    """A progress beacon's dwell is bounded by the session's span so far, as a
    completion's is by start → completion: a progress claim of an hour on a
    30 s session must not be kept up to the global ceiling and survive the
    completion. These beacons carry no usable sent_at (missing, malformed or
    out of range), so the span runs to the server's now."""
    tour = _Tour(slide_id=slide.id, title="cap", is_published=True)
    db_session.add(tour)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)

    def post(sid, event, **extra):
        r = client.post("/api/analytics/tour-event", json={
            "event": event, "session_id": sid, "tour_public_id": tour.public_id, **extra,
        }, headers=admin_headers)
        assert r.status_code == 204

    def stored(sid):
        db_session.expire_all()
        return db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()

    sid = "22345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    post(sid, "start", started_at=(now - _dt.timedelta(seconds=30)).isoformat(), total_steps=2)
    post(sid, "progress", step_index=1, total_dwell_ms=3_600_000)
    assert 30_000 <= stored(sid).total_dwell_ms <= 40_000
    post(sid, "complete", completed_at=now.isoformat(), step_index=1, total_dwell_ms=3_600_000)
    row = stored(sid)
    assert row.completed and 30_000 <= row.total_dwell_ms <= 40_000

    # "complete" goes out on reaching the last step; the time the reader then
    # lingers there arrives in a trailing progress beacon, past the
    # completion but still bounded by start → now.
    sid = "32345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    started = now - _dt.timedelta(minutes=10)
    beacon_clock.at(started)
    post(sid, "start", started_at=started.isoformat(), total_steps=2)
    beacon_clock.at(started + _dt.timedelta(minutes=2))
    post(sid, "complete", completed_at=(started + _dt.timedelta(minutes=2)).isoformat(),
         step_index=1, total_dwell_ms=120_000)
    beacon_clock.at(now)
    post(sid, "progress", step_index=1, total_dwell_ms=540_000)
    assert stored(sid).total_dwell_ms == 540_000
    post(sid, "progress", step_index=1, total_dwell_ms=3_600_000)
    assert 600_000 <= stored(sid).total_dwell_ms <= 610_000

    # An unparseable sent_at, or one further off than any client timestamp may
    # be, does not lift the cap.
    sid = "a2345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    post(sid, "start", started_at=(now - _dt.timedelta(seconds=30)).isoformat(), total_steps=2)
    post(sid, "progress", step_index=1, total_dwell_ms=3_600_000, sent_at="soon")
    post(sid, "progress", step_index=1, total_dwell_ms=3_600_000,
         sent_at=(now + _dt.timedelta(days=8)).isoformat())
    assert 30_000 <= stored(sid).total_dwell_ms <= 40_000


def test_tour_progress_dwell_span_is_measured_on_the_clients_clock(
    client, admin_headers, db_session, slide, beacon_clock,
):
    """started_at and sent_at come from the same client clock, so a clock
    within the receipt skew has its span measured on its own time: set
    against the server's now instead, a clock S ahead would lose S from every
    dwell (an abandoned 90 s tour with the clock 2 min fast would store 0). A clock
    further off has each stamp pulled in to its receipt time + the skew,
    which gives the right span too. Each beacon is delivered at its own
    moment."""
    tour = _Tour(slide_id=slide.id, title="skew", is_published=True)
    db_session.add(tour)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)

    def post(sid, event, **extra):
        r = client.post("/api/analytics/tour-event", json={
            "event": event, "session_id": sid, "tour_public_id": tour.public_id, **extra,
        }, headers=admin_headers)
        assert r.status_code == 204

    def stored(sid):
        db_session.expire_all()
        return db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()

    def client_clock(skew):
        return lambda t: (t + skew).isoformat()

    # Abandoned after 90 s, client clock 2 min fast.
    at = client_clock(_dt.timedelta(minutes=2))
    sid = "b2345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(now - _dt.timedelta(seconds=90))
    post(sid, "start", started_at=at(now - _dt.timedelta(seconds=90)), total_steps=5)
    beacon_clock.at(now)
    post(sid, "progress", step_index=2, total_dwell_ms=90_000, sent_at=at(now))
    assert stored(sid).total_dwell_ms == 90_000

    # 20 min tour on a machine whose OS time zone is wrong by 9 h.
    at = client_clock(_dt.timedelta(hours=9))
    sid = "c2345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(now - _dt.timedelta(minutes=20))
    post(sid, "start", started_at=at(now - _dt.timedelta(minutes=20)), total_steps=5)
    beacon_clock.at(now)
    post(sid, "progress", step_index=3, total_dwell_ms=1_200_000, sent_at=at(now))
    assert stored(sid).total_dwell_ms == 1_200_000

    # Lingering on the last step after "complete", client clock 5 min fast.
    at = client_clock(_dt.timedelta(minutes=5))
    started = now - _dt.timedelta(minutes=10)
    sid = "d2345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(started)
    post(sid, "start", started_at=at(started), total_steps=2)
    beacon_clock.at(started + _dt.timedelta(minutes=2))
    post(sid, "complete", completed_at=at(started + _dt.timedelta(minutes=2)),
         step_index=1, total_dwell_ms=120_000)
    beacon_clock.at(now)
    post(sid, "progress", step_index=1, total_dwell_ms=600_000, sent_at=at(now))
    assert stored(sid).total_dwell_ms == 600_000

    # The cap still holds on the client's clock — a slow one included, which
    # the server's now would have let claim the size of its lag on top.
    at = client_clock(_dt.timedelta(minutes=-10))
    sid = "e2345678-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(now - _dt.timedelta(seconds=30))
    post(sid, "start", started_at=at(now - _dt.timedelta(seconds=30)), total_steps=2)
    beacon_clock.at(now)
    post(sid, "progress", step_index=1, total_dwell_ms=3_600_000, sent_at=at(now))
    assert 30_000 <= stored(sid).total_dwell_ms <= 35_000


def test_views_of_unpublished_content_are_recorded_for_staff_only(
    client, admin_headers, db_session, monkeypatch,
):
    draft = Slide(title="Draft", catalog_id="DRAFT-1", is_published=False)
    db_session.add(draft)
    db_session.commit()
    draft_tour = _Tour(slide_id=draft.id, title="Draft tour", is_published=True)
    db_session.add(draft_tour)
    db_session.commit()
    # A learner with an app-access identity (no staff bearer).
    monkeypatch.setattr(_analytics, "probe_identity", lambda *a, **k: (None, "learner@example.org"))
    now = _dt.datetime.now(_dt.timezone.utc)

    def send(headers, sid):
        client.post("/api/analytics/pageview",
                    json={"kind": "viewer", "slide_public_id": draft.public_id}, headers=headers)
        client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": draft.public_id, "started_at": (now - _dt.timedelta(seconds=5)).isoformat(),
            "ended_at": now.isoformat(), "dwell_ms": 1_000,
        }, headers=headers)
        client.post("/api/analytics/tour-event", json={
            "event": "start", "session_id": sid,
            "tour_public_id": draft_tour.public_id, "started_at": now.isoformat(),
        }, headers=headers)

    send({}, "11111111-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    db_session.expire_all()
    assert db_session.query(_PageView).filter(_PageView.slide_id == draft.id).count() == 0
    assert db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.slide_id == draft.id).count() == 0
    assert db_session.query(_TourSession).filter(_TourSession.tour_id == draft_tour.id).count() == 0

    send(admin_headers, "22222222-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    db_session.expire_all()
    pv = db_session.query(_PageView).filter(_PageView.slide_id == draft.id).one()
    assert pv.actor_role == "admin"
    dwell = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.slide_id == draft.id).one()
    assert dwell.actor_role == "admin"
    ts = db_session.query(_TourSession).filter(_TourSession.tour_id == draft_tour.id).one()
    assert ts.actor_role == "admin"


def test_report_title_lookups_never_read_the_slide_image_blobs(client, admin_headers, db_session):
    """Every ranked table resolves its slide titles with one lookup over the
    ranked ids, and the dashboard's CSV export asks for 10 000 of them. A
    full-row ``select(Slide)`` there would pull each slide's macro and label JPEGs
    into the worker to read three short strings (measured: 300 slides x
    264 KB of blobs -> 82 MB peak for one top-slides pass). No statement any
    report runs may name either blob column."""
    from sqlalchemy import event

    from wsi_app.db import engine

    _wipe_analytics_tables(db_session)
    blob = b"\xff" * (64 * 1024)
    s = Slide(title="Blobbed", catalog_id="BLOB-1", is_published=True,
              macro_data=blob, macro_etag="m", label_data=blob, label_etag="l")
    db_session.add(s)
    db_session.commit()
    t = _Tour(slide_id=s.id, title="Blobbed tour", is_published=True)
    db_session.add(t)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)
    db_session.add_all([
        _PageView(kind="viewer", slide_id=s.id, client_ip="1.1.1.1", ua_class="desktop",
                  occurred_at=now - _dt.timedelta(hours=1)),
        _PageView(kind="tour", slide_id=s.id, tour_id=t.id, client_ip="1.1.1.1", ua_class="desktop",
                  occurred_at=now - _dt.timedelta(hours=1)),
        _SlideDwellEvent(slide_id=s.id, user_identifier="u@x", dwell_ms=1_000,
                         started_at=now - _dt.timedelta(minutes=2), ended_at=now - _dt.timedelta(minutes=1)),
        _TourSession(session_id="b1000000-bbbb-4ccc-8ddd-eeeeeeeeeeee", tour_id=t.id,
                     user_identifier="u@x", started_at=now - _dt.timedelta(minutes=2),
                     completed_at=now - _dt.timedelta(minutes=1), completed=True, total_steps=1),
    ])
    db_session.commit()

    seen: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        for url in (
            "/api/admin/analytics/dashboard?days=7",
            "/api/admin/analytics/recent?days=7",
            "/api/admin/analytics/tour-completion?days=7",
            "/api/admin/analytics/slide-dwell?days=7",
            "/api/admin/analytics/export.csv?dataset=top-slides&days=7",
        ):
            r = client.get(url, headers=admin_headers)
            assert r.status_code == 200, (url, r.text)
            # The title lookup ran and resolved on every one of them.
            assert "Blobbed" in r.text, (url, r.text)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        _wipe_analytics_tables(db_session)

    slide_reads = [q for q in seen if "FROM slides" in q]
    assert slide_reads
    for q in slide_reads:
        assert "macro_data" not in q and "label_data" not in q, q


def test_slide_image_blobs_load_only_when_read(db_session, slide):
    """The blob columns are deferred on the model, so a plain
    ``db.get(Slide, ...)`` / ``select(Slide)`` anywhere — sync's room titles,
    the tile handle cache's per-miss lookup, every admin edit — leaves them
    on disk. Reading the attribute in the open session still fetches them
    (the macro / label image endpoints)."""
    from sqlalchemy import inspect as sa_inspect

    slide.macro_data = b"MACRO"
    slide.label_data = b"LABEL"
    db_session.commit()
    slide_id = slide.id
    db_session.expunge_all()

    loaded = db_session.get(Slide, slide_id)
    unloaded = sa_inspect(loaded).unloaded
    assert {"macro_data", "label_data"} <= unloaded, unloaded
    assert "title" not in unloaded and "macro_etag" not in unloaded
    assert loaded.macro_data == b"MACRO"
    assert loaded.label_data == b"LABEL"


# ---------------------------------------------------------------------------
# Clear scope, server-anchored dwell, plans, windows, budgets, merge-on-race,
# CSV filters, deleted tours, keyset deletes, background prune, ranking
# tie-breaks, retired index, purge preview, pseudonymize.
# ---------------------------------------------------------------------------
def _seed_rows(db, *rows):
    for row in rows:
        db.add(row)
    db.commit()


def test_clear_without_a_scope_deletes_nothing(client, admin_headers, db_session):
    """A full wipe has to be asked for with all=1: no scope, a misspelt range
    parameter, an empty date, or all=1 together with a date is a 400 and
    leaves every row in place."""
    from wsi_app.models import AuditLog

    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, _PageView(kind="top"), _PageView(kind="top"))
    audits_before = db_session.query(AuditLog).filter(AuditLog.action == "analytics.clear").count()
    for qs in (
        "",
        "?from=2020-01-01",              # misnamed: unknown params are not a scope
        "?all=0",
        "?from_date=",
        "?to_date=",
        "?from_date=&to_date=2020-01-01",
        "?all=1&from_date=2020-01-01",
        "?all=1&to_date=2020-01-01",
    ):
        r = client.delete(f"/api/admin/analytics/data{qs}", headers=admin_headers)
        assert r.status_code == 400, (qs, r.text)
        assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsClearScopeRequired", qs
    db_session.expire_all()
    assert db_session.query(_PageView).count() == 2
    assert db_session.query(AuditLog).filter(
        AuditLog.action == "analytics.clear"
    ).count() == audits_before
    r = client.delete("/api/admin/analytics/data?all=1", headers=admin_headers)
    assert r.status_code == 200 and r.json()["deleted_page_views"] == 2


def test_dwell_span_cannot_outrun_the_servers_clock(client, admin_headers, db_session, slide, beacon_clock):
    """ended_at is anchored to the receipt: a later beacon claiming an end
    days ahead (inside the week-long drift guard) must not stretch the
    session's span — and so its dwell — to whatever it claimed."""
    now = _dt.datetime.now(_dt.timezone.utc)
    beacon_clock.at(now)
    sid = "f0000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def beacon(ended, dwell):
        assert client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id, "session_id": sid,
            "started_at": (now - _dt.timedelta(seconds=60)).isoformat(),
            "ended_at": ended.isoformat(), "dwell_ms": dwell,
        }, headers=admin_headers).status_code == 204

    beacon(now, 60_000)
    beacon(now + _dt.timedelta(days=3), 3_600_000)
    db_session.expire_all()
    row = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).one()
    assert row.dwell_ms <= 65_000 + 5 * 60_000  # span to now + the receipt skew at most
    assert row.ended_at.replace(tzinfo=None) <= (now + _ar._CLIENT_CLOCK_SKEW).replace(tzinfo=None)
    # Ten real minutes later the session may have grown by those ten minutes.
    beacon_clock.at(now + _dt.timedelta(minutes=10))
    beacon(now + _dt.timedelta(minutes=10), 3_600_000)
    db_session.expire_all()
    assert 660_000 <= db_session.get(_SlideDwellEvent, row.id).dwell_ms <= 665_000


def test_tour_start_claiming_an_old_start_does_not_prepay_dwell(
    client, admin_headers, db_session, slide, beacon_clock,
):
    tour = _Tour(slide_id=slide.id, title="old start", is_published=True)
    db_session.add(tour)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)
    beacon_clock.at(now)
    sid = "f1000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    for body in (
        {"event": "start", "started_at": (now - _dt.timedelta(hours=4)).isoformat(), "total_steps": 3},
        {"event": "progress", "step_index": 1, "total_dwell_ms": 4 * 3_600_000,
         "sent_at": (now + _dt.timedelta(days=2)).isoformat()},
    ):
        assert client.post("/api/analytics/tour-event", json={
            "session_id": sid, "tour_public_id": tour.public_id, **body,
        }, headers=admin_headers).status_code == 204
    db_session.expire_all()
    row = db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()
    # Both stamps are pulled in to the receipt ± the skew: the hours-old
    # start and the days-ahead sent_at buy the skew at each end, nothing more.
    skew = _ar._CLIENT_CLOCK_SKEW
    assert abs(coerce_naive(row.started_at) - (now - skew).replace(tzinfo=None)) < _dt.timedelta(seconds=1)
    assert row.total_dwell_ms <= (2 * skew + _ar._DWELL_SPAN_SLACK).total_seconds() * 1000


def test_receipt_offset_is_continuous_at_the_skew_edge(
    client, admin_headers, db_session, slide, beacon_clock,
):
    """A client clock just under the skew has its start taken as given; were
    the completion 60 s later, pushed just over the skew by network latency,
    moved back by the whole offset, it would land ~5 min before the start and
    be dropped as clock nonsense. Likewise a dwell session's end would
    freeze. Clamped to receipt ± skew, latency jitter moves a stamp only by
    the jitter."""
    tour = _Tour(slide_id=slide.id, title="edge", is_published=True)
    db_session.add(tour)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)
    skew = _ar._CLIENT_CLOCK_SKEW
    below = skew - _dt.timedelta(milliseconds=500)   # 4:59.5 fast
    above = skew + _dt.timedelta(milliseconds=50)    # 5:00.05 fast
    later = now + _dt.timedelta(seconds=60)

    sid = "f8000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(now)
    client.post("/api/analytics/tour-event", json={
        "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
        "started_at": (now + below).isoformat(), "total_steps": 2,
    }, headers=admin_headers)
    beacon_clock.at(later)
    client.post("/api/analytics/tour-event", json={
        "event": "complete", "session_id": sid, "tour_public_id": tour.public_id,
        "completed_at": (later + above).isoformat(), "step_index": 1, "total_dwell_ms": 60_000,
    }, headers=admin_headers)
    db_session.expire_all()
    row = db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()
    assert row.completed
    span = coerce_naive(row.completed_at) - coerce_naive(row.started_at)
    assert _dt.timedelta(seconds=59) <= span <= _dt.timedelta(seconds=61)
    assert row.total_dwell_ms == 60_000

    # The same clock on a slide-dwell session: the second beacon's end must
    # still advance past the first's, and the dwell grow with it.
    dsid = "f9000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    started = now - _dt.timedelta(seconds=30)

    def dwell(at, offset, ms):
        beacon_clock.at(at)
        assert client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id, "session_id": dsid,
            "started_at": (started + offset).isoformat(),
            "ended_at": (at + offset).isoformat(), "dwell_ms": ms,
        }, headers=admin_headers).status_code == 204
        db_session.expire_all()
        return db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == dsid).one()

    first = dwell(now, below, 30_000)
    assert first.dwell_ms == 30_000
    first_end = coerce_naive(first.ended_at)
    row = dwell(later, above, 90_000)
    assert coerce_naive(row.ended_at) - first_end >= _dt.timedelta(seconds=59)
    assert row.dwell_ms == 90_000


def coerce_naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def test_shared_account_burst_of_new_sessions_is_never_dropped(client, db_session, slide):
    """A class of 40 on one shared sign-in, each opening 10 slides in ten
    minutes: 400 new dwell sessions and 400 tour sessions under one identity,
    every one of them recorded. (An LDAP subject, as a basic: cookie is
    re-checked against a local AuthUser row; the identity path is the same.)"""
    from wsi_app import app_auth

    tour = _Tour(slide_id=slide.id, title="shared", is_published=True)
    db_session.add(tour)
    db_session.commit()
    client.cookies.set(app_auth.SESSION_COOKIE, app_auth.create_session_token("ldap:student"))
    now = _dt.datetime.now(_dt.timezone.utc)
    sids = [f"{i:08x}-aaaa-4bbb-8ccc-dddddddddddd" for i in range(400)]
    for sid in sids:
        assert client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id, "session_id": sid,
            "started_at": (now - _dt.timedelta(seconds=60)).isoformat(),
            "ended_at": now.isoformat(), "dwell_ms": 55_000,
        }).status_code == 204
        assert client.post("/api/analytics/tour-event", json={
            "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
            "started_at": now.isoformat(), "total_steps": 2,
        }).status_code == 204
    db_session.expire_all()
    dwell = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id.in_(sids)).all()
    assert len(dwell) == 400
    assert {r.user_identifier for r in dwell} == {"ldap:student"}
    assert {r.dwell_ms for r in dwell} == {55_000}
    assert db_session.query(_TourSession).filter(_TourSession.session_id.in_(sids)).count() == 400


def test_first_dwell_beacon_is_capped_whatever_span_it_claims(
    client, admin_headers, db_session, slide, beacon_clock,
):
    """A new session's first beacon may claim at most
    _FIRST_BEACON_MAX_DWELL_MS, however early its started_at; and an
    immediate follow-up beacon cannot top it up to the ceiling either: past
    the first dwell the session grows only with the server's clock."""
    cap = _ar._FIRST_BEACON_MAX_DWELL_MS
    skew_ms = int(_ar._CLIENT_CLOCK_SKEW.total_seconds() * 1000)
    slack_ms = int(_ar._DWELL_SPAN_SLACK.total_seconds() * 1000)
    now = _dt.datetime.now(_dt.timezone.utc)
    beacon_clock.at(now)
    sid = "f6000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def beacon(ended, dwell, started=now - _dt.timedelta(days=6)):
        assert client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id, "session_id": sid,
            "started_at": started.isoformat(), "ended_at": ended.isoformat(), "dwell_ms": dwell,
        }, headers=admin_headers).status_code == 204
        db_session.expire_all()
        return db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).one()

    row = beacon(now, 4 * 3_600_000)
    assert row.dwell_ms == cap
    # The stored start was pulled up to just before that dwell.
    assert coerce_naive(row.started_at) >= (now - _dt.timedelta(milliseconds=cap + slack_ms)).replace(tzinfo=None)
    # Straight after, with the end pushed to the skew's edge: at most the skew more.
    row = beacon(now + _ar._CLIENT_CLOCK_SKEW, 4 * 3_600_000)
    assert row.dwell_ms <= cap + skew_ms + 2 * slack_ms
    # An hour of server time later it may have grown by that hour, no more.
    beacon_clock.at(now + _dt.timedelta(hours=1))
    row = beacon(now + _dt.timedelta(hours=1), 4 * 3_600_000)
    assert 3_600_000 + cap <= row.dwell_ms <= 3_600_000 + cap + skew_ms + 2 * slack_ms


def test_one_shot_dwell_beacon_is_capped_too(client, admin_headers, db_session, slide):
    """A beacon without a session_id is a new row every time; each is capped."""
    now = _dt.datetime.now(_dt.timezone.utc)
    _wipe_analytics_tables(db_session)
    for _ in range(3):
        client.post("/api/analytics/slide-dwell", json={
            "slide_public_id": slide.public_id,
            "started_at": (now - _dt.timedelta(days=5)).isoformat(),
            "ended_at": now.isoformat(), "dwell_ms": 4 * 3_600_000,
        }, headers=admin_headers)
    db_session.expire_all()
    assert [r.dwell_ms for r in db_session.query(_SlideDwellEvent)] == [_ar._FIRST_BEACON_MAX_DWELL_MS] * 3


def test_tour_completion_time_is_bounded_by_server_elapsed_since_start(
    client, admin_headers, db_session, slide, beacon_clock,
):
    """Start and completion are both receipt-anchored, so a pair sent a
    second apart cannot record an hour-long completion (or dwell), whatever
    their stamps claim."""
    tour = _Tour(slide_id=slide.id, title="pair", is_published=True)
    db_session.add(tour)
    db_session.commit()
    now = _dt.datetime.now(_dt.timezone.utc)
    skew = _ar._CLIENT_CLOCK_SKEW
    sid = "f7000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    beacon_clock.at(now)
    client.post("/api/analytics/tour-event", json={
        "event": "start", "session_id": sid, "tour_public_id": tour.public_id,
        "started_at": (now - _dt.timedelta(hours=3)).isoformat(), "total_steps": 2,
    }, headers=admin_headers)
    beacon_clock.at(now + _dt.timedelta(seconds=1))
    client.post("/api/analytics/tour-event", json={
        "event": "complete", "session_id": sid, "tour_public_id": tour.public_id,
        "completed_at": (now + _dt.timedelta(hours=1)).isoformat(),
        "step_index": 1, "total_dwell_ms": 3_600_000,
    }, headers=admin_headers)
    db_session.expire_all()
    row = db_session.query(_TourSession).filter(_TourSession.session_id == sid).one()
    assert row.completed
    span = coerce_naive(row.completed_at) - coerce_naive(row.started_at)
    assert span <= _dt.timedelta(seconds=1) + 2 * skew
    assert row.total_dwell_ms <= (span + _ar._DWELL_SPAN_SLACK).total_seconds() * 1000


def test_kind_filtered_rankings_with_staff_seek_the_kind_window_index():
    """With include_staff=1 the actor_role clause is gone and SQLite would
    choose the single-column `kind` index: every viewer/tour row of the whole
    history."""
    from wsi_app.db import engine

    until = _dt.datetime.now(_dt.timezone.utc)
    for kinds in (PageView.kind.in_(("viewer", "tour")), PageView.kind == "tour"):
        stmt = _ar._apply_default_filters(
            _ar._window_filter(select(func.count()), until - _dt.timedelta(days=7), until)
            .where(kinds),
            include_bots=False, include_staff=True,
        ).select_from(PageView)
        sql = str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
        with engine.connect() as conn:
            plan = " ".join(str(r[-1]) for r in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}"))
        assert "ix_page_views_kind_occurred_at" in plan, plan
        assert "occurred_at>" in plan, plan


def test_start_only_window_counts_calendar_days(client, admin_headers):
    """A start-date-only window runs to now (part-way through today), so its
    elapsed `.days` comes out one short: counted that way, a start 365 days
    back would pass the cap while covering 366 calendar days."""
    today = _dt.datetime.now(_ar.analytics_tz()).date()
    r = client.get(
        f"/api/admin/analytics/summary?start_date={(today - _dt.timedelta(days=364)).isoformat()}",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["days"] == 365 == len(r.json()["daily"])
    r = client.get(
        f"/api/admin/analytics/summary?start_date={(today - _dt.timedelta(days=365)).isoformat()}",
        headers=admin_headers,
    )
    assert r.status_code == 400
    r = client.get(f"/api/admin/analytics/summary?start_date={today.isoformat()}", headers=admin_headers)
    assert r.json()["days"] == 1 == len(r.json()["daily"])


def test_preset_days_window_starts_at_local_midnight(client, admin_headers, db_session):
    """days=N is today and the N-1 days before it, from local midnight: N
    daily buckets, none partial, totals matching the buckets."""
    tz = _ar.analytics_tz()
    today = _dt.datetime.now(tz).date()
    first_midnight = _dt.datetime.combine(today - _dt.timedelta(days=6), _dt.time.min, tzinfo=tz)
    _wipe_analytics_tables(db_session)
    _backdate_pageview(db_session, first_midnight + _dt.timedelta(minutes=1))
    _backdate_pageview(db_session, first_midnight - _dt.timedelta(minutes=1))
    body = client.get("/api/admin/analytics/summary?days=7", headers=admin_headers).json()
    assert body["days"] == 7 == len(body["daily"])
    assert body["start_date"] == first_midnight.date().isoformat()
    assert body["daily"][0]["day"] == first_midnight.date().isoformat()
    assert sum(d["top"] + d["viewer"] + d["tour"] for d in body["daily"]) == 1
    assert sum(body["totals"].values()) == 1


@pytest.mark.parametrize("path", [
    "/api/admin/analytics/tour-completion?days=7",
    "/api/admin/analytics/slide-dwell?days=7",
    "/api/admin/analytics/recent",
])
def test_remaining_report_endpoints_run_under_the_query_budget(client, admin_headers, db_session, monkeypatch, path):
    _wipe_analytics_tables(db_session)
    db_session.add(_PageView(kind="top", client_ip="1.1.1.1"))
    db_session.commit()
    monkeypatch.setattr(_ar, "_QUERY_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(_ar, "_QUERY_BUDGET_CHECK_STEPS", 1)
    r = client.get(path, headers=admin_headers)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsQueryTimeout"


def test_dwell_insert_race_loser_is_merged_not_dropped(
    client, admin_headers, admin_user, db_session, slide, monkeypatch,
):
    """Two first beacons of one session race past the existence check; the
    UNIQUE constraint keeps the winner's row, and the loser's (larger) dwell
    is folded into it instead of being lost."""
    sid = "f3000000-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    now = _dt.datetime.now(_dt.timezone.utc)
    started = now - _dt.timedelta(minutes=3)
    real_cap = _ar._cap_dwell_to_span
    raced = []

    def _racing_cap(*args):
        """The insert path's first call: let the competing beacon's row land
        between this beacon's existence check and its INSERT — exactly the
        race window."""
        if not raced:
            raced.append(1)
            db = _SessionLocal()
            try:
                ident = admin_user.username
                db.add(_SlideDwellEvent(
                    slide_id=slide.id, session_id=sid, user_identifier=ident,
                    started_at=started, ended_at=now - _dt.timedelta(minutes=2), dwell_ms=40_000,
                ))
                db.commit()
            finally:
                db.close()
        return real_cap(*args)

    monkeypatch.setattr(_ar, "_cap_dwell_to_span", _racing_cap)
    r = client.post("/api/analytics/slide-dwell", json={
        "slide_public_id": slide.public_id, "session_id": sid,
        "started_at": started.isoformat(), "ended_at": now.isoformat(), "dwell_ms": 170_000,
    }, headers=admin_headers)
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.query(_SlideDwellEvent).filter(_SlideDwellEvent.session_id == sid).one()
    assert row.dwell_ms == 170_000
    assert coerce_naive(row.ended_at) == now.replace(tzinfo=None)


@pytest.mark.parametrize("param", ["user_id=1", "user_email=a%40x", "client_ip=1.1.1.1", "kind=viewer", "user_email="])
@pytest.mark.parametrize("dataset", ["summary", "daily", "top-slides", "top-tours", "top-users", "referrers", "ua"])
def test_csv_aggregate_datasets_reject_row_filters(client, admin_headers, dataset, param):
    r = client.get(f"/api/admin/analytics/export.csv?dataset={dataset}&{param}", headers=admin_headers)
    assert r.status_code == 400, (dataset, param, r.text)
    assert "only supported for dataset=recent" in r.json()["detail"]


def test_csv_recent_still_takes_row_filters(client, admin_headers):
    r = client.get(
        "/api/admin/analytics/export.csv?dataset=recent&kind=viewer&user_email=a%40x&client_ip=1.1.1.1&user_id=1",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


def test_top_tours_keeps_a_deleted_tour_with_blank_labels(client, admin_headers, db_session, slide):
    """Like a deleted slide in top-slides: the row keeps its rank (and its
    slide, from the page views), so the table has `limit` rows."""
    _wipe_analytics_tables(db_session)
    live = _Tour(slide_id=slide.id, title="Live", is_published=True)
    db_session.add(live)
    db_session.commit()
    gone_id = live.id + 1000
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    _seed_rows(db_session, *[
        _PageView(kind="tour", slide_id=slide.id, tour_id=tid, client_ip=f"10.0.0.{i}", occurred_at=now)
        for tid, n in ((gone_id, 3), (live.id, 2)) for i in range(n)
    ])
    body = client.get("/api/admin/analytics/top-tours?days=7&limit=2", headers=admin_headers).json()
    assert [r["tour_id"] for r in body] == [gone_id, live.id]
    gone = body[0]
    assert (gone["tour_title"], gone["public_id"]) == ("", "")
    assert gone["slide_id"] == slide.id and gone["slide_title"] == slide.title
    assert gone["views"] == 3 and gone["unique_ips"] == 3


def test_chunked_delete_walks_forward_by_id(db_session, monkeypatch):
    """Each slice starts after the previous one (keyset), instead of
    re-selecting "the first N matches" from id 1 every time."""
    from sqlalchemy import event

    from wsi_app.db import engine

    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, *[
        _PageView(kind="top", user_email="x@x" if i % 2 else "keep@x") for i in range(11)
    ])
    keep = set(db_session.execute(select(_PageView.id).where(_PageView.user_email == "keep@x")).scalars())
    seen: list = []

    def _capture(conn, cursor, statement, params, context, executemany):
        if "page_views" in statement and ("SELECT" in statement or "DELETE" in statement):
            seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        n, finished = _analytics.delete_until(db_session, _PageView, _PageView.user_email == "x@x")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert (n, finished) == (5, True)
    db_session.expire_all()
    assert set(db_session.execute(select(_PageView.id)).scalars()) == keep
    selects = [q for q in seen if q.startswith("SELECT")]
    assert len(selects) >= 3
    assert "page_views.id >" not in selects[0]
    assert all("page_views.id >" in q for q in selects[1:]), selects


def test_beacon_prune_runs_as_a_background_task(client, slide, monkeypatch):
    calls = []
    monkeypatch.setattr(_analytics, "maybe_prune_analytics", lambda: calls.append(1))
    resp = _ar._with_prune(_ar.Response(status_code=204))
    assert resp.background is not None
    client.post("/api/analytics/pageview", json={"kind": "top"})
    assert calls == [1]


def test_opportunistic_prune_is_bounded_and_never_overlaps(db_session, monkeypatch):
    from wsi_app import retention

    throttle = retention.PruneThrottle()
    monkeypatch.setattr(_analytics, "_prune_throttle", throttle)
    monkeypatch.setattr(_analytics, "_DELETE_CHUNK_ROWS", 2)
    monkeypatch.setattr(_analytics, "_OPPORTUNISTIC_PRUNE_BUDGET_S", 0.0)
    monkeypatch.setattr(_analytics, "_OPPORTUNISTIC_PRUNE_RETRY_S", 0.0)
    import dataclasses
    monkeypatch.setattr(
        _analytics, "settings",
        dataclasses.replace(_analytics.settings, analytics_log_max_rows=1, analytics_log_max_age_days=0),
    )
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, *[_PageView(kind="top") for _ in range(7)])

    # A prune already running in this worker: the next caller skips without
    # spending the throttle.
    assert _analytics._prune_running.acquire(blocking=False)
    try:
        _analytics.maybe_prune_analytics()
    finally:
        _analytics._prune_running.release()
    db_session.expire_all()
    assert db_session.query(_PageView).count() == 7

    # Budget exhausted after one slice: rows remain, and the throttle is
    # rearmed for an early next round instead of waiting the full hour.
    _analytics.maybe_prune_analytics()
    db_session.expire_all()
    assert db_session.query(_PageView).count() == 5
    for _ in range(5):
        _analytics.maybe_prune_analytics()
    db_session.expire_all()
    assert db_session.query(_PageView).count() == 1
    # Finished: back to the normal interval.
    assert throttle.claim() is False


def test_rankings_break_count_ties_by_their_key(client, admin_headers, db_session):
    _wipe_analytics_tables(db_session)
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    rows = []
    for sid, email, ref in ((7, "c@x", "c.example"), (3, "a@x", "a.example"), (5, "b@x", "b.example")):
        rows.append(_PageView(kind="viewer", slide_id=sid, user_email=email, referrer_host=ref,
                              client_ip="1.1.1.1", occurred_at=when))
    _seed_rows(db_session, *rows)
    body = client.get("/api/admin/analytics/dashboard?days=7", headers=admin_headers).json()
    assert [r["slide_id"] for r in body["top_slides"]] == [3, 5, 7]
    assert [r["user_email"] for r in body["top_users"]] == ["a@x", "b@x", "c@x"]
    assert [r["referrer_host"] for r in body["referrers"]] == ["a.example", "b.example", "c.example"]
    import csv
    import io
    r = client.get("/api/admin/analytics/export.csv?dataset=top-slides&days=7", headers=admin_headers)
    assert [row["slide_id"] for row in csv.DictReader(io.StringIO(r.text.lstrip("﻿")))] == ["3", "5", "7"]


@pytest.mark.parametrize("path, key, group_col", [
    ("/api/admin/analytics/top-users?days=7", "user_email", "page_views.user_email"),
    ("/api/admin/analytics/top-tours?days=7", "tour_id", "page_views.tour_id"),
    ("/api/admin/analytics/tour-completion?days=7", "tour_id", "tour_sessions.tour_id"),
    ("/api/admin/analytics/slide-dwell?days=7", "slide_id", "slide_dwell_events.slide_id"),
])
def test_ranking_ties_break_by_the_group_key_in_the_query(
    client, admin_headers, db_session, path, key, group_col,
):
    """Tied rows rank by their group key, so which of them make a LIMIT cut
    is deterministic. SQLite happens to emit groups in key order, so the
    result alone passes without the tie-break; the ORDER BY is pinned too
    (as in test_recent_ties_on_occurred_at_break_by_id_desc)."""
    import re

    from sqlalchemy import event

    from wsi_app.db import engine

    _wipe_analytics_tables(db_session)
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    for k, email in ((7, "c@x"), (3, "a@x"), (5, "b@x")):
        db_session.add_all([
            _PageView(kind="tour", slide_id=1, tour_id=k, user_email=email,
                      client_ip="1.1.1.1", occurred_at=when),
            _TourSession(session_id=f"{k:08x}-cccc-4ccc-8ddd-eeeeeeeeeeee", tour_id=k,
                         user_identifier=email, started_at=when),
            _SlideDwellEvent(slide_id=k, user_identifier=email, started_at=when,
                             ended_at=when, dwell_ms=1_000),
        ])
    db_session.commit()

    sql: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        if f"GROUP BY {group_col}" in statement:
            sql.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        r = client.get(path, headers=admin_headers)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert r.status_code == 200, r.text
    expected = ["a@x", "b@x", "c@x"] if key == "user_email" else [3, 5, 7]
    assert [row[key] for row in r.json()] == expected
    ranked = [s for s in sql if "ORDER BY" in s and "LIMIT" in s]
    assert ranked, sql
    for stmt in ranked:
        assert re.search(rf"ORDER BY [^,]+ DESC, {re.escape(group_col)}\b", stmt), stmt


def test_reconcile_drops_the_retired_kind_index(db_session):
    """ix_page_views_kind is a prefix of ix_page_views_kind_occurred_at; an
    existing DB that still has it loses it at boot, so beacon INSERTs stop
    maintaining it. The reconcile never drops an index nobody retired."""
    from wsi_app import main
    from wsi_app.db import engine

    def names():
        with engine.connect() as conn:
            return {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(page_views)")}

    assert "ix_page_views_kind" not in names()  # a fresh DB never builds it
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE INDEX "ix_page_views_kind" ON page_views (kind)')
        conn.exec_driver_sql('CREATE INDEX "ix_operator_own" ON page_views (referrer_host)')
    try:
        main._reconcile_indexes()
        after = names()
        assert "ix_page_views_kind" not in after
        assert "ix_page_views_kind_occurred_at" in after
        assert "ix_operator_own" in after
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql('DROP INDEX IF EXISTS "ix_operator_own"')
            conn.exec_driver_sql('DROP INDEX IF EXISTS "ix_page_views_kind"')


def test_kind_filtered_recent_log_seeks_the_composite_index():
    """The recent log filtered by kind (no window, staff included: no other
    indexed predicate) needs no single-column `kind` index: the
    (kind, occurred_at) composite serves it as a seek that also yields the
    newest-first order, without a sort."""
    from wsi_app.db import engine

    stmt = _ar._apply_default_filters(
        select(PageView).where(PageView.kind == "tour")
        .order_by(PageView.occurred_at.desc(), PageView.id.desc()),
        include_bots=True, include_staff=True,
    ).limit(200)
    sql = str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as conn:
        plan = " ".join(str(r[-1]) for r in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}"))
    assert "ix_page_views_kind_occurred_at (kind=?)" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_unique_ips_ignore_empty_addresses(client, admin_headers, db_session):
    _wipe_analytics_tables(db_session)
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    _seed_rows(db_session, *[
        _PageView(kind="viewer", slide_id=9, client_ip=ip, user_email="u@x", occurred_at=when)
        for ip in ("", "", "1.1.1.1")
    ])
    body = client.get("/api/admin/analytics/dashboard?days=7", headers=admin_headers).json()
    assert body["summary"]["unique_ips"] == 1
    assert body["top_slides"][0]["unique_ips"] == 1
    assert body["top_users"][0]["unique_ips"] == 1


def test_purge_preview_lists_what_the_purge_would_delete(client, admin_headers, db_session, caplog):
    """A bare name also reaches the app-access subjects; the preview shows
    them (as stored) and the per-table counts, and changes nothing."""
    from wsi_app.models import AuditLog

    _wipe_analytics_tables(db_session)
    now = _dt.datetime.now(_dt.timezone.utc)
    _seed_rows(
        db_session,
        _PageView(kind="top", user_email="alice"),
        _PageView(kind="top", user_email="ldap:alice"),
        _PageView(kind="top", user_email="basic:Alice"),
        _PageView(kind="top", user_email="bob"),
        _SlideDwellEvent(slide_id=1, user_identifier="ldap:alice", started_at=now, ended_at=now, dwell_ms=1),
        _TourSession(session_id="f4000000-bbbb-4ccc-8ddd-eeeeeeeeeeee", tour_id=1,
                     user_identifier="google:alice", started_at=now),
    )
    audits = db_session.query(AuditLog).count()
    with caplog.at_level("INFO"):
        r = client.post("/api/admin/analytics/user/preview", data={"identifier": "alice"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["subjects"]) == ["alice", "basic:Alice", "google:alice", "ldap:alice"]
    assert (body["page_views"], body["slide_dwell_events"], body["tour_sessions"]) == (3, 1, 1)
    db_session.expire_all()
    assert db_session.query(_PageView).count() == 4
    assert db_session.query(AuditLog).count() == audits
    # The preview agrees with the purge.
    r = client.request("DELETE", "/api/admin/analytics/user", data={"identifier": "alice"}, headers=admin_headers)
    assert (r.json()["deleted_page_views"], r.json()["deleted_slide_dwell_events"],
            r.json()["deleted_tour_sessions"]) == (3, 1, 1)


def test_purge_preview_caps_subjects_and_requires_admin(client, admin_headers, instructor_headers, db_session, monkeypatch):
    monkeypatch.setattr(_ar, "_PREVIEW_MAX_SUBJECTS", 2)
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, *[_PageView(kind="top", user_email=e) for e in ("x", "ldap:x", "basic:x")])
    url = "/api/admin/analytics/user/preview"
    body = client.post(url, data={"identifier": "x"}, headers=admin_headers).json()
    assert len(body["subjects"]) == 2 and body["page_views"] == 3
    assert client.post(url, data={"identifier": "x"}).status_code == 401
    assert client.post(url, data={"identifier": "x"}, headers=instructor_headers).status_code == 403
    assert client.post(url, data={"identifier": " "}, headers=admin_headers).status_code == 422
    # Never in the URL: the query form of the identifier is not read here.
    assert client.post(f"{url}?identifier=x", headers=admin_headers).status_code == 422


def test_purge_takes_the_identifier_from_the_form_body(client, admin_headers, db_session):
    """The identifier is read from the body only (kept out of access logs):
    urlencoded or multipart; the query form is not read, so it is a 422."""
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, _PageView(kind="top", user_email="carol"), _PageView(kind="top", user_email="dave"),
               _PageView(kind="top", user_email="erin"))
    r = client.request("DELETE", "/api/admin/analytics/user", data={"identifier": "carol"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 1
    # Multipart, as the SPA's FormData sends it.
    r = client.request("DELETE", "/api/admin/analytics/user", files={"identifier": (None, "erin")},
                       headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_page_views"] == 1
    r = client.delete("/api/admin/analytics/user?identifier=dave", headers=admin_headers)
    assert r.status_code == 422
    assert db_session.execute(select(_PageView.user_email)).scalars().all() == ["dave"]
    assert client.delete("/api/admin/analytics/user", headers=admin_headers).status_code == 422


def test_pseudonymize_converts_plain_rows_to_the_hashed_form(client, admin_headers, admin_user, db_session, slide):
    """Rows recorded in plain mode end up exactly as hashed mode records
    them; already-hashed values are left alone and a re-run is a no-op."""
    from wsi_app import analytics_hash as _ah
    from wsi_app.models import AuditLog

    _wipe_analytics_tables(db_session)
    # A real plain-mode beacon, then the same view recorded in hashed mode.
    _set_analytics_mode("plain")
    ua = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id},
                headers={**admin_headers, "User-Agent": ua, "x-real-ip": "198.51.100.7"})
    # The mode stays "hashed": conversion is refused in any other.
    _set_analytics_mode("hashed")
    client.post("/api/analytics/pageview", json={"kind": "viewer", "slide_public_id": slide.public_id},
                headers={**admin_headers, "User-Agent": ua, "x-real-ip": "198.51.100.7"})
    now = _dt.datetime.now(_dt.timezone.utc)
    digest = _ah.compute_user_hash("someone@example.com")
    ip_digest = _analytics.pseudonymise_client_ip("203.0.113.9")
    _seed_rows(
        db_session,
        _PageView(kind="top", user_email="", client_ip="2001:db8::1", user_agent="ua"),  # anonymous
        # Selected for its user_agent alone: the values already in hashed form
        # must not be hashed a second time (DIGEST_RE guard in _hashed_form).
        _PageView(kind="top", user_email=digest, client_ip=ip_digest, user_agent="ua"),
        _SlideDwellEvent(slide_id=1, user_identifier="Basic:Carol", started_at=now, ended_at=now, dwell_ms=1),
        _SlideDwellEvent(slide_id=1, user_identifier=digest, started_at=now, ended_at=now, dwell_ms=1),
        _TourSession(session_id="f5000000-bbbb-4ccc-8ddd-eeeeeeeeeeee", tour_id=1,
                     user_identifier="dave@example.com", started_at=now),
    )
    r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "updated": {"page_views": 3, "slide_dwell_events": 1, "tour_sessions": 1},
        "complete": True,
    }
    db_session.expire_all()
    plain_row, hashed_row, anon, half = db_session.query(_PageView).order_by(_PageView.id).all()
    assert (half.user_email, half.client_ip, half.user_agent) == (digest, ip_digest, "")
    for col in ("user_email", "client_ip", "user_agent", "user_id", "ua_class"):
        assert getattr(plain_row, col) == getattr(hashed_row, col), col
    assert plain_row.user_email == _ah.compute_user_hash(admin_user.username)
    assert anon.user_email == "" and anon.user_agent == "" and anon.user_id is None
    assert anon.client_ip == _analytics.pseudonymise_client_ip("2001:db8::1")
    dwell = {r.user_identifier for r in db_session.query(_SlideDwellEvent)}
    assert dwell == {_ah.compute_user_hash("basic:carol"), digest}
    assert db_session.query(_TourSession).one().user_identifier == _ah.compute_user_hash("dave@example.com")
    # Idempotent.
    r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
    assert r.json() == {
        "updated": {"page_views": 0, "slide_dwell_events": 0, "tour_sessions": 0}, "complete": True,
    }
    assert db_session.query(AuditLog).filter(AuditLog.action == "analytics.pseudonymize").count() >= 2


def test_pseudonymize_is_chunked_within_the_time_budget(client, admin_headers, db_session, monkeypatch):
    _set_analytics_mode("hashed")
    monkeypatch.setattr(_analytics, "_PSEUDONYMIZE_CHUNK_ROWS", 2)
    monkeypatch.setattr(_ar, "_CLEAR_TIME_BUDGET_S", 0.0)
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, *[_PageView(kind="top", user_email=f"p{i}@x", client_ip="1.2.3.4") for i in range(5)])
    total, calls = 0, 0
    while True:
        r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
        assert r.status_code == 200, r.text
        total += r.json()["updated"]["page_views"]
        calls += 1
        if r.json()["complete"]:
            break
        assert calls < 10
    assert total == 5 and calls > 1
    db_session.expire_all()
    assert all(_analytics.DIGEST_RE.match(e) for e in db_session.execute(select(_PageView.user_email)).scalars())


def test_pseudonymize_without_a_salt_is_refused(client, admin_headers, instructor_headers, db_session, monkeypatch):
    from wsi_app import analytics_hash as _ah

    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, _PageView(kind="top", user_email="kept@x"))
    assert client.post("/api/admin/analytics/pseudonymize", headers=instructor_headers).status_code == 403
    monkeypatch.setattr(_ah, "_configured_salt", None)
    monkeypatch.setattr(_ah, "_ephemeral_salt", None)
    # In either mode: still "plain" (the PATCH refuses "hashed" without a salt)
    # the missing salt is what the admin has to fix first, so it is the answer
    # — not the mode refusal below, which would send them to a switch they
    # cannot make. And "hashed" with the salt removed out of band.
    for mode in ("plain", "hashed"):
        _set_analytics_mode(mode)
        r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
        assert r.status_code == 409, mode
        assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsSaltUnavailable", mode
    db_session.expire_all()
    assert db_session.query(_PageView).one().user_email == "kept@x"


def test_pseudonymize_is_refused_while_the_mode_is_plain(client, admin_headers, db_session):
    """While the mode is "plain", a live session's later beacons still carry
    the plaintext identity, which no longer matches its converted row — the
    hijack guards then drop them. So conversion waits for the switch, and is
    refused before touching anything (a coded 4xx: nothing changed)."""
    from wsi_app.models import AuditLog

    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, _PageView(kind="top", user_email="kept@x", client_ip="1.2.3.4"))
    audits_before = db_session.query(AuditLog).filter(AuditLog.action == "analytics.pseudonymize").count()
    _set_analytics_mode("plain")
    r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.analyticsPseudonymizeNeedsHashed"
    db_session.expire_all()
    row = db_session.query(_PageView).one()
    assert (row.user_email, row.client_ip) == ("kept@x", "1.2.3.4")
    assert db_session.query(AuditLog).filter(
        AuditLog.action == "analytics.pseudonymize"
    ).count() == audits_before


def test_pseudonymize_runs_while_recording_is_off(client, admin_headers, db_session):
    """With recording off, beacons are discarded before the hijack guards,
    so converting what was kept is safe — no need to turn recording back on."""
    _wipe_analytics_tables(db_session)
    _seed_rows(db_session, _PageView(kind="top", user_email="kept@x", client_ip="1.2.3.4"))
    _set_analytics_mode("off")
    try:
        r = client.post("/api/admin/analytics/pseudonymize", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["updated"]["page_views"] == 1
        db_session.expire_all()
        row = db_session.query(_PageView).one()
        assert row.user_email != "kept@x" and row.client_ip != "1.2.3.4"
    finally:
        _set_analytics_mode("plain")
