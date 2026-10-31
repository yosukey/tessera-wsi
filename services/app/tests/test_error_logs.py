"""Tests for the DB error-log capture: persist_log_record, the logging
handler end-to-end, the /api/admin/logs endpoint + CSV export, and
retention pruning. Mirrors test_audit_history.py.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.requests import Request

from wsi_app import error_log
from wsi_app.main import _log_unhandled_exception
from wsi_app.models import ErrorLog


def _rows(db, logger: str) -> list[ErrorLog]:
    return db.execute(
        select(ErrorLog).where(ErrorLog.logger == logger).order_by(ErrorLog.id)
    ).scalars().all()


def _seed(db, logger: str, *, level: str = "ERROR", message: str = "",
          occurred_at=None) -> ErrorLog:
    row = ErrorLog(level=level, logger=logger, message=message, traceback="")
    if occurred_at is not None:
        row.occurred_at = occurred_at
    db.add(row)
    db.commit()
    return row


def _record(name: str, level: int, msg: str, *, exc_info=None, **extra) -> logging.LogRecord:
    rec = logging.LogRecord(name, level, "some/module.py", 42, msg, None, exc_info,
                            func="do_thing")
    rec.module = "module"
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


# ---------------------------------------------------------------------------
# persist_log_record
# ---------------------------------------------------------------------------

def test_persist_writes_row_from_record(db_session):
    error_log.persist_log_record(_record("wsi_app.persist", logging.WARNING, "hello %s", args=None))
    rows = _rows(db_session, "wsi_app.persist")
    assert len(rows) == 1
    row = rows[0]
    assert row.level == "WARNING"
    assert row.message == "hello %s"
    assert row.module == "module"
    assert row.func == "do_thing"
    assert row.line == 42
    assert row.traceback == ""
    assert row.occurred_at is not None


def test_persist_captures_exception_traceback(db_session):
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        rec = _record("wsi_app.trace", logging.ERROR, "boom", exc_info=sys.exc_info())
    error_log.persist_log_record(rec)
    row = _rows(db_session, "wsi_app.trace")[0]
    assert "ValueError: kaboom" in row.traceback
    assert "Traceback" in row.traceback


def test_persist_captures_request_context(db_session):
    rec = _record("wsi_app.reqctx", logging.ERROR, "unhandled",
                  request_path="/api/admin/x", request_method="POST")
    error_log.persist_log_record(rec)
    row = _rows(db_session, "wsi_app.reqctx")[0]
    assert row.request_path == "/api/admin/x"
    assert row.request_method == "POST"


def test_persist_truncates_oversized_traceback(db_session):
    import sys
    try:
        raise RuntimeError("x" * 40000)
    except RuntimeError:
        rec = _record("wsi_app.trunc", logging.ERROR, "big", exc_info=sys.exc_info())
    error_log.persist_log_record(rec)
    row = _rows(db_session, "wsi_app.trunc")[0]
    assert len(row.traceback.encode("utf-8")) <= 16 * 1024 + 32
    assert row.traceback.endswith("… [truncated]")


def test_app_engine_hides_sql_parameters_in_errors():
    """A failing statement's bound values (e.g. an LDAP bind password on a
    settings UPDATE) must not appear in the exception text that gets logged."""
    from wsi_app.db import engine

    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "UPDATE no_such_table SET ldap_bind_password = ?", ("s3cret-bind-pw",)
            )
    except Exception as exc:
        text = str(exc)
    else:  # pragma: no cover - the statement must fail
        raise AssertionError("statement unexpectedly succeeded")
    assert "no_such_table" in text
    assert "s3cret-bind-pw" not in text


def test_persist_scrubs_sql_parameters_from_other_engines(db_session):
    """Backstop for an engine without hide_parameters: the persisted message
    and traceback drop SQLAlchemy's "[parameters: ...]" line."""
    import sys

    from sqlalchemy import create_engine

    leaky = create_engine("sqlite://")
    # Held in a variable: the traceback quotes this source line, and a literal
    # there would defeat the assertion below.
    secret = "-".join(["s3cret", "bind", "pw"])
    try:
        with leaky.connect() as conn:
            conn.exec_driver_sql("INSERT INTO nope VALUES (?)", (secret,))
    except Exception as exc:
        rec = _record("wsi_app.sqlparams", logging.ERROR, f"failed: {exc}",
                      exc_info=sys.exc_info())
    error_log.persist_log_record(rec)
    row = _rows(db_session, "wsi_app.sqlparams")[0]
    for text in (row.message, row.traceback):
        assert secret not in text
        assert "[SQL parameters hidden]" in text
        assert "[SQL: INSERT INTO nope VALUES (?)]" in text


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is new in Python 3.11")
def test_persist_scrubs_sql_parameters_inside_exception_groups(db_session):
    """An ExceptionGroup traceback prefixes each member's lines with "  | ",
    so a line-anchored "[parameters: ...]" pattern would never match them."""
    import sys

    from sqlalchemy import create_engine

    leaky = create_engine("sqlite://")
    secret = "-".join(["s3cret", "group", "pw"])  # see the sibling test
    try:
        with leaky.connect() as conn:
            conn.exec_driver_sql("INSERT INTO nope VALUES (?)", (secret,))
    except Exception as exc:
        inner = exc
    try:
        # Nested, so the member's lines carry a deeper "    | " prefix too.
        raise ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    except ExceptionGroup:
        rec = _record("wsi_app.sqlparams_group", logging.ERROR, "group failed",
                      exc_info=sys.exc_info())
    assert "| [parameters: " in error_log._formatter.formatException(rec.exc_info)

    error_log.persist_log_record(rec)
    tb = _rows(db_session, "wsi_app.sqlparams_group")[0].traceback
    assert secret not in tb
    # The prefix survives, so the frame drawing stays aligned.
    assert "| [SQL parameters hidden]" in tb
    assert "| [SQL: INSERT INTO nope VALUES (?)]" in tb


def test_scrub_sql_params_keeps_prefix_and_neighbouring_lines():
    text = "x\n\n  | [parameters: ('pw',)]\n  | (Background on this error)\n"
    assert error_log._scrub_sql_params(text) == (
        "x\n\n  | [SQL parameters hidden]\n  | (Background on this error)\n"
    )


