"""Admin Performance page: server benchmark runs, browser-test results, and
the two tiny endpoints the browser test measures the network with.

Running anything (a server benchmark, the browser tile test that posts its
results, the ping / payload probes) is admin-only because it loads the server;
reading results follows the Server Info page's ``*_can_view_server`` permission.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import socket
import time
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session, defer

from .. import perf_bench, perf_export
from .._serializers import server_local_iso
from ..admin_helpers import api_error, audit
from ..auth import require_admin, require_server_access
from ..csv_export import csv_response
from ..db import SessionLocal, get_db
from ..models import PerfRun, User

router = APIRouter()
_logger = logging.getLogger("wsi_app.routers.admin_perf")

# Every stored run: prune() keeps _MAX_STORED_RUNS finished ones of each
# kind, plus the one that may be running.
_LIST_LIMIT = len(perf_bench.RUN_KINDS) * perf_bench._MAX_STORED_RUNS + 1
# A browser-test result is a few KB of numbers; the cap only stops a runaway
# client from parking megabytes of JSON in the history table.
_CLIENT_RESULT_MAX_BYTES = 128 * 1024
_PAYLOAD_DEFAULT_BYTES = 8 * 1024 * 1024
_PAYLOAD_MAX_BYTES = 64 * 1024 * 1024
_PAYLOAD_BLOCK = 1024 * 1024
_payload_block: bytes = b""
# The longest browser test whose time window the overlap check believes; a
# longer (or negative) reported duration is a broken clock, not a test.
_CLIENT_WINDOW_MAX_SECONDS = 6 * 3600
# Slack on the start of that window: the browser's own start stamp precedes
# its first request by a little, and the save follows the last by a little.
_CLIENT_WINDOW_SLACK_SECONDS = 5.0

# A row id / slide id as SQLite can bind it: a larger integer (which JSON and
# a URL path both carry happily) would raise OverflowError in the driver — a 500.
DbId = Annotated[int, Field(ge=1, le=2**63 - 1)]
RunId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class PerfRunCreate(BaseModel):
    slide_ids: List[DbId] = Field(default_factory=list, max_length=perf_bench.MAX_SLIDES)
    cold: bool = False
    tiles_per_band: int = Field(30, ge=5, le=200)
    # 0 = automatic (the effective CPU count, capped at 8). Larger requests
    # are capped at perf_bench.concurrency_cap() (twice the effective CPUs).
    max_concurrency: int = Field(0, ge=0, le=perf_bench.MAX_CONCURRENCY)
    step_seconds: float = Field(3.0, ge=1.0, le=10.0)


def _loads(raw: str, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _summary_of(row: PerfRun, result: Any = None) -> Optional[dict]:
    """The run's summary: its own column, or — for a row whose column is
    NULL (see _backfill_summaries) — that of ``result``, the
    parsed full result the caller read. Never ``row.result_json`` itself: the
    list defers that column, and a lazy load of a row deleted meanwhile (a
    prune, a DELETE from another tab) would raise ObjectDeletedError — a 500."""
    if row.summary_json is not None:
        summary = _loads(row.summary_json, None)
    else:
        summary = result.get("summary") if isinstance(result, dict) else None
    # A NaN / Infinity in a hand-edited row would make every response
    # carrying this run fail to serialise (and an export invalid JSON).
    return perf_export.finite(summary) if isinstance(summary, dict) else None


def _run_out(row: PerfRun, *, full: bool, result: Any = None) -> dict:
    # Only a full record parses the result: the list reads the summary
    # column (see _summary_of), not up to _LIST_LIMIT result blobs, and
    # passes the parsed result of a row that still lacks one.
    if full:
        result = perf_export.finite(_loads(row.result_json, None))
    out = {
        "id": row.id,
        "kind": row.kind,
        "state": row.state,
        "created_at": server_local_iso(row.created_at),
        "finished_at": server_local_iso(row.finished_at),
        "actor": row.actor_username or "",
        "params": _loads(row.params_json, {}),
        "progress": _loads(row.progress_json, None) if row.state == "running" else None,
        "cancel_requested": bool(row.cancel_requested),
        "error": row.error or None,
        "summary": _summary_of(row, result),
    }
    if full:
        out["result"] = result
    return out


@router.get("/api/admin/perf/runs")
def admin_perf_list_runs(
    response: Response,
    admin: User = Depends(require_server_access),
    db: Session = Depends(get_db),
):
    """Newest-first history (summaries only) plus the id of a running run."""
    response.headers["Cache-Control"] = "no-store"
    perf_bench.reap_orphans(db)
    # result_json is left unloaded: the summaries come from their own column.
    rows = db.execute(
        select(PerfRun).options(defer(PerfRun.result_json)).order_by(PerfRun.id.desc()).limit(_LIST_LIMIT)
    ).scalars().all()
    # A row that still lacks one (see _backfill_summaries) needs its result:
    # read for all of them in one explicit query, not by a lazy load per row,
    # which on a row pruned or deleted since the SELECT above would raise
    # ObjectDeletedError. Such a row is simply absent here; it is listed
    # without a summary (and not backfilled — nothing is left to fill).
    lacking = [r.id for r in rows if r.summary_json is None]
    results: dict[int, Any] = {}
    if lacking:
        results = {
            rid: _loads(raw, None)
            for rid, raw in db.execute(
                select(PerfRun.id, PerfRun.result_json).where(PerfRun.id.in_(lacking))
            ).all()
        }
    running = next((r.id for r in rows if r.state == "running"), None)
    runs = [_run_out(r, full=False, result=results.get(r.id)) for r in rows]
    _backfill_summaries([
        (r.id, out["summary"]) for r, out in zip(rows, runs)
        if r.summary_json is None and r.state != "running" and r.id in results
    ])
    return {"runs": runs, "running_id": running}


def _backfill_summaries(found: list[tuple[int, Optional[dict]]]) -> None:
    """Store the summaries the list just had to dig out of full results.

    Rows whose column is NULL are filled the first time the list meets
    them; every later write of a result sets the column itself.

    Best-effort and separate from the request: its own session and commit,
    every failure logged and swallowed, so a locked database can never fail
    the GET (the next list just tries again). Conditional on the column
    still being NULL, so it cannot overwrite what the run's own final write
    stored meanwhile; a running row (no result yet) is never filled.
    """
    if not found:
        return
    db = SessionLocal()
    try:
        for run_id, summary in found:
            db.execute(
                update(PerfRun)
                .where(PerfRun.id == run_id, PerfRun.summary_json.is_(None), PerfRun.state != "running")
                .values(summary_json=json.dumps(summary, ensure_ascii=False, allow_nan=False))
            )
        db.commit()
    except Exception:
        db.rollback()
        _logger.warning("perf: storing %d backfilled run summaries failed", len(found), exc_info=True)
    finally:
        db.close()


@router.get("/api/admin/perf/runs/{run_id}")
def admin_perf_get_run(
    run_id: RunId,
    response: Response,
    admin: User = Depends(require_server_access),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    perf_bench.reap_orphans(db)
    row = db.get(PerfRun, run_id)
    if row is None:
        raise api_error(404, "adminPerf.err.notFound")
    return _run_out(row, full=True)


@router.post("/api/admin/perf/runs", status_code=202)
def admin_perf_start_run(
    body: PerfRunCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Start a server benchmark in a background thread of this worker."""
    # A backup / restore / slide transfer both competes for the same disk and
    # would skew every number, so refuse rather than measure a busy server.
    from .. import jobs
    from .admin_backup import _restore_apply_pending

    params = perf_bench.BenchParams(**body.model_dump()).normalised()
    try:
        targets = perf_bench.resolve_targets(db, params.slide_ids)
        with contextlib.ExitStack() as stack:
            # Under the job-slot lock: a job claim checks for a running
            # benchmark under the same lock (jobs.try_claim_running), so a
            # backup started at the same moment as this cannot slip in
            # between the check below and the row that claims the run slot.
            # Without a lock file (an unwritable BACKUP_DIR, where no job can
            # start anyway) the check runs unguarded; a job that wins that
            # race is still recorded on the result (summary.job_overlap).
            try:
                stack.enter_context(jobs.store.slot_guard())
            except OSError:
                pass
            # So is a restore / reset apply waiting for its restart: its job
            # slot already reads "succeeded", but the restart it scheduled
            # kills this worker — and the run — any moment, after which the
            # orphan reaper reports the run interrupted. The same refusal as
            # a job in flight, i.e. what every job start answers then (the
            # Backup page's endpoints, jobs.try_claim_running); the same
            # code, as the page has one message for "a backup / restore is
            # under way, try afterwards". Read-only, safe under the lock.
            if jobs.store.has_running() or _restore_apply_pending():
                raise api_error(409, "adminPerf.err.jobRunning")
            row = perf_bench.create_run(db, admin.username, params, targets)
    except perf_bench.BenchError as exc:
        raise api_error(exc.status, exc.code, **exc.params)
    perf_bench.start_in_background(row.id, targets, params)
    audit(
        admin, "perf.run", target=str(row.id),
        slides=[t.slide_id for t in targets], cold=params.cold,
        max_concurrency=params.max_concurrency,
    )
    return {"id": row.id}


