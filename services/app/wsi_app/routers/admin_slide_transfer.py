"""Admin per-slide export / import endpoints (cross-tessera migration).

Distinct from ``admin_backup`` (whole-DB snapshot + destructive restore): these
routes move *selected slides* — with their tours, assets and WSI files — between
separate tessera installs, importing ADDITIVELY into the live DB with no service
restart.

  POST   /api/admin/slides/export                        start an export job
  GET    /api/admin/slides/export/history                 list generated bundles (history)
  DELETE /api/admin/slides/export/{export_id}             delete a bundle early
  GET    /api/admin/slides/transfer/jobs/{job_id}         poll an export/import job
  POST   /api/admin/slides/export/{export_id}/download_url mint a short-lived URL
  GET    /api/admin/slides/export/{export_id}/download     download a bundle
  POST   /api/admin/slides/import                          upload a bundle; 202 + a staging job
  POST   /api/admin/slides/import/apply                    apply a staged import (additive; re-run = retry)
  POST   /api/admin/slides/import/retry-assets             re-copy attachments an apply skipped
  GET    /api/admin/slides/import/stage/{stage_id}         re-read a kept stage's preflight
  DELETE /api/admin/slides/import/stage/{stage_id}         drop a staged import

All endpoints require admin auth (the bundle carries the raw WSI + vendor label
images, and import preserves publish state — restore-shaped, not upload-shaped).
Export and import share the single background-job slot with backup/restore/reset
via ``jobs.try_claim_running`` so they can't run concurrently. Every long step is
such a job, polled through the jobs endpoint: export, apply, retry-assets — and
staging, which copies an uploaded bundle into staging once the request has
received it, then extracts and preflights it (its result is
``{stage_id, plan}``). Import is purely additive,
so — unlike restore/reset — it needs no password second factor.

An apply keeps its stage while something is left to retry (a failed slide, or
an attachment this system's settings refused); running apply again on it, or
retry-assets, picks up from the per-slide outcomes recorded in the stage (see
``slide_transfer``'s "Import: apply state"). The stage is otherwise reclaimed
by the 24 h staging sweep, or dropped early with the DELETE.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import jobs as _jobs
from .. import slide_transfer as _slide_transfer
from ..admin_helpers import audit, error_detail
from ..auth import (
    bearer_token,
    create_export_download_token,
    export_download_token_user,
    require_admin,
    resolve_staff_user,
)
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Slide, User
from .admin_backup import (
    UploadNoSpace,
    _claim_job_slot,
    _restore_apply_pending,
    hold_upload,
    receive_held_upload,
)

router = APIRouter()
_logger = logging.getLogger("wsi_app.routers.admin_slide_transfer")

# Cap per export so a runaway/malicious payload can't pin a worker (mirrors
# BULK_MAX_SLIDES in admin_slides).
EXPORT_MAX_SLIDES = 500

_STAGE_ID_PATTERN = r"^import-[0-9a-f]{12}$"
_EXPORT_ID_PATTERN = r"^export-[0-9a-f]{12}$"

# Coded (i18n key) details for the dashboard; see adminDash.apiErr.* in the
# frontend's locale files.
_TRANSFER_BUSY_DETAIL = error_detail("adminDash.apiErr.transferBusy")
_RESTORE_PENDING_DETAIL = error_detail("adminDash.apiErr.transferRestorePending")


class ExportRequest(BaseModel):
    slide_ids: List[int]
    note: str = Field("", max_length=500)


class ImportApplyRequest(BaseModel):
    stage_id: str = Field(..., pattern=_STAGE_ID_PATTERN)
    # Per-slide conflict resolution keyed by the bundle's slide public_id:
    # "copy" (default) imports as a copy (renamed file / reminted public_id),
    # "skip" leaves out a slide whose WSI name or public_id is already taken
    # here (see slide_transfer._conflict_reason). On a retry, a slide an
    # earlier run skipped is only re-attempted when named.
    #
    # Only those two values: the apply treats anything but "skip" as copy, so
    # a typo ("Skip", "skipped") would import — and publish — the very
    # duplicate it was meant to leave out. So it is a 422.
    #
    # A slide named by no resolution still defaults to copy, a public_id
    # conflict included: that is this API's documented contract, which
    # scripted callers and a whole-bundle "import everything" rely on. The
    # dashboard is what defaults a public_id conflict to "skip", and it sends
    # its choice for every conflicting slide explicitly.
    resolutions: Dict[str, Literal["copy", "skip"]] = Field(default_factory=dict)


class ImportRetryAssetsRequest(BaseModel):
    stage_id: str = Field(..., pattern=_STAGE_ID_PATTERN)


# Job kinds that read a stage while they run; deleting the stage under either
# corrupts it (see cancel_slide_import).
_STAGE_CONSUMING_KINDS = ("slide_import", "slide_import_assets")


def _transfer_error(exc: "_slide_transfer.SlideTransferError", status: int = 400) -> HTTPException:
    """Map a SlideTransferError to a localisable HTTPException detail."""
    code = exc.code or "importFailed"
    return HTTPException(status_code=status, detail=error_detail(f"adminDash.apiErr.{code}"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.post("/api/admin/slides/export")
def start_export(
    payload: ExportRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not payload.slide_ids:
        raise HTTPException(status_code=400, detail="slide_ids must not be empty")
    if len(payload.slide_ids) > EXPORT_MAX_SLIDES:
        raise HTTPException(
            status_code=422,
            detail=f"too many slides selected (max {EXPORT_MAX_SLIDES})",
        )
    # Fast synchronous existence check so an obviously-bad request 404s here
    # rather than surfacing as an async job failure.
    known = {
        sid for (sid,) in db.execute(
            select(Slide.id).where(Slide.id.in_(payload.slide_ids))
        ).all()
    }
    missing = [sid for sid in payload.slide_ids if sid not in known]
    if missing:
        raise HTTPException(status_code=404, detail=f"unknown slide id(s): {missing}")

    if _restore_apply_pending():
        raise HTTPException(status_code=409, detail=_RESTORE_PENDING_DETAIL)
    job = _claim_job_slot("slide_export", _TRANSFER_BUSY_DETAIL)
    _jobs.store.update(job.id, state="running", progress={"phase": "queued", "bytes_written": 0})

    slide_ids = list(payload.slide_ids)
    note = payload.note

    def _run() -> None:
        try:
            def progress(p: dict) -> None:
                _jobs.store.update(job.id, progress=p)
            record = _slide_transfer.export_slides(slide_ids, note=note, progress_cb=progress)
            _jobs.store.update(
                job.id,
                state="succeeded",
                progress={"phase": "done", "bytes_written": record.size_bytes},
                result=record.to_dict(),
            )
            audit(admin, "slide.export", target=record.id,
                  slide_ids=slide_ids, slide_count=record.slide_count,
                  tour_count=record.tour_count, size_bytes=record.size_bytes,
                  sha256=record.sha256)
        except _slide_transfer.SlideTransferError as exc:
            _logger.warning("slide export job %s failed: %s", job.id, exc)
            # Store the full i18n key (and its parameters) so the frontend can
            # translate the failure (mirrors the sync stage endpoint's
            # error_detail shape).
            code = f"adminDash.apiErr.{exc.code}" if exc.code else None
            _jobs.store.update(
                job.id, state="failed", error=str(exc),
                result={"code": code, "params": exc.params},
            )
            audit(admin, "slide.export.failed", target=job.id, error=str(exc), code=exc.code)
        except BaseException as exc:  # noqa: BLE001
            _logger.exception("slide export job %s failed", job.id)
            _jobs.store.update(job.id, state="failed", error=str(exc))
            audit(admin, "slide.export.failed", target=job.id, error=str(exc))

    # Release the job slot if the worker thread never starts (thread
    # exhaustion): the claimed record is owned by this live pid, so the
    # orphan reaper would not free it and every later job would 409 until
    # this worker process exits.
    try:
        threading.Thread(target=_run, name=f"slide-export-{job.id}", daemon=True).start()
    except BaseException as exc:
        _logger.exception("could not start slide export job %s", job.id)
        _jobs.store.update(job.id, state="failed", error=str(exc))
        raise
    return {"job_id": job.id}


@router.get("/api/admin/slides/transfer/jobs/{job_id}")
def get_transfer_job(job_id: str, admin: User = Depends(require_admin)):
    # As the backup job GET: a record whose worker died is answered as the
    # terminal ``failed`` it is (result.code adminDash.apiErr.jobWorkerExited,
    # which transferJobError translates), so pollTransferJob — which has no
    # time limit — ends instead of reading "running" until another job POST
    # happens to reap it.
    job = _jobs.store.get_reaping_orphan(job_id)
    if job is None:
        raise HTTPException(
            status_code=404, detail=error_detail("adminDash.apiErr.transferJobMissing"),
        )
    return job.to_dict()


# NB: the bare ``/api/admin/slides/export`` path can't be a GET listing — the
# admin_slides router's ``GET /api/admin/slides/{slide_id}`` greedily matches it
# (parsing "export" as a slide id → 422). The history therefore lives one
# segment deeper, past that catch-all.
@router.get("/api/admin/slides/export/history")
def list_exports(admin: User = Depends(require_admin)):
    """List generated export bundles awaiting download (the export history).

    These live under ``backup_dir/.staging`` and are reclaimed by the 24h
    ``sweep_stale_staging`` TTL; the history lets an admin re-download or delete
    one early within that window."""
    return {"items": [rec.to_dict() for rec in _slide_transfer.list_exports()]}


@router.delete("/api/admin/slides/export/{export_id}")
def delete_export(
    export_id: str = Path(..., pattern=_EXPORT_ID_PATTERN),
    admin: User = Depends(require_admin),
):
    """Delete an export bundle early (before the 24h TTL sweep would)."""
    if not _slide_transfer.delete_export(export_id):
        raise HTTPException(status_code=404, detail="export bundle not found")
    audit(admin, "slide.export.delete", target=export_id)
    return {"ok": True}


@router.post("/api/admin/slides/export/{export_id}/download_url")
def make_export_download_url(
    export_id: str = Path(..., pattern=_EXPORT_ID_PATTERN),
    admin: User = Depends(require_admin),
):
    rec = _slide_transfer.find_export(export_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="export bundle not found")
    token = create_export_download_token(export_id, admin)
    audit(admin, "slide.export.download_url", target=export_id)
    return {"url": f"/api/admin/slides/export/{export_id}/download?t={token}"}


@router.get("/api/admin/slides/export/{export_id}/download")
def download_export(
    request: Request,
    export_id: str = Path(..., pattern=_EXPORT_ID_PATTERN),
    t: Optional[str] = Query(None),
):
    """Download an export bundle, authorised by a ``?t=`` token (so the browser
    can stream it natively) or an admin Bearer header. Mirrors download_backup."""
    authorized = False
    actor: Optional[User] = None
    # One session serves both auth paths — the ?t= token is bound to the minting
    # user's live row (uid + tv), so following a leaked export URL is
    # blocked by the same revocation the bearer path enforces.
    db = SessionLocal()
    try:
        # The token path is audited against the admin who minted the URL (the
        # token's uid, checked live above), not as an anonymous download: a
        # leaked link followed by someone else still shows whose it was.
        token_user = export_download_token_user(t, export_id, db) if t else None
        if token_user is not None:
            authorized = True
            actor = token_user
        else:
            token = bearer_token(request.headers.get("authorization", ""))
            if token:
                # resolve_staff_user also enforces token_version, so a password
                # change revokes a leaked JWT immediately rather than letting it
                # keep downloading export bundles for the rest of its TTL.
                user = resolve_staff_user(token, db)
                if user and user.role == "admin":
                    authorized = True
                    actor = user
    finally:
        db.close()
    if not authorized:
        raise HTTPException(status_code=401, detail="unauthorized")

    rec = _slide_transfer.find_export(export_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="export bundle not found")
    audit(actor, "slide.export.download", target=export_id, via_token=bool(token_user))
    media_type = (
        "application/zstd" if rec.archive_format == "tar.zst" else "application/gzip"
    )
    return FileResponse(rec.path, media_type=media_type, filename=os.path.basename(rec.path))


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

def _fail_if_running(job_id: str, exc: BaseException) -> None:
    """Release a claimed job slot whose request failed before its job ran."""
    j = _jobs.store.get(job_id)
    if j is not None and j.state == "running":
        _jobs.store.update(job_id, state="failed", error=str(exc))


def _launch_job(
    job: "_jobs.Job",
    kind: str,
    admin: User,
    work: Callable[[Callable[[dict], None]], dict],
    audit_action: str,
    audit_fields: Callable[[dict], dict],
    *,
    target: Callable[[dict], str],
    failure_target: Optional[str] = None,
    release: Optional[Callable[[], None]] = None,
) -> dict:
    """Run ``work(progress_cb)`` for an already-claimed ``job`` in a background
    thread, recording the outcome on the job and in the audit log: on success
    against ``target(result)``, on failure against ``failure_target`` (the
    job id when the work has no target yet). ``release``, when given, is
    called once the job is over, or when its thread never starts: it frees
    what the request handed the work. Returns the ``{job_id}`` response.
    Shared by import staging, apply and retry-assets."""

    failed_on = failure_target or job.id

    def _run() -> None:
        try:
            def progress(p: dict) -> None:
                _jobs.store.update(job.id, progress=p)
            result = work(progress)
            _jobs.store.update(
                job.id, state="succeeded",
                progress={"phase": "done"}, result=result,
            )
            audit(admin, audit_action, target=target(result), **audit_fields(result))
        except _slide_transfer.SlideTransferError as exc:
            _logger.warning("%s job %s failed: %s", kind, job.id, exc)
            # Store the full i18n key (and its parameters) so the frontend can
            # translate the failure.
            code = f"adminDash.apiErr.{exc.code}" if exc.code else None
            _jobs.store.update(
                job.id, state="failed", error=str(exc),
                result={"code": code, "params": exc.params},
            )
            audit(admin, f"{audit_action}.failed", target=failed_on, error=str(exc), code=exc.code)
        except BaseException as exc:  # noqa: BLE001
            _logger.exception("%s job %s failed", kind, job.id)
            _jobs.store.update(job.id, state="failed", error=str(exc))
            audit(admin, f"{audit_action}.failed", target=failed_on, error=str(exc))
        finally:
            if release is not None:
                release()

    # Release the job slot if the worker thread never starts (thread
    # exhaustion): the claimed record is owned by this live pid, so the
    # orphan reaper would not free it and every later job would 409 until
    # this worker process exits.
    try:
        threading.Thread(
            target=_run, name=f"{kind.replace('_', '-')}-{job.id}", daemon=True,
        ).start()
    except BaseException as exc:
        _logger.exception("could not start %s job %s", kind, job.id)
        _jobs.store.update(job.id, state="failed", error=str(exc))
        if release is not None:
            release()
        raise
    return {"job_id": job.id}


# ---------------------------------------------------------------------------
# Import: stage + preflight
# ---------------------------------------------------------------------------

@router.post("/api/admin/slides/import", status_code=202)
async def stage_slide_import(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
):
    """Receive a bundle and stage it in a background job, answering 202
    ``{job_id}`` as soon as the body has been received: copying a multi-GB
    bundle into staging, extracting and checking it take far longer than the
    proxy waits for a response (see ``admin_backup.HeldUpload``). The client
    polls ``GET …/transfer/jobs/{job_id}``; the job's result is
    ``{stage_id, plan}`` — the stage to apply and its preflight (the same
    shape ``GET …/import/stage/{stage_id}`` returns)."""
    if _restore_apply_pending():
        raise HTTPException(status_code=409, detail=_RESTORE_PENDING_DETAIL)
    job = await run_in_threadpool(_claim_job_slot, "slide_import_stage", _TRANSFER_BUSY_DETAIL)

    held = None
    try:
        await run_in_threadpool(
            _jobs.store.update, job.id, state="running", progress={"phase": "copying"},
        )
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="a bundle file is required")
        held = await run_in_threadpool(hold_upload, file, settings.backup_max_upload_bytes)
        await run_in_threadpool(
            _jobs.store.update, job.id,
            progress={"bytes_copied": 0, "total_bytes": held.size},
        )
    except BaseException as exc:
        if held is not None:
            held.close()
        await run_in_threadpool(_fail_if_running, job.id, exc)
        raise

    def _stage(progress: Callable[[dict], None]) -> dict:
        try:
            archive_path = receive_held_upload(held, progress)
        except UploadNoSpace as exc:
            raise _slide_transfer.SlideTransferError(str(exc), code="uploadNoSpace") from exc
        progress({"phase": "extracting"})
        try:
            stage_id, manifest, slides_doc = _slide_transfer.stage_import(archive_path)
        finally:
            try:
                os.remove(archive_path)
            except FileNotFoundError:
                pass
        # The preflight is read-only but not cheap: an indexed SELECT and a
        # directory probe per bundled slide (up to the 500-slide cap), plus a
        # full-column scan of the library. It gets its own session.
        progress({"phase": "checking"})
        session = SessionLocal()
        try:
            plan = _slide_transfer.plan_import(stage_id, manifest, slides_doc, session)
        except BaseException:
            # The job fails without handing anyone this stage_id, so nothing
            # would ever cancel it: drop it now rather than leave an extracted
            # bundle for the 24 h sweep.
            _slide_transfer.cleanup_import_stage(stage_id)
            raise
        finally:
            session.close()
        return {"stage_id": stage_id, "plan": plan}

    return _launch_job(
        job, "slide_import_stage", admin, _stage, "slide.import.stage",
        lambda result: {
            "bundle_app_version": result["plan"].get("bundle_app_version"),
            "slide_count": result["plan"].get("slide_count"),
        },
        target=lambda result: result["stage_id"],
        release=held.close,
    )


def _start_stage_job(
    kind: str,
    admin: User,
    stage_id: str,
    work: Callable[[Callable[[dict], None]], dict],
    audit_action: str,
    audit_fields: Callable[[dict], dict],
) -> dict:
    """Claim the job slot and run ``work(progress_cb)`` against a stage in a
    background thread. Shared by apply and retry-assets, which differ only in
    the work and what they audit."""
    if _restore_apply_pending():
        raise HTTPException(status_code=409, detail=_RESTORE_PENDING_DETAIL)
    job = _claim_job_slot(kind, _TRANSFER_BUSY_DETAIL)
    _jobs.store.update(job.id, state="running", progress={"phase": "queued"})
    return _launch_job(
        job, kind, admin, work, audit_action, audit_fields,
        target=lambda _result: stage_id, failure_target=stage_id,
    )


@router.post("/api/admin/slides/import/apply")
def apply_slide_import(
    payload: ImportApplyRequest,
    admin: User = Depends(require_admin),
):
    """Apply a staged import. Also the "retry failed slides" action: run
    against a stage an earlier apply kept, it leaves what that apply imported
    alone and attempts only the slides still missing."""
    stage_id = payload.stage_id
    resolutions = dict(payload.resolutions)
    return _start_stage_job(
        "slide_import", admin, stage_id,
        lambda progress: _slide_transfer.apply_import(stage_id, resolutions, progress_cb=progress),
        "slide.import",
        lambda result: {
            "imported": result.get("imported_count"),
            "skipped": result.get("skipped_count"),
            "failed": result.get("failed_count"),
            "already_imported": result.get("already_imported_count"),
            "stage_kept": result.get("stage_kept"),
        },
    )


@router.post("/api/admin/slides/import/retry-assets")
def retry_slide_import_assets(
    payload: ImportRetryAssetsRequest,
    admin: User = Depends(require_admin),
):
    """Re-copy, from a kept stage, the attachments an earlier apply skipped
    because of this system's limits or type allow-list (after the admin has
    raised them) into the tours that apply created. Same job slot and
    polling as apply."""
    stage_id = payload.stage_id
    return _start_stage_job(
        "slide_import_assets", admin, stage_id,
        lambda progress: _slide_transfer.retry_import_assets(stage_id, progress_cb=progress),
        "slide.import.retry_assets",
        lambda result: {
            "copied": result.get("copied_count"),
            "still_skipped": result.get("still_skipped_count"),
            "stage_kept": result.get("stage_kept"),
        },
    )


@router.get("/api/admin/slides/import/stage/{stage_id}")
def get_slide_import_stage(
    stage_id: str = Path(..., pattern=_STAGE_ID_PATTERN),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Recompute the preflight of a stage still on disk — how the dashboard
    returns to a bundle kept for a retry, or staged and not yet applied. Same
    shape as the staging job's result; each slide's ``apply_status`` says what
    an earlier apply did with it.
    Read-only, so it needs no job slot (a running apply rewrites the state
    file atomically; this sees the old or the new one)."""
    try:
        return _slide_transfer.stage_plan(stage_id, db)
    except _slide_transfer.SlideTransferError as exc:
        raise _transfer_error(exc, status=404 if exc.code == "importStageMissing" else 400)


@router.delete("/api/admin/slides/import/stage/{stage_id}")
def cancel_slide_import(
    stage_id: str = Path(..., pattern=_STAGE_ID_PATTERN),
    admin: User = Depends(require_admin),
):
    # Refuse to drop the stage while an import-apply (or an attachment retry)
    # is running: both read the staged files into the live tree and record
    # progress in the stage, so rmtree'ing the stage from under them corrupts
    # the in-flight import (leaving a half-imported, bundle-looking mess).
    # Mirrors restore_stage_cancel's pending-apply guard.
    #
    # Both the check and the removal run under the job-slot lock. Checking
    # without holding it would leave the exact window the guard exists to close:
    # an apply POST could claim the slot between has_running_kind() returning
    # False and the rmtree, and its worker thread would then be reading the tree
    # this request is deleting.
    with _jobs.store.slot_guard():
        if any(_jobs.store.has_running_kind(k) for k in _STAGE_CONSUMING_KINDS):
            raise HTTPException(
                status_code=409, detail=error_detail("adminDash.apiErr.importCancelBusy"),
            )
        _slide_transfer.cleanup_import_stage(stage_id)
    audit(admin, "slide.import.cancel", target=stage_id)
    return {"ok": True}