def test_persist_never_raises_when_db_breaks(monkeypatch, db_session):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(error_log, "SessionLocal", _boom)
    # Must swallow the failure — the journald line is the only record.
    error_log.persist_log_record(_record("wsi_app.nb", logging.ERROR, "x"))


# ---------------------------------------------------------------------------
# Logging handler end-to-end (QueueHandler -> QueueListener -> DB)
# ---------------------------------------------------------------------------

def test_capture_flows_from_logger_to_db(db_session):
    error_log.install_error_log_capture()
    try:
        logging.getLogger("wsi_app.e2e").warning("captured warning")
        try:
            raise KeyError("missing")
        except KeyError:
            logging.getLogger("wsi_app.e2e").exception("captured error")
    finally:
        # stop() drains the queue on the listener thread before returning,
        # so the rows are visible synchronously after this call.
        error_log.shutdown_error_log_capture()

    rows = _rows(db_session, "wsi_app.e2e")
    assert {r.level for r in rows} == {"WARNING", "ERROR"}
    err = [r for r in rows if r.level == "ERROR"][0]
    assert "KeyError" in err.traceback


def test_capture_ignores_below_threshold_and_foreign_loggers(db_session):
    error_log.install_error_log_capture()
    try:
        logging.getLogger("wsi_app.thresh").info("info is below WARNING")
        logging.getLogger("sqlalchemy.engine").error("foreign logger, not wsi_app")
    finally:
        error_log.shutdown_error_log_capture()
    assert _rows(db_session, "wsi_app.thresh") == []
    assert _rows(db_session, "sqlalchemy.engine") == []


def test_configured_info_level_actually_captures_info(db_session):
    # Setting WSI_ERROR_LOG_LEVEL=INFO must actually capture INFO
    # records. The handler level alone is not enough — the ``wsi_app`` logger's
    # own effective level defaults to WARNING and would drop INFO before any
    # handler sees it, so install must lower the logger level too (and shutdown
    # must restore it).
    from wsi_app.config import settings
    wsi_logger = logging.getLogger("wsi_app")
    original = settings.error_log_level
    level_before = wsi_logger.level
    object.__setattr__(settings, "error_log_level", "INFO")
    try:
        error_log.install_error_log_capture()
        try:
            logging.getLogger("wsi_app.lvlcfg").info("info now captured")
            logging.getLogger("wsi_app.lvlcfg").debug("debug still below INFO")
        finally:
            error_log.shutdown_error_log_capture()
    finally:
        object.__setattr__(settings, "error_log_level", original)
    rows = _rows(db_session, "wsi_app.lvlcfg")
    assert [r.level for r in rows] == ["INFO"]
    # The logger level is restored to whatever it was before install ran.
    assert wsi_logger.level == level_before


def test_install_replays_backlog_at_the_capture_level(db_session):
    def record(level, msg):
        return logging.getLogger("wsi_app.backlog").makeRecord(
            "wsi_app.backlog", level, __file__, 1, msg, (), None,
        )

    error_log.install_error_log_capture(
        backlog=[record(logging.INFO, "buffered info"),
                 record(logging.WARNING, "buffered warning")],
    )
    error_log.shutdown_error_log_capture()
    rows = _rows(db_session, "wsi_app.backlog")
    assert [(r.level, r.message) for r in rows] == [("WARNING", "buffered warning")]


def test_boot_warnings_reach_error_logs(monkeypatch, db_session):
    """Records logged during the lifespan's schema phase — before error_logs
    is guaranteed to exist — must not reach only journald, as they would with
    the capture installed after the schema block, the startup banners and the
    janitors. They are buffered and written once the capture starts, and the
    banners after the schema block are captured directly."""
    from fastapi.testclient import TestClient

    from wsi_app import admin_helpers, main
    from wsi_app.config import settings

    def pool_warning():
        logging.getLogger("wsi_app.db").warning("boot probe: before the schema lock")

    def schema_warning():
        logging.getLogger("wsi_app.startup").warning("boot probe: inside the schema phase")

    monkeypatch.setattr(main, "configure_request_concurrency", pool_warning)
    monkeypatch.setattr(main, "_warn_nullability_divergence", schema_warning)
    monkeypatch.setattr(main, "bootstrap_admin", lambda: None)
    monkeypatch.setattr(admin_helpers, "reap_orphan_slide_reservations", lambda db: None)
    handlers_before = list(logging.getLogger("wsi_app").handlers)
    object.__setattr__(settings, "allow_admin_register", True)
    try:
        with TestClient(main.app):
            pass
    finally:
        object.__setattr__(settings, "allow_admin_register", False)
    # The buffer is gone once the capture has taken over.
    assert logging.getLogger("wsi_app").handlers == handlers_before

    messages = [
        r.message
        for r in db_session.execute(select(ErrorLog)).scalars()
        if r.message.startswith(("boot probe:", "SECURITY: ALLOW_ADMIN_REGISTER"))
    ]
    assert "boot probe: before the schema lock" in messages
    assert "boot probe: inside the schema phase" in messages
    assert any(m.startswith("SECURITY: ALLOW_ADMIN_REGISTER") for m in messages)


def test_failed_boot_detaches_the_buffer(monkeypatch):
    from fastapi.testclient import TestClient

    from wsi_app import main

    def boom():
        raise RuntimeError("boot aborted")

    monkeypatch.setattr(main, "_reconcile_interrupted_restore_apply", boom)
    handlers_before = list(logging.getLogger("wsi_app").handlers)
    with pytest.raises(RuntimeError, match="boot aborted"):
        with TestClient(main.app):
            pass
    assert logging.getLogger("wsi_app").handlers == handlers_before


def test_persist_stamps_the_time_the_record_was_logged(db_session):
    """occurred_at is the record's own time, not when the listener got round
    to the INSERT (a busy_timeout wait, or the boot backlog's replay)."""
    rec = _record("wsi_app.when", logging.ERROR, "late write")
    rec.created = datetime(2024, 3, 1, 12, 30, 45, 678000, tzinfo=timezone.utc).timestamp()
    error_log.persist_log_record(rec)
    row = _rows(db_session, "wsi_app.when")[0]
    assert row.occurred_at.replace(tzinfo=None) == datetime(2024, 3, 1, 12, 30, 45)