@router.post("/api/admin/perf/runs/{run_id}/cancel")
def admin_perf_cancel_run(
    run_id: RunId,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.get(PerfRun, run_id) is None:
        raise api_error(404, "adminPerf.err.notFound")
    if not perf_bench.request_cancel(db, run_id):
        raise api_error(409, "adminPerf.err.notRunning")
    audit(admin, "perf.cancel", target=str(run_id))
    return {"ok": True}


@router.delete("/api/admin/perf/runs/{run_id}")
def admin_perf_delete_run(
    run_id: RunId,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.get(PerfRun, run_id)
    if row is None:
        raise api_error(404, "adminPerf.err.notFound")
    if row.state == "running":
        raise api_error(409, "adminPerf.err.deleteRunning")
    db.delete(row)
    db.commit()
    audit(admin, "perf.delete", target=str(run_id))
    return {"ok": True}


def _iso_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A browser's toISOString() is always UTC; a bare stamp is read so.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _client_window(body: dict, now: float) -> Optional[tuple[float, float]]:
    """[start, end] (server epoch seconds) of the browser test being saved.

    It ends now, as the save follows the test. Its length comes from the
    browser's own clock — client.finished_at minus client.started_at, or
    client.duration_s — so a browser clock that is off by minutes from the
    server's moves nothing; only a bare start stamp (client.started_at
    without either, or a top-level ``started_at``) is compared with the
    server's clock. None when no usable time was sent (a page that sends no timing)."""
    client = body.get("client") if isinstance(body.get("client"), dict) else {}
    started = _iso_epoch(client.get("started_at"))
    finished = _iso_epoch(client.get("finished_at"))
    duration = client.get("duration_s")
    if started is not None and finished is not None:
        length: Optional[float] = finished - started
    elif type(duration) in (int, float) and math.isfinite(duration):
        length = float(duration)
    else:
        start = started if started is not None else _iso_epoch(body.get("started_at"))
        length = now - start if start is not None else None
    if length is None or not 0 <= length <= _CLIENT_WINDOW_MAX_SECONDS:
        return None
    return now - length - _CLIENT_WINDOW_SLACK_SECONDS, now


@router.post("/api/admin/perf/client-runs", status_code=201)
def admin_perf_save_client_run(
    body: dict = Body(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Store the result of a browser-side test run on the Performance page."""
    try:
        # allow_nan=False: NaN / Infinity (which Python's JSON parser
        # accepts) would be stored fine but make every later history
        # response fail to serialise.
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise api_error(422, "adminPerf.err.badResult")
    if len(encoded.encode("utf-8")) > _CLIENT_RESULT_MAX_BYTES:
        raise api_error(413, "adminPerf.err.resultTooLarge")
    schema = body.get("schema")
    if type(schema) is not int or schema != perf_bench.SCHEMA:  # True == 1, but isn't a schema
        raise api_error(422, "adminPerf.err.badResult")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    summary = body.get("summary", {})
    if not isinstance(summary, dict):
        # The history list shows it as an object of headline numbers.
        raise api_error(422, "adminPerf.err.badResult")
    # Whether the server was busy with something else while the browser
    # measured it: a server benchmark, or a backup / restore / slide
    # transfer. The page warns on either. Unknown (None) when the browser
    # sent no usable start time. The server's word, like "server" — except
    # that a browser which itself saw a server benchmark running during its
    # test (it re-checks when done) is believed: that can only add a warning,
    # never clear one, so a forged value buys nothing.
    now = time.time()
    window = _client_window(body, now)
    client_saw_server_run = summary.get("server_overlap") is True
    summary["server_overlap"] = perf_bench.server_run_overlap(db, *window) if window else None
    if client_saw_server_run:
        summary["server_overlap"] = True
    summary["job_overlap"] = perf_bench.job_overlap(*window) if window else None
    body["summary"] = summary
    # The browser can only report its own side. The server half of the
    # conditions (hardware, workers, tile settings, caches, the viewer's
    # rendering settings) is stamped here, so an exported browser run says
    # what it was measured against. Always the server's word: a "server" key
    # the client sent is overwritten.
    body["server"] = {
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "environment": perf_bench.conditions_snapshot(db),
    }
    row = PerfRun(
        kind="client",
        state="succeeded",
        actor_username=admin.username or "",
        params_json=json.dumps(params, ensure_ascii=False),
        result_json=json.dumps(body, ensure_ascii=False),
        summary_json=json.dumps(summary, ensure_ascii=False),
        # The browser ran the test; the server only records when it arrived.
        finished_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    perf_bench.prune(db)
    audit(admin, "perf.client_run", target=str(row.id))
    return {"id": row.id}


# A comma-separated id list. The pattern also bounds every id to 18 digits
# so int() never meets a number SQLite can't bind. [0-9], not \d: pydantic
# checks the pattern with a regex engine whose \d takes any Unicode digit
# ("１", or ones newer than this Python, which int() then rejects with a 500).
_IDS_PATTERN = r"^[0-9]{1,18}(,[0-9]{1,18})*$"


def _export_rows(db: Session, ids: Optional[str]) -> list[PerfRun]:
    """The runs to export, oldest first (so a CSV reads as a time series).

    No ``ids`` means the whole stored history.
    """
    if ids:
        wanted = sorted({int(x) for x in ids.split(",")})
        rows = db.execute(
            select(PerfRun).where(PerfRun.id.in_(wanted)).order_by(PerfRun.id.asc())
        ).scalars().all()
        if not rows:
            raise api_error(404, "adminPerf.err.notFound")
        return list(rows)
    rows = db.execute(
        select(PerfRun).order_by(PerfRun.id.desc()).limit(_LIST_LIMIT)
    ).scalars().all()
    return list(reversed(rows))


def _export_name(rows: list[PerfRun], ids: Optional[str], ext: str) -> str:
    if ids and len(rows) == 1:
        return f"tessera-perf-run-{rows[0].id}.{ext}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"tessera-perf-{stamp}.{ext}"


@router.get("/api/admin/perf/export.json")
def admin_perf_export_json(
    ids: Optional[str] = Query(None, max_length=2000, pattern=_IDS_PATTERN),
    admin: User = Depends(require_server_access),
    db: Session = Depends(get_db),
):
    """Full records of the chosen runs (default: all), with their conditions."""
    from ..version import read_version

    perf_bench.reap_orphans(db)
    rows = _export_rows(db, ids)
    envelope = perf_export.export_envelope(
        [_run_out(r, full=True) for r in rows],
        exported_by=admin.username or "",
        source={"app_version": read_version(), "hostname": socket.gethostname()},
    )
    return Response(
        content=perf_export.dumps(envelope),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_export_name(rows, ids, "json")}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/admin/perf/export.csv")
def admin_perf_export_csv(
    ids: Optional[str] = Query(None, max_length=2000, pattern=_IDS_PATTERN),
    admin: User = Depends(require_server_access),
    db: Session = Depends(get_db),
):
    """One row per run: settings, headline numbers and conditions as columns."""
    perf_bench.reap_orphans(db)
    rows = _export_rows(db, ids)
    runs = [_run_out(r, full=True) for r in rows]
    return csv_response(
        _export_name(rows, ids, "csv"),
        perf_export.CSV_HEADER,
        (perf_export.csv_row(run) for run in runs),
    )


@router.get("/api/admin/perf/ping")
def admin_perf_ping(response: Response, admin: User = Depends(require_admin)):
    """Minimal authenticated round trip for the browser's latency probe."""
    response.headers["Cache-Control"] = "no-store"
    return {"t": time.time()}


@router.get("/api/admin/perf/payload")
def admin_perf_payload(
    size: int = Query(_PAYLOAD_DEFAULT_BYTES, ge=64 * 1024, le=_PAYLOAD_MAX_BYTES),
    admin: User = Depends(require_admin),
):
    """``size`` bytes of incompressible data for the browser's download probe.

    One random 1 MiB block (generated once per worker) is streamed repeatedly:
    random bytes defeat any gzip on the way, and a 1 MiB period is far beyond
    DEFLATE's 32 KiB window, so no layer can shrink the transfer.
    """
    global _payload_block
    if not _payload_block:
        _payload_block = os.urandom(_PAYLOAD_BLOCK)
    block = _payload_block

    def chunks():
        left = size
        while left > 0:
            n = min(left, len(block))
            yield block[:n]
            left -= n

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "Content-Length": str(size)},
    )
