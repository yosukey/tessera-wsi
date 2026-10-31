"""Lightweight cross-worker job store for backup/restore.

This module deliberately avoids Celery / RQ. Backups serialise to one job at
a time across all FastAPI workers, the UI polls progress, and crash recovery
is "the job is gone" — which is fine because the user can just click again.

Uvicorn runs with multiple workers in production, so each worker has its own
process memory. The POST that starts a backup lands on one worker, but the
polling GETs are load-balanced across workers; a pure in-memory store would
make every poll on a different worker return "job not found". This module
therefore persists each job to a small JSON file under ``{backup_dir}/.jobs``
so any worker can read the latest state. Only the worker that owns the running
thread writes progress updates, so there's no write-write contention. A
cancel request from another worker is therefore a separate ``{id}.cancel``
marker the owner polls, never a write to the record (see
:meth:`JobStore.request_cancel`).

That record is also the cross-worker claim on the single job slot, so the two
kinds of write fail differently. Creating it (``new`` / ``try_claim_running``)
must succeed or raise :class:`JobStoreError` — a claim only this worker knows
about would let another worker start a second backup/restore. Progress and
state updates after that are best-effort (logged), except that a terminal
update that cannot be written drops the record so the slot is not wedged.
There is no memory-only fallback for claims: every job kind also needs
``backup_dir`` for its own staging, so a store that cannot write there has
nothing useful to run anyway.

For the restore path the same store tracks the staging step (and later the
in-process apply, in dev). Production restore-apply hands off via a file in
``/run/wsi/`` so the pending restore survives the service restart that
applies it.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional


_logger = logging.getLogger("wsi_app.jobs")

# "cancelled" is a backup that stopped on an operator's cancel request (see
# :meth:`JobStore.request_cancel`); it releases the slot like the other two.
_TERMINAL_STATES = ("succeeded", "failed", "cancelled")

# Suffix of the cancel-request marker that sits next to a job's ``.json``
# record (see :meth:`JobStore.request_cancel`).
_CANCEL_SUFFIX = ".cancel"

# ``Job.id`` is always ``uuid4().hex`` (see :meth:`JobStore.new`). The cancel
# endpoint takes the id from the URL and turns it into a file name it
# *creates*, so anything else is refused before it gets near a path.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")

# What :meth:`JobStore.get_reaping_orphan` records on a pending/running job
# whose owning worker has exited: ``error`` for the log / a script,
# ``result.code`` (a full i18n key, the shape transfer-job failures use) for
# the dashboard to translate.
WORKER_EXITED_ERROR = (
    "the server worker running this job exited before the job finished (a "
    "service restart or a crash), so its outcome was never recorded"
)
WORKER_EXITED_CODE = "adminDash.apiErr.jobWorkerExited"


class JobStoreError(RuntimeError):
    """The shared on-disk job record that backs a slot claim could not be
    written, so other workers could not see the claim. Raised by
    :meth:`JobStore.try_claim_running` / :meth:`JobStore.new`; endpoints map it
    to a 503 instead of starting a job whose single-run guarantee no longer
    holds across workers."""


def _pid_alive(pid: Optional[int]) -> bool:
    """Best-effort check that process ``pid`` is still running on this host.

    Distinguishes a job whose owning worker is still alive from one orphaned
    by a crash / restart. The job runs in a ``daemon`` thread inside the
    worker that created it; if that process is killed (systemd restart, OOM,
    deploy) the thread dies *without* running the ``except`` clause that would
    mark the job ``failed``, leaving the persisted record stuck in
    ``running``. Since ``has_running`` then reports True forever and
    ``prune_terminal`` only reaps terminal jobs, the single-job slot would be
    permanently wedged and every future backup/restore POST would 409.

    A missing / non-positive pid counts as dead: a record that never captured
    one cannot be shown to belong to a live process, and leaving the slot
    wedged forever is the worse failure. PID reuse can make a dead job's pid
    resolve to an
    unrelated live process — that errs toward "still running" (the job is kept
    a while longer, never falsely reaped into a concurrent run), and the age
    backstop in ``prune_terminal`` clears it regardless after the TTL.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, owned by another uid
    except OSError:
        return False
    return True


