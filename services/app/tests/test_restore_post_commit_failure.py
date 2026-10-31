"""What a post-apply hook failing *after* a committed in-process apply leaves
behind.

backup.trial_schema_reconcile runs the startup reconcile on a copy of the
staged DB before any move (test_restore_hardening.py: a failing one refuses
the apply with the live data untouched). Should the real reconcile (or the
bootstrap, or the sign-out retry) still fail once the swap has committed — a
failure the trial could not foresee — the restore stands: the moves are
committed and the journal closed, and nothing rolls a committed restore back.
This pins down what the operator then sees on the dev in-process path, for a
restore and for a reset alike: the endpoint / job report the hook's message,
and the result file — written ``ok: true`` by the swap before the hooks ran,
and all the dashboard has to read once everyone is signed out — carries that
same message as ``post_apply_error`` instead of reading as a clean success.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from wsi_app import backup as _backup
from wsi_app.config import settings
from wsi_app.main import app
from wsi_app.routers import admin_backup

from .test_backup import _await_job_record, _make_backup_via_api, _stage_via_api


def _fail_live_reconcile(monkeypatch):
    """Let the trial reconcile (on its throwaway copy) pass and fail the live
    engine's, as a failure only the real swapped-in DB hits would."""
    from wsi_app import main as _main
    from wsi_app.db import engine

    real_reconcile = _main.reconcile_schema

    def reconcile(bind):
        if bind is engine:
            raise RuntimeError("simulated reconcile failure after the swap")
        return real_reconcile(bind)

    monkeypatch.setattr(_main, "reconcile_schema", reconcile)


def _fail_session_retry(monkeypatch):
    def retry():
        raise RuntimeError("simulated sign-out retry failure")

    monkeypatch.setattr(_backup, "retry_pending_session_revocation", retry)


def test_a_reconcile_failing_after_the_inprocess_swap_is_reported_not_undone(
    client, admin_headers, monkeypatch,
):
    from wsi_app import jobs as _jobs
    from wsi_app import slide_resolve_cache
    from wsi_app.db import SessionLocal
    from wsi_app.models import AuditLog

    assert settings.backup_dev_inprocess_apply
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    live_ino = os.stat(_backup._live_db_path()).st_ino
    # A resolution of the pre-swap DB, as a /dzi/ request would have cached it.
    slide_resolve_cache.put("pre-swap-public-id", (1, True, 0))
    assert slide_resolve_cache.get("pre-swap-public-id") == (1, True, 0)

    claimed = []
    real_claim = admin_backup._claim_job_slot

    def claim(kind, *a, **kw):
        job = real_claim(kind, *a, **kw)
        claimed.append(job.id)
        return job

    _fail_live_reconcile(monkeypatch)
    monkeypatch.setattr(admin_backup, "_claim_job_slot", claim)
    try:
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/admin/restore/apply",
            json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 500, r.text
        detail = r.json()["detail"]
        assert "schema could not be brought up to date" in detail
        assert "simulated reconcile failure" in detail

        # The swap committed: the restored DB is live, the journal and the
        # stage are gone, and a rollback copy of the pre-restore data exists.
        assert os.stat(_backup._live_db_path()).st_ino != live_ino
        assert not os.path.lexists(_backup.restore_journal_path())
        assert not os.path.isdir(os.path.join(settings.backup_dir, ".staging", stage_id))

        # The result file still reports the swap as done, and says what the
        # 500 said — the endpoint, the job and the file agree.
        result = _backup.read_apply_result()
        assert result["ok"] is True and result["stage_id"] == stage_id, result
        assert result["rollback_root"] and os.path.isdir(result["rollback_root"])
        assert result["post_apply_error"] == detail
        assert result["post_apply_stage"] == "schema"

        # The job carries the same cause, and its slot is free again.
        (job_id,) = claimed
        job = _jobs.store.get(job_id)
        assert job.state == "failed"
        assert job.error == detail
        assert not _jobs.store.has_running()

        # This worker stops serving the pre-swap resolution, although the
        # schema step after the flush failed.
        assert slide_resolve_cache.get("pre-swap-public-id") is slide_resolve_cache.MISS

        # The apply that did replace the data is audited, with the failure.
        db = SessionLocal()
        try:
            rows = db.execute(
                select(AuditLog).where(
                    AuditLog.action == "restore.apply", AuditLog.target == stage_id,
                )
            ).scalars().all()
        finally:
            db.close()
        assert [json.loads(row.details).get("post_apply_error") for row in rows] == [detail]
    finally:
        monkeypatch.undo()
        # Bring this test process up to the swapped-in DB, as a restart would.
        admin_backup._run_post_inprocess_apply_hooks()
        _backup.delete_backup(backup_id)


@pytest.mark.parametrize("fail, stage, needle", [
    (_fail_live_reconcile, "schema", "schema could not be brought up to date"),
    (_fail_session_retry, "session_revocation",
     "sessions issued before it could not be signed out"),
])
def test_a_hook_failing_after_the_inprocess_reset_is_reported_in_the_result(
    client, admin_headers, monkeypatch, fail, stage, needle,
):
    fail(monkeypatch)
    try:
        r = client.post(
            "/api/admin/reset",
            json={"confirm_text": "RESET", "password": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 202, r.text
        stage_id = r.json()["stage_id"]
        rec = _await_job_record(r.json()["job_id"])
        assert rec["state"] == "failed", rec
        assert needle in rec["error"] and "simulated" in rec["error"], rec
        # A sign-out failure is not reported as a schema problem.
        if stage != "schema":
            assert "schema" not in rec["error"]

        result = _backup.read_apply_result()
        assert result["ok"] is True and result["stage_id"] == stage_id, result
        assert result["scope"] == "reset"
        assert result["post_apply_error"] == rec["error"]
        assert result["post_apply_stage"] == stage
    finally:
        monkeypatch.undo()
        admin_backup._run_post_inprocess_apply_hooks()


def test_post_apply_error_replaces_a_missing_swap_report():
    """The swap's own ``ok: true`` write is best effort; when it is absent (or
    belongs to another stage) the amendment still says the swap committed."""
    path = settings.restore_result_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ok": False, "stage_id": "stage-older", "error": "boom"}, f)
    try:
        _backup.record_post_apply_error("stage-new", "full", "bootstrap", "it failed")

        result = _backup.read_apply_result()
        assert result["ok"] is True and result["stage_id"] == "stage-new"
        assert result["scope"] == "full"
        assert result["post_apply_error"] == "it failed"
        assert result["post_apply_stage"] == "bootstrap"
        assert "error" not in result and result.get("applied_at")
    finally:
        os.remove(path)


def test_post_apply_hooks_flush_this_workers_slide_caches(monkeypatch):
    """An in-process swap has no restart to empty the per-worker caches: the
    hooks drop the resolve cache (bumping the cross-worker sentinel) and the
    DZI handle cache, both of which describe the pre-swap DB."""
    from wsi_app import dzi, slide_resolve_cache

    cleared = []
    monkeypatch.setattr(dzi, "clear_dz_cache", lambda slide_id=None: cleared.append(slide_id) or 0)
    slide_resolve_cache.put("pre-swap-public-id", (7, False, 0))
    before = slide_resolve_cache.change_count()

    admin_backup._run_post_inprocess_apply_hooks()

    assert slide_resolve_cache.get("pre-swap-public-id") is slide_resolve_cache.MISS
    assert slide_resolve_cache.change_count() == before + 1
    assert os.path.isfile(slide_resolve_cache._sentinel_path())
    assert cleared == [None]
