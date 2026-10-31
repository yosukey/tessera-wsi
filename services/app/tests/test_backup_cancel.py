"""Cancelling a running backup (POST /api/admin/backup/jobs/{id}/cancel).

The request is recorded as a ``{id}.cancel`` marker in the shared job store,
so the worker that takes it need not be the one running the backup; the
backup polls the marker at phase / file boundaries and, time-throttled, while
one file streams. A cancelled run leaves no partial archive, no snapshot dir
and no published archive, ends in state ``cancelled`` with an audit row, and
frees the job slot at once. A request that lands after the last check before
the archive is published is too late: the job succeeds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

from wsi_app import backup as _backup
from wsi_app import jobs as _jobs
from wsi_app.config import settings

from .test_backup import _reset_slides, _seed_slide_with_files

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TERMINAL = ("succeeded", "failed", "cancelled")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _await_terminal(client, headers, job_id, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    rj = None
    while time.monotonic() < deadline:
        rj = client.get(f"/api/admin/backup/jobs/{job_id}", headers=headers).json()
        if rj.get("state") in _TERMINAL:
            return rj
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state: {rj}")


def _published_ids() -> set:
    return {r.id for r in _backup.list_backups()}


def _partial_archives() -> list:
    return [n for n in os.listdir(settings.backup_dir) if n.endswith(".tmp")]


def _snapshot_dirs() -> list:
    staging = os.path.join(settings.backup_dir, ".staging")
    if not os.path.isdir(staging):
        return []
    return [n for n in os.listdir(staging) if n.startswith(_backup._SNAPSHOT_TMPDIR_PREFIX)]


def _cancel_from_another_process(job_id: str) -> None:
    """Record the cancel the way a different uvicorn worker would: a separate
    interpreter, sharing nothing with this one but BACKUP_DIR (inherited via
    the environment conftest set up)."""
    code = (
        "import sys; from wsi_app import jobs; "
        "jobs.store.request_cancel(sys.argv[1], requested_by={'id': 0, 'username': 'other-worker'})"
    )
    subprocess.run(
        [sys.executable, "-c", code, job_id],
        cwd=_APP_ROOT, env=dict(os.environ), check=True, timeout=60,
    )


def _audit_rows(db_session, action: str, target: str, expect: int = 1) -> list:
    """The audit rows for ``action``; waits (briefly) for ``expect`` of them,
    since the job thread writes its row just after the terminal state."""
    from wsi_app.models import AuditLog

    deadline = time.monotonic() + 10.0
    while True:
        db_session.expire_all()
        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.action == action, AuditLog.target == target)
            .all()
        )
        if len(rows) >= expect or time.monotonic() > deadline:
            return rows
        time.sleep(0.02)


@pytest.fixture
def jstore(tmp_path):
    d = tmp_path / ".jobs"
    d.mkdir()

    def make():
        s = _jobs.JobStore()
        s._persist_dir = lambda: str(d)
        return s
    return make


@pytest.fixture
def held_backup(monkeypatch):
    """Make the next backup pause right after its DB snapshot until released,
    so a test can act while the job is genuinely running."""
    started = threading.Event()
    release = threading.Event()
    real = _backup._snapshot_sqlite

    def held(src, dst):
        real(src, dst)
        started.set()
        release.wait(20)

    monkeypatch.setattr(_backup, "_snapshot_sqlite", held)
    yield started, release
    release.set()


def _start_backup(client, headers, *, full: bool = False) -> str:
    r = client.post(
        "/api/admin/backup", json={"include_wsi": full, "note": ""}, headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


# ---------------------------------------------------------------------------
# job store: the cross-worker marker
# ---------------------------------------------------------------------------

def test_cancel_marker_is_seen_by_the_owning_store_and_cleared_on_terminal(jstore):
    owner, other = jstore(), jstore()
    job = owner.new("backup")
    owner.update(job.id, state="running")
    assert owner.cancel_requested(job.id) is False

    other.request_cancel(job.id, requested_by={"id": 7, "username": "bob"})
    assert owner.cancel_requested(job.id) is True
    assert owner.cancel_request_info(job.id)["requested_by"] == {"id": 7, "username": "bob"}
    # The marker is not a job record and does not free (or take) the slot.
    assert owner.has_running() is True
    assert other.try_claim_running("backup") is None

    owner.update(job.id, state="cancelled")
    assert owner.cancel_requested(job.id) is False
    assert other.has_running() is False
    assert other.get(job.id).state == "cancelled"
    assert other.try_claim_running("backup") is not None


def test_cancelled_is_terminal_for_pruning(jstore):
    s = jstore()
    job = s.new("backup")
    s.update(job.id, state="cancelled")
    path = s._job_path(job.id)
    old = time.time() - 30 * 24 * 3600
    os.utime(path, (old, old))
    assert s.prune_terminal() == 1
    assert not os.path.exists(path)


def test_prune_drops_stray_markers_only(jstore):
    s = jstore()
    running = s.new("backup")
    s.update(running.id, state="running")
    done = s.new("backup")
    s.update(done.id, state="succeeded")
    gone = uuid.uuid4().hex
    for jid in (running.id, done.id, gone):
        s.request_cancel(jid)
    s.prune_terminal()
    assert s.cancel_requested(running.id) is True
    assert s.cancel_requested(done.id) is False
    assert s.cancel_requested(gone) is False
    s.update(running.id, state="failed")


def test_reaping_an_orphan_drops_its_marker(jstore):
    s = jstore()
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    orphan = _jobs.Job(id=uuid.uuid4().hex, kind="backup", state="running",
                       created_at="x", pid=p.pid)
    s._persist(orphan)
    s.request_cancel(orphan.id)
    assert s.has_running() is False
    assert s.cancel_requested(orphan.id) is False


def test_marker_names_are_confined_to_job_ids(jstore):
    s = jstore()
    assert s.cancel_requested("../../etc/passwd") is False
    with pytest.raises(_jobs.JobStoreError):
        s.request_cancel("../evil")


# ---------------------------------------------------------------------------
# create_backup: where the cancel is honoured, and what it leaves behind
# ---------------------------------------------------------------------------

def test_cancel_before_start_publishes_nothing(db_session):
    before = _published_ids()
    with pytest.raises(_backup.BackupCancelled):
        _backup.create_backup(scope=_backup.SCOPE_METADATA, should_cancel=lambda: True)
    assert _published_ids() == before
    assert _partial_archives() == []
    assert _snapshot_dirs() == []


def test_cancel_mid_file_stops_streaming_a_large_wsi(db_session, monkeypatch):
    """The check runs inside one file's copy loop, not only between files."""
    _reset_slides(db_session)
    _s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="CANCEL-BIG")
    size = 48 * 1024 * 1024
    with open(os.path.join(settings.data_dir, "wsi", wsi_name), "wb") as f:
        f.write(os.urandom(size))
    monkeypatch.setattr(_backup, "_CANCEL_CHECK_INTERVAL_SEC", 0.0)

    read_bytes = {"wsi": 0}
    real_read = _backup._CancellableReader.read

    def counting_read(self, n=-1):
        data = real_read(self, n)
        read_bytes["wsi"] += len(data)
        return data

    monkeypatch.setattr(_backup._CancellableReader, "read", counting_read)
    phases = []

    def should_cancel():
        # Cancel once a third of the big WSI has streamed.
        return read_bytes["wsi"] > size // 3

    before = _published_ids()
    try:
        with pytest.raises(_backup.BackupCancelled):
            _backup.create_backup(
                scope=_backup.SCOPE_FULL, should_cancel=should_cancel,
                progress_cb=lambda p: phases.append(p["phase"]),
            )
        assert size // 3 < read_bytes["wsi"] < size, read_bytes
        assert "writing_wsi" in phases and "finalizing" not in phases
        assert _published_ids() == before
        assert _partial_archives() == []
        assert _snapshot_dirs() == []
    finally:
        _reset_slides(db_session)


