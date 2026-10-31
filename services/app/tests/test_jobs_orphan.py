"""Regression tests for crash-orphaned job records in the JobStore.

A backup/restore job runs in a daemon thread inside the worker that created
it. If that process is killed (systemd restart, OOM, deploy) the thread dies
without running the ``except`` clause that marks the job ``failed``, leaving
the persisted record stuck in ``running``. Without pid-liveness detection,
``has_running()`` would report True forever and ``prune_terminal`` only reaps
terminal jobs, so the single-job slot would be permanently wedged and every
subsequent backup/restore POST would 409 until an operator deleted the stray
``.jobs/<id>.json`` by hand.
"""
import json
import os
import subprocess
import sys
import threading

import pytest

from wsi_app import jobs


def _dead_pid() -> int:
    """Return a pid that is guaranteed not to be alive right now."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def store(tmp_path):
    jobs_dir = tmp_path / ".jobs"
    jobs_dir.mkdir()
    s = jobs.JobStore()
    s._persist_dir = lambda: str(jobs_dir)  # redirect persistence to tmp
    return s


def test_orphaned_running_job_is_reaped_and_unblocks(store):
    """A running record whose owning worker is gone must not wedge the slot."""
    orphan = jobs.Job(
        id="orphan", kind="backup", state="running", created_at="x", pid=_dead_pid()
    )
    store._persist(orphan)

    # The dead owner means the slot is actually free.
    assert store.has_running() is False
    # has_running reaps the stale file so it stops being parsed every call.
    assert not os.path.isfile(store._job_path("orphan"))
    # A fresh job can now claim the slot.
    assert store.try_claim_running("backup") is not None


def test_live_running_job_still_blocks(store):
    """A job owned by a live process (this one) must keep blocking the slot."""
    job = store.new("backup")
    store.update(job.id, state="running")
    assert store.has_running() is True
    assert store.try_claim_running("backup") is None


def test_prune_terminal_sweeps_orphaned_nonterminal(store):
    """The startup prune_terminal() must clear crash-orphaned running records."""
    orphan = jobs.Job(
        id="orphan", kind="restore_apply", state="running", created_at="x",
        pid=_dead_pid(),
    )
    store._persist(orphan)
    assert store.prune_terminal() >= 1
    assert not os.path.isfile(store._job_path("orphan"))


def test_record_without_pid_counts_as_dead(store):
    """A record without a pid (pid=None) cannot still be running."""
    rec = jobs.Job(id="nopid", kind="backup", state="running", created_at="x")
    assert rec.pid is None
    store._persist(rec)
    assert store.has_running() is False


def test_prune_terminal_evicts_old_inmemory_terminal(store):
    """prune_terminal must evict terminal in-memory entries past the TTL so a
    long-lived worker's _jobs map stays bounded, not only clean the disk
    records while pinning terminal Job objects for the process lifetime."""
    job = store.new("backup")
    store.update(job.id, state="succeeded")
    # Backdate the in-memory record well beyond the TTL.
    with store._lock:
        store._jobs[job.id].created_at = "2000-01-01T00:00:00Z"
    assert job.id in store._jobs
    store.prune_terminal()
    assert job.id not in store._jobs


def test_prune_terminal_keeps_recent_inmemory_terminal(store):
    """A freshly-finished terminal job is still retrievable after a prune."""
    job = store.new("backup")
    store.update(job.id, state="succeeded")
    store.prune_terminal()
    assert job.id in store._jobs
    assert store.get(job.id) is not None


def test_prune_terminal_keeps_running_inmemory_entry(store):
    """Eviction must never touch a still-running in-memory entry, even if its
    created_at is ancient (has_running semantics must be preserved)."""
    job = store.new("backup")
    store.update(job.id, state="running")
    with store._lock:
        store._jobs[job.id].created_at = "2000-01-01T00:00:00Z"
    store.prune_terminal()
    assert job.id in store._jobs


def test_prune_terminal_caps_inmemory_terminal_count(store):
    """The count backstop trims the oldest terminal entries beyond the cap."""
    cap = jobs._MAX_INMEMORY_TERMINAL
    for _ in range(cap + 12):
        j = store.new("backup")
        store.update(j.id, state="succeeded")
    terminal_before = sum(
        1 for x in store._jobs.values() if x.state in jobs._TERMINAL_STATES
    )
    assert terminal_before > cap
    store.prune_terminal()
    terminal_after = sum(
        1 for x in store._jobs.values() if x.state in jobs._TERMINAL_STATES
    )
    assert terminal_after <= cap


def test_to_dict_returns_detached_copies(store):
    """to_dict must copy progress/result so a poll serialising the result
    can't iterate a dict the worker is mutating in place."""
    job = store.new("backup")
    store.update(job.id, progress={"phase": "x", "n": 1})
    j = store.get(job.id)
    d = j.to_dict()
    assert d["progress"] == {"phase": "x", "n": 1}
    assert d["progress"] is not j.progress
    assert d["result"] is not j.result
    # A later in-place mutation doesn't reach back into the already-returned copy.
    j.progress["n"] = 2
    assert d["progress"]["n"] == 1


def test_to_dict_survives_concurrent_progress_mutation(store):
    """Serialising a job's dict while its worker rapidly rewrites progress
    must not raise 'dictionary changed size during iteration'."""
    job = store.new("backup")
    store.update(job.id, state="running")
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            # Vary the key set so the dict changes size, not just values.
            store.update(job.id, progress={f"k{i % 40}": i, "phase": "run", "i": i})

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(4000):
            try:
                json.dumps(store.get(job.id).to_dict())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                break
    finally:
        stop.set()
        t.join(timeout=5)
    assert not errors, errors[0]


def test_try_claim_refuses_while_restore_apply_pending(store, tmp_path):
    """The restore-pending check must be atomic with the claim: a restore-apply
    writes restore.request and only then releases its slot, so a POST that
    passed its own (unlocked) pending check could otherwise claim the freed
    slot and start a job the imminent restart kills."""
    from wsi_app.config import settings

    req = tmp_path / "restore.request"
    req.write_text("{}")
    saved = settings.restore_request_path
    object.__setattr__(settings, "restore_request_path", str(req))
    try:
        assert store.has_running() is False
        assert store.try_claim_running("backup") is None
        req.unlink()
        assert store.try_claim_running("backup") is not None
    finally:
        object.__setattr__(settings, "restore_request_path", saved)