def test_capture_renders_the_message_when_it_is_logged(db_session):
    """The row carries the message as it read at the logging call, even when
    the caller changes the argument before the listener writes the row."""
    args = ["before"]
    error_log.install_error_log_capture()
    try:
        logging.getLogger("wsi_app.eager").warning("value=%s", args)
        args[0] = "after"
        try:
            raise ValueError("eager tb")
        except ValueError:
            logging.getLogger("wsi_app.eager").exception("with trace")
    finally:
        error_log.shutdown_error_log_capture()
    rows = _rows(db_session, "wsi_app.eager")
    assert rows[0].message == "value=['before']"
    assert "ValueError: eager tb" in rows[1].traceback
    assert rows[1].message == "with trace"


def test_queued_copy_leaves_the_original_record_untouched():
    import queue as _queue

    qh = error_log._InProcessQueueHandler(_queue.Queue())
    try:
        raise KeyError("k")
    except KeyError:
        rec = _record("wsi_app.copy", logging.ERROR, "x=%s", exc_info=sys.exc_info(), args=None)
    rec.args = (1,)
    qh.emit(rec)
    queued = qh.queue.get_nowait()
    assert queued is not rec
    assert queued.exc_info is None and queued.args is None and queued.msg == "x=1"
    # The stderr/journald handlers further up still see the original record.
    assert rec.exc_info is not None and rec.args == (1,) and rec.msg == "x=%s"


def test_repeat_throttle_admits_a_burst_per_call_site(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(error_log.time, "monotonic", lambda: now[0])
    throttle = error_log._RepeatThrottle(window=60, burst=3, max_keys=8)
    site = _record("wsi_app.flood", logging.ERROR, "slide %s failed")
    other = _record("wsi_app.flood", logging.ERROR, "another template")

    assert [throttle.admit(site) for _ in range(5)] == [0, 0, 0, None, None]
    # A different template is its own call site.
    assert throttle.admit(other) == 0
    # Next window: admitted, reporting the two held back.
    now[0] += 61
    assert throttle.admit(site) == 2
    assert throttle.admit(site) == 0


def test_repeat_throttle_key_table_is_bounded(monkeypatch):
    monkeypatch.setattr(error_log.time, "monotonic", lambda: 5.0)
    throttle = error_log._RepeatThrottle(window=60, burst=1, max_keys=4)
    for i in range(10):
        assert throttle.admit(_record("wsi_app.keys", logging.ERROR, f"t{i}")) == 0
    assert len(throttle._slots) <= 4


def test_capture_throttles_a_repeating_call_site(db_session):
    error_log.install_error_log_capture()
    try:
        for i in range(error_log._REPEAT_BURST + 7):
            logging.getLogger("wsi_app.repeat").warning("broken slide %s", i)
    finally:
        error_log.shutdown_error_log_capture()
    assert len(_rows(db_session, "wsi_app.repeat")) == error_log._REPEAT_BURST


def test_full_queue_drops_are_reported_in_a_row_of_their_own(db_session):
    """The drops are counted and written as their own row once the listener
    gets to them — not as a note on a record that was queued before them."""
    import queue as _queue

    error_log._take_dropped()
    qh = error_log._InProcessQueueHandler(_queue.Queue(maxsize=1))
    for i in range(3):
        qh.emit(_record("wsi_app.full", logging.ERROR, f"site {i}"))
    assert qh.queue.qsize() == 1
    error_log.DBLogHandler().emit(qh.queue.get_nowait())
    assert [r.message for r in _rows(db_session, "wsi_app.full")] == ["site 0"]
    note = _rows(db_session, "wsi_app.error_log")[-1]
    assert note.level == "WARNING"
    assert note.message.startswith("2 log record(s) were not stored")
    assert error_log._take_dropped() == 0


def test_dropped_count_survives_a_failed_note(monkeypatch, db_session):
    error_log._take_dropped()
    error_log._note_dropped(3)
    monkeypatch.setattr(error_log, "_persist_dropped_note", lambda n: False)
    error_log.DBLogHandler().emit(_record("wsi_app.fullfail", logging.ERROR, "x"))
    assert error_log._take_dropped() == 3


def test_listener_stops_with_a_full_queue(db_session):
    """stop() must not raise when the queue is full at shutdown."""
    import queue as _queue

    q = _queue.Queue(maxsize=1)
    listener = error_log._QueueListener(q, error_log.DBLogHandler(), respect_handler_level=False)
    q.put_nowait(error_log._InProcessQueueHandler(q).prepare(
        _record("wsi_app.stopfull", logging.ERROR, "queued")))
    listener.start()
    listener.stop()
    assert [r.message for r in _rows(db_session, "wsi_app.stopfull")] == ["queued"]


def test_dzi_open_failure_is_logged_once_per_window(caplog):
    from wsi_app import dzi

    dzi._tile_err_last.clear()
    try:
        with caplog.at_level(logging.DEBUG, logger="wsi_app.dzi"):
            for _ in range(3):
                try:
                    raise OSError("unreadable pyramid")
                except OSError as exc:
                    dzi._log_open_failure(4242, exc)
        full = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(full) == 1 and full[0].exc_info is not None
        assert sum(r.levelno == logging.DEBUG for r in caplog.records) == 2
    finally:
        dzi._tile_err_last.clear()


# ---------------------------------------------------------------------------
# Global unhandled-exception handler
# ---------------------------------------------------------------------------

def test_unhandled_exception_handler_captures_and_returns_500(db_session):
    scope = {
        "type": "http", "method": "DELETE", "path": "/api/admin/boom",
        "headers": [], "query_string": b"",
    }
    req = Request(scope)
    error_log.install_error_log_capture()
    try:
        try:
            raise RuntimeError("unexpected boom")
        except RuntimeError as exc:
            resp = asyncio.run(_log_unhandled_exception(req, exc))
    finally:
        error_log.shutdown_error_log_capture()

    # Response shape is preserved: the same plain 500 Starlette would emit.
    assert resp.status_code == 500
    assert resp.body == b"Internal Server Error"

    rows = _rows(db_session, "wsi_app.unhandled")
    assert len(rows) == 1
    row = rows[0]
    assert row.level == "ERROR"
    assert row.request_method == "DELETE"
    assert row.request_path == "/api/admin/boom"
    assert "RuntimeError: unexpected boom" in row.traceback


# ---------------------------------------------------------------------------
# /api/admin/logs endpoint
# ---------------------------------------------------------------------------

def test_logs_requires_admin(client, admin_headers, staff_client):
    assert client.get("/api/admin/logs").status_code == 401
    assert client.get("/api/admin/logs", headers=staff_client("manager")).status_code == 403
    assert client.get("/api/admin/logs", headers=staff_client("instructor")).status_code == 403
    assert client.get("/api/admin/logs", headers=admin_headers).status_code == 200


def test_logs_level_filter(client, admin_headers, db_session):
    _seed(db_session, "flvl.one", level="ERROR")
    _seed(db_session, "flvl.two", level="WARNING")
    r = client.get("/api/admin/logs", params={"level": "ERROR", "logger": "flvl."}, headers=admin_headers)
    assert r.status_code == 200
    assert {i["logger"] for i in r.json()["items"]} == {"flvl.one"}


def test_logs_level_filter_accepts_info_and_debug(client, admin_headers, db_session):
    # WSI_ERROR_LOG_LEVEL can lower the capture threshold, so the UI offers
    # INFO / DEBUG; the filter must match those rows like any other level.
    _seed(db_session, "flvx.info", level="INFO")
    _seed(db_session, "flvx.debug", level="DEBUG")
    for level, name in (("INFO", "flvx.info"), ("DEBUG", "flvx.debug")):
        r = client.get("/api/admin/logs", params={"level": level, "logger": "flvx."}, headers=admin_headers)
        assert {i["logger"] for i in r.json()["items"]} == {name}


def test_logs_reports_the_analytics_timezone(client, admin_headers):
    from wsi_app.config import settings

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "Asia/Tokyo")
    try:
        tz = client.get("/api/admin/logs", headers=admin_headers).json()["server_tz"]
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)
    assert tz["tz_name"] == "Asia/Tokyo"
    assert tz["utc_offset"] == "+09:00"