def test_cancel_check_is_throttled_by_time_within_a_file(db_session, monkeypatch):
    """With the default interval a fast stream asks rarely, not per read."""
    _reset_slides(db_session)
    _s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="CANCEL-THROTTLE")
    with open(os.path.join(settings.data_dir, "wsi", wsi_name), "wb") as f:
        f.write(b"\0" * (32 * 1024 * 1024))
    monkeypatch.setattr(_backup, "_CANCEL_CHECK_INTERVAL_SEC", 3600.0)
    asked = {"n": 0}

    def should_cancel():
        asked["n"] += 1
        return False

    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, should_cancel=should_cancel)
    try:
        # Phase / per-file checks only (a handful); none of the ~2000 reads.
        assert asked["n"] < 20, asked
    finally:
        _backup.delete_backup(rec.id)
        _reset_slides(db_session)


def test_failing_cancel_check_does_not_fail_the_backup(db_session):
    def broken():
        raise OSError("job store unreadable")

    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, should_cancel=broken)
    try:
        assert rec.id in _published_ids()
    finally:
        _backup.delete_backup(rec.id)


def test_cancel_at_finalize_leaves_no_archive(db_session):
    """Requested after the archive is written and fsync'd, before the rename:
    still honoured, and nothing is published."""
    state = {"finalizing": False}

    def progress(p):
        if p["phase"] == "finalizing":
            state["finalizing"] = True

    before = _published_ids()
    with pytest.raises(_backup.BackupCancelled):
        _backup.create_backup(
            scope=_backup.SCOPE_METADATA, progress_cb=progress,
            should_cancel=lambda: state["finalizing"],
        )
    assert state["finalizing"]
    assert _published_ids() == before
    assert _partial_archives() == []
    assert _snapshot_dirs() == []
    # The sidecar is written before the publish; a cancel after it takes the
    # sidecar along.
    assert not [
        n for n in os.listdir(settings.backup_dir)
        if n.endswith(".meta.json") and not os.path.exists(
            os.path.join(settings.backup_dir, n[: -len(".meta.json")]))
    ]


