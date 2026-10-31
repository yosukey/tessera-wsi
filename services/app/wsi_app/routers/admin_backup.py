"""Admin backup / restore / reset endpoints.

Exposes:

  POST   /api/admin/backup                          start a backup job
  GET    /api/admin/backup                          list snapshots
  GET    /api/admin/backup/estimate                 size hint per component
  GET    /api/admin/backup/jobs/{job_id}            poll job state + progress
  POST   /api/admin/backup/jobs/{job_id}/cancel     cancel a running backup job
  POST   /api/admin/backup/{id}/download_url        mint a short-lived URL
  GET    /api/admin/backup/{id}/download            download an archive
  DELETE /api/admin/backup/{id}                     delete a snapshot
  POST   /api/admin/restore                         stage from upload OR snapshot id (job)
  GET    /api/admin/restore/preflight/{stage_id}    summarise live / staged state
  POST   /api/admin/restore/apply                   commit a staged restore (restart)
  GET    /api/admin/restore/result                  read last apply result
  DELETE /api/admin/restore/result                  clear last apply result
  DELETE /api/admin/restore/stage/{stage_id}        drop a staged archive
  GET    /api/admin/restore/rollbacks               list parked rollback trees
  DELETE /api/admin/restore/rollbacks/{name}        delete one rollback tree
  GET    /api/admin/reset/preflight                 summarise live state for reset
  POST   /api/admin/reset                           wipe content data (job, restart)

All endpoints require admin auth via the existing :func:`require_admin`
dependency. The apply / reset endpoints additionally require the admin's
current password as a second factor.
"""
from __future__ import annotations

import errno
import functools
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .. import backup as _backup
from .. import disk_space as _disk_space
from .. import jobs as _jobs
from .. import restore_apply as _restore_apply
from .. import service_restart as _service_restart
from ..admin_helpers import audit, error_detail
from ..auth import (
    MAX_PASSWORD_LEN,
    bearer_token,
    create_backup_download_token,
    decode_backup_download_token,
    require_admin,
    resolve_staff_user,
)
from ..config import settings
from ..db import SessionLocal
from ..models import User
from ..version import APP_VERSION
from .admin_auth import verify_reauth_password

router = APIRouter()
_logger = logging.getLogger("wsi_app.routers.admin_backup")


class CreateBackupRequest(BaseModel):
    # The three selectable components, all default-on (a default request
    # includes everything — scope "full"). WSI/thumbnail inclusion maps to the
    # backup scope (full vs metadata); the other two strip their DB table
    # groups when off.
    include_wsi: bool = True
    include_analytics: bool = True
    include_history: bool = True
    note: str = Field("", max_length=500)


# `stage_restore()` (backup.py) always emits stage_id = "stage-" + uuid.uuid4().hex[:12].
# Pinning the pattern here keeps the request body from naming arbitrary
# directories under settings.backup_dir/.staging via "../" segments.
_STAGE_ID_PATTERN = r"^stage-[0-9a-f]{12}$"
_STAGE_ID_RE = re.compile(_STAGE_ID_PATTERN)


class RestoreApplyRequest(BaseModel):
    stage_id: str = Field(..., pattern=_STAGE_ID_PATTERN)
    confirm_text: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LEN)


class ResetRequest(BaseModel):
    confirm_text: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LEN)


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------

class _PostApplyStepError(RuntimeError):
    """A post-apply hook step that the running process cannot serve without.

    ``stage`` names the step (``schema`` / ``bootstrap`` /
    ``session_revocation``) for the result file's ``post_apply_stage``; the
    message is what the endpoint, the job and the result file all report, so
    the three never disagree about why a committed swap left the app needing a
    restart.
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _reconcile_schema_after_inprocess_apply() -> None:
    """Run the lifespan's schema steps against a DB swapped in in-process.

    A production restore is applied before the service starts, so the boot
    reconciles an older archive's schema (missing columns, data migrations)
    before anything reads it. The dev in-process swap has no boot after it:
    without this, a restore of an archive older than a column addition 500s
    on every query touching that column until the process restarts. Same
    order and the same cross-worker lock as ``main.lifespan``.

    Raises :class:`_PostApplyStepError` naming the step that failed: a failed
    sign-out retry is not a schema problem, and telling the operator "the
    schema could not be brought up to date" for it would send them looking in
    the wrong place.
    """
    from .. import main as _main
    from ..db import engine

    step = "schema"
    try:
        with _main._startup_schema_lock():
            _main.reconcile_schema(engine)
            _main._warn_nullability_divergence()
            step = "bootstrap"
            _main.bootstrap_admin()
            _main.bootstrap_app_version(engine)
            # As the lifespan: finish a sign-out the swap could not do (or
            # fail), then record the session floor it may have left.
            step = "session_revocation"
            _backup.retry_pending_session_revocation()
            _backup.apply_session_generation_floor()
    except Exception as exc:
        what = {
            "schema": "its schema could not be brought up to date",
            "bootstrap": "bootstrapping it (admin account, app version) failed",
            "session_revocation": (
                "the sessions issued before it could not be signed out"
            ),
        }[step]
        raise _PostApplyStepError(
            step, f"the data was swapped in, but {what} ({exc}); restart the app",
        ) from exc


def _flush_slide_caches_after_inprocess_apply() -> None:
    """Drop this worker's slide caches, which describe the pre-swap DB.

    ``slide_resolve_cache`` maps public_id -> (slide_id, is_published,
    wsi_generation) for up to ``slide_meta_cache_ttl``; after a swap those
    ids, publish flags and generations belong to the database that was just
    parked, so a draft in the restored data could keep being served as
    published (or a slide id resolve to the wrong row) until the TTL ran out.
    The DZI handle cache is keyed by slide_id and holds the parked WSI files
    open. Nothing else flushes either: a production restore restarts every
    worker, the dev in-process swap restarts none.

    ``invalidate_slide_meta`` also bumps the cross-worker sentinel, so the
    other dev workers (if any) flush their resolve cache within
    ``slide_meta_gen_check_interval`` and re-stat their DZI handles on their
    next /dzi/ request (dzi._retire_handles_of_changed_files). Their auth /
    network caches and schema are NOT refreshed by this request — the
    in-process path is dev-only; restart the app when running several
    workers. Run first, before the schema steps, so the flush happens even
    when those fail: the swap stands either way. Best effort.
    """
    try:
        from .. import slide_resolve_cache
        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        _logger.exception("post-inprocess-apply slide-meta cache flush failed")
    try:
        from ..dzi import clear_dz_cache
        clear_dz_cache()
    except Exception:
        _logger.exception("post-inprocess-apply DZI cache flush failed")


def _run_post_inprocess_apply_hooks(
    stage_id: Optional[str] = None, scope: Optional[str] = None,
) -> None:
    """Bring the running process up to a dev-only in-process restore/reset
    apply: the swapped-in DB's schema, the data_dir layout, the caches.

    The slide caches are flushed first (see
    :func:`_flush_slide_caches_after_inprocess_apply`), then the schema is
    reconciled (see :func:`_reconcile_schema_after_inprocess_apply`); a
    failure there is raised, since the process cannot serve that DB.
    ``backup.inprocess_apply`` parks ``data_dir/{wsi,assets}`` under
    ``.pre-restore-*`` and, for the ``metadata`` / ``reset`` scopes, does not
    put a replacement tree in place. Without recreating the empty
    directories, the very next slide upload (or tour-asset save) trips over a
    missing parent path. The cache reloaders pull ``NetworkSettings`` /
    ``AuthSettings`` — including the session generation the apply moved on —
    from the freshly-swapped DB so the new auth config takes effect
    immediately. Imports are local so backup.py does not depend on main and
    tests that swap routers individually don't pull the full lifespan graph.

    ``stage_id`` / ``scope`` name the apply these hooks follow. When a fatal
    step fails, the result file the swap already wrote (``ok: true``) is
    amended with ``post_apply_error`` / ``post_apply_stage`` before the error
    is raised: the apply signs everyone out, so after signing in again that
    file is all the dashboard has to show, and a bare ``ok: true`` there would
    read as a clean restore while the endpoint has answered 500 and the job
    has failed. ``ok`` stays true — the swap committed and is not undone.
    """
    from .. import main as _main
    _flush_slide_caches_after_inprocess_apply()
    try:
        _reconcile_schema_after_inprocess_apply()
    except _PostApplyStepError as exc:
        _logger.exception("post-inprocess-apply %s step failed", exc.stage)
        if stage_id is not None:
            _backup.record_post_apply_error(stage_id, scope, exc.stage, str(exc))
        raise
    try:
        _main.ensure_dirs()
    except Exception:
        _logger.exception("post-inprocess-apply ensure_dirs failed; continuing")
    try:
        _main._load_network_cache()
    except Exception:
        _logger.exception("post-inprocess-apply network cache reload failed")
    try:
        _main._load_auth_cache()
    except Exception:
        _logger.exception("post-inprocess-apply auth cache reload failed")


# How long a configured RESTORE_RESTART_CMD is watched, so an immediate
# non-zero exit (masked unit, systemd degraded, …) is caught before the restart
# it should have caused would have killed this worker. ``systemctl --no-block``
# queues the restart and exits in well under a second; 1s is generous and still
# safely shorter than systemd's typical TimeoutStopSec.
_KICK_OBSERVE_WINDOW_SEC = 1.0


def _kick_restart(captured_job_id: str) -> None:
    """Trigger the service restart that applies a staged restore or reset.

    The mechanism is ``service_restart.trigger_restart`` — by default the
    trigger file the root-owned wsi-restart.path unit acts on (the app runs
    under ``NoNewPrivileges=true`` and cannot ``sudo``), else the configured
    ``RESTORE_RESTART_CMD``. A restore calls this from a BackgroundTask, so it
    runs *after* the 202 has flushed — the trigger is a separate file from
    ``restore.request`` precisely so the path unit doesn't fire mid-request
    and SIGTERM the worker before the client gets its response; a reset calls
    it from its job thread once the request is written. If the restart cannot
    be triggered the pending restart is aborted so future POSTs aren't locked
    out by ``_restore_apply_pending()`` forever.
    """
    try:
        _service_restart.trigger_restart(observe_seconds=_KICK_OBSERVE_WINDOW_SEC)
    except _service_restart.RestartKickError as exc:
        _logger.error("could not trigger the restart that applies the restore: %s", exc)
        _abort_pending_restart(captured_job_id, str(exc))


def _abort_pending_restart(job_id: str, reason: str) -> None:
    """Recover from a failed restart kick.

    Called only by ``_kick_restart`` when the restart could not be triggered:
    the trigger file could not be written, or the configured restart command
    is unusable or failed. At this point ``restore.request`` has already been
    written and the job has already been marked ``succeeded``; left alone,
    every future backup/restore/reset POST trips ``_restore_apply_pending()``
    and is rejected with 409 forever, with no path back via the dashboard.
    """
    try:
        os.remove(settings.restore_request_path)
    except FileNotFoundError:
        pass
    except OSError:
        _logger.exception(
            "failed to clear stale restore-request file at %s",
            settings.restore_request_path,
        )
    try:
        _jobs.store.update(
            job_id, state="failed",
            error=f"restart kick failed: {reason}",
        )
    except Exception:
        _logger.exception("failed to demote job %s after failed restart kick", job_id)


def _restore_apply_pending() -> bool:
    """True while a restore / reset request awaits the restart that applies
    it. An expired one does not count: no restart picked it up in time and
    the boot refuses it (``restore_apply.REQUEST_MAX_AGE_SECONDS``), so it
    must not keep every backup, restore, import and stage cancel at 409 until
    some unrelated restart. Read-only, so it is safe on the event loop and
    under ``slot_guard``; :func:`_expire_stale_apply_request` drops the file.
    Also imported by the slide transfer router and main's upload pre-auth."""
    pending = _backup.read_apply_request()
    return pending is not None and not pending[1]