@dataclass
class Job:
    id: str
    # "backup" | "backup_verify" | "restore_stage" | "restore_apply" (a
    # restore or reset apply)
    # | "slide_export" | "slide_import_stage" | "slide_import"
    # | "slide_import_assets"
    kind: str
    # "pending" | "running" | "succeeded" | "failed" | "cancelled" (a backup
    # stopped by request_cancel; no other kind ends in it today)
    state: str = "pending"
    created_at: str = ""
    progress: dict = field(default_factory=dict)
    error: Optional[str] = None
    result: dict = field(default_factory=dict)
    # OS pid of the worker that created (and runs) this job, so a record left
    # in pending/running by a crashed worker can be told apart from one a live
    # worker is still driving. See _pid_alive.
    pid: Optional[int] = None
    # Guards in-place mutation of ``progress`` / ``result`` (done by
    # ``JobStore.update``) against a concurrent ``to_dict`` snapshot. Without
    # it, a poll serialising the returned dict while the owning worker mutates
    # ``progress`` in place raises "dictionary changed size during iteration"
    # → a transient 500 on the progress-poll endpoint. Excluded from equality /
    # repr and never serialised.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )

    def to_dict(self) -> dict:
        # Copy the mutable ``progress`` / ``result`` dicts under the per-job
        # lock so the caller (e.g. a poll about to JSON-serialise this) never
        # iterates a dict that ``JobStore.update`` is mutating in place.
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "created_at": self.created_at,
                "progress": dict(self.progress),
                "error": self.error,
                "result": dict(self.result),
                "pid": self.pid,
            }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        raw_pid = d.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        return cls(
            id=str(d["id"]),
            kind=str(d.get("kind", "backup")),
            state=str(d.get("state", "pending")),
            created_at=str(d.get("created_at", "")),
            progress=dict(d.get("progress") or {}),
            error=d.get("error"),
            result=dict(d.get("result") or {}),
            pid=pid,
        )


_OPPORTUNISTIC_PRUNE_INTERVAL = 3600  # 1 hour between auto-prunes per worker.

# Backstop cap on terminal ``Job`` objects kept in ``_jobs``. Age-based
# eviction (same TTL as the on-disk sweep) is the primary bound; this cap
# additionally trims the oldest terminal entries when a burst of jobs
# accumulates faster than the TTL retires them, so a long-lived worker's
# in-memory map can't grow without limit.
_MAX_INMEMORY_TERMINAL = 256


def _created_at_epoch(raw: str) -> Optional[float]:
    """Parse a ``Job.created_at`` string back to a UTC epoch, or None.

    ``new()`` writes a tz-naive ISO timestamp with a trailing ``"Z"``; treat a
    naive value as UTC (matching how it was produced). Returns None for an
    empty / unparseable value so the caller can decide (the in-memory eviction
    treats an unknown age as evictable only under the count cap, never by the
    age rule).
    """
    if not raw:
        return None
    s = raw[:-1] if raw.endswith("Z") else raw
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def restore_apply_pending() -> bool:
    """True when a restore / reset apply has scheduled a service restart but
    the swap-on-boot phase has not yet consumed its request file.

    The apply marks its job slot ``succeeded`` before the restart is kicked,
    so ``has_running()`` alone would let a backup claim the slot in that
    window — only for the restart to kill it mid-archive and leave a
    "running" record behind. ``start-wsi-app.sh`` removes the file before
    uvicorn comes back up, so this clears itself on the next boot.
    """
    try:
        from .config import settings
        return os.path.exists(settings.restore_request_path)
    except Exception:
        return False


# The 409 a job claim answers while a server performance benchmark runs (see
# _refuse_during_perf_run); an i18n key the job pages translate.
PERF_RUN_ACTIVE_CODE = "adminPerf.err.perfRunActive"