def test_close_error_after_cancel_still_reports_the_cancel(db_session, monkeypatch):
    """A stream cut mid-member may fail to close; that must not turn the
    cancel into a failure."""
    real_open = _backup._open_archive_writer

    def opener(path):
        raw, tar, counter, comp = real_open(path)
        real_close = tar.close

        def bad_close():
            real_close()
            raise OSError("trailer write failed")
        tar.close = bad_close
        return raw, tar, counter, comp

    monkeypatch.setattr(_backup, "_open_archive_writer", opener)
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 3  # after the archive is open

    with pytest.raises(_backup.BackupCancelled):
        _backup.create_backup(scope=_backup.SCOPE_METADATA, should_cancel=should_cancel)
    assert _partial_archives() == []


# ---------------------------------------------------------------------------
# the endpoint and the job lifecycle
# ---------------------------------------------------------------------------

def test_cancel_from_another_worker_ends_cancelled_and_frees_the_slot(
    client, admin_headers, admin_user, db_session, held_backup,
):
    started, release = held_backup
    before = _published_ids()
    job_id = _start_backup(client, admin_headers)
    assert started.wait(20)
    rj = client.get(f"/api/admin/backup/jobs/{job_id}", headers=admin_headers).json()
    assert rj["state"] == "running" and rj["cancel_requested"] is False

    _cancel_from_another_process(job_id)
    rj = client.get(f"/api/admin/backup/jobs/{job_id}", headers=admin_headers).json()
    assert rj["cancel_requested"] is True
    release.set()

    rj = _await_terminal(client, admin_headers, job_id)
    assert rj["state"] == "cancelled", rj
    assert rj["error"] is None
    assert rj["result"] == {"cancelled_by": "other-worker"}
    assert "cancel_requested" not in rj
    assert _published_ids() == before
    assert _partial_archives() == []
    assert _snapshot_dirs() == []
    assert not _jobs.store.cancel_requested(job_id)

    rows = _audit_rows(db_session, "backup.create.cancelled", job_id)
    assert len(rows) == 1
    assert rows[0].actor_username == admin_user.username
    assert json.loads(rows[0].details)["cancelled_by"] == "other-worker"

    # The slot is free at once: another backup starts and completes.
    release.set()
    new_id = _start_backup(client, admin_headers)
    rj2 = _await_terminal(client, admin_headers, new_id)
    assert rj2["state"] == "succeeded", rj2
    _backup.delete_backup(rj2["result"]["backup_id"])