def _expire_stale_apply_request() -> None:
    """Drop an expired restore / reset request and report it in the result
    file, where the Backup & Restore tab shows it (the boot would report the same).

    Under the job-slot lock, and only with no restore_apply job in flight:
    every request is written by such a job, between claiming the slot (which
    takes this lock) and finishing, so the file read here cannot be swapped
    for a new request before it is removed. Blocking; not for the event loop
    or a ``slot_guard`` holder (the flock is not re-entrant). The result is
    written before the removal, so a crash in between leaves a request the
    boot refuses with the same report."""
    pending = _backup.read_apply_request()
    if pending is None or not pending[1]:
        return
    with _jobs.store.slot_guard():
        if _jobs.store.has_running_kind("restore_apply"):
            return
        pending = _backup.read_apply_request()
        if pending is None or not pending[1]:
            return
        result = _restore_apply.expired_request_result(pending[0])
        _backup._write_apply_result(result)
        try:
            os.remove(settings.restore_request_path)
        except FileNotFoundError:
            pass
        except OSError:
            # Still ignored by _restore_apply_pending and refused at boot.
            _logger.exception("could not remove the expired restore request")
    _logger.warning("dropped an expired restore request: %s", result["error"])


_JOB_BUSY_DETAIL = "another backup/restore job is in progress"
# The 409 these endpoints answer while a restore-apply waits for its restart;
# main._UPLOAD_ROUTES answers it before auth for POST /api/admin/restore.
_RESTORE_PENDING_DETAIL = (
    "a restore-apply is pending service restart; retry after the service has "
    "restarted (a request no restart picks up is dropped after "
    f"{_restore_apply.REQUEST_MAX_AGE_SECONDS // 60} minutes)"
)
# The 409 the apply and preflight endpoints answer for a stage directory
# without backup.STAGE_COMPLETE_MARKER.
_STAGE_INCOMPLETE_DETAIL = (
    "this stage was not completely staged (its staging job was interrupted "
    "or failed before the archive passed its checks); stage the archive again"
)


def _claim_job_slot(kind: str, busy_detail: str = _JOB_BUSY_DETAIL) -> _jobs.Job:
    """Claim the single cross-worker job slot or raise the HTTP error to return.

    409 when another job (or a pending restore-apply) holds it. 503 when the
    claim could not be recorded in the shared job store (ENOSPC / EPERM under
    ``BACKUP_DIR/.jobs``): the other workers decide "busy" from that record
    alone, so starting the job anyway would let a second backup/restore run
    beside it. Blocking — async callers go through ``run_in_threadpool``.
    Shared by the slide export / import endpoints.
    """
    # try_claim_running refuses while any request file exists, expired or not.
    _expire_stale_apply_request()
    try:
        job = _jobs.store.try_claim_running(kind)
    except _jobs.JobStoreError as exc:
        _logger.error("refusing to start %s job: %s", kind, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "the job could not be recorded in the shared job store, so it "
                f"was not started ({exc}); check free space and permissions "
                "under BACKUP_DIR/.jobs"
            ),
        )
    if job is None:
        raise HTTPException(status_code=409, detail=busy_detail)
    return job