def test_logs_logger_prefix_and_exact_filters(client, admin_headers, db_session):
    _seed(db_session, "fpfx.alpha")
    _seed(db_session, "fpfx.beta")
    _seed(db_session, "fpfxother.gamma")
    r = client.get("/api/admin/logs", params={"logger": "fpfx."}, headers=admin_headers)
    assert r.json()["total"] == 2
    r = client.get("/api/admin/logs", params={"logger": "fpfx.alpha"}, headers=admin_headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["logger"] == "fpfx.alpha"


def test_logs_search_filter(client, admin_headers, db_session):
    _seed(db_session, "fsearch.a", message="disk is full")
    _seed(db_session, "fsearch.b", message="all good here")
    r = client.get("/api/admin/logs", params={"search": "disk", "logger": "fsearch."}, headers=admin_headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["logger"] == "fsearch.a"


def test_logs_date_window_filter(client, admin_headers, db_session):
    old = datetime(2024, 1, 10, 12, 0, 0)
    mid = datetime(2024, 1, 15, 12, 0, 0)
    new = datetime(2024, 1, 20, 12, 0, 0)
    _seed(db_session, "fdt.old", occurred_at=old)
    _seed(db_session, "fdt.mid", occurred_at=mid)
    _seed(db_session, "fdt.new", occurred_at=new)
    r = client.get(
        "/api/admin/logs",
        params={"logger": "fdt.", "since": "2024-01-15", "until": "2024-01-15"},
        headers=admin_headers,
    )
    assert {i["logger"] for i in r.json()["items"]} == {"fdt.mid"}


def test_logs_invalid_date_is_400(client, admin_headers):
    r = client.get("/api/admin/logs", params={"since": "not-a-date"}, headers=admin_headers)
    assert r.status_code == 400


def test_logs_pagination_newest_first(client, admin_headers, db_session):
    for i in range(5):
        _seed(db_session, f"fpg.{i}")
    r = client.get("/api/admin/logs", params={"logger": "fpg.", "limit": 2, "offset": 0}, headers=admin_headers)
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["items"][0]["logger"] == "fpg.4"


# ---------------------------------------------------------------------------
# /api/admin/logs/export.csv
# ---------------------------------------------------------------------------

def test_logs_export_csv_basic(client, admin_headers, db_session):
    _seed(db_session, "fcsv.a", level="ERROR", message="boom")
    r = client.get("/api/admin/logs/export.csv", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    cd = r.headers.get("content-disposition", "")
    assert 'filename="error-logs-all.csv"' in cd
    body = r.text
    assert "occurred_at,id,level,logger,message" in body
    assert "fcsv.a" in body


def test_logs_export_csv_requires_admin(client, admin_headers, staff_client):
    assert client.get("/api/admin/logs/export.csv").status_code == 401
    assert client.get("/api/admin/logs/export.csv", headers=staff_client("manager")).status_code == 403
    assert client.get("/api/admin/logs/export.csv", headers=admin_headers).status_code == 200


def test_logs_export_csv_follows_retention_cap(client, admin_headers, db_session):
    # The export ceiling follows WSI_ERROR_LOG_MAX_ROWS (via
    # resolve_export_limit): with the cap set to 2, an export of 4 rows must
    # return only the newest 2 data rows, never the whole table. Regression
    # guard against a hardcoded le=100_000 that would ignore the retention cap.
    from wsi_app.config import settings
    for i in range(4):
        _seed(db_session, "fcsvcap.x", level="ERROR", message=f"row{i}")
    original = settings.error_log_max_rows
    object.__setattr__(settings, "error_log_max_rows", 2)
    try:
        r = client.get(
            "/api/admin/logs/export.csv",
            params={"logger": "fcsvcap.x"},
            headers=admin_headers,
        )
    finally:
        object.__setattr__(settings, "error_log_max_rows", original)
    assert r.status_code == 200
    # Body = BOM+header line + one line per data row (tracebacks are empty here
    # so no embedded newlines). Count only the non-empty data lines.
    data_lines = [ln for ln in r.text.splitlines() if "fcsvcap.x" in ln]
    assert len(data_lines) == 2
    # Newest-first ordering: the two most recent rows survive the cap.
    assert "row3" in r.text and "row2" in r.text
    assert "row0" not in r.text and "row1" not in r.text


def test_logs_export_csv_honours_filters_and_span(client, admin_headers, db_session):
    _seed(db_session, "fcsvf.keep", level="ERROR")
    _seed(db_session, "fcsvf.drop", level="WARNING")
    r = client.get(
        "/api/admin/logs/export.csv",
        params={"level": "ERROR", "logger": "fcsvf."},
        headers=admin_headers,
    )
    body = r.text
    assert "fcsvf.keep" in body
    assert "fcsvf.drop" not in body
    r = client.get(
        "/api/admin/logs/export.csv",
        params={"since": "2024-01-01", "until": "2024-01-31"},
        headers=admin_headers,
    )
    assert 'filename="error-logs-2024-01-01_2024-01-31.csv"' in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------

def test_prune_row_cap_keeps_newest(db_session):
    seeded = [_seed(db_session, f"fprune.row.{i}").id for i in range(6)]
    assert seeded == sorted(seeded)
    deleted = error_log.prune_error_logs(db_session, max_rows=3, max_age_days=0)
    db_session.commit()
    assert deleted >= 3
    survivors = db_session.execute(
        select(ErrorLog.logger).where(ErrorLog.logger.like("fprune.row.%"))
    ).scalars().all()
    assert set(survivors) == {"fprune.row.3", "fprune.row.4", "fprune.row.5"}


def test_prune_age_cap_drops_old_rows(db_session):
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=120)
    _seed(db_session, "fprune.age.old", occurred_at=stale)
    _seed(db_session, "fprune.age.new")
    deleted = error_log.prune_error_logs(db_session, max_rows=0, max_age_days=90)
    db_session.commit()
    assert deleted >= 1
    survivors = db_session.execute(
        select(ErrorLog.logger).where(ErrorLog.logger.like("fprune.age.%"))
    ).scalars().all()
    assert survivors == ["fprune.age.new"]


def test_prune_zero_caps_disable(db_session):
    _seed(db_session, "fprune.off", occurred_at=datetime(2000, 1, 1))
    deleted = error_log.prune_error_logs(db_session, max_rows=0, max_age_days=0)
    db_session.commit()
    assert deleted == 0
    assert len(_rows(db_session, "fprune.off")) == 1


# ---------------------------------------------------------------------------
# resolve_export_limit — the shared CSV export row ceiling
# ---------------------------------------------------------------------------

def test_resolve_export_limit_follows_and_bounds():
    from wsi_app.csv_export import resolve_export_limit, EXPORT_ABSOLUTE_MAX_ROWS

    # Follows the retention cap when no explicit limit is requested.
    assert resolve_export_limit(None, 100) == 100
    # An explicit limit only narrows, never lifts, the cap.
    assert resolve_export_limit(50, 100) == 50
    assert resolve_export_limit(500, 100) == 100
    # Disabled retention (0 = unbounded table) falls back to the absolute
    # safety ceiling instead of running an unbounded query.
    assert resolve_export_limit(None, 0) == EXPORT_ABSOLUTE_MAX_ROWS
    # A retention cap above the absolute ceiling is still clamped to it.
    assert resolve_export_limit(None, EXPORT_ABSOLUTE_MAX_ROWS + 1) == EXPORT_ABSOLUTE_MAX_ROWS
    # An explicit limit can never exceed the absolute ceiling either.
    assert resolve_export_limit(EXPORT_ABSOLUTE_MAX_ROWS + 5, 0) == EXPORT_ABSOLUTE_MAX_ROWS


def test_logs_date_window_honours_analytics_timezone(client, admin_headers, db_session):
    # The error-log since/until day boundaries bucket in the
    # analytics timezone, the same as the audit-history filter
    # (test_audit_history.test_history_date_window_honours_analytics_timezone).
    # Windows that disagreed would give an operator two views shifted from each
    # other by the analytics timezone's UTC offset — exactly when that operator
    # is correlating an audit event with the error it produced. A row at 20:00
    # UTC on Jan 1 belongs to Jan 2 in Asia/Tokyo (UTC+9), and both surfaces
    # must agree on that.
    from wsi_app.config import settings

    _seed(db_session, "ltz.evt", occurred_at=datetime(2026, 1, 1, 20, 0, 0))

    orig = settings.analytics_timezone
    object.__setattr__(settings, "analytics_timezone", "Asia/Tokyo")
    try:
        r = client.get(
            "/api/admin/logs",
            params={"logger": "ltz.", "since": "2026-01-02"},
            headers=admin_headers,
        )
        assert r.json()["total"] == 1
        r = client.get(
            "/api/admin/logs",
            params={"logger": "ltz.", "until": "2026-01-01"},
            headers=admin_headers,
        )
        assert r.json()["total"] == 0
    finally:
        object.__setattr__(settings, "analytics_timezone", orig)


def test_routine_associated_image_read_stays_out_of_the_error_log(tmp_path):
    """A slide with no macro/label page is normal, not an error.

    ``read_associated_images`` logs the associated-image names it found as a
    diagnostic. At WARNING, DBLogHandler would mirror it into ``error_logs``
    along with every other WARNING+ ``wsi_app.*`` record — so a routine upload
    would write a row to the table backing the admin Error-log screen,
    spending its retention cap on non-errors. The genuine failure paths in
    the same function stay at WARNING; only the success-path diagnostic is
    INFO.
    """
    import logging as _logging

    from wsi_app import openslide_utils

    records: list[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    logger = _logging.getLogger("wsi_app.openslide")
    handler = _Capture(level=_logging.DEBUG)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)
    try:
        # A file OpenSlide cannot open exercises the *failure* branch, which
        # must still be WARNING. Use a real read for the success branch below.
        class _FakeSlide:
            associated_images: dict = {}

            def close(self) -> None:
                pass

        orig_open = openslide_utils.openslide.OpenSlide
        openslide_utils.openslide.OpenSlide = lambda _p: _FakeSlide()
        try:
            out = openslide_utils.read_associated_images(str(tmp_path / "x.svs"))
        finally:
            openslide_utils.openslide.OpenSlide = orig_open
        assert out == {}
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    diagnostics = [r for r in records if "associated images for" in r.getMessage()]
    assert diagnostics, "the diagnostic should still be emitted"
    assert all(r.levelno < _logging.WARNING for r in diagnostics), (
        "routine associated-image diagnostics must not reach the error_logs "
        "table, which DBLogHandler fills from WARNING upwards"
    )


# ---------------------------------------------------------------------------
# stderr (journald) output of the wsi_app tree
# ---------------------------------------------------------------------------

_STDERR_PROBE = r"""
import logging, sys
if sys.argv[1] == "root-configured":
    logging.basicConfig(level=logging.INFO, format="ROOT %(name)s: %(message)s")
import wsi_app.main  # installs the stderr handler
from wsi_app.db import Base, engine
Base.metadata.create_all(engine)
from wsi_app import error_log
from wsi_app.admin_helpers import audit
error_log.install_error_log_capture()  # the lifespan's queue handler
audit(None, "probe.action", target="probe-target")
logging.getLogger("wsi_app.probe").warning("probe-warning")
error_log.shutdown_error_log_capture()
"""


def _run_stderr_probe(tmp_path, variant: str) -> str:
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path}/probe.db",
        "DATA_DIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
    })
    proc = subprocess.run(
        [sys.executable, "-c", _STDERR_PROBE, variant],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout + proc.stderr


def test_app_logs_reach_stderr_once_with_capture_installed(tmp_path):
    """Audit INFO and WARNING lines must reach stderr (journald) even after the
    DB queue handler is attached — which would otherwise silence Python's
    last-resort output and leave INFO unwritten altogether."""
    out = _run_stderr_probe(tmp_path, "bare")
    assert out.count('"action": "probe.action"') == 1, out
    assert out.count("probe-warning") == 1, out


def test_app_logs_defer_to_a_configured_root_logger(tmp_path):
    """With a root handler configured (uvicorn --log-config, basicConfig), the
    app's own stderr handler stays quiet so nothing is printed twice."""
    out = _run_stderr_probe(tmp_path, "root-configured")
    assert out.count('"action": "probe.action"') == 1, out
    assert out.count("probe-warning") == 1, out
    assert "ROOT wsi_app.probe: probe-warning" in out


# ---------------------------------------------------------------------------
# Listing: traceback on demand, wider search, snapshot paging, level counts
# ---------------------------------------------------------------------------

def _seed_full(db, logger: str, **fields) -> ErrorLog:
    row = ErrorLog(level=fields.pop("level", "ERROR"), logger=logger,
                   message=fields.pop("message", ""), traceback=fields.pop("traceback", ""),
                   **fields)
    db.add(row)
    db.commit()
    return row


def test_logs_listing_leaves_the_traceback_to_the_detail_route(client, admin_headers, db_session):
    with_tb = _seed_full(db_session, "ftb.with", traceback="Traceback...\nKeyError: 'x'")
    _seed_full(db_session, "ftb.without")
    items = client.get("/api/admin/logs", params={"logger": "ftb."}, headers=admin_headers).json()["items"]
    by_logger = {i["logger"]: i for i in items}
    assert "traceback" not in by_logger["ftb.with"]
    assert by_logger["ftb.with"]["has_traceback"] is True
    assert by_logger["ftb.without"]["has_traceback"] is False

    r = client.get(f"/api/admin/logs/{with_tb.id}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["traceback"].endswith("KeyError: 'x'")
    assert r.json()["has_traceback"] is True


def test_logs_detail_route_is_admin_only_and_404s(client, admin_headers, staff_client, db_session):
    row = _seed_full(db_session, "ftb.gate")
    assert client.get(f"/api/admin/logs/{row.id}").status_code == 401
    assert client.get(f"/api/admin/logs/{row.id}", headers=staff_client("manager")).status_code == 403
    assert client.get("/api/admin/logs/999999999", headers=admin_headers).status_code == 404
    assert client.get(f"/api/admin/logs/{2**63}", headers=admin_headers).status_code == 404


def test_logs_search_covers_traceback_and_request_path(client, admin_headers, db_session):
    _seed_full(db_session, "fsrch2.tb", message="plain", traceback="ZeroDivisionError: boom")
    _seed_full(db_session, "fsrch2.path", message="plain", request_path="/api/admin/slides/77")
    _seed_full(db_session, "fsrch2.none", message="plain")
    def found(term):
        r = client.get("/api/admin/logs", params={"logger": "fsrch2.", "search": term},
                       headers=admin_headers)
        return {i["logger"] for i in r.json()["items"]}
    assert found("ZeroDivision") == {"fsrch2.tb"}
    assert found("slides/77") == {"fsrch2.path"}


def test_logs_snapshot_keeps_pages_stable(client, admin_headers, db_session):
    for i in range(4):
        _seed_full(db_session, f"fsnap.{i}")
    first = client.get("/api/admin/logs", params={"logger": "fsnap.", "limit": 2},
                       headers=admin_headers).json()
    assert first["newer"] == 0
    snap = first["snapshot_id"]
    _seed_full(db_session, "fsnap.late")
    second = client.get(
        "/api/admin/logs",
        params={"logger": "fsnap.", "limit": 2, "offset": 2, "max_id": snap},
        headers=admin_headers,
    ).json()
    assert second["total"] == 4
    assert second["newer"] == 1
    assert second["snapshot_id"] == snap
    assert [i["logger"] for i in second["items"]] == ["fsnap.1", "fsnap.0"]


def test_logs_level_counts_ignore_the_level_filter(client, admin_headers, db_session):
    _seed_full(db_session, "flvl.a", level="ERROR")
    _seed_full(db_session, "flvl.b", level="ERROR")
    _seed_full(db_session, "flvl.c", level="WARNING")
    body = client.get("/api/admin/logs", params={"logger": "flvl.", "level": "WARNING"},
                      headers=admin_headers).json()
    assert body["total"] == 1
    assert body["level_counts"] == {"ERROR": 2, "WARNING": 1}


def test_logs_clear_deletes_up_to_the_snapshot_and_is_audited(client, admin_headers, staff_client, db_session):
    from wsi_app.models import AuditLog

    old = _seed_full(db_session, "fclear.old")
    snap = client.get("/api/admin/logs", headers=admin_headers).json()["snapshot_id"]
    assert snap >= old.id
    late = _seed_full(db_session, "fclear.late")

    assert client.delete("/api/admin/logs", params={"max_id": snap}).status_code == 401
    assert client.delete("/api/admin/logs", params={"max_id": snap},
                         headers=staff_client("manager")).status_code == 403
    assert client.delete("/api/admin/logs", headers=admin_headers).status_code == 422

    r = client.delete("/api/admin/logs", params={"max_id": snap}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["deleted"] >= 1
    db_session.expire_all()
    assert _rows(db_session, "fclear.old") == []
    assert [x.id for x in _rows(db_session, "fclear.late")] == [late.id]
    audit_row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "system.error_logs.clear")
        .order_by(AuditLog.id.desc())
    ).scalars().first()
    assert audit_row is not None and audit_row.target == str(snap)


# ---------------------------------------------------------------------------
# CSV export: batching, traceback opt-out, concurrency slots
# ---------------------------------------------------------------------------

def test_logs_export_csv_can_leave_tracebacks_out(client, admin_headers, db_session):
    _seed_full(db_session, "fcsvtb.a", traceback="Traceback: secret-ish frame text")
    with_tb = client.get("/api/admin/logs/export.csv", params={"logger": "fcsvtb."},
                         headers=admin_headers).text
    without = client.get("/api/admin/logs/export.csv",
                         params={"logger": "fcsvtb.", "include_traceback": "false"},
                         headers=admin_headers).text
    assert "secret-ish frame text" in with_tb
    assert "secret-ish frame text" not in without
    assert without.splitlines()[0] == with_tb.splitlines()[0]


def test_iter_newest_first_reads_in_keyset_batches(db_session):
    from wsi_app.admin_reports import iter_newest_first

    ids = [_seed_full(db_session, f"fbatch.{i}").id for i in range(5)]
    filters = [ErrorLog.logger.like("fbatch.%")]
    got = list(iter_newest_first(ErrorLog, filters, 4, lambda r: r.id, batch_size=2))
    assert got == sorted(ids, reverse=True)[:4]
    assert list(iter_newest_first(ErrorLog, filters, 10, lambda r: r.id, batch_size=2)) == \
        sorted(ids, reverse=True)


def test_export_is_refused_while_the_slots_are_taken(client, admin_headers, monkeypatch):
    import threading

    from wsi_app import csv_export

    monkeypatch.setattr(csv_export, "_export_slots", threading.BoundedSemaphore(1))
    assert csv_export._export_slots.acquire(blocking=False)
    try:
        r = client.get("/api/admin/logs/export.csv", headers=admin_headers)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "adminExport.busy"
        assert client.get("/api/admin/history/export.csv", headers=admin_headers).status_code == 409
    finally:
        csv_export._export_slots.release()
    # A finished download gives its slot back.
    for _ in range(3):
        assert client.get("/api/admin/logs/export.csv", headers=admin_headers).status_code == 200


def test_export_slot_is_released_when_the_body_is_never_read(monkeypatch):
    import gc
    import threading

    from wsi_app import csv_export

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(csv_export, "_export_slots", sem)
    csv_export.claim_export_slot()
    rows = csv_export.release_on_exhaustion(iter([[1], [2]]))
    del rows
    gc.collect()
    assert sem.acquire(blocking=False)
    sem.release()
    # Iterated to the end: released once, and closing again is harmless.
    csv_export.claim_export_slot()
    rows = csv_export.release_on_exhaustion(iter([[1]]))
    assert list(rows) == [[1]]
    rows.close()
    assert sem.acquire(blocking=False)
    sem.release()


def test_export_bad_date_does_not_take_a_slot(client, admin_headers, monkeypatch):
    import threading

    from wsi_app import csv_export

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(csv_export, "_export_slots", sem)
    r = client.get("/api/admin/logs/export.csv", params={"since": "nope"}, headers=admin_headers)
    assert r.status_code == 400
    assert sem.acquire(blocking=False)
    sem.release()


def test_repeat_throttle_keeps_distinct_exceptions_apart(monkeypatch):
    """Every unhandled request exception is logged from one line; two different
    bugs must not share that line's burst."""
    monkeypatch.setattr(error_log.time, "monotonic", lambda: 50.0)
    throttle = error_log._RepeatThrottle(window=60, burst=1, max_keys=16)

    def unhandled(exc: BaseException) -> logging.LogRecord:
        try:
            raise exc
        except BaseException:
            return _record("wsi_app.unhandled", logging.ERROR,
                           "unhandled exception during %s %s", exc_info=sys.exc_info())

    def raise_key_error():
        raise KeyError("a")

    def raise_value_error():
        raise ValueError("b")

    def from_site(fn) -> logging.LogRecord:
        try:
            fn()
        except Exception:
            return _record("wsi_app.unhandled", logging.ERROR,
                           "unhandled exception during %s %s", exc_info=sys.exc_info())

    assert throttle.admit(from_site(raise_key_error)) == 0
    assert throttle.admit(from_site(raise_value_error)) == 0      # another bug: own burst
    assert throttle.admit(from_site(raise_key_error)) is None     # same bug again: held back
    assert throttle.admit(unhandled(KeyError("c"))) == 0          # same type, other raising line


@pytest.mark.parametrize("ending", ["complete", "send_raises", "disconnect"])
def test_slot_csv_response_releases_the_slot_however_it_ends(monkeypatch, ending):
    """The slot comes back, and the row source is closed, when the response is
    over — not when garbage collection gets round to it: sent in full, cut off
    by a send that raises (ASGI >= 2.4 servers), or by a disconnect Starlette
    notices itself (uvicorn, spec 2.3)."""
    import asyncio
    import threading

    from wsi_app import csv_export

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(csv_export, "_export_slots", sem)
    closed = []

    def rows():
        try:
            for i in range(1000):
                yield [i]
        finally:
            closed.append(True)

    csv_export.claim_export_slot()
    resp = csv_export.slot_csv_response("x.csv", ["a"], rows())
    sent = []
    disconnect = asyncio.Event()

    async def receive():
        if ending == "disconnect":
            await disconnect.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and len(sent) > 3:
            if ending == "send_raises":
                raise OSError("client went away")
            if ending == "disconnect":
                disconnect.set()
                await asyncio.sleep(0.05)

    spec = "2.4" if ending == "send_raises" else "2.3"
    scope = {"type": "http", "asgi": {"spec_version": spec}}

    async def run():
        try:
            await resp(scope, receive, send)
        except Exception:
            pass

    asyncio.run(run())
    assert closed == [True]
    assert sem.acquire(blocking=False)
    sem.release()


def test_export_does_not_hold_the_auth_session_while_streaming(client, admin_headers, db_session, monkeypatch):
    """The request session the auth dependency used is closed before the body
    streams; each batch then takes the only connection checked out."""
    from wsi_app import admin_reports
    from wsi_app.db import engine
    from wsi_app.routers import admin_logs

    _seed_full(db_session, "fauthdb.a")
    db_session.close()
    seen = []
    real = admin_reports.iter_newest_first

    def spy(*a, **k):
        for row in real(*a, **k):
            seen.append(engine.pool.checkedout())
            yield row

    monkeypatch.setattr(admin_logs, "iter_newest_first", spy)
    with client.stream("GET", "/api/admin/logs/export.csv", params={"logger": "fauthdb."},
                       headers=admin_headers) as r:
        assert r.status_code == 200
        r.read()
    # The batch's own session is closed before its rows are yielded.
    assert seen and max(seen) == 0


def test_clear_keeps_ids_growing(client, admin_headers, db_session):
    """Clearing every row must not let SQLite hand out id 1 again: an open
    tab's snapshot would then miss the new rows and a stale clear from it
    would delete them unseen."""
    _seed_full(db_session, "fmono.a")
    snap = client.get("/api/admin/logs", headers=admin_headers).json()["snapshot_id"]
    r = client.delete("/api/admin/logs", params={"max_id": snap}, headers=admin_headers)
    assert r.status_code == 200
    db_session.expire_all()
    marker = db_session.execute(
        select(ErrorLog).where(ErrorLog.logger == "wsi_app.error_log.clear").order_by(ErrorLog.id.desc())
    ).scalars().first()
    assert marker is not None and marker.id > snap and marker.level == "INFO"
    assert "cleared by" in marker.message

    late = _seed_full(db_session, "fmono.late")
    assert late.id > snap
    body = client.get("/api/admin/logs", params={"max_id": snap}, headers=admin_headers).json()
    assert body["newer"] >= 2  # the marker and the late row
    # A stale clear (the old snapshot) cannot reach rows written after it.
    client.delete("/api/admin/logs", params={"max_id": snap}, headers=admin_headers)
    db_session.expire_all()
    assert [x.id for x in _rows(db_session, "fmono.late")] == [late.id]


def test_oversized_clear_keeps_the_marker(client, admin_headers, db_session):
    _seed_full(db_session, "fmono.big")
    client.delete("/api/admin/logs", params={"max_id": 2**61}, headers=admin_headers)
    db_session.expire_all()
    left = db_session.execute(select(ErrorLog)).scalars().all()
    assert len(left) == 1 and left[0].logger == "wsi_app.error_log.clear"


def test_age_prune_never_empties_the_table(db_session):
    from sqlalchemy import delete as sa_delete

    db_session.execute(sa_delete(ErrorLog))
    db_session.commit()
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    rows = [_seed(db_session, f"fagemono.{i}", occurred_at=old) for i in range(3)]
    error_log.prune_error_logs(db_session, max_rows=0, max_age_days=90)
    db_session.commit()
    left = db_session.execute(select(ErrorLog.id)).scalars().all()
    assert left == [rows[-1].id]


def test_exception_key_uses_the_app_frame_not_the_library_raise_line(monkeypatch, tmp_path):
    """Two different bugs whose IntegrityError is raised inside SQLAlchemy
    (the same engine line for both) must not share one call site's burst."""
    import os

    from sqlalchemy import create_engine, text

    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL UNIQUE)"))
        c.execute(text("INSERT INTO t (v) VALUES ('a')"))

    def bug_one():
        with eng.begin() as c:
            c.execute(text("INSERT INTO t (v) VALUES ('a')"))   # duplicate

    def bug_two():
        with eng.begin() as c:
            c.execute(text("INSERT INTO t (v) VALUES (NULL)"))  # NOT NULL

    def key_of(fn):
        try:
            fn()
        except Exception:
            rec = _record("wsi_app.unhandled", logging.ERROR, "unhandled exception during %s %s",
                          exc_info=sys.exc_info())
        return error_log._exception_key(rec)

    # This test file stands in for the app package.
    monkeypatch.setattr(error_log, "_APP_DIR", os.path.dirname(os.path.abspath(__file__)) + os.sep)
    one, two = key_of(bug_one), key_of(bug_two)
    assert one[0] == two[0] == "IntegrityError"
    assert one != two
    assert one[1][0] == os.path.abspath(__file__)

    # With no app frame on the stack, both fall back to the library's line.
    monkeypatch.setattr(error_log, "_APP_DIR", str(tmp_path) + os.sep)
    assert key_of(bug_one) == key_of(bug_two)


def test_clear_with_nothing_new_writes_no_second_marker(client, admin_headers, db_session):
    _seed_full(db_session, "fmono.again")
    snap = client.get("/api/admin/logs", headers=admin_headers).json()["snapshot_id"]
    assert client.delete("/api/admin/logs", params={"max_id": snap}, headers=admin_headers).json()["deleted"] >= 1
    snap2 = client.get("/api/admin/logs", headers=admin_headers).json()["snapshot_id"]
    db_session.expire_all()
    markers = _rows(db_session, "wsi_app.error_log.clear")
    r = client.delete("/api/admin/logs", params={"max_id": snap2}, headers=admin_headers)
    assert r.json() == {"deleted": 0}
    db_session.expire_all()
    assert len(_rows(db_session, "wsi_app.error_log.clear")) == len(markers)