def test_cancel_endpoint_mid_file_and_restore_can_start_right_after(
    client, admin_headers, admin_user, db_session, monkeypatch,
):
    """Cancel through the endpoint while a large WSI is mid-stream; the run
    stops inside that file, cleans up, and a restore staging job can claim
    the slot immediately."""
    _reset_slides(db_session)
    _s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="CANCEL-API")
    size = 48 * 1024 * 1024
    with open(os.path.join(settings.data_dir, "wsi", wsi_name), "wb") as f:
        f.write(os.urandom(size))
    monkeypatch.setattr(_backup, "_CANCEL_CHECK_INTERVAL_SEC", 0.0)

    midfile, release = threading.Event(), threading.Event()
    streamed = {"n": 0}
    real_read = _backup._CancellableReader.read

    def read(self, n=-1):
        data = real_read(self, n)
        streamed["n"] += len(data)
        if streamed["n"] > 8 * 1024 * 1024 and not midfile.is_set():
            midfile.set()
            release.wait(20)
        return data

    monkeypatch.setattr(_backup._CancellableReader, "read", read)
    before = _published_ids()
    try:
        job_id = _start_backup(client, admin_headers, full=True)
        assert midfile.wait(30)
        r = client.post(f"/api/admin/backup/jobs/{job_id}/cancel", headers=admin_headers)
        assert r.status_code == 202, r.text
        assert r.json() == {"job_id": job_id, "cancel_requested": True}
        # Idempotent, audited once.
        r = client.post(f"/api/admin/backup/jobs/{job_id}/cancel", headers=admin_headers)
        assert r.status_code == 202, r.text
        release.set()

        rj = _await_terminal(client, admin_headers, job_id)
        assert rj["state"] == "cancelled", rj
        assert rj["result"]["cancelled_by"] == admin_user.username
        assert streamed["n"] < size
        assert _published_ids() == before
        assert _partial_archives() == []
        assert _snapshot_dirs() == []
        assert len(_audit_rows(db_session, "backup.create.cancel_requested", job_id)) == 1
        assert len(_audit_rows(db_session, "backup.create.cancelled", job_id)) == 1

        # A finished job cannot be cancelled.
        r = client.post(f"/api/admin/backup/jobs/{job_id}/cancel", headers=admin_headers)
        assert r.status_code == 409

        # The slot is free: a restore can claim it at once.
        claim = _jobs.store.try_claim_running("restore_stage")
        assert claim is not None
        _jobs.store.update(claim.id, state="failed", error="test")
    finally:
        release.set()
        _reset_slides(db_session)


def test_cancel_landing_after_publish_completes_as_succeeded(
    client, admin_headers, monkeypatch,
):
    """The race with completion: a request after the last check is too late.
    The job says succeeded (with cancel_too_late) and the archive is listed —
    never "cancelled" with an archive, nor "succeeded" without one."""
    real = _backup._fsync_parent_dir
    seen = {}

    def after_publish(path):
        # The rename has happened; record a cancel for the running job now.
        running = [j for j in _jobs.store._iter_disk_jobs()
                   if j.kind == "backup" and j.state == "running"]
        assert len(running) == 1
        seen["id"] = running[0].id
        _cancel_from_another_process(running[0].id)
        real(path)

    monkeypatch.setattr(_backup, "_fsync_parent_dir", after_publish)
    job_id = _start_backup(client, admin_headers)
    rj = _await_terminal(client, admin_headers, job_id)
    assert seen["id"] == job_id
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["cancel_too_late"] is True
    assert rj["result"]["backup_id"] in _published_ids()
    assert not _jobs.store.cancel_requested(job_id)
    _backup.delete_backup(rj["result"]["backup_id"])


def test_cancel_endpoint_404_and_409(client, admin_headers):
    r = client.post(f"/api/admin/backup/jobs/{uuid.uuid4().hex}/cancel", headers=admin_headers)
    assert r.status_code == 404
    r = client.post("/api/admin/backup/jobs/not-a-job/cancel", headers=admin_headers)
    assert r.status_code == 404

    # A running job of another kind is not cancellable.
    other = _jobs.store.new("restore_stage")
    try:
        _jobs.store.update(other.id, state="running")
        r = client.post(f"/api/admin/backup/jobs/{other.id}/cancel", headers=admin_headers)
        assert r.status_code == 409
        assert r.json()["detail"] == {
            "code": "adminDash.apiErr.jobNotCancellable",
            "params": {"kind": "restore_stage"},
        }
        assert not _jobs.store.cancel_requested(other.id)
    finally:
        _jobs.store.update(other.id, state="failed", error="test")

    # A finished backup is not either.
    done = _jobs.store.new("backup")
    _jobs.store.update(done.id, state="succeeded")
    r = client.post(f"/api/admin/backup/jobs/{done.id}/cancel", headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "adminDash.apiErr.backupCancelFinished",
        "params": {"state": "succeeded"},
    }
    assert not _jobs.store.cancel_requested(done.id)


def test_cancel_endpoint_is_admin_only(client, manager_headers, admin_headers, held_backup):
    started, release = held_backup
    job_id = _start_backup(client, admin_headers)
    try:
        assert started.wait(20)
        r = client.post(f"/api/admin/backup/jobs/{job_id}/cancel", headers=manager_headers)
        assert r.status_code == 403
        assert not _jobs.store.cancel_requested(job_id)
    finally:
        release.set()
    rj = _await_terminal(client, admin_headers, job_id)
    assert rj["state"] == "succeeded"
    _backup.delete_backup(rj["result"]["backup_id"])