@router.post("/api/admin/backup")
async def create_backup(
    payload: CreateBackupRequest,
    admin: User = Depends(require_admin),
):
    if _restore_apply_pending():
        raise HTTPException(
            status_code=409,
            detail=_RESTORE_PENDING_DETAIL,
        )
    job = await run_in_threadpool(_claim_job_slot, "backup")

    # The WSI toggle maps onto the backup scope, which the restore/reset
    # machinery branches on (full vs metadata).
    scope = _backup.SCOPE_FULL if payload.include_wsi else _backup.SCOPE_METADATA

    def _run() -> None:
        try:
            # On the job's thread rather than in the endpoint: the record write
            # is file I/O under BACKUP_DIR/.jobs, which must not stall the event
            # loop, and an await between the claim above and the thread start
            # could be cut short by a cancelled request and leave the slot
            # claimed with nothing running it. Pending until then, which the
            # dashboard shows as in progress too.
            _jobs.store.update(job.id, state="running", progress={"phase": "queued", "bytes_written": 0})

            def progress(p: dict) -> None:
                _jobs.store.update(job.id, progress=p)
            record = _backup.create_backup(
                scope=scope,
                note=payload.note,
                progress_cb=progress,
                include_analytics=payload.include_analytics,
                include_history=payload.include_history,
                # The cross-worker cancel marker (see cancel_backup_job);
                # create_backup throttles how often it asks.
                should_cancel=lambda: _jobs.store.cancel_requested(job.id),
            )
            result = {"backup_id": record.id, "size_bytes": record.size_bytes,
                      "sha256": record.sha256, "scope": record.scope,
                      "app_version": record.app_version,
                      "warnings": list(record.warnings),
                      # The same, as {code, params} for the dashboard to
                      # translate (``warnings`` stays as text for scripts).
                      "warning_items": list(record.warning_items)}
            # A cancel that arrived after create_backup's last check (just
            # before the archive was published) is moot: the archive exists,
            # so the job is "succeeded". Say so, so the UI that showed
            # "cancelling…" can explain why the backup is in the list anyway.
            if _jobs.store.cancel_requested(job.id):
                _logger.info(
                    "backup job %s: cancel requested after the archive was "
                    "published; completed as succeeded", job.id,
                )
                result["cancel_too_late"] = True
            _jobs.store.update(
                job.id,
                state="succeeded",
                progress={"phase": "done", "bytes_written": record.size_bytes},
                # ``warnings``: what the published archive left out (a tour
                # attachment removed mid-run, a thumbnail that changed since
                # the snapshot) — empty for a clean run. Only this result
                # carries them; the listing describes the archive alone.
                result=result,
            )
            audit(admin, "backup.create", target=record.id,
                  scope=record.scope, size_bytes=record.size_bytes,
                  sha256=record.sha256, app_version=record.app_version,
                  includes_analytics=record.includes_analytics,
                  includes_history=record.includes_history)
        except _backup.BackupCancelled:
            # create_backup has already removed the partial archive and the
            # snapshot dir, and published nothing. The terminal update frees
            # the slot (and drops the cancel marker). ``cancelled_by`` is who
            # asked, read from the marker before that update removes it; the
            # row's actor stays the admin who started the backup, matching
            # backup.create / backup.create.failed.
            info = _jobs.store.cancel_request_info(job.id)
            by = info.get("requested_by") or {}
            _logger.info("backup job %s cancelled", job.id)
            _jobs.store.update(
                job.id, state="cancelled", error=None,
                result={"cancelled_by": by.get("username")},
            )
            audit(admin, "backup.create.cancelled", target=job.id,
                  scope=scope, cancelled_by=by.get("username"),
                  cancelled_by_id=by.get("id"))
        except BaseException as exc:
            _logger.exception("backup job %s failed", job.id)
            _jobs.store.update(job.id, state="failed", error=str(exc))
            audit(admin, "backup.create.failed", target=job.id,
                  scope=scope, error=str(exc))

    # Mark the job failed if the worker thread never gets off the ground
    # (``RuntimeError: can't start new thread`` under thread exhaustion). The
    # slot is already claimed by a record owned by THIS live pid, so the orphan
    # reaper — which only fires once the owning pid is gone — would not release
    # it: a single failed POST would 409 every subsequent backup and restore
    # until this worker process happens to exit.
    try:
        threading.Thread(target=_run, name=f"backup-{job.id}", daemon=True).start()
    except BaseException as exc:
        _logger.exception("could not start backup job %s", job.id)
        _jobs.store.update(job.id, state="failed", error=str(exc))
        raise
    return {"job_id": job.id}


@router.get("/api/admin/backup/jobs/active")
def get_active_job(admin: User = Depends(require_admin)):
    """The job holding the single backup / restore / import slot, if any:
    ``{"job": <job> | null}``.

    The dashboard re-attaches to a job it started by the id it kept in the
    tab, so without this any other tab, admin or browser would see an idle
    Backup screen, a Start that answers 409 and no Cancel for as long as a
    multi-hour backup runs. This lets it find that job on load, and after a 409 for a busy
    slot. Declared before ``/jobs/{job_id}``, which would otherwise take
    "active" for an id."""
    job = _jobs.store.active_job()
    if job is None:
        return {"job": None}
    out = job.to_dict()
    out["cancel_requested"] = _jobs.store.cancel_requested(job.id)
    return {"job": out}


