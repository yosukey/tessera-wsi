"""Tests for the persistent audit history: audit() DB persistence, the
/api/admin/history endpoint, retention pruning, and the version-upgrade
bootstrap event.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from wsi_app import app_meta, audit_log
from wsi_app.admin_helpers import audit
from wsi_app.app_meta import KEY_APP_VERSION, bootstrap_app_version, set_value
from wsi_app.db import engine
from wsi_app.models import AuditLog
from wsi_app.version import APP_VERSION


def _rows(db, action: str) -> list[AuditLog]:
    return db.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    ).scalars().all()


def _seed(db, action: str, *, actor: str = "", occurred_at=None, target=None) -> AuditLog:
    row = AuditLog(actor_id=None, actor_username=actor, actor_role=None,
                   action=action, target=target, details="")
    if occurred_at is not None:
        row.occurred_at = occurred_at
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# audit() persistence
# ---------------------------------------------------------------------------

def test_audit_persists_row_with_actor_snapshot(db_session, admin_user):
    audit(admin_user, "test.persist", target="42", foo="bar", n=7)
    rows = _rows(db_session, "test.persist")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == admin_user.id
    assert row.actor_username == admin_user.username
    assert row.actor_role == "admin"
    assert row.target == "42"
    assert json.loads(row.details) == {"foo": "bar", "n": 7}
    assert row.occurred_at is not None


def test_audit_persists_none_actor(db_session):
    audit(None, "test.persist_none", target="someone")
    row = _rows(db_session, "test.persist_none")[0]
    assert row.actor_id is None
    assert row.actor_username == ""
    assert row.actor_role is None
    assert row.details == ""


def test_audit_never_raises_when_persistence_breaks(monkeypatch, client, admin_headers):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(audit_log, "SessionLocal", _boom)
    # Direct call: must not raise.
    audit(None, "test.never_break")
    # Endpoint that audits on success still succeeds.
    r = client.post(
        "/api/admin/users",
        json={"username": "audit-nb-user", "password": "longenough1", "role": "manager"},
        headers=admin_headers,
    )
    assert r.status_code == 200


def test_audit_truncates_oversized_details(db_session):
    audit(None, "test.truncate", blob="x" * 20000)
    row = _rows(db_session, "test.truncate")[0]
    parsed = json.loads(row.details)
    assert parsed["truncated"] is True
    assert len(row.details.encode("utf-8")) < 16 * 1024


# ---------------------------------------------------------------------------
# /api/admin/history endpoint
# ---------------------------------------------------------------------------

def test_history_records_real_endpoint_action(client, admin_headers, admin_user, db_session):
    # Unique per run, and the row is then located by username rather than by
    # the created id: the suite shares one SQLite file, and SQLite reuses a
    # rowid once the highest row is deleted (test_admin_users deletes users),
    # so an id can legitimately appear on two different user.create rows.
    username = f"audit-hist-user-{os.urandom(4).hex()}"
    r = client.post(
        "/api/admin/users",
        json={"username": username, "password": "longenough1", "role": "instructor"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    created_id = r.json()["id"]

    r = client.get("/api/admin/history", params={"action": "user.create"}, headers=admin_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    mine = [i for i in items if i["details"].get("target_username") == username]
    assert len(mine) == 1
    item = mine[0]
    assert item["target"] == str(created_id)
    assert item["actor_username"] == admin_user.username
    assert item["actor_role"] == "admin"
    assert item["details"]["target_username"] == username
    # occurred_at is ISO with explicit offset.
    assert datetime.fromisoformat(item["occurred_at"]).tzinfo is not None


def test_history_requires_admin(client, admin_headers, staff_client):
    assert client.get("/api/admin/history").status_code == 401
    assert client.get("/api/admin/history", headers=staff_client("manager")).status_code == 403
    assert client.get("/api/admin/history", headers=staff_client("instructor")).status_code == 403
    assert client.get("/api/admin/history", headers=admin_headers).status_code == 200


def test_history_action_prefix_and_exact_filters(client, admin_headers, db_session):
    _seed(db_session, "fprefix.alpha")
    _seed(db_session, "fprefix.beta")
    _seed(db_session, "fprefixother.gamma")

    r = client.get("/api/admin/history", params={"action": "fprefix."}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert {i["action"] for i in r.json()["items"]} == {"fprefix.alpha", "fprefix.beta"}

    r = client.get("/api/admin/history", params={"action": "fprefix.alpha"}, headers=admin_headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["action"] == "fprefix.alpha"


def test_history_actor_filter(client, admin_headers, db_session):
    _seed(db_session, "factor.one", actor="factor-alice")
    _seed(db_session, "factor.two", actor="factor-bob")
    r = client.get("/api/admin/history", params={"actor": "factor-alice"}, headers=admin_headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["action"] == "factor.one"


def test_history_date_window_filter(client, admin_headers, db_session):
    old = datetime(2024, 1, 10, 12, 0, 0)
    mid = datetime(2024, 1, 15, 12, 0, 0)
    new = datetime(2024, 1, 20, 12, 0, 0)
    _seed(db_session, "fdate.old", occurred_at=old)
    _seed(db_session, "fdate.mid", occurred_at=mid)
    _seed(db_session, "fdate.new", occurred_at=new)

    r = client.get(
        "/api/admin/history",
        params={"action": "fdate.", "since": "2024-01-15", "until": "2024-01-15"},
        headers=admin_headers,
    )
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["action"] == "fdate.mid"

    r = client.get(
        "/api/admin/history",
        params={"action": "fdate.", "since": "2024-01-11"},
        headers=admin_headers,
    )
    assert {i["action"] for i in r.json()["items"]} == {"fdate.mid", "fdate.new"}


def test_history_date_window_honours_analytics_timezone(client, admin_headers, db_session):
    # The since/until day boundaries bucket in the analytics
    # timezone (WSI_ANALYTICS_TIMEZONE), matching the dashboard's daily
    # buckets — not raw UTC midnight. A row at 20:00 UTC on Jan 1 falls on
    # Jan 2 in Asia/Tokyo (UTC+9), so a Tokyo-local day filter must classify
    # it as a Jan 2 event.
    from wsi_app.config import settings

    _seed(db_session, "ftz.evt", occurred_at=datetime(2026, 1, 1, 20, 0, 0))

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "Asia/Tokyo")
    try:
        # since=Jan 2 (local) includes the event ...
        r = client.get(
            "/api/admin/history",
            params={"action": "ftz.", "since": "2026-01-02"},
            headers=admin_headers,
        )
        assert r.json()["total"] == 1
        # ... and an inclusive until=Jan 1 (local) excludes it, since in Tokyo
        # the event belongs to Jan 2, not Jan 1.
        r = client.get(
            "/api/admin/history",
            params={"action": "ftz.", "until": "2026-01-01"},
            headers=admin_headers,
        )
        assert r.json()["total"] == 0
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)


def test_history_reports_the_analytics_timezone(client, admin_headers):
    # The UI renders occurred_at in the zone the since/until filters are read
    # in, so the listing has to say which one that is.
    from wsi_app.config import settings

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "Asia/Tokyo")
    try:
        tz = client.get("/api/admin/history", headers=admin_headers).json()["server_tz"]
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)
    assert tz["tz_name"] == "Asia/Tokyo"
    assert tz["utc_offset"] == "+09:00"


def test_history_invalid_date_is_400(client, admin_headers):
    r = client.get("/api/admin/history", params={"since": "not-a-date"}, headers=admin_headers)
    assert r.status_code == 400


def test_history_pagination(client, admin_headers, db_session):
    for i in range(5):
        _seed(db_session, f"fpage.{i}")
    r = client.get(
        "/api/admin/history",
        params={"action": "fpage.", "limit": 2, "offset": 0},
        headers=admin_headers,
    )
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    # Newest first.
    assert body["items"][0]["action"] == "fpage.4"

    r = client.get(
        "/api/admin/history",
        params={"action": "fpage.", "limit": 2, "offset": 4},
        headers=admin_headers,
    )
    assert [i["action"] for i in r.json()["items"]] == ["fpage.0"]


def test_viewer_settings_update_is_audited(client, admin_headers, db_session):
    r = client.patch(
        "/api/admin/viewer/settings",
        data={"site_title": "Audit Test Title"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    rows = _rows(db_session, "viewer.settings.update")
    assert rows, "viewer settings PATCH should write an audit row"
    assert json.loads(rows[-1].details)["fields"] == ["site_title"]


# ---------------------------------------------------------------------------
# /api/admin/history/export.csv
# ---------------------------------------------------------------------------

def test_history_export_csv_basic(client, admin_headers, db_session):
    _seed(db_session, "fcsv.alpha", actor="csv-alice", target="t1")
    r = client.get("/api/admin/history/export.csv", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert 'filename="audit-history-all.csv"' in cd
    body = r.text
    # UTF-8 BOM for Excel + header row mirroring the JSON listing.
    assert body.startswith("﻿")
    assert "occurred_at,id,actor_id,actor_username,actor_role,action,target,details" in body
    assert "fcsv.alpha" in body
    assert "csv-alice" in body


def test_history_export_csv_requires_admin(client, admin_headers, staff_client):
    assert client.get("/api/admin/history/export.csv").status_code == 401
    assert client.get("/api/admin/history/export.csv", headers=staff_client("manager")).status_code == 403
    assert client.get("/api/admin/history/export.csv", headers=admin_headers).status_code == 200


def test_history_export_csv_honours_filters(client, admin_headers, db_session):
    _seed(db_session, "fcsvflt.alpha", actor="csv-keep")
    _seed(db_session, "fcsvflt.beta", actor="csv-drop")
    r = client.get(
        "/api/admin/history/export.csv",
        params={"action": "fcsvflt.alpha", "actor": "csv-keep"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    assert "fcsvflt.alpha" in body
    assert "fcsvflt.beta" not in body
    # Date range narrows the filename span label.
    r = client.get(
        "/api/admin/history/export.csv",
        params={"since": "2024-01-01", "until": "2024-01-31"},
        headers=admin_headers,
    )
    assert 'filename="audit-history-2024-01-01_2024-01-31.csv"' in r.headers.get("content-disposition", "")


def test_history_export_csv_defangs_formula_cells(client, admin_headers, db_session):
    # A crafted actor/target beginning with a formula trigger must be
    # neutralised so spreadsheets treat the cell as text, not a formula.
    _seed(db_session, "fcsvinj.alpha", actor="=HYPERLINK(\"http://evil\")", target="+danger")
    r = client.get(
        "/api/admin/history/export.csv",
        params={"action": "fcsvinj.alpha"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.text
    assert "'=HYPERLINK" in body
    assert "'+danger" in body


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------

def test_prune_row_cap_keeps_newest(db_session):
    seeded_ids = [_seed(db_session, f"fprune.row.{i}").id for i in range(6)]
    assert seeded_ids == sorted(seeded_ids)  # insertion order == id order
    deleted = audit_log.prune_audit_logs(db_session, max_rows=3, max_age_days=0)
    db_session.commit()
    assert deleted >= 3
    survivors = db_session.execute(
        select(AuditLog.action).where(AuditLog.action.like("fprune.row.%"))
    ).scalars().all()
    # The 3 newest rows in the whole table are the last 3 seeded here.
    assert set(survivors) == {"fprune.row.3", "fprune.row.4", "fprune.row.5"}


def test_prune_age_cap_drops_old_rows(db_session):
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
    _seed(db_session, "fprune.age.old", occurred_at=stale)
    _seed(db_session, "fprune.age.new")
    deleted = audit_log.prune_audit_logs(db_session, max_rows=0, max_age_days=30)
    db_session.commit()
    assert deleted >= 1
    survivors = db_session.execute(
        select(AuditLog.action).where(AuditLog.action.like("fprune.age.%"))
    ).scalars().all()
    assert survivors == ["fprune.age.new"]


def test_prune_zero_caps_disable(db_session):
    _seed(db_session, "fprune.off", occurred_at=datetime(2000, 1, 1))
    deleted = audit_log.prune_audit_logs(db_session, max_rows=0, max_age_days=0)
    db_session.commit()
    assert deleted == 0
    assert len(_rows(db_session, "fprune.off")) == 1


# ---------------------------------------------------------------------------
# Version-upgrade bootstrap event
# ---------------------------------------------------------------------------

def _upgrade_rows(db):
    return [
        r for r in _rows(db, "system.version_upgrade")
        if json.loads(r.details).get("from") == "0.0.1"
    ]


def test_bootstrap_records_version_upgrade_once(db_session):
    set_value(db_session, KEY_APP_VERSION, "0.0.1")
    db_session.commit()

    bootstrap_app_version(engine)
    db_session.expire_all()
    rows = _upgrade_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id is None
    assert row.target == APP_VERSION
    assert json.loads(row.details) == {"from": "0.0.1", "to": APP_VERSION}

    # Re-running with the version already stamped adds nothing.
    bootstrap_app_version(engine)
    db_session.expire_all()
    assert len(_upgrade_rows(db_session)) == 1


def test_bootstrap_fresh_install_records_no_upgrade(db_session):
    # Simulate a fresh DB: no recorded version at all.
    row = db_session.get(app_meta.AppMeta, KEY_APP_VERSION)
    if row is not None:
        db_session.delete(row)
        db_session.commit()
    before = len(_rows(db_session, "system.version_upgrade"))
    bootstrap_app_version(engine)
    db_session.expire_all()
    assert len(_rows(db_session, "system.version_upgrade")) == before


def test_auth_settings_audit_records_what_changed_without_secrets(client, admin_headers, db_session):
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "lan_direct_noauth_mode": "lan",
            "ldap_server_uri": "ldap://dir.example.org",
            "ldap_bind_password": "s3cret-value",
            "google_allowed_domains": "example.org",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    details = json.loads(_rows(db_session, "auth.settings.update")[-1].details)
    changes = details["changes"]
    assert changes["lan_direct_noauth_mode"][1] == "lan"
    assert changes["ldap_server_uri"] == ["", "ldap://dir.example.org"]
    assert changes["google_allowed_domains"] == ["", "example.org"]
    assert changes["ldap_bind_password"] == "set"
    assert "s3cret-value" not in json.dumps(details)
    assert details["sessions_revoked"] is True


def test_network_settings_audit_records_ga_id_change(client, admin_headers, db_session):
    r = client.patch(
        "/api/admin/network/settings",
        data={"ga_measurement_id": "G-ABCDEF1234"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    details = json.loads(_rows(db_session, "network.settings.update")[-1].details)
    assert details["ga_measurement_id_changed"][1] == "G-ABCDEF1234"


def test_admin_login_audit_carries_source_ip(client, admin_user, db_session):
    client.post("/api/admin/login", json={"username": "nobody-here", "password": "x"})
    row = _rows(db_session, "admin.login.failure")[-1]
    assert json.loads(row.details)["ip"]


def test_prune_caps_auth_failure_rows_separately(db_session):
    """A spray of failed logins must not push real admin actions out: failure
    rows keep only the newest N, while everything else is untouched by that
    cap."""
    real = [_seed(db_session, f"fprune.real.{i}").id for i in range(3)]
    for i in range(5):
        _seed(db_session, f"admin.login.failure.{i}")
        _seed(db_session, f"app.login.failure.{i}")
    # Interleave a later real action so the failures are not simply the tail.
    last_real = _seed(db_session, "fprune.real.last").id

    deleted = audit_log.prune_audit_logs(
        db_session, max_rows=0, max_age_days=0, max_auth_failure_rows=4
    )
    db_session.commit()
    assert deleted >= 6

    failures = db_session.execute(
        select(AuditLog.action).where(
            AuditLog.action.like("admin.login.failure%")
            | AuditLog.action.like("app.login.failure%")
        ).order_by(AuditLog.id)
    ).scalars().all()
    # Newest 4 of the 10 failure rows survive (across both prefixes).
    assert failures == [
        "admin.login.failure.3", "app.login.failure.3",
        "admin.login.failure.4", "app.login.failure.4",
    ]
    kept = set(db_session.execute(
        select(AuditLog.id).where(AuditLog.action.like("fprune.real.%"))
    ).scalars())
    assert set(real) | {last_real} <= kept


def test_prune_auth_failure_cap_zero_disables(db_session):
    for i in range(3):
        _seed(db_session, f"admin.login.failure.z{i}")
    audit_log.prune_audit_logs(
        db_session, max_rows=0, max_age_days=0, max_auth_failure_rows=0
    )
    db_session.commit()
    assert len(db_session.execute(
        select(AuditLog.id).where(AuditLog.action.like("admin.login.failure.z%"))
    ).scalars().all()) == 3


def test_failure_burst_larger_than_global_cap_does_not_evict_real_actions(db_session):
    """The failure cap must run before the global cap: otherwise a burst of
    failed-login rows that alone exceeds max_rows makes the global cap drop the
    OLDEST rows first, i.e. the real admin actions the failure cap protects."""
    from sqlalchemy import delete

    db_session.execute(delete(AuditLog))
    db_session.commit()
    real = [_seed(db_session, f"fburst.real.{i}").id for i in range(5)]
    for i in range(100):
        _seed(db_session, "admin.login.failure")

    audit_log.prune_audit_logs(
        db_session, max_rows=50, max_age_days=0, max_auth_failure_rows=10
    )
    db_session.commit()

    kept_real = set(db_session.execute(
        select(AuditLog.id).where(AuditLog.action.like("fburst.real.%"))
    ).scalars())
    assert kept_real == set(real)
    n_fail = db_session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "admin.login.failure")
    ).scalar()
    assert n_fail == 10


# ---------------------------------------------------------------------------
# Shared plumbing: the retention throttle (audit / error / analytics tables)
# and the analytics-timezone resolution the history filters read days in
# ---------------------------------------------------------------------------

def test_prune_throttle_hands_out_one_prune_per_interval(monkeypatch):
    from wsi_app import retention

    clock = [5000.0]
    monkeypatch.setattr(retention.time, "monotonic", lambda: clock[0])
    throttle = retention.PruneThrottle(3600)
    assert throttle.claim() is True
    assert throttle.claim() is False
    clock[0] += 3599
    assert throttle.claim() is False
    clock[0] += 2
    assert throttle.claim() is True


def test_opportunistic_prune_failure_is_logged_not_raised(caplog):
    import logging

    from wsi_app import retention

    def boom(db):
        raise RuntimeError("disk full")

    with caplog.at_level(logging.WARNING, logger="wsi_app.retention_test"):
        retention.maybe_prune(
            retention.PruneThrottle(), boom, logging.getLogger("wsi_app.retention_test"), "t",
        )
    assert "opportunistic t prune failed" in caplog.text


def test_host_timezone_is_resolved_once_per_process(monkeypatch):
    from wsi_app import admin_reports
    from wsi_app.config import settings

    calls = []
    real_readlink = admin_reports.os.readlink

    def counting_readlink(path):
        calls.append(path)
        return real_readlink(path)

    monkeypatch.setattr(admin_reports.os, "readlink", counting_readlink)
    monkeypatch.delenv("TZ", raising=False)
    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "")
    admin_reports._host_tz_name.cache_clear()
    try:
        for _ in range(3):
            admin_reports.analytics_tz()
            admin_reports.server_tz_info()
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)
        admin_reports._host_tz_name.cache_clear()
    assert len(calls) <= 1


def test_invalid_analytics_timezone_is_reported_once(caplog):
    import logging

    from wsi_app import admin_reports
    from wsi_app.config import settings

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "Asai/Tokyo")
    admin_reports._tz_warned.discard("Asai/Tokyo")
    try:
        with caplog.at_level(logging.WARNING, logger="wsi_app.analytics"):
            for _ in range(3):
                admin_reports.analytics_tz()
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)
    assert sum("Asai/Tokyo" in m for m in caplog.messages) == 1


# ---------------------------------------------------------------------------
# Listing filters: partial actor (and login-failure targets), target, system
# ---------------------------------------------------------------------------

def _actions(client, headers, **params) -> set[str]:
    r = client.get("/api/admin/history", params=params, headers=headers)
    assert r.status_code == 200
    return {i["action"] for i in r.json()["items"]}


def test_history_actor_filter_matches_partial_names(client, admin_headers, db_session):
    _seed(db_session, "fpart.one", actor="fpart-alice-admin")
    _seed(db_session, "fpart.two", actor="fpart-bob")
    assert _actions(client, admin_headers, action="fpart.", actor="ALICE") == {"fpart.one"}


def test_history_actor_filter_finds_failed_logins_against_the_account(client, admin_headers, db_session):
    _seed(db_session, "admin.login.failure", target="fpartlogin-carol")
    _seed(db_session, "fpartlogin.other", target="fpartlogin-carol")
    got = _actions(client, admin_headers, actor="fpartlogin-carol")
    # The failed login matches on its target; an unrelated action targeting
    # the same string does not (the actor filter is about who acted).
    assert "admin.login.failure" in got
    assert "fpartlogin.other" not in got


def test_history_target_and_system_filters(client, admin_headers, db_session):
    _seed(db_session, "ftgt.sys", target="slide-ftgt-91")
    _seed(db_session, "ftgt.user", actor="ftgt-dave", target="slide-ftgt-91")
    _seed(db_session, "ftgt.elsewhere", target="tour-ftgt-1")
    assert _actions(client, admin_headers, action="ftgt.", target="ftgt-91") == {"ftgt.sys", "ftgt.user"}
    assert _actions(client, admin_headers, action="ftgt.", system="true") == {"ftgt.sys", "ftgt.elsewhere"}


def test_history_snapshot_keeps_pages_stable(client, admin_headers, db_session):
    for i in range(3):
        _seed(db_session, f"fhsnap.{i}")
    first = client.get("/api/admin/history", params={"action": "fhsnap.", "limit": 1},
                       headers=admin_headers).json()
    snap = first["snapshot_id"]
    _seed(db_session, "fhsnap.late")
    later = client.get("/api/admin/history",
                       params={"action": "fhsnap.", "limit": 1, "offset": 1, "max_id": snap},
                       headers=admin_headers).json()
    assert later["total"] == 3 and later["newer"] == 1
    assert [i["action"] for i in later["items"]] == ["fhsnap.1"]


def test_history_export_honours_the_filters(client, admin_headers, db_session):
    _seed(db_session, "fhexp.keep", target="fhexp-target")
    _seed(db_session, "fhexp.drop", actor="fhexp-erin", target="other")
    body = client.get("/api/admin/history/export.csv",
                      params={"action": "fhexp.", "target": "fhexp-t", "system": "true"},
                      headers=admin_headers).text
    assert "fhexp.keep" in body and "fhexp.drop" not in body


def test_unknown_admin_login_username_is_masked(client, admin_user, db_session):
    typed = "Tr0ub4dor&3-mistyped-into-username"
    client.post("/api/admin/login", json={"username": typed, "password": "x"})
    row = _rows(db_session, "admin.login.failure")[-1]
    assert typed not in (row.target or "")
    assert row.target == f"T… ({len(typed)} chars)"
    assert json.loads(row.details)["unknown_user"] is True


def test_known_admin_login_username_is_kept(client, admin_user, db_session):
    client.post("/api/admin/login", json={"username": admin_user.username, "password": "wrong"})
    row = _rows(db_session, "admin.login.failure")[-1]
    assert row.target == admin_user.username


def test_audit_age_prune_never_empties_the_table(db_session):
    from sqlalchemy import delete as sa_delete

    db_session.execute(sa_delete(AuditLog))
    db_session.commit()
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4000)
    rows = [_seed(db_session, f"fagemono.{i}", occurred_at=old) for i in range(3)]
    audit_log.prune_audit_logs(db_session, max_rows=0, max_age_days=730)
    db_session.commit()
    assert db_session.execute(select(AuditLog.id)).scalars().all() == [rows[-1].id]
