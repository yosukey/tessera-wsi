"""Regression tests for backup jobs and the job store.

* A slide deleted after the DB snapshot (while the snapshot is being
  switched out of WAL / stripped / stamped) must not make the WSI pre-scan
  report "not on disk — re-upload or delete those slides", since a retry
  succeeds.
* A pending/running job whose worker died must not read as "running" on
  every poll (with a cancel of it answering 202 "cancelling") until some
  later job POST happens to reap it. The polled GETs and the cancel fail it.
* A cancel whose re-check finds the job already "cancelled" must not answer
  409 for a cancel that worked.
* Backup warnings also come as ``{code, params}`` for the dashboard.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid

import pytest

from wsi_app import backup as _backup
from wsi_app import jobs as _jobs
from wsi_app.config import settings

from .test_backup import _await_job, _reset_slides, _seed_slide_with_files


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _delete_slide_live(slide_id: int, wsi_name: str) -> None:
    """What a slide delete does: under the slide's mutation lock, the row
    goes and its file with it."""
    from wsi_app.admin_helpers import slide_mutation_lock

    with slide_mutation_lock(slide_id):
        conn = sqlite3.connect(_backup._live_db_path())
        try:
            conn.execute("DELETE FROM slides WHERE id = ?", (slide_id,))
            conn.commit()
        finally:
            conn.close()
        os.remove(os.path.join(settings.data_dir, "wsi", wsi_name))


# ---------------------------------------------------------------------------
# A slide deleted after the DB snapshot
# ---------------------------------------------------------------------------

def test_slide_deleted_after_the_snapshot_says_retry_not_reupload(db_session, monkeypatch):
    _reset_slides(db_session)
    s, wsi_name, _thumb = _seed_slide_with_files(db_session, catalog_id="B1-RACE")
    # Right after the snapshot, before the WSI files are pinned (a slide
    # deleted after its pin does not fail the backup at all).
    real = _backup._snapshot_sqlite

    def snapshot_then_delete(src, dst):
        real(src, dst)
        _delete_slide_live(s.id, wsi_name)

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_delete)
    before = {r.id for r in _backup.list_backups()}
    try:
        with pytest.raises(RuntimeError) as ei:
            _backup.create_backup(scope=_backup.SCOPE_FULL, note="b1")
        msg = str(ei.value)
        assert "replaced or deleted after the DB snapshot" in msg, msg
        assert "Retry" in msg
        assert "not on disk" not in msg and "Re-upload" not in msg
        assert {r.id for r in _backup.list_backups()} == before
        assert not [n for n in os.listdir(settings.backup_dir) if n.endswith(".tmp")]
        # The retry the message asks for succeeds.
        monkeypatch.setattr(_backup, "_snapshot_sqlite", real)
        rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="b1-retry")
        _backup.delete_backup(rec.id)
    finally:
        _reset_slides(db_session)


def test_a_truly_missing_file_still_gets_the_permanent_error(db_session, monkeypatch):
    """One slide raced (deleted after the snapshot), one row never had its
    file: a retry cannot fix the second, so that is what the error names."""
    from wsi_app.models import Slide

    _reset_slides(db_session)
    raced, raced_name, _t = _seed_slide_with_files(db_session, catalog_id="B1-RACED")
    lost = Slide(title="lost", catalog_id="B1-LOST")
    db_session.add(lost)
    db_session.commit()
    lost.wsi_filename = f"{lost.id}_gone.svs"
    db_session.commit()
    lost_name = lost.wsi_filename
    real = _backup._snapshot_sqlite

    def snapshot_then_delete(src, dst):
        real(src, dst)
        _delete_slide_live(raced.id, raced_name)

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_delete)
    try:
        with pytest.raises(RuntimeError) as ei:
            _backup.create_backup(scope=_backup.SCOPE_FULL)
        msg = str(ei.value)
        assert "not on disk" in msg and lost_name in msg, msg
        assert raced_name not in msg
    finally:
        _reset_slides(db_session)


# ---------------------------------------------------------------------------
# A job whose worker died
# ---------------------------------------------------------------------------

@pytest.fixture
def orphan_record():
    """A pending/running record on disk whose owning worker has exited."""
    made: list[str] = []

    def make(kind: str = "backup", state: str = "running") -> _jobs.Job:
        job = _jobs.Job(
            id=uuid.uuid4().hex, kind=kind, state=state,
            created_at="2026-10-15T00:00:00Z", pid=_dead_pid(),
            progress={"phase": "writing_wsi"},
        )
        assert _jobs.store._persist(job)
        made.append(job.id)
        return job

    yield make
    for jid in made:
        _jobs.store._reap_orphan(jid)


def _on_disk(job_id: str) -> dict:
    with open(_jobs.store._job_path(job_id), encoding="utf-8") as f:
        return json.load(f)


def test_backup_job_get_fails_a_dead_workers_job(client, admin_headers, orphan_record):
    job = orphan_record("backup", "running")
    r = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    rj = r.json()
    assert rj["state"] == "failed"
    assert rj["error"] == _jobs.WORKER_EXITED_ERROR
    assert rj["result"] == {"code": _jobs.WORKER_EXITED_CODE}
    assert "cancel_requested" not in rj
    # Recorded, so every worker's poll sees the same terminal outcome, and the
    # slot is free.
    assert _on_disk(job.id)["state"] == "failed"
    assert _jobs.store.has_running() is False


def test_restore_stage_job_get_fails_a_dead_workers_job(client, admin_headers, orphan_record):
    job = orphan_record("restore_stage", "pending")
    rj = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers).json()
    assert rj["state"] == "failed"
    assert rj["result"]["code"] == _jobs.WORKER_EXITED_CODE


def test_transfer_job_get_fails_a_dead_workers_job(client, admin_headers, orphan_record):
    job = orphan_record("slide_export", "running")
    r = client.get(f"/api/admin/slides/transfer/jobs/{job.id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    rj = r.json()
    assert rj["state"] == "failed"
    # The shape transferJobError translates: result.code is the i18n key.
    assert rj["result"]["code"] == "adminDash.apiErr.jobWorkerExited"


def test_get_leaves_a_live_workers_job_running(client, admin_headers):
    job = _jobs.store.new("backup")
    try:
        _jobs.store.update(job.id, state="running")
        rj = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers).json()
        assert rj["state"] == "running"
        assert rj["cancel_requested"] is False
    finally:
        _jobs.store.update(job.id, state="failed", error="test")


def test_a_job_the_slot_reaper_already_removed_is_404(client, admin_headers, orphan_record):
    """has_running's reap deletes the record; the poll then gets the 404 the
    dashboard treats as "job lost" — never "running" again."""
    job = orphan_record("backup", "running")
    assert _jobs.store.has_running() is False
    r = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers)
    assert r.status_code == 404


def test_cancel_of_a_dead_workers_job_is_409_not_cancelling(client, admin_headers, orphan_record):
    job = orphan_record("backup", "running")
    r = client.post(f"/api/admin/backup/jobs/{job.id}/cancel", headers=admin_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "adminDash.apiErr.jobWorkerExited"}
    assert not _jobs.store.cancel_requested(job.id)
    rj = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers).json()
    assert rj["state"] == "failed"


def test_reaping_drops_a_pending_cancel_marker(client, admin_headers, orphan_record):
    """A cancel recorded before the worker died: the poll that reaps the job
    must not leave the dashboard on "cancelling…" — the job is failed and
    its marker gone."""
    job = orphan_record("backup", "running")
    _jobs.store.request_cancel(job.id, requested_by={"id": 1, "username": "x"})
    rj = client.get(f"/api/admin/backup/jobs/{job.id}", headers=admin_headers).json()
    assert rj["state"] == "failed"
    assert not _jobs.store.cancel_requested(job.id)


def test_get_reaping_orphan_does_not_resurrect_a_removed_record(tmp_path):
    """Between the first read and the locked re-read, has_running (another
    worker) may delete the record: the reap must answer None, not write a
    "failed" record back."""
    store = _jobs.JobStore()
    store._persist_dir = lambda: str(tmp_path)
    job = _jobs.Job(id=uuid.uuid4().hex, kind="backup", state="running",
                    created_at="x", pid=_dead_pid())
    store._persist(job)
    real_lock = store._cross_worker_lock

    def lock_after_removal(*a, **kw):
        os.remove(store._job_path(job.id))
        return real_lock(*a, **kw)

    store._cross_worker_lock = lock_after_removal
    assert store.get_reaping_orphan(job.id) is None
    assert not os.path.exists(store._job_path(job.id))


# ---------------------------------------------------------------------------
# The cancel worked in the instant before the re-check
# ---------------------------------------------------------------------------

def test_cancel_that_lands_as_the_job_ends_cancelled_is_202(
    client, admin_headers, monkeypatch,
):
    job = _jobs.store.new("backup")
    _jobs.store.update(job.id, state="running")
    real = _jobs.store.request_cancel

    def request_then_job_stops(job_id, requested_by=None):
        real(job_id, requested_by=requested_by)
        # The job thread reads the marker and ends before the re-check.
        _jobs.store.update(job_id, state="cancelled", result={"cancelled_by": "admin"})

    monkeypatch.setattr(_jobs.store, "request_cancel", request_then_job_stops)
    r = client.post(f"/api/admin/backup/jobs/{job.id}/cancel", headers=admin_headers)
    assert r.status_code == 202, r.text
    assert r.json() == {"job_id": job.id, "cancel_requested": True}
    assert not _jobs.store.cancel_requested(job.id)


def test_cancel_that_lands_as_the_job_succeeds_is_still_409(
    client, admin_headers, monkeypatch,
):
    job = _jobs.store.new("backup")
    _jobs.store.update(job.id, state="running")
    real = _jobs.store.request_cancel

    def request_then_job_succeeds(job_id, requested_by=None):
        real(job_id, requested_by=requested_by)
        _jobs.store.update(job_id, state="succeeded")

    monkeypatch.setattr(_jobs.store, "request_cancel", request_then_job_succeeds)
    r = client.post(f"/api/admin/backup/jobs/{job.id}/cancel", headers=admin_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "adminDash.apiErr.backupCancelFinished", "params": {"state": "succeeded"},
    }
    assert not _jobs.store.cancel_requested(job.id)


# ---------------------------------------------------------------------------
# Coded warnings
# ---------------------------------------------------------------------------

def test_backup_warnings_come_with_codes(client, admin_headers, db_session):
    _reset_slides(db_session)
    s, _w, thumb_name = _seed_slide_with_files(db_session, catalog_id="F4-1")
    os.remove(os.path.join(settings.data_dir, "thumbs", thumb_name))
    r = client.post("/api/admin/backup", json={"include_wsi": True, "note": ""},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    try:
        assert rj["state"] == "succeeded", rj
        res = rj["result"]
        assert len(res["warning_items"]) == len(res["warnings"]) == 1
        assert res["warning_items"][0] == {
            "code": "thumbnailMissing", "params": {"name": thumb_name, "slide": s.id},
        }
        assert thumb_name in res["warnings"][0]
    finally:
        _backup.delete_backup(rj["result"]["backup_id"])
        _reset_slides(db_session)


def test_warning_items_are_capped_with_a_more_item(db_session, slide):
    from wsi_app.admin_helpers import tour_asset_dir
    from wsi_app.models import Tour

    tour = Tour(slide_id=slide.id, title="coded")
    db_session.add(tour)
    db_session.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    for i in range(_backup._MAX_BACKUP_WARNINGS + 3):
        with open(os.path.join(adir, f".dot{i:03d}"), "wb") as f:
            f.write(b"x")
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
        items = rec.warning_items
        assert len(items) == len(rec.warnings) == _backup._MAX_BACKUP_WARNINGS + 1
        assert items[0] == {
            "code": "assetLeftOut",
            "params": {"tour": tour.public_id, "name": ".dot000", "kind": "dotfile"},
        }
        assert items[-1] == {"code": "more", "params": {"count": 3}}
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


def test_warning_items_stay_aligned_with_the_sentences():
    """An uncoded add (none today) still gets an item, so index i of
    warning_items always describes warnings[i] and the dashboard can fall
    back to that sentence."""
    w = _backup._BackupWarnings(cap=2)
    w.add("a", "tourDeletedAfterSnapshot", tour="t1")
    w.add("b")  # an uncoded call site: still aligned, code "unknown"
    w.add("c", "sidecarWriteFailed")
    assert w.as_list() == ["a", "b", "…and 1 more (see the server log)"]
    assert w.as_items() == [
        {"code": "tourDeletedAfterSnapshot", "params": {"tour": "t1"}},
        {"code": "unknown", "params": {}},
        {"code": "more", "params": {"count": 1}},
    ]


# ---------------------------------------------------------------------------
# Finding the job that holds the slot from another tab
# ---------------------------------------------------------------------------

def test_active_job_is_none_when_the_slot_is_free(client, admin_headers):
    r = client.get("/api/admin/backup/jobs/active", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"job": None}


def test_active_job_returns_a_live_workers_job(client, admin_headers):
    job = _jobs.store.new("backup")
    try:
        _jobs.store.update(job.id, state="running", progress={"phase": "writing_wsi"})
        rj = client.get("/api/admin/backup/jobs/active", headers=admin_headers).json()["job"]
        assert rj["id"] == job.id
        assert rj["kind"] == "backup"
        assert rj["state"] == "running"
        assert rj["cancel_requested"] is False
    finally:
        _jobs.store.update(job.id, state="failed", error="test")
    assert client.get("/api/admin/backup/jobs/active", headers=admin_headers).json() == {"job": None}


def test_active_job_skips_a_dead_workers_job(client, admin_headers, orphan_record):
    orphan_record("backup", "running")
    assert client.get("/api/admin/backup/jobs/active", headers=admin_headers).json() == {"job": None}
    assert _jobs.store.has_running() is False


def test_active_job_is_admin_only(client, manager_headers):
    assert client.get("/api/admin/backup/jobs/active", headers=manager_headers).status_code == 403


# ---------------------------------------------------------------------------
# A backup a restore is being staged from cannot be deleted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("progress,refused", [
    ({}, True),                               # claimed, source not recorded yet
    ({"snapshot_id": "__THIS__"}, True),      # staging this very backup
    ({"snapshot_id": "someone-else"}, False),
    ({"snapshot_id": None}, False),           # staging an upload
])
def test_delete_is_refused_while_a_restore_stages_from_it(
    client, admin_headers, db_session, progress, refused,
):
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    job = _jobs.store.try_claim_running("restore_stage")
    assert job is not None
    try:
        if progress:
            p = {k: (backup_id if v == "__THIS__" else v) for k, v in progress.items()}
            _jobs.store.update(job.id, state="running", progress=p)
        r = client.delete(f"/api/admin/backup/{backup_id}", headers=admin_headers)
        if refused:
            assert r.status_code == 409, r.text
            assert r.json()["detail"] == {"code": "adminDash.apiErr.backupDeleteWhileStaging"}
            assert _backup.find_backup(backup_id) is not None
        else:
            assert r.status_code == 200, r.text
    finally:
        _jobs.store.update(job.id, state="failed", error="test")
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# Verifying a saved backup
# ---------------------------------------------------------------------------

def _verify(client, admin_headers, backup_id: str) -> dict:
    from .test_backup import _await_job as _await

    r = client.post(f"/api/admin/backup/{backup_id}/verify", headers=admin_headers)
    assert r.status_code == 202, r.text
    return _await(client, admin_headers, r.json()["job_id"])


def test_verify_passes_an_intact_backup(client, admin_headers, db_session):
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        rj = _verify(client, admin_headers, backup_id)
        assert rj["state"] == "succeeded", rj
        assert rj["result"]["sha256_checked"] is True
        assert rj["result"]["members"] >= 2
    finally:
        _backup.delete_backup(backup_id)


@pytest.mark.parametrize("damage", ["flip", "truncate"])
def test_verify_fails_a_damaged_backup(client, admin_headers, db_session, damage):
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        path = _backup.find_backup(backup_id).path
        size = os.path.getsize(path)
        if damage == "flip":
            with open(path, "r+b") as f:
                f.seek(size // 2)
                b = f.read(1)
                f.seek(size // 2)
                f.write(bytes([b[0] ^ 0xFF]))
        else:
            with open(path, "r+b") as f:
                f.truncate(size - 64)
        rj = _verify(client, admin_headers, backup_id)
        assert rj["state"] == "failed", rj
        assert rj["result"].get("damaged") is True, rj
    finally:
        _backup.delete_backup(backup_id)


def test_verify_that_cannot_read_the_archive_does_not_call_it_damaged(
    client, admin_headers, db_session, monkeypatch,
):
    from contextlib import contextmanager

    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)

    @contextmanager
    def unreadable(*a, **kw):
        raise PermissionError(13, "Permission denied")
        yield  # pragma: no cover

    monkeypatch.setattr(_backup, "_open_archive_reader", unreadable)
    try:
        rj = _verify(client, admin_headers, backup_id)
        assert rj["state"] == "failed", rj
        assert not rj["result"].get("damaged"), rj
        assert "could not be read" in rj["error"]
    finally:
        _backup.delete_backup(backup_id)


def test_verify_can_be_cancelled(db_session, client, admin_headers):
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        with pytest.raises(_backup.BackupCancelled):
            _backup.verify_backup(backup_id, should_cancel=lambda: True)
        job = _jobs.store.try_claim_running("backup_verify")
        try:
            _jobs.store.update(job.id, state="running", progress={"backup_id": backup_id})
            r = client.post(f"/api/admin/backup/jobs/{job.id}/cancel", headers=admin_headers)
            assert r.status_code == 202, r.text
        finally:
            _jobs.store.update(job.id, state="cancelled")
    finally:
        _backup.delete_backup(backup_id)


def test_verify_of_an_unknown_backup_is_404(client, admin_headers):
    r = client.post("/api/admin/backup/20990101T000000Z-nope0000/verify", headers=admin_headers)
    assert r.status_code == 404


def test_delete_is_refused_while_that_backup_is_verified(client, admin_headers, db_session):
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    job = _jobs.store.try_claim_running("backup_verify")
    try:
        _jobs.store.update(job.id, state="running", progress={"backup_id": backup_id})
        r = client.delete(f"/api/admin/backup/{backup_id}", headers=admin_headers)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"code": "adminDash.apiErr.backupDeleteWhileVerifying"}
    finally:
        _jobs.store.update(job.id, state="failed", error="test")
        _backup.delete_backup(backup_id)



def test_verify_fails_when_the_recorded_sha256_does_not_match(client, admin_headers, db_session):
    """The codec checksums can all be good while the bytes are not the ones
    the backup wrote (e.g. a re-compressed copy put in its place)."""
    from .test_backup import _make_backup_via_api

    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        sidecar = _backup.find_backup(backup_id).path + ".meta.json"
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        data["sha256"] = "0" * 64
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(data, f)
        rj = _verify(client, admin_headers, backup_id)
        assert rj["state"] == "failed", rj
        assert rj["result"].get("damaged") is True, rj
    finally:
        _backup.delete_backup(backup_id)