@router.get("/api/admin/backup/jobs/{job_id}")
def get_job(job_id: str, admin: User = Depends(require_admin)):
    # A record its (dead) worker left pending/running is answered as the
    # ``failed`` it is (``result.code`` = jobWorkerExited), not as "running"
    # until some later POST happens to reap it — see get_reaping_orphan.
    job = _jobs.store.get_reaping_orphan(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    out = job.to_dict()
    # Whether a cancel is pending, from the cross-worker marker, so every
    # poller — on any worker, or a remounted page — can show "cancelling…"
    # until the job reaches "cancelled" (or "succeeded", if it was too late).
    if job.state in ("pending", "running"):
        out["cancel_requested"] = _jobs.store.cancel_requested(job.id)
    return out


# Job kinds an operator can cancel: a backup — the job that runs for hours,
# whose cleanup (drop a .tmp archive and a snapshot dir) is self-contained —
# and a backup check, which only reads (and also holds the slot for hours on
# a large archive). Restore staging and slide export/import would need their
# own cancel points and cleanup, and an apply must never stop half-way.
_CANCELLABLE_KINDS = ("backup", "backup_verify")
# The audit action a cancel request is recorded under, per kind.
_CANCEL_AUDIT_ACTIONS = {
    "backup": "backup.create.cancel_requested",
    "backup_verify": "backup.verify.cancel_requested",
}


def _cancel_refused(job: _jobs.Job) -> HTTPException:
    """The 409 for a cancel of a job that is no longer running, as an i18n
    code (the dashboard shows it in the admin's language rather than an
    English sentence shown raw). A job failed because its worker exited gets
    that reason rather than a bare "already finished (failed)"."""
    if job.state == "failed" and (job.result or {}).get("code") == _jobs.WORKER_EXITED_CODE:
        return HTTPException(
            status_code=409, detail=error_detail(_jobs.WORKER_EXITED_CODE),
        )
    return HTTPException(
        status_code=409,
        detail=error_detail("adminDash.apiErr.backupCancelFinished", state=job.state),
    )


@router.post("/api/admin/backup/jobs/{job_id}/cancel", status_code=202)
def cancel_backup_job(job_id: str, admin: User = Depends(require_admin)):
    """Ask a running backup job to stop.

    The job usually runs on another uvicorn worker, so this only records the
    request (a marker file in the shared job store, see
    ``JobStore.request_cancel``); the job's thread checks for it between
    phases and files and about twice a second while streaming a large WSI,
    then removes its partial archive and snapshot dir and ends in state
    ``cancelled``, which frees the job slot. That is usually within a second
    or so, but not always: copying the live DB into the snapshot and, when
    statistics or history are left out, the VACUUM that compacts the
    stripped copy are single SQLite calls that cannot be interrupted, so a
    request made during one takes effect when it returns (seconds, or
    minutes on a large DB). 202 with ``cancel_requested: true``; the caller
    polls the job for the outcome, which can still be ``succeeded`` when the
    request lands after the backup's final check before publishing.

    404 for an unknown job, 409 for one that is not a backup or has already
    finished — including one whose worker exited, which is reaped to
    ``failed`` here (see ``JobStore.get_reaping_orphan``) instead of
    answering "cancelling" for a request no thread will ever read. The 409
    details are ``{code, params}`` i18n payloads. Repeating the request for a
    job already being cancelled is a no-op answered like the first (and
    audited once).
    """
    job = _jobs.store.get_reaping_orphan(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.kind not in _CANCELLABLE_KINDS:
        raise HTTPException(
            status_code=409,
            detail=error_detail("adminDash.apiErr.jobNotCancellable", kind=job.kind),
        )
    if job.state not in ("pending", "running"):
        raise _cancel_refused(job)
    if _jobs.store.cancel_requested(job_id):
        return {"job_id": job_id, "cancel_requested": True}
    try:
        _jobs.store.request_cancel(
            job_id, requested_by={"id": admin.id, "username": admin.username},
        )
    except _jobs.JobStoreError as exc:
        _logger.error("could not record the cancel of job %s: %s", job_id, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"the cancel request could not be recorded ({exc}); check free "
                "space and permissions under BACKUP_DIR/.jobs"
            ),
        )
    # The job may have finished between the state check above and the
    # marker write (its thread does not take the slot lock for its terminal
    # update). Its outcome is final then, so say so rather than answer
    # "cancelling" for a job that will never read the marker — unless that
    # outcome is "cancelled": then the job read a marker (this one, or a
    # concurrent request's) in that instant, and the cancel the caller asked
    # for is exactly what happened; a 409 "already finished (cancelled)"
    # would show an error for a cancel that worked.
    now = _jobs.store.get_reaping_orphan(job_id)
    if now is None or now.state not in ("pending", "running"):
        _jobs.store.clear_cancel_request(job_id)
        if now is not None and now.state == "cancelled":
            audit(admin, _CANCEL_AUDIT_ACTIONS[job.kind], target=job_id)
            return {"job_id": job_id, "cancel_requested": True}
        if now is None:
            # The record went with the terminal update (one that could not
            # be written is removed to free the slot): finished, outcome
            # unknown.
            raise HTTPException(
                status_code=409,
                detail=error_detail("adminDash.apiErr.backupCancelFinished", state="gone"),
            )
        raise _cancel_refused(now)
    audit(admin, _CANCEL_AUDIT_ACTIONS[job.kind], target=job_id)
    return {"job_id": job_id, "cancel_requested": True}


# ---------------------------------------------------------------------------
# List / download / delete
# ---------------------------------------------------------------------------

@router.get("/api/admin/backup")
def list_backups(admin: User = Depends(require_admin)):
    items = [r.to_dict() for r in _backup.list_backups()]
    return {
        "current_app_version": APP_VERSION,
        "backup_dir": settings.backup_dir,
        "items": items,
    }


@router.get("/api/admin/backup/estimate")
def backup_estimate(admin: User = Depends(require_admin)):
    """Approximate uncompressed byte size of each selectable backup component.

    Lets the create-backup UI show a size hint next to each toggle (WSI image
    files, access statistics, activity history) plus the always-included DB
    core. Numbers are best-effort and read live; the real archive is smaller
    after compression.
    """
    return _backup.estimate_backup_components()


@router.post("/api/admin/backup/{backup_id}/download_url")
def make_backup_download_url(
    backup_id: str,
    admin: User = Depends(require_admin),
):
    """Return a short-lived signed URL for downloading the archive.

    The download itself is then performed via a plain `<a href download>`
    click so the browser uses its native streaming download path. That
    avoids `fetch -> Blob -> blob:` URLs, which Chrome blocks for archive
    MIME types over plain HTTP.
    """
    rec = _backup.find_backup(backup_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="backup not found")
    token = create_backup_download_token(backup_id, admin)
    # The issuance is audited; the actual download via ?t= can be made
    # actor-less (see download_backup), so logging who minted the token is
    # the only reliable trail back to the human responsible.
    audit(admin, "backup.download_url", target=backup_id)
    return {"url": f"/api/admin/backup/{backup_id}/download?t={token}"}


@router.get("/api/admin/backup/{backup_id}/download")
def download_backup(
    backup_id: str,
    request: Request,
    t: Optional[str] = Query(None),
):
    """Download a backup archive.

    Authorized either by a short-lived `?t=<token>` query string (issued by
    `/download_url`) or by an admin Bearer token on the Authorization
    header. The token path lets the browser navigate to the URL directly
    so the download streams via the native browser pipeline.
    """
    authorized = False
    actor: Optional[User] = None
    # One session serves both auth paths. The ?t= token is bound to the minting
    # user's live row (uid + tv), so it is a revocation check too and needs the
    # DB as much as the bearer path does — a leaked download URL for a whole
    # install backup must die with the account, not outlive it by a TTL.
    db = SessionLocal()
    try:
        if t and decode_backup_download_token(t, backup_id, db):
            authorized = True
        else:
            token = bearer_token(request.headers.get("authorization", ""))
            if token:
                # Re-check the live user state via resolve_staff_user, which
                # enforces token_version too: a JWT issued before the account was
                # disabled / demoted / had its password changed must not keep
                # downloading the full backup archive for the remainder of its
                # TTL. Mirrors the bearer path in admin_slides.admin_download_slide.
                user = resolve_staff_user(token, db)
                if user and user.role == "admin":
                    authorized = True
                    actor = user
    finally:
        db.close()
    if not authorized:
        raise HTTPException(status_code=401, detail="unauthorized")

    rec = _backup.find_backup(backup_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="backup not found")
    # Always audit the download regardless of auth path. Token path has no
    # actor (a previously-minted URL was followed); the matching
    # backup.download_url audit row records who minted it. Bearer path
    # carries the live admin User. Mirrors the slide.download pattern.
    audit(actor, "backup.download", target=backup_id, via_token=(actor is None))
    media_type = (
        "application/zstd" if rec.archive_format == "tar.zst" else "application/gzip"
    )
    filename = os.path.basename(rec.path)
    return FileResponse(rec.path, media_type=media_type, filename=filename)


@router.post("/api/admin/backup/{backup_id}/verify", status_code=202)
def verify_backup(backup_id: str, admin: User = Depends(require_admin)):
    """Check a saved backup end to end (codec checksums, the sidecar's
    sha256) as a job: 202 ``{"job_id"}``, polled like any backup job. Its
    ``result`` is ``backup.verify_backup``'s. A damaged archive fails the job
    with ``result.damaged`` true; any other failure (an I/O error, the worker
    exiting) leaves it unset, since it says nothing about the archive. Takes
    the job slot — it reads the whole archive, and the delete refuses while
    it runs — and can be cancelled like a backup (``…/jobs/{id}/cancel``),
    ending ``cancelled``."""
    if _backup.find_backup(backup_id) is None:
        raise HTTPException(status_code=404, detail="backup not found")
    if _restore_apply_pending():
        raise HTTPException(status_code=409, detail=_RESTORE_PENDING_DETAIL)
    job = _claim_job_slot("backup_verify")

    def _run() -> None:
        try:
            _jobs.store.update(
                job.id, state="running",
                progress={"phase": "verifying", "backup_id": backup_id},
            )
            result = _backup.verify_backup(
                backup_id,
                progress_cb=lambda p: _jobs.store.update(job.id, progress=p),
                should_cancel=lambda: _jobs.store.cancel_requested(job.id),
            )
            _jobs.store.update(
                job.id, state="succeeded", result=result,
                progress={"phase": "done", "backup_id": backup_id},
            )
            audit(admin, "backup.verify", target=backup_id, ok=True,
                  sha256_checked=result["sha256_checked"])
        except _backup.BackupCancelled:
            _jobs.store.update(
                job.id, state="cancelled", error=None,
                progress={"phase": "done", "backup_id": backup_id},
            )
            audit(admin, "backup.verify.cancelled", target=backup_id)
        except BaseException as exc:
            error = str(exc) or type(exc).__name__
            damaged = isinstance(exc, _backup.BackupDamaged)
            if isinstance(exc, FileNotFoundError):
                error = f"backup {backup_id} was deleted before it could be verified"
            if isinstance(exc, (RuntimeError, FileNotFoundError)):
                _logger.warning("backup verify job %s: %s", job.id, error)
            else:
                _logger.exception("backup verify job %s failed", job.id)
            _jobs.store.update(
                job.id, state="failed", error=error,
                result={"damaged": True} if damaged else {},
            )
            audit(admin, "backup.verify", target=backup_id, ok=False,
                  damaged=damaged, error=error)

    try:
        threading.Thread(target=_run, name=f"backup-verify-{job.id}", daemon=True).start()
    except BaseException as exc:
        _jobs.store.update(job.id, state="failed", error=str(exc))
        raise
    return {"job_id": job.id}


@router.delete("/api/admin/backup/{backup_id}")
def delete_backup(backup_id: str, admin: User = Depends(require_admin)):
    """Delete a saved backup — unless a restore is being staged from it.

    stage_restore reads the sidecar's sha256 and then opens the archive; a
    delete landing between the two (it removes the sidecar first) would let the
    staging go ahead, on the still-open archive, with no sha256 to check it
    against. Checked under the job-slot lock, which a staging job claims its
    slot under, so the job either already holds the slot here (409) or starts
    after the delete and finds the backup gone (404). A staging job that has
    not recorded its source yet counts as staging this one.
    """
    with _jobs.store.slot_guard():
        active = _jobs.store.active_job()
        source_key = {"restore_stage": "snapshot_id", "backup_verify": "backup_id"}.get(
            active.kind if active is not None else "",
        )
        if source_key is not None:
            progress = active.progress or {}
            if source_key not in progress or progress.get(source_key) == backup_id:
                raise HTTPException(
                    status_code=409,
                    detail=error_detail(
                        "adminDash.apiErr.backupDeleteWhileStaging"
                        if active.kind == "restore_stage"
                        else "adminDash.apiErr.backupDeleteWhileVerifying"
                    ),
                )
        if not _backup.delete_backup(backup_id):
            raise HTTPException(status_code=404, detail="backup not found")
    audit(admin, "backup.delete", target=backup_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Restore staging
# ---------------------------------------------------------------------------

def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        _logger.warning("could not remove %s", path, exc_info=True)


# backup.stage_restore's answer for an archive it could not read — which is
# also what a FileNotFoundError opening it becomes.
_CORRUPT_ARCHIVE_ANSWER = "archive is corrupt or not a supported format"


def _is_missing_archive_failure(exc: BaseException) -> bool:
    """Whether a staging failure is one a vanished archive file causes: a
    FileNotFoundError (raised, or the cause of stage_restore's answer) or
    stage_restore's corrupt / unreadable answer."""
    seen: Optional[BaseException] = exc
    for _ in range(8):  # a chain is short; never loop on a cyclic one
        if seen is None:
            break
        if isinstance(seen, FileNotFoundError):
            return True
        seen = seen.__cause__ or seen.__context__
    return isinstance(exc, RuntimeError) and str(exc) == _CORRUPT_ARCHIVE_ANSWER


@router.post("/api/admin/restore", status_code=202)
async def restore_stage(
    request: Request,
    snapshot_id: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    replaces_stage_id: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
):
    """Stage an uploaded archive, or a saved snapshot, for restore — as a job.

    The request only takes hold of the upload Starlette has spooled (see
    :class:`HeldUpload`) and answers; copying it into staging, then extracting
    and verifying the archive (minutes to hours for a full-scope archive, far
    past any proxy timeout) run on a thread. Answers 202 ``{"job_id"}``;
    ``GET /api/admin/backup/jobs/{job_id}`` reports progress (``copying`` with
    ``bytes_copied`` / ``total_bytes``, then ``bytes_extracted``) and, once it
    succeeded, ``result`` holds the ``stage_id``, ``manifest``, ``plan`` and
    ``current_app_version`` to preview before applying. An archive that cannot
    be staged fails the job with the reason as its ``error`` (and, for a full
    staging volume, ``result.code`` ``adminDash.apiErr.uploadNoSpace``).

    ``replaces_stage_id`` names the stage this one replaces (the preview the
    dashboard had up). The job removes it first thing, inside the slot and
    before anything measures free space: released by the browser before the
    request, a refused attempt would lose an archive staged already; released
    after it was accepted, the new job's free-space checks would already have
    counted the old stage's space and could refuse an archive the volume holds
    once it is gone — losing both.
    """
    if _restore_apply_pending():
        raise HTTPException(
            status_code=409,
            detail=_RESTORE_PENDING_DETAIL,
        )

    # Reserve the running-job slot atomically (mirrors create_backup /
    # restore_apply). Using ``has_running()`` alone leaves a TOCTOU window
    # where a concurrent backup POST and a restore POST can both pass the
    # check and proceed in parallel; the extract is potentially multi-GB
    # and would race the snapshot taken by the backup.
    job = await run_in_threadpool(_claim_job_slot, "restore_stage")

    held: Optional[HeldUpload] = None
    try:
        # Normalize "present but empty" to absent. A multipart form that
        # submits snapshot_id= (empty string) delivers "" — not None — and an
        # empty file part delivers an UploadFile with no filename; either would
        # slip past a raw ``is None`` check and crash the upload branch on a
        # None/empty file with AttributeError (a 500 instead of the 400).
        sid = (snapshot_id or "").strip() or None
        replaces = (replaces_stage_id or "").strip() or None
        if replaces is not None and not _STAGE_ID_RE.match(replaces):
            raise HTTPException(status_code=400, detail="malformed replaces_stage_id")
        has_file = file is not None and bool(file.filename)
        if (sid is not None) == has_file:
            raise HTTPException(
                status_code=400,
                detail="exactly one of snapshot_id or file must be provided",
            )

        snapshot_path: Optional[str] = None
        if sid is not None:
            rec = await run_in_threadpool(_backup.find_backup, sid)
            if rec is None:
                raise HTTPException(status_code=404, detail="snapshot not found")
            snapshot_path = rec.path
            progress: dict = {"phase": "queued", "snapshot_id": sid}
        else:
            held = await run_in_threadpool(hold_upload, file, settings.backup_max_upload_bytes)
            progress = {"phase": "copying", "snapshot_id": None,
                        "bytes_copied": 0, "total_bytes": held.size}
        _jobs.store.update(job.id, state="running", progress=progress)

        def _run() -> None:
            try:
                def report(p: dict) -> None:
                    _jobs.store.update(job.id, progress=p)
                if replaces is not None:
                    # In the slot: no apply can be reading it (a pending
                    # request refused this POST with 409).
                    _backup.cleanup_stage(replaces)
                if held is not None:
                    staged_from = receive_held_upload(held, report)
                else:
                    staged_from = snapshot_path
                try:
                    stage_id, manifest = _backup.stage_restore(staged_from, progress_cb=report)
                finally:
                    if held is not None:
                        _remove_quietly(staged_from)
                # With what staging found (a failed schema-upgrade trial):
                # the preview shows it as the plan's blocking reason.
                plan = _backup.plan_staged_restore(stage_id, manifest)
                _jobs.store.update(
                    job.id, state="succeeded",
                    progress={"phase": "done"},
                    result={
                        "stage_id": stage_id,
                        "scope": manifest.scope,
                        "manifest": manifest.to_dict(),
                        "plan": plan.to_dict(),
                        "current_app_version": APP_VERSION,
                    },
                )
                audit(admin, "restore.stage", target=stage_id,
                      scope=manifest.scope, snapshot_id=sid,
                      backup_app_version=manifest.app_version)
            except UploadNoSpace as exc:
                _logger.warning("restore staging job %s: %s", job.id, exc)
                _jobs.store.update(
                    job.id, state="failed", error=str(exc),
                    result={"code": _UPLOAD_NO_SPACE_CODE},
                )
            except BaseException as exc:
                # stage_restore raises RuntimeError for an archive it refuses
                # (corrupt, wrong kind, no space, version stamps): the
                # uploader's problem, reported as the job's error. Anything
                # else is a server fault worth a traceback.
                error = str(exc) or type(exc).__name__
                if (
                    snapshot_path is not None
                    and _is_missing_archive_failure(exc)
                    and not os.path.lexists(snapshot_path)
                ):
                    # A DELETE of this snapshot landed between the lookup
                    # above and stage_restore opening it (once open, the fd
                    # keeps the bytes readable). stage_restore folds the
                    # FileNotFoundError into its "archive is corrupt" answer,
                    # which would send the operator hunting for damage in a
                    # file that simply is not there any more. Only for that
                    # failure, though: a snapshot deleted while a long
                    # staging ran on its open fd is gone too, and a refusal
                    # of its own (version stamps, integrity, a full staging
                    # volume) must not be reported as the deletion.
                    error = f"backup {sid} was deleted before it could be staged"
                    _logger.warning("restore staging job %s: %s", job.id, error)
                elif isinstance(exc, RuntimeError):
                    _logger.warning("restore staging job %s: %s", job.id, exc)
                else:
                    _logger.exception("restore staging job %s failed", job.id)
                _jobs.store.update(job.id, state="failed", error=error)
            finally:
                if held is not None:
                    held.close()

        threading.Thread(target=_run, name=f"restore-stage-{job.id}", daemon=True).start()
    except BaseException as exc:
        # Release the slot (and the held upload) on any failure before the
        # thread took over — an HTTPException, an upload over the cap, no
        # thread — so a follow-up attempt isn't blocked by a stuck "running"
        # record.
        if held is not None:
            held.close()
        j = _jobs.store.get(job.id)
        if j is not None and j.state in ("pending", "running"):
            _jobs.store.update(job.id, state="failed", error=str(exc))
        raise
    return JSONResponse(status_code=202, content={"job_id": job.id})


# ---------------------------------------------------------------------------
# Uploaded archives: held past the request, copied into staging by the job
# (shared with the slide import endpoint)
# ---------------------------------------------------------------------------

_UPLOAD_NO_SPACE_CODE = "adminDash.apiErr.uploadNoSpace"
# Bytes per copy_file_range / pread call; the copy reports its progress at
# most once per interval (seconds).
_UPLOAD_COPY_CHUNK = 64 * 1024 * 1024
_UPLOAD_COPY_PROGRESS_INTERVAL = 1.0


class UploadNoSpace(RuntimeError):
    """The staging volume is full (or over quota): the upload could not be
    copied into it. Its jobs report ``adminDash.apiErr.uploadNoSpace``."""


class HeldUpload:
    """An uploaded file part, kept readable after its request has ended.

    Starlette spools each file part to an anonymous temporary file before the
    endpoint runs, and closes it (so the file is gone) once the response has
    been sent. Copying a multi-GB archive into staging inside the request would keep
    the 202 back for as long as the copy took, past a proxy's response timeout
    (Cloudflare Tunnel 100 s, nginx 600 s) on a filesystem that cannot share
    the blocks. The job copies it instead, from a descriptor of its own
    (``os.dup``) that keeps the spool file on disk until :meth:`close`. A part
    Starlette still holds in memory (at most its 1 MiB spool threshold) is
    held as bytes.
    """

    def __init__(self, *, fd: Optional[int], data: Optional[bytes], size: int, suffix: str) -> None:
        self._lock = threading.Lock()
        self._fd = fd
        self.data = data
        self.size = size
        self.suffix = suffix

    @property
    def fd(self) -> Optional[int]:
        return self._fd

    def close(self) -> None:
        """Release the spool file. Idempotent."""
        with self._lock:
            fd, self._fd = self._fd, None
            self.data = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def hold_upload(upload: UploadFile, max_bytes: int) -> HeldUpload:
    """Take hold of ``upload`` for a job to copy into staging (see
    :class:`HeldUpload`). Blocking — async callers go through
    ``run_in_threadpool``.

    The size is known once the body is spooled, so an upload over
    ``max_bytes`` is refused here (413), before any job runs.
    """
    src = upload.file
    fd: Optional[int] = None
    data: Optional[bytes] = None
    # A part still in memory has no spool file, and fileno() would roll it over
    # to TMPDIR — a write that can fail on a full disk.
    if getattr(src, "_rolled", True) is not False:
        try:
            # flush(): Starlette's last write may still sit in the buffer.
            src.flush()
            fileno = src.fileno()
        except (AttributeError, OSError, ValueError):
            fileno = None  # no file behind it: an in-memory object
        if fileno is not None:
            try:
                fd = os.dup(fileno)
            except OSError as exc:
                # Out of descriptors (EMFILE / ENFILE). The spool may hold
                # anything up to the cap, so reading it into memory instead
                # would take the worker down with it.
                raise HTTPException(
                    status_code=503,
                    detail=f"could not keep the upload open ({exc.strerror}); try again",
                ) from exc
    if fd is None:
        src.seek(0)
        data = src.read(max_bytes + 1)
        size = len(data)
        head = data[:4]
    else:
        try:
            size = os.fstat(fd).st_size
            head = os.pread(fd, 4, 0)
        except OSError:
            os.close(fd)
            raise
    held = HeldUpload(fd=fd, data=data, size=size, suffix=_upload_suffix(upload.filename, head))
    if size > max_bytes:
        held.close()
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeded BACKUP_MAX_UPLOAD_BYTES ({max_bytes} bytes)",
        )
    return held


def _upload_suffix(filename: Optional[str], head: bytes) -> str:
    # The suffix names the staged file and so how it is read back. Take it
    # from the content (magic bytes), not the uploader's filename: an
    # upper-case or unrecognised name would default to .tar.zst and fail a
    # gzip archive.
    sniffed = _backup.archive_format_from_magic(head)
    if sniffed is not None:
        return "." + sniffed
    name = (filename or "").lower()
    for s in (".tar.zst", ".tar.gz", ".tgz"):
        if name.endswith(s):
            return s
    return ".tar.zst"


def receive_held_upload(held: HeldUpload, progress_cb=None) -> str:
    """Copy a held upload into a fresh ``BACKUP_DIR/.staging/upload-*`` file,
    fsync it and return its path. Runs in the job; always releases ``held``.

    Reports ``{"phase": "copying", "bytes_copied", "total_bytes"}`` through
    ``progress_cb``. A full (or over-quota) staging volume raises
    :class:`UploadNoSpace`; the partial file is removed on any failure.
    """
    try:
        staging_root = os.path.join(settings.backup_dir, ".staging")
        os.makedirs(staging_root, exist_ok=True)
        dst = os.path.join(staging_root, "upload-" + uuid.uuid4().hex[:12] + held.suffix)
        last_report = 0.0

        def copied(done: int) -> None:
            nonlocal last_report
            now = time.monotonic()
            if progress_cb is None or (
                done < held.size and now - last_report < _UPLOAD_COPY_PROGRESS_INTERVAL
            ):
                return
            last_report = now
            progress_cb({"phase": "copying", "bytes_copied": done, "total_bytes": held.size})

        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        ok = False
        try:
            if held.data is None and held.fd is not None and _reflink(held.fd, out):
                # The staged copy shares the spool file's blocks: no space taken.
                copied(held.size)
            else:
                # A real copy: refuse it up front when the staging volume cannot
                # take it with the shared reserve left free, rather than fill
                # the volume — by default the data volume the live DB writes
                # to — to ENOSPC and only then give up.
                short = _disk_space.check(staging_root, held.size)
                if short is not None:
                    raise UploadNoSpace(short.message("staging volume"))
                _copy_held_upload(held, out, copied)
            # fsync before stage_restore reads it, so the staged archive
            # survives a crash between the copy and the extraction.
            os.fsync(out)
            ok = True
        finally:
            os.close(out)
            if not ok:
                _remove_quietly(dst)
        return dst
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise UploadNoSpace(
                f"not enough free space under {settings.backup_dir} to stage the upload ({exc})"
            ) from exc
        raise
    finally:
        held.close()


# Linux's FICLONE ioctl (_IOW(0x94, 9, int)): make ``dst`` share all of
# ``src``'s blocks, on a filesystem that can (XFS with reflink, btrfs).
_FICLONE = 0x40049409


def _reflink(src: int, dst: int) -> bool:
    """Clone ``src`` into the empty ``dst`` without copying data; False when
    the filesystem (or a cross-volume pair) cannot, and the caller copies."""
    try:
        import fcntl

        fcntl.ioctl(dst, _FICLONE, src)
    except (OSError, ImportError, AttributeError):
        return False
    return True


def _copy_held_upload(held: HeldUpload, out: int, copied) -> None:
    """Write ``held`` to the descriptor ``out``, calling ``copied(bytes_so_far)``
    as it goes.

    ``copy_file_range`` keeps the copy in the kernel, and on a filesystem that
    can (XFS with reflink, btrfs) shares the spool file's blocks instead of
    writing them again; where it is unavailable or refused, the rest is copied
    with pread / pwrite. A full volume's ENOSPC / EDQUOT propagates.
    """
    if held.data is not None:
        view = memoryview(held.data)
        done = 0
        while done < len(view):
            done += os.write(out, view[done:])
        copied(done)
        return
    src = held.fd
    if src is None:
        raise RuntimeError("the upload was released before it was copied")
    copy_range = getattr(os, "copy_file_range", None)
    done = 0
    while done < held.size:
        want = min(_UPLOAD_COPY_CHUNK, held.size - done)
        n = 0
        if copy_range is not None:
            try:
                n = copy_range(src, out, want, done, done)
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise
                n = 0
            if n <= 0:
                copy_range = None
                continue
        else:
            chunk = os.pread(src, want, done)
            if not chunk:
                break
            view = memoryview(chunk)
            written = 0
            while written < len(view):
                written += os.pwrite(out, view[written:], done + written)
            n = len(chunk)
        done += n
        copied(done)
    if done != held.size:
        raise RuntimeError(
            f"the upload's spool file ended after {done} of {held.size} bytes"
        )


# ---------------------------------------------------------------------------
# Restore apply
# ---------------------------------------------------------------------------

@router.post("/api/admin/restore/apply")
async def restore_apply(
    payload: RestoreApplyRequest,
    request: Request,
    admin: User = Depends(require_admin),
):
    if payload.confirm_text != "RESTORE":
        raise HTTPException(status_code=400, detail="confirm_text must equal 'RESTORE'")
    # Throttled per token and audited on failure, like every password re-auth
    # (admin_auth.verify_reauth_password): this one guards the most
    # destructive action there is, so a stolen token must not buy unbounded
    # guesses at the password here either.
    if not await run_in_threadpool(
        verify_reauth_password, request, admin, payload.password, purpose="restore.apply",
    ):
        raise HTTPException(status_code=401, detail="password incorrect")

    if _restore_apply_pending():
        # A prior restore-apply has already written the request file and is
        # waiting for the service to restart. Accepting another apply would
        # overwrite the request file and confuse the swap-on-boot phase.
        raise HTTPException(
            status_code=409,
            detail=_RESTORE_PENDING_DETAIL,
        )

    # Reserve the running-job slot atomically against any concurrent backup
    # POST. The slot stays reserved for the entire duration of the apply
    # work so a backup that lands between has_running() and the swap can't
    # race the SQLite snapshot. Marked terminal before the function returns.
    job = await run_in_threadpool(_claim_job_slot, "restore_apply")
    _jobs.store.update(job.id, state="running")

    try:
        stage_dir = os.path.join(settings.backup_dir, ".staging", payload.stage_id)
        if not os.path.isdir(stage_dir):
            raise HTTPException(status_code=404, detail="stage not found (re-stage the archive)")
        # A directory alone is no stage: one whose staging job died mid-extract
        # holds a partial tree that never passed the archive's checks.
        if not _backup.stage_is_complete(payload.stage_id):
            raise HTTPException(status_code=409, detail=_STAGE_INCOMPLETE_DETAIL)

        # Re-read the manifest to make sure the staged archive is still intact.
        try:
            with open(os.path.join(stage_dir, "manifest.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"staged manifest unreadable: {exc}")
        # Same strict parse as staging: this is the *re-guard*, so reading the
        # staged manifest leniently here would undo it. A manifest that lost
        # its schema_version between staging and apply must fail, not read as
        # 0 and slip past plan_restore's downgrade check below.
        try:
            manifest = _backup.Manifest.from_dict(data)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Includes the stage's schema-upgrade trial, which the staging job
        # ran (backup.plan_staged_restore): a failed one is refused here with
        # the 409 SCHEMA_UPGRADE_FAILED, so the apply does not run the trial
        # itself — a full copy of the staged DB, inside this request's 60 s
        # proxy timeout.
        plan = _backup.plan_staged_restore(payload.stage_id, manifest)
        if plan.blocking_reason:
            raise HTTPException(status_code=409, detail={
                "code": plan.blocking_code,
                "message": plan.blocking_reason,
            })

        # Catch internally-inconsistent archives (DB rows referencing files
        # that the archive doesn't carry) BEFORE the swap. Without this a
        # full-scope restore can promote a DB whose wsi_filename column
        # points at files the archive forgot, and every freshly-promoted
        # slide would return 404 on first tile request.
        integrity = await run_in_threadpool(
            _backup.validate_archive_integrity, payload.stage_id, manifest,
        )
        if not integrity.ok:
            raise HTTPException(status_code=409, detail={
                "code": "ARCHIVE_INCONSISTENT",
                "message": "; ".join(integrity.errors) or "archive failed integrity check",
                "integrity": integrity.to_dict(),
            })

        # Apply migrations on the staged DB BEFORE any swap, so the live DB
        # only ever sees a fully-migrated file.
        try:
            await run_in_threadpool(_backup.apply_migrations_to_stage, payload.stage_id, plan)
        except Exception as exc:
            _logger.exception("migration apply failed for stage %s", payload.stage_id)
            raise HTTPException(status_code=500, detail=f"migration failed: {exc}")

        if settings.backup_dev_inprocess_apply:
            # Dev path: swap in-process and report success synchronously.
            from ..db import engine
            try:
                await run_in_threadpool(
                    _backup.inprocess_apply, payload.stage_id, manifest.scope, engine
                )
            except Exception as exc:
                _logger.exception("inprocess_apply failed for stage %s", payload.stage_id)
                raise HTTPException(status_code=500, detail=str(exc))
            # The swap parked data_dir/{wsi,assets} under .pre-restore-* and,
            # for ``metadata`` / ``reset`` scopes, did NOT move a replacement
            # tree into place. Without recreating the empty dirs, the next
            # upload would hit FileNotFoundError on open(wsi_path, "wb").
            # The cache loaders re-read NetworkSettings / AuthSettings from
            # the freshly-swapped DB so an archive that ships different auth
            # config takes effect immediately instead of waiting for the
            # operator to restart the process.
            try:
                await run_in_threadpool(
                    _run_post_inprocess_apply_hooks, payload.stage_id, manifest.scope,
                )
            except Exception as exc:
                # The swap committed and stands; only bringing this process up
                # to it failed. Report the hooks' message verbatim everywhere —
                # the job, the 500 (not a bare "Internal Server Error") and the
                # result file the hooks just amended — and audit the apply,
                # which did replace the data, instead of losing that record
                # because the 500 skips the audit below.
                message = str(exc) or type(exc).__name__
                _jobs.store.update(job.id, state="failed", error=message)
                await run_in_threadpool(
                    audit, admin, "restore.apply", target=payload.stage_id,
                    mode="inprocess", scope=manifest.scope,
                    backup_app_version=manifest.app_version,
                    post_apply_error=message,
                )
                raise HTTPException(status_code=500, detail=message)
            _jobs.store.update(
                job.id,
                state="succeeded",
                result={"mode": "inprocess", "stage_id": payload.stage_id},
            )
            await run_in_threadpool(
                audit, admin, "restore.apply", target=payload.stage_id,
                mode="inprocess", scope=manifest.scope,
                backup_app_version=manifest.app_version,
            )
            return {"ok": True, "mode": "inprocess", "manifest": manifest.to_dict()}

        # Production path: write the request file and ask systemd to restart the service.
        try:
            await run_in_threadpool(
                _backup.request_apply,
                payload.stage_id, manifest.scope, requested_by=admin.username,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # Audit BEFORE the response is flushed; the restart is about to kill
        # this worker so a post-response hook is too late.
        await run_in_threadpool(
            audit, admin, "restore.apply", target=payload.stage_id,
            mode="restart", scope=manifest.scope,
            backup_app_version=manifest.app_version,
        )

        # Schedule the restart AFTER the response has flushed so the client
        # actually gets the 202 before uvicorn dies.
        captured_job_id = job.id

        # Mark terminal *before* the response (and the BackgroundTask) so the
        # job slot is released the instant control is handed back. The actual
        # file swap happens in the next start-wsi-app.sh invocation; the API
        # has nothing left to do once the request file is written.
        _jobs.store.update(
            job.id,
            state="succeeded",
            result={"mode": "restart", "stage_id": payload.stage_id},
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "mode": "restart",
                "manifest": manifest.to_dict(),
                "message": "service restart scheduled; poll /api/admin/restore/result for the new applied_at",
            },
            background=BackgroundTask(_kick_restart, captured_job_id),
        )
    except BaseException as exc:
        # Release the slot so a follow-up apply or backup isn't blocked by a
        # stuck "running" record. HTTPExceptions still get re-raised so
        # FastAPI emits the right status.
        j = _jobs.store.get(job.id)
        if j is not None and j.state == "running":
            _jobs.store.update(job.id, state="failed", error=str(exc))
        raise


@router.get("/api/admin/restore/result")
def restore_result(admin: User = Depends(require_admin)):
    """Return the most recent restore-apply outcome written by start-wsi-app.sh.

    The result file is left in place so multiple browser tabs / reloads can
    observe it; deleting it requires an explicit DELETE call so an operator
    won't accidentally lose the failure transcript.

    The Backup & Restore tab reads this when it opens, so a request no restart picked
    up in time is reported (and dropped) here first.
    """
    _expire_stale_apply_request()
    data = _backup.read_apply_result()
    return {"result": data}


@router.delete("/api/admin/restore/result")
def restore_result_clear(admin: User = Depends(require_admin)):
    p = settings.restore_result_path
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
    return {"ok": True}


@router.get("/api/admin/restore/preflight/{stage_id}")
async def restore_preflight(
    stage_id: str,
    admin: User = Depends(require_admin),
):
    """Summarise live state, staged archive state, and integrity check.

    Surfaced to the UI as a "what's about to happen" panel before the
    destructive confirm dialog. The shape is intentionally informational —
    the apply endpoint itself re-runs the integrity check (defence in depth)
    and is what actually decides whether to swap.
    """
    if not _STAGE_ID_RE.match(stage_id):
        raise HTTPException(status_code=400, detail="malformed stage_id")
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    if not os.path.isdir(stage_dir):
        raise HTTPException(status_code=404, detail="stage not found")
    # Summarising a partial tree would show the operator a plan for data the
    # apply then refuses (see restore_apply).
    if not _backup.stage_is_complete(stage_id):
        raise HTTPException(status_code=409, detail=_STAGE_INCOMPLETE_DETAIL)

    try:
        with open(os.path.join(stage_dir, "manifest.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"staged manifest unreadable: {exc}")
    # Strict, like staging and the apply re-guard: the preflight is what the
    # operator reads before confirming, so it must not report a shape the
    # manifest does not actually state.
    try:
        manifest = _backup.Manifest.from_dict(data)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    integrity = await run_in_threadpool(
        _backup.validate_archive_integrity, stage_id, manifest,
    )
    live = await run_in_threadpool(_backup.get_live_state_summary)
    archive = await run_in_threadpool(
        _backup.get_archive_state_summary, stage_id, manifest,
    )

    return {
        "stage_id": stage_id,
        "scope": manifest.scope,
        "manifest": manifest.to_dict(),
        # The plan the apply will judge by, staging's findings included
        # (a failed schema-upgrade trial blocks it as SCHEMA_UPGRADE_FAILED).
        "plan": _backup.plan_staged_restore(stage_id, manifest).to_dict(),
        "integrity": integrity.to_dict(),
        "live_state": live,
        "archive_state": archive,
    }


# ---------------------------------------------------------------------------
# Reset (wipe content data, keep admin / config rows)
# ---------------------------------------------------------------------------

@router.get("/api/admin/reset/preflight")
async def reset_preflight(admin: User = Depends(require_admin)):
    """Return a snapshot of what the live install holds so the UI can show
    "this much data will be deleted" before the operator confirms a reset.

    Reset preserves admin / config rows and wipes slide / tour / page-view
    content plus the assets/ and wsi/ trees. No archive is involved — the
    apply machinery rebuilds an empty DB from the live one and swaps it
    via the same restore-apply path.
    """
    live = await run_in_threadpool(_backup.get_live_state_summary)
    return {
        "live_state": live,
        "preserved": {
            # Surface the names the UI labels its "kept" section with so the
            # backend stays the source of truth on what survives a reset.
            "tables": list(_backup._RESET_PRESERVE_TABLES),
        },
    }


@router.post("/api/admin/reset", status_code=202)
async def reset_apply(
    payload: ResetRequest,
    request: Request,
    admin: User = Depends(require_admin),
):
    """Wipe content data, preserve admin / config rows — as a job.

    Requires the admin's current password and the literal ``RESET`` confirm
    string, mirroring the second-factor pattern :func:`restore_apply` uses.
    Building the reset stage snapshots and VACUUMs the live DB, which takes
    longer than a proxy waits for an analytics-heavy one, so it runs on a
    thread (:func:`_run_reset_job`), followed by the same apply as a restore:
    a service restart in production, the in-process swap in dev.

    Answers 202 ``{"job_id", "stage_id", "scope"}``. The stage id is handed
    out up front because the restart that applies the reset can cost the
    dashboard its view of the job (the process goes away; the reset signs
    everyone out): ``/api/admin/restore/result`` reports the outcome under
    that stage id either way.
    """
    if payload.confirm_text != "RESET":
        raise HTTPException(status_code=400, detail="confirm_text must equal 'RESET'")
    # Throttled and audited, sharing the token's bucket with every other
    # re-auth (see restore_apply).
    if not await run_in_threadpool(
        verify_reauth_password, request, admin, payload.password, purpose="reset.apply",
    ):
        raise HTTPException(status_code=401, detail="password incorrect")

    if _restore_apply_pending():
        raise HTTPException(
            status_code=409,
            detail=_RESTORE_PENDING_DETAIL,
        )

    job = await run_in_threadpool(_claim_job_slot, "restore_apply")
    stage_id = _backup.new_stage_id()
    try:
        _jobs.store.update(
            job.id, state="running", progress={"phase": "staging", "stage_id": stage_id},
        )
        threading.Thread(
            target=_run_reset_job, args=(job.id, stage_id, admin),
            name=f"reset-{job.id}", daemon=True,
        ).start()
    except BaseException as exc:
        j = _jobs.store.get(job.id)
        if j is not None and j.state in ("pending", "running"):
            _jobs.store.update(job.id, state="failed", error=str(exc))
        raise
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "stage_id": stage_id, "scope": _backup.SCOPE_RESET},
    )


def _run_reset_job(job_id: str, stage_id: str, admin: User) -> None:
    """Build the reset stage and apply it; the job's result says how
    (``mode``: ``"restart"`` / ``"inprocess"``). In production the restart is
    kicked after the job is marked succeeded, so the record already says so
    when the restart takes this worker down."""
    try:
        try:
            _backup.create_reset_stage(stage_id)
        except Exception as exc:
            raise RuntimeError(f"reset staging failed: {exc}") from exc

        result = {"stage_id": stage_id, "scope": _backup.SCOPE_RESET}
        if settings.backup_dev_inprocess_apply:
            from ..db import engine
            _backup.inprocess_apply(stage_id, _backup.SCOPE_RESET, engine)
            # Same as restore_apply: reset wipes data_dir/{wsi,assets} and
            # doesn't move replacements into place; recreate the empty dirs
            # and reload the in-memory caches.
            try:
                _run_post_inprocess_apply_hooks(stage_id, _backup.SCOPE_RESET)
            except Exception as exc:
                # As restore_apply: the reset stands, the hooks amended the
                # result file, and the job fails below with the same message.
                # Audit the wipe that did happen.
                audit(
                    admin, "reset.apply", target=stage_id, mode="inprocess",
                    post_apply_error=str(exc) or type(exc).__name__,
                )
                raise
            _jobs.store.update(
                job_id, state="succeeded", progress={"phase": "done"},
                result={**result, "mode": "inprocess"},
            )
            audit(admin, "reset.apply", target=stage_id, mode="inprocess")
            return

        try:
            _backup.request_apply(stage_id, _backup.SCOPE_RESET, requested_by=admin.username)
        except BaseException:
            # Nothing else can use this stage.
            _backup.cleanup_stage(stage_id)
            raise
        audit(admin, "reset.apply", target=stage_id, mode="restart")
        _jobs.store.update(
            job_id, state="succeeded", progress={"phase": "done"},
            result={**result, "mode": "restart"},
        )
    except BaseException as exc:
        _logger.exception("reset job %s failed", job_id)
        _jobs.store.update(job_id, state="failed", error=str(exc) or type(exc).__name__)
        return
    _kick_restart(job_id)


@router.delete("/api/admin/restore/stage/{stage_id}")
def restore_stage_cancel(
    stage_id: str,
    admin: User = Depends(require_admin),
):
    """Drop a staged-but-not-yet-applied restore tree.

    The existing 24h ``sweep_stale_staging`` TTL eventually reclaims
    abandoned stages, but for a SCOPE_FULL archive that's potentially
    many TB of disk pinned for a day. The frontend's "Cancel" button
    routes through here so an operator who changes their mind frees
    the space immediately. The endpoint is idempotent: a missing /
    already-swept stage returns 200 with ``"removed": False``.
    """
    # Match the shape stage ids are minted in so a path-traversal id can't
    # reach into ``settings.backup_dir/.staging/...``.
    if not _STAGE_ID_RE.match(stage_id):
        raise HTTPException(status_code=400, detail="malformed stage_id")
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    if not os.path.isdir(stage_dir):
        return {"ok": True, "removed": False}
    # Check and remove under the job-slot lock, so an apply POST cannot claim
    # the slot in the window between the guards below and the removal and then
    # start consuming the stage this request is deleting. Mirrors
    # cancel_slide_import.
    with _jobs.store.slot_guard():
        if _jobs.store.has_running_kind("restore_apply"):
            # A restore-apply is mid-flight in the job slot but has not yet
            # written its request file (the preflight / integrity / migration
            # window, or the dev in-process swap). ``_restore_apply_pending()``
            # below only catches the post-request-file phase, so without this
            # guard a cancel landing in that window would rmtree the stage out
            # from under the running apply.
            raise HTTPException(
                status_code=409,
                detail="a restore-apply is in progress; cancel is not allowed until it finishes",
            )
        if _restore_apply_pending():
            # A restore-apply already wrote the request file; the swap-on-boot
            # phase will consume this stage. Refuse to remove it now so the
            # restart finds a complete tree.
            raise HTTPException(
                status_code=409,
                detail="this stage is referenced by a pending restore-apply",
            )
        _backup.cleanup_stage(stage_id)
    audit(admin, "restore.stage.cancel", target=stage_id)
    return {"ok": True, "removed": True}


# ---------------------------------------------------------------------------
# Pre-restore rollback directories (reclaim disk)
# ---------------------------------------------------------------------------

@router.get("/api/admin/restore/rollbacks")
async def list_rollbacks(admin: User = Depends(require_admin)):
    """List the ``.pre-restore-*`` trees a restore / reset apply parked under
    DATA_DIR so the operator can see how much disk they hold and reclaim it.

    These are the automatic-rollback artefacts: a full-scope apply parks a
    complete copy of the WSI corpus, and they are deliberately never
    auto-deleted, so they accumulate until removed here (or over SSH)."""
    items = [r.to_dict() for r in await run_in_threadpool(_backup.list_rollback_dirs)]
    return {"items": items, "data_dir": settings.data_dir}


@router.delete("/api/admin/restore/rollbacks/{name}")
async def delete_rollback(name: str, admin: User = Depends(require_admin)):
    """Delete a single parked rollback tree.

    It leaves the listing at once (renamed to a ``.rollback-trash-*`` name)
    and is removed on a background thread, since a full-scope tree can take
    longer than a proxy waits; an entry that cannot be removed is logged, and
    a removal cut short by a restart is finished at the next start.

    Idempotent: an already-removed tree returns 200 with ``"removed": False``.
    Refused while a restore-apply is pending a restart, since the swap-on-boot
    phase is still creating / depending on a rollback tree at that point."""
    if _restore_apply_pending():
        raise HTTPException(
            status_code=409,
            detail=_RESTORE_PENDING_DETAIL,
        )
    try:
        removed = await run_in_threadpool(
            functools.partial(_backup.delete_rollback_dir, name, background=True)
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed rollback name")
    if not removed:
        return {"ok": True, "removed": False}
    await run_in_threadpool(audit, admin, "restore.rollback.delete", target=name)
    return {"ok": True, "removed": True}