@contextmanager
def _perf_check_db():
    """A session for :func:`_refuse_during_perf_run`, its connection checked
    out up front — to be entered *before* the job-slot lock.

    The check reads the perf_runs table, i.e. takes a pooled connection on
    top of the one the calling request usually holds. Checked out under the
    flock, a pool momentarily exhausted (a crowd of tile requests) made the
    claim wait up to ``pool_timeout`` there — and with it every other
    worker's job claim and benchmark start queued on the same lock. Taken
    first, that wait (rare: the pool is sized well above the request
    threadpool, see db.py) delays only this claim, and the lock is held for
    the few queries alone. The session is bound to that one connection, so
    the reaper's commit inside the check cannot hand it back and make the
    next query check out another. None when no connection could be had:
    the check is best-effort and the claim goes ahead, as when the table
    cannot be read."""
    conn = db = None
    try:
        from sqlalchemy.orm import Session

        from .db import engine

        conn = engine.connect()
        db = Session(bind=conn, autoflush=False, future=True)
    except Exception:
        _logger.warning("no database connection to check for a running performance benchmark", exc_info=True)
        if conn is not None:
            conn.close()
        conn = db = None
    try:
        yield db
    finally:
        if db is not None:
            db.close()
        if conn is not None:
            conn.close()


def _refuse_during_perf_run(kind: str, db=None) -> None:
    """Raise the 409 for a job claim made while a server benchmark runs.

    The benchmark already refuses to start during a job (it would measure a
    busy disk); this is the other direction — a backup or transfer started
    mid-benchmark would both skew every number after it and be slowed by the
    benchmark's own load. Runs are short (perf_bench.MAX_RUN_SECONDS) and can
    be cancelled, so refusing costs the admin a short wait at most. Called
    under the slot lock, which the benchmark's start also takes around its
    own check, so the two cannot both pass; ``db`` is the session
    :func:`_perf_check_db` opened before the lock (None: none could be had —
    skipped). An HTTPException rather than a ``None`` return: the callers'
    generic "busy" text names a backup/restore, which is not what holds the
    server. Best-effort: when the run table cannot be read, the job starts
    (the benchmark then records the overlap).
    """
    if db is None:
        return
    try:
        from . import perf_bench

        active = perf_bench.server_run_active(db)
    except Exception:
        _logger.warning("could not check for a running performance benchmark before a %s job", kind, exc_info=True)
        return
    if active:
        from .admin_helpers import api_error

        raise api_error(409, PERF_RUN_ACTIVE_CODE)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # ``has_running`` walks every persisted job record on every call.
        # Without an in-flight pruner, the cost grows linearly with total
        # jobs ever created (one record per backup / restore), so a year
        # of nightly backups makes every admin POST gradually slower.
        # Track the last opportunistic prune per-worker and call
        # ``prune_terminal`` from ``has_running`` whenever the throttle
        # interval has elapsed.
        self._last_prune_at = 0.0

    # ------------------------------------------------------------------
    # Disk persistence (cross-worker visibility)
    # ------------------------------------------------------------------
    def _persist_dir(self) -> Optional[str]:
        # Lazily resolve so importing this module never depends on settings.
        try:
            from .config import settings
            d = os.path.join(settings.backup_dir, ".jobs")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            return None

    def _job_path(self, job_id: str) -> Optional[str]:
        d = self._persist_dir()
        if d is None:
            return None
        return os.path.join(d, f"{job_id}.json")

    def _write_record(self, path: str, job: Job, *, durable: bool) -> None:
        """tmp + os.replace so readers never see a torn record; raises OSError.

        ``durable`` adds an fsync before the rename: a claim must be known to
        be written, and some filesystems only report ENOSPC / EIO at writeback.
        A failed write leaves the previous record (if any) untouched and
        removes its tmp.
        """
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job.to_dict(), f)
                if durable:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _persist(self, job: Job) -> bool:
        """Best-effort persist for progress / state updates; logs on failure.

        Only for writes whose loss is cosmetic (a poll on another worker sees
        older progress). Ownership — creating the record that claims the job
        slot — goes through :meth:`_persist_claim`, which fails loudly instead.
        """
        p = self._job_path(job.id)
        if not p:
            return False
        try:
            self._write_record(p, job, durable=False)
            return True
        except OSError as exc:
            _logger.warning("failed to persist job %s: %s", job.id, exc)
            return False

    def _persist_claim(self, job: Job) -> None:
        """Durably write the record that makes ``job`` visible to every worker.

        This record IS the cross-worker claim: ``has_running()`` on another
        worker reads only disk, so a claim that exists only in this process's
        memory lets a second worker claim the slot too. Raises
        :class:`JobStoreError` on any failure — including an unavailable
        store directory — rather than degrading to a memory-only job.
        """
        p = self._job_path(job.id)
        if not p:
            raise JobStoreError(f"job store directory {self._configured_dir()!r} is unavailable")
        try:
            self._write_record(p, job, durable=True)
        except OSError as exc:
            raise JobStoreError(f"could not write job record {p!r}: {exc}") from exc

    @staticmethod
    def _configured_dir() -> str:
        try:
            from .config import settings
            return os.path.join(settings.backup_dir, ".jobs")
        except Exception:
            return "<BACKUP_DIR>/.jobs"

    def _load_from_disk(self, job_id: str) -> Optional[Job]:
        p = self._job_path(job_id)
        if not p or not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return Job.from_dict(json.load(f))
        except (OSError, ValueError, KeyError) as exc:
            _logger.warning("failed to load job %s from disk: %s", job_id, exc)
            return None

    def _iter_disk_jobs(self):
        d = self._persist_dir()
        if not d or not os.path.isdir(d):
            return
        try:
            entries = os.listdir(d)
        except OSError:
            return
        for name in entries:
            if not name.endswith(".json"):
                continue
            job = self._load_from_disk(name[:-5])
            if job is not None:
                yield job

    @contextmanager
    def _cross_worker_lock(self, *, required: bool = False):
        """Hold an exclusive advisory lock so concurrent uvicorn workers can
        do "check has_running, then create a new job" atomically.

        With ``required`` (the claim path) an unavailable store raises
        :class:`JobStoreError`: without the shared directory there is neither
        a lock nor a record another worker could see, so the claim would only
        be serialised within this process. Otherwise (``slot_guard``) it falls
        through unlocked — no claim can have succeeded without the directory,
        so there is nothing to race.
        """
        d = self._persist_dir()
        if d is None:
            if required:
                raise JobStoreError(
                    f"job store directory {self._configured_dir()!r} is unavailable"
                )
            yield
            return
        lock_path = os.path.join(d, ".lock")
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            if required:
                raise JobStoreError(f"could not open job-slot lock {lock_path!r}: {exc}") from exc
            raise
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_fd)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @contextmanager
    def slot_guard(self):
        """Hold the job-slot lock so a caller can do "check, then act" atomically.

        ``try_claim_running`` takes the same lock, so work done inside this
        block cannot interleave with another worker claiming the slot. Needed by
        the stage-cancel endpoints: ``has_running_kind()`` followed by an
        unguarded ``rmtree`` leaves a TOCTOU window in which an apply claims the
        slot and starts reading the stage the cancel is deleting.
        """
        with self._cross_worker_lock():
            yield

    def try_claim_running(self, kind: str) -> Optional[Job]:
        """Atomically reserve the single running-job slot.

        Returns a fresh ``Job`` (state ``"pending"``) if no other backup or
        restore is in flight, or ``None`` if one already is. The check and
        the create happen under a cross-worker advisory lock so two
        concurrent admin POSTs cannot both pass ``has_running()`` and start
        parallel jobs.

        Also returns ``None`` while a restore-apply is pending service
        restart. Callers check that up front for a clearer 409, but only this
        check is atomic with the claim: a restore-apply writes the request
        file and *then* releases its slot, so a POST that passed its own
        pending check while the apply still held the slot would otherwise
        claim it here and start a job the imminent restart kills.

        Raises :class:`JobStoreError` when the claim cannot be recorded where
        other workers look (``.jobs`` unavailable, ENOSPC / EPERM writing the
        record): the flock only orders the claims, it is the on-disk record
        that makes a later ``has_running()`` on another worker say "busy". A
        claim that lived only in this process's memory would let that worker
        start a second backup/restore alongside this one.

        While a server performance benchmark runs it raises that refusal's
        409 instead (:func:`_refuse_during_perf_run`).

        Blocking (flock + a scan of the job records): async endpoints call it
        via ``run_in_threadpool`` so a long ``slot_guard`` holder can't stall
        the event loop.
        """
        # The benchmark check's connection first, the lock second (see
        # _perf_check_db): never wait on the pool while holding the lock.
        with _perf_check_db() as perf_db, self._cross_worker_lock(required=True):
            if self.has_running() or restore_apply_pending():
                return None
            _refuse_during_perf_run(kind, perf_db)
            return self.new(kind)

    def has_running(self) -> bool:
        # Opportunistic prune so the per-call cost of this method stays
        # bounded as the backup history grows. Throttled to once per
        # ``_OPPORTUNISTIC_PRUNE_INTERVAL`` seconds per worker — repeated
        # prunes across workers are idempotent, but redundant.
        now = time.time()
        if now - self._last_prune_at > _OPPORTUNISTIC_PRUNE_INTERVAL:
            self._last_prune_at = now
            try:
                self.prune_terminal()
            except Exception:
                _logger.exception("opportunistic prune_terminal failed; continuing")
        # Cross-worker check: any pending/running job on disk counts — but
        # only if the worker that owns it is still alive. A record orphaned by
        # a crashed worker (daemon thread died before it could mark the job
        # failed) would otherwise wedge the single-job slot forever; reap it
        # here so the slot frees up on the next POST rather than needing a
        # manual file deletion.
        for job in self._iter_disk_jobs():
            if job.state not in ("pending", "running"):
                continue
            if _pid_alive(job.pid):
                return True
            self._reap_orphan(job.id)
        with self._lock:
            return any(
                j.state in ("pending", "running") and _pid_alive(j.pid)
                for j in self._jobs.values()
            )

    def has_running_kind(self, kind: str) -> bool:
        """True iff a pending/running job of ``kind`` is owned by a live worker.

        Like ``has_running`` but scoped to a single job kind, so a caller can
        ask specifically "is a slide-import apply in flight?" rather than "is
        any job in flight?". Used by the import-stage cancel endpoint to refuse
        deleting a stage while an apply is streaming its files into the live
        tree (deleting mid-apply corrupts the in-flight import). Reaps orphaned
        records of a crashed worker on the way through, mirroring
        ``has_running``, so a stale record can't wedge the check forever.
        """
        for job in self._iter_disk_jobs():
            if job.kind != kind or job.state not in ("pending", "running"):
                continue
            if _pid_alive(job.pid):
                return True
            self._reap_orphan(job.id)
        with self._lock:
            return any(
                j.kind == kind
                and j.state in ("pending", "running")
                and _pid_alive(j.pid)
                for j in self._jobs.values()
            )

    def active_job(self) -> Optional[Job]:
        """The pending/running job a live worker owns, or None.

        At most one exists (the slot is single), so this is "what holds the
        slot". It lets a dashboard that did not start the job — another tab,
        another admin, a reopened browser — find it, show its progress and
        cancel it; the job id otherwise lives only in the starting tab. Orphans
        of a dead worker are reaped on the way, as in ``has_running``."""
        for job in self._iter_disk_jobs():
            if job.state not in ("pending", "running"):
                continue
            if _pid_alive(job.pid):
                return job
            self._reap_orphan(job.id)
        with self._lock:
            for j in self._jobs.values():
                if j.state in ("pending", "running") and _pid_alive(j.pid):
                    return j
        return None

    def _reap_orphan(self, job_id: str) -> None:
        """Drop a pending/running record whose owning process is gone.

        Removes the in-memory entry and the on-disk file. Best-effort: a
        failure to unlink is logged and left for the age backstop in
        ``prune_terminal`` to clean up later.
        """
        with self._lock:
            self._jobs.pop(job_id, None)
        p = self._job_path(job_id)
        if not p:
            return
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.warning("failed to reap orphaned job %s: %s", job_id, exc)
        self.clear_cancel_request(job_id)

    def get_reaping_orphan(self, job_id: str) -> Optional[Job]:
        """``get``, except that a pending/running record whose owning worker
        is gone comes back as the terminal ``failed`` it really is.

        For the endpoints a client polls (the backup / restore and slide-
        transfer job GETs) and the backup cancel. Were only ``has_running``
        (the next job POST) and the startup prune to reap an orphan, a poll
        on a job whose worker was killed (OOM, deploy, systemd restart of one
        worker) would keep reading "running" for as long as nobody started
        another job: the dashboard would sit on its progress line — or, after
        a cancel, on "cancelling…" — indefinitely, and the transfer poll,
        which has no time limit, would never end. Failing the record here
        (rather than deleting it as ``_reap_orphan`` does for the slot) gives
        the poller a terminal state *with a reason*, which it already knows
        how to show; ``result.code`` is the i18n key, as the transfer jobs'
        own failures carry one. A poll that arrives after ``has_running``
        deleted the record gets the 404 every poller already treats as "job
        lost" — the two reaps agree on "not running".

        Done under the slot lock with the record re-read, so it cannot write
        over a terminal update (impossible from a dead owner anyway) or
        resurrect a record ``has_running`` just deleted. The owner is dead,
        so this worker is the only writer the record can have. ``_pid_alive``
        errs toward "alive" on PID reuse; such a record stays "running"
        until the age backstop in ``prune_terminal`` removes it (then 404),
        which is the trade-off ``_pid_alive`` already carries.
        """
        job = self.get(job_id)
        if job is None or job.state not in ("pending", "running") or _pid_alive(job.pid):
            return job
        try:
            return self._fail_orphan(job_id)
        except OSError as exc:
            # The slot lock could not be opened: answer what was read rather
            # than fail the poll.
            _logger.warning("could not check orphaned job %s: %s", job_id, exc)
            return job

    def _fail_orphan(self, job_id: str) -> Optional[Job]:
        with self._cross_worker_lock():
            current = self._load_from_disk(job_id)
            if current is None:
                with self._lock:
                    mem = self._jobs.get(job_id)
                # Gone from disk: has_running reaped it (-> 404). A memory-
                # only entry with a dead pid (none in production: new()
                # persists before registering) is failed like a record.
                if mem is None:
                    return None
                current = mem
            if current.state not in ("pending", "running") or _pid_alive(current.pid):
                return current
            _logger.warning(
                "job %s (%s) was %s but its worker (pid %s) is gone; marking it failed",
                job_id, current.kind, current.state, current.pid,
            )
            current.state = "failed"
            current.error = WORKER_EXITED_ERROR
            current.result = {"code": WORKER_EXITED_CODE}
            with self._lock:
                self._jobs.pop(job_id, None)
            if not self._persist(current):
                # Cannot record the outcome: drop the record instead, so the
                # next poll gets the 404 "lost" rather than "running" again.
                self._reap_orphan(job_id)
            self.clear_cancel_request(job_id)
            return current

    # ------------------------------------------------------------------
    # Cancellation (cross-worker)
    # ------------------------------------------------------------------
    # The cancel request usually lands on a different uvicorn worker than the
    # one whose daemon thread runs the job, so it cannot be an in-memory flag
    # or an event. It cannot be a field in the job's own ``.json`` either:
    # that record has exactly one writer, the owning worker (progress updates
    # every couple of seconds, each a whole-record os.replace from that
    # worker's in-memory copy), so a second writer's field would be
    # overwritten by the next progress tick unless every write became a
    # cross-worker read-modify-write under a lock. Instead the request is its
    # own file, ``{id}.cancel`` beside ``{id}.json``: created once by
    # whichever worker takes the request, only ever *read* (one stat) by the
    # owner, and removed when the job reaches a terminal state (or is reaped,
    # or pruned as a stray). ``_iter_disk_jobs`` reads only ``*.json``, so the
    # marker never counts as a job record (``has_running`` / the slot are
    # unaffected), and ``prune_terminal`` handles it separately.
    def _cancel_path(self, job_id: str) -> Optional[str]:
        if not _JOB_ID_RE.match(job_id or ""):
            return None
        d = self._persist_dir()
        if d is None:
            return None
        return os.path.join(d, job_id + _CANCEL_SUFFIX)

    def cancel_requested(self, job_id: str) -> bool:
        """True once :meth:`request_cancel` has recorded a request for this
        job, from any worker. One ``stat``; the job's own thread polls it
        (throttled by the caller), so it never parses JSON or takes a lock.
        An unavailable store reads as "not requested": a cancel that cannot
        be seen leaves the job running, which is the pre-cancel behaviour."""
        p = self._cancel_path(job_id)
        return bool(p) and os.path.exists(p)

    def request_cancel(self, job_id: str, requested_by: Optional[dict] = None) -> None:
        """Record a cancel request for ``job_id`` where every worker sees it.

        Validation (does the job exist, is it a kind that honours the request,
        is it still running) is the caller's: this only writes the marker. The
        content (who asked, when) is informational — the owner only checks
        that the file exists — but it is still written to a tmp and renamed,
        so a reader never sees a half-written one. Raises
        :class:`JobStoreError` when the marker cannot be written: the endpoint
        must not answer "cancelling" for a request no worker will ever see.

        A request that races the job's own terminal update can leave a marker
        behind for a job that already finished; the marker names that job
        only (ids are never reused), :meth:`prune_terminal` removes it, and
        nothing reads it for a terminal job.
        """
        p = self._cancel_path(job_id)
        if not p:
            raise JobStoreError(
                f"job store directory {self._configured_dir()!r} is unavailable"
            )
        body = {
            "requested_at": datetime.datetime.now(datetime.timezone.utc)
            .replace(tzinfo=None).isoformat() + "Z",
            "requested_by": requested_by or {},
        }
        tmp = f"{p}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, p)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise JobStoreError(f"could not write cancel request {p!r}: {exc}") from exc

    def cancel_request_info(self, job_id: str) -> dict:
        """The marker's content (``requested_by`` / ``requested_at``), or {}
        when there is none or it cannot be read. For the audit row only."""
        p = self._cancel_path(job_id)
        if not p:
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def clear_cancel_request(self, job_id: str) -> None:
        p = self._cancel_path(job_id)
        if not p:
            return
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Harmless: nothing reads a terminal job's marker, and
            # prune_terminal retries the removal.
            _logger.warning("could not remove cancel marker of job %s: %s", job_id, exc)

    def new(self, kind: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            # UTC isoformat with a trailing "Z" so the wire shape matches the
            # JSON the on-disk JobStore writers serialize. The offset is dropped
            # before the "Z" is appended (rather than left as "+00:00Z"), so the
            # string is naive-UTC-plus-Z, which is what ``_created_at_epoch``
            # parses back; do not read it as an offset-aware value.
            created_at=datetime.datetime.now(datetime.timezone.utc)
            .replace(tzinfo=None)
            .isoformat()
            + "Z",
            # The job runs in a daemon thread inside this process; record the
            # pid so a crash leaves a record whose owner can be probed for
            # liveness (see _pid_alive / has_running / prune_terminal).
            pid=os.getpid(),
        )
        # Shared record first, in-memory registration only once it exists: a
        # failed write raises (JobStoreError) with nothing to undo, instead of
        # leaving a job only this worker knows about.
        self._persist_claim(job)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            j = self._jobs.get(job_id)
        if j is not None:
            return j
        # Fall back to disk so polls hitting a worker that didn't create
        # the job still return the latest state.
        return self._load_from_disk(job_id)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                disk = self._load_from_disk(job_id)
                if disk is None:
                    return
                self._jobs[job_id] = disk
                job = disk
            # Mutate progress/result/fields under the per-job lock so a
            # concurrent ``job.to_dict()`` (a poll) can't observe a dict
            # mid-mutation. Ordering is always store lock -> job lock; to_dict
            # takes only the job lock, so there is no deadlock.
            with job._lock:
                for k, v in fields.items():
                    if k == "progress" and isinstance(v, dict):
                        job.progress.update(v)
                    else:
                        setattr(job, k, v)
            # to_dict re-acquires the job lock for its own consistent copy; the
            # store lock still held here keeps any other update serialised.
            snapshot = Job.from_dict(job.to_dict())
        # Persist outside the lock so a slow disk doesn't block other readers.
        # A lost progress / "running" write is cosmetic: the claim record
        # already holds the slot. A lost *terminal* write is not — the record
        # would stay pending/running under this live pid, and every worker's
        # has_running() would report the slot busy until this process exits.
        # Releasing the slot then means dropping the record (an unlink usually
        # still works on a full disk); polls on other workers lose the final
        # state, which beats wedging every future backup / restore.
        persisted = self._persist(snapshot)
        p = self._job_path(job_id)
        if snapshot.state in _TERMINAL_STATES:
            # A pending cancel request means nothing once the job is over;
            # drop its marker (after the terminal record, so a poll never sees
            # "running" without the "cancelling" it was just shown).
            self.clear_cancel_request(job_id)
        if not persisted and p and snapshot.state in _TERMINAL_STATES:
            try:
                os.remove(p)
                _logger.error(
                    "job %s finished (%s) but its record could not be updated; "
                    "removed it so the job slot is released",
                    job_id, snapshot.state,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                _logger.error(
                    "job %s finished (%s) but its record could neither be updated "
                    "nor removed (%s); the job slot stays held until this worker "
                    "exits", job_id, snapshot.state, exc,
                )


    def prune_terminal(self, max_age_seconds: int = 7 * 24 * 3600) -> int:
        """Delete on-disk records of terminal jobs older than the TTL.

        ``has_running()`` JSON-parses every persisted job record on each
        call. Without pruning, the cost of every admin POST grows linearly
        with total jobs ever created — fine for a few weeks of operation,
        painful after a year of nightly backups.

        Returns the count of pruned files.
        """
        d = self._persist_dir()
        if d is None:
            return 0
        cutoff = time.time() - max_age_seconds
        cleaned = 0
        try:
            entries = os.listdir(d)
        except OSError:
            return 0
        for name in entries:
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            job = self._load_from_disk(name[:-5])
            # Pending/running records are reaped as soon as their owning
            # worker is gone (a crash leaves the record stuck in 'running'
            # forever, wedging the single-job slot). The age cutoff below is a
            # backstop for the PID-reuse case where _pid_alive can't tell the
            # owner died: a non-terminal record older than the TTL is reaped
            # regardless, since no real backup/restore runs for days.
            if job is not None and job.state not in _TERMINAL_STATES:
                if not _pid_alive(job.pid):
                    try:
                        os.remove(path)
                        cleaned += 1
                    except OSError:
                        pass
                    continue
                # Owner still alive — leave it; fall through to the age check
                # only as the reuse backstop below.
            try:
                stat_result = os.stat(path)
            except OSError:
                continue
            if stat_result.st_mtime > cutoff:
                continue
            try:
                os.remove(path)
                cleaned += 1
            except OSError:
                pass
        # Cancel markers, after the records above (an orphan's record may just
        # have gone). A marker outlives its job only when the request raced
        # the job's terminal update, the job was reaped, or a removal failed:
        # drop it once its job is terminal or gone. Not counted — it is not a
        # job record.
        for name in entries:
            if not name.endswith(_CANCEL_SUFFIX):
                continue
            owner = self._load_from_disk(name[: -len(_CANCEL_SUFFIX)])
            if owner is None or owner.state in _TERMINAL_STATES:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
        self._evict_terminal_inmemory(max_age_seconds)
        return cleaned

    def _evict_terminal_inmemory(self, max_age_seconds: int) -> None:
        """Drop terminal ``Job`` objects from ``_jobs`` so it stays bounded.

        The on-disk sweep above only unlinks files; terminal ``Job`` objects
        would otherwise stay pinned in ``_jobs`` for the whole process
        lifetime, growing a long-lived worker's map with every finished
        backup/restore. Evict terminal entries older than the same TTL, then —
        as a backstop against a burst that outpaces the TTL — trim the oldest
        terminal entries beyond ``_MAX_INMEMORY_TERMINAL``. Pending/running
        entries are never touched here, so ``has_running`` / ``get`` semantics
        for still-relevant jobs are unaffected; an evicted terminal job is still
        recoverable from its on-disk record via ``get`` until the TTL retires
        that too.
        """
        now = time.time()
        with self._lock:
            terminal = [
                (jid, job)
                for jid, job in self._jobs.items()
                if job.state in _TERMINAL_STATES
            ]
            for jid, job in terminal:
                epoch = _created_at_epoch(job.created_at)
                if epoch is not None and now - epoch > max_age_seconds:
                    self._jobs.pop(jid, None)
            remaining = [
                (jid, job)
                for jid, job in self._jobs.items()
                if job.state in _TERMINAL_STATES
            ]
            if len(remaining) > _MAX_INMEMORY_TERMINAL:
                # Keep the youngest _MAX_INMEMORY_TERMINAL; an unparseable
                # created_at sorts as oldest (evicted first under cap pressure).
                remaining.sort(
                    key=lambda kv: _created_at_epoch(kv[1].created_at) or 0.0,
                    reverse=True,
                )
                for jid, _job in remaining[_MAX_INMEMORY_TERMINAL:]:
                    self._jobs.pop(jid, None)


# Module-level singleton shared within one worker process; cross-worker
# visibility goes through the on-disk records.
store = JobStore()
