"""A job-slot claim must exist where other workers look.

Each uvicorn worker has its own JobStore memory; what makes a backup/restore on
one worker visible to ``has_running()`` on another is the JSON record under
``BACKUP_DIR/.jobs``. Were ``_persist`` to log a failed write (ENOSPC, EPERM)
and carry on, ``new()`` would register the job in memory only: the flock in
``try_claim_running`` would still order the claims, but the next worker would
find no record and claim the slot too. Two stores pointed at one directory
stand in for two workers here.
"""
from __future__ import annotations

import errno
import json
import os

import pytest

from wsi_app import jobs


def _store(jobs_dir) -> jobs.JobStore:
    s = jobs.JobStore()
    s._persist_dir = lambda: str(jobs_dir)  # redirect persistence to tmp
    return s


@pytest.fixture
def workers(tmp_path):
    jobs_dir = tmp_path / ".jobs"
    jobs_dir.mkdir()
    return _store(jobs_dir), _store(jobs_dir), jobs_dir


def _enospc(*_a, **_k):
    raise OSError(errno.ENOSPC, "No space left on device")


def test_claim_fails_when_record_cannot_be_written(workers, monkeypatch):
    a, b, jobs_dir = workers
    with monkeypatch.context() as m:
        # The record write itself fails, as on a full volume.
        m.setattr(jobs.json, "dump", _enospc)
        with pytest.raises(jobs.JobStoreError):
            a.try_claim_running("backup")
    # Nothing half-registered: no memory-only job on A, no record or tmp left.
    assert a._jobs == {}
    assert a.has_running() is False
    assert [n for n in os.listdir(jobs_dir) if n != ".lock"] == []
    # So the slot is genuinely free, and the normal exclusion holds again.
    job = a.try_claim_running("backup")
    assert job is not None
    assert b.try_claim_running("restore_stage") is None


def test_unavailable_store_refuses_claims(tmp_path):
    s = jobs.JobStore()
    s._persist_dir = lambda: None  # e.g. BACKUP_DIR not writable
    with pytest.raises(jobs.JobStoreError, match="unavailable"):
        s.try_claim_running("backup")
    with pytest.raises(jobs.JobStoreError):
        s.new("backup")
    assert s._jobs == {}


def test_progress_write_failure_keeps_the_claim(workers, monkeypatch):
    """Progress persistence is best-effort: the job keeps running and the
    already-written claim keeps every other worker out."""
    a, b, _ = workers
    job = a.try_claim_running("backup")
    a.update(job.id, state="running")
    with monkeypatch.context() as m:
        m.setattr(jobs.json, "dump", _enospc)
        a.update(job.id, progress={"phase": "writing_wsi", "bytes_written": 10})
    assert a.get(job.id).progress["phase"] == "writing_wsi"
    on_disk = b.get(job.id)
    assert on_disk is not None and on_disk.state == "running"
    assert b.try_claim_running("backup") is None


def test_terminal_write_failure_releases_the_slot(workers, monkeypatch):
    """A finished job whose final state cannot be written must not leave its
    record pending/running under a live pid — that would 409 every later
    backup/restore on every worker until this process exits."""
    a, b, jobs_dir = workers
    job = a.try_claim_running("backup")
    a.update(job.id, state="running")
    with monkeypatch.context() as m:
        m.setattr(jobs.json, "dump", _enospc)
        a.update(job.id, state="succeeded")
    assert not (jobs_dir / f"{job.id}.json").exists()
    assert a.has_running() is False
    assert b.try_claim_running("backup") is not None


def test_claim_record_is_complete_on_disk(workers):
    a, b, jobs_dir = workers
    job = a.try_claim_running("restore_apply")
    rec = json.loads((jobs_dir / f"{job.id}.json").read_text())
    assert rec["id"] == job.id and rec["state"] == "pending" and rec["pid"] == os.getpid()
    assert b.has_running() is True


def test_backup_endpoint_returns_503_when_claim_cannot_be_recorded(
    client, admin_headers, monkeypatch,
):
    from wsi_app.routers import admin_backup

    def failing_write(path, job, *, durable):
        raise OSError(errno.ENOSPC, "No space left on device")

    started = []
    monkeypatch.setattr(jobs.store, "_write_record", failing_write)
    monkeypatch.setattr(admin_backup.threading, "Thread", lambda *a, **k: started.append(1))
    r = client.post("/api/admin/backup", json={"include_wsi": False}, headers=admin_headers)
    assert r.status_code == 503, r.text
    assert "job store" in r.json()["detail"]
    assert started == []
    assert jobs.store.has_running() is False


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/admin/restore/apply",
         {"stage_id": "stage-0123456789ab", "confirm_text": "RESTORE", "password": "x"}),
        ("/api/admin/reset", {"confirm_text": "RESET", "password": "x"}),
        ("/api/admin/slides/import/apply", {"stage_id": "import-0123456789ab"}),
    ],
)
def test_other_job_endpoints_map_claim_failure_to_503(
    client, admin_headers, monkeypatch, path, body,
):
    def failing_write(path_, job, *, durable):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(jobs.store, "_write_record", failing_write)
    r = client.post(path, headers=admin_headers, json=body)
    assert r.status_code == 503, r.text
    assert jobs.store.has_running() is False
