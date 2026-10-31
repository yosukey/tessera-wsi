"""Retention-cap plumbing shared by the capped log tables.

``audit_logs``, ``error_logs`` and the analytics event tables are each pruned
to a row / age cap once at boot and then opportunistically, at most once an
hour per worker, from the path that writes them — so a deployment that never
restarts still enforces its caps. Each module supplies its own prune
function; the session handling and the throttle live here.

Imports only ``db`` so the log modules can use it without import cycles.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from sqlalchemy.orm import Session

from .db import SessionLocal

OPPORTUNISTIC_PRUNE_INTERVAL = 3600  # seconds between auto-prunes per worker

PruneFn = Callable[[Session], int]


class PruneThrottle:
    """Hands out at most one prune per ``interval`` seconds in this process."""

    def __init__(self, interval: float = OPPORTUNISTIC_PRUNE_INTERVAL) -> None:
        self.interval = interval
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def claim(self) -> bool:
        """True if the caller should prune now.

        The check and the stamp are a read-modify-write, so without the lock a
        burst of writes arriving right after the interval elapses would let
        several threads all pass the check and run the same DELETEs at once.
        The lock covers only the check-and-stamp, never the prune: the stamp
        is written before it is released, so every later caller inside the
        interval loses the comparison. ``blocking=False`` means a caller that
        collides on the stamp itself skips this round instead of queueing —
        a writer must never wait for someone else's prune.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if self._last is not None and now - self._last < self.interval:
                return False
            self._last = now
            return True
        finally:
            self._lock.release()

    def rearm(self, after: float) -> None:
        """Let the next :meth:`claim` succeed ``after`` seconds from now rather
        than a full interval after the last one (a prune that ran out of its
        time budget with rows left)."""
        with self._lock:
            self._last = time.monotonic() - self.interval + max(0.0, after)


def run_prune(prune: PruneFn) -> int:
    """Run ``prune`` in its own short-lived session and commit what it left
    pending (prunes that commit as they go leave nothing). Returns rows
    deleted; raises whatever the prune raises."""
    db = SessionLocal()
    try:
        n = prune(db)
        db.commit()
        return n
    finally:
        db.close()


def prune_at_boot(prune: PruneFn, log: logging.Logger, table: str) -> None:
    """Boot-time prune (called from the lifespan janitor block, which logs
    and swallows a failure)."""
    n = run_prune(prune)
    if n:
        log.info("%s boot prune removed %d rows", table, n)


def maybe_prune(
    throttle: PruneThrottle, prune: PruneFn, log: logging.Logger, table: str,
) -> None:
    """Throttled opportunistic prune. Never raises into the writer that
    triggered it; a failure is logged on ``log``."""
    if not throttle.claim():
        return
    try:
        run_prune(prune)
    except Exception:
        log.warning("opportunistic %s prune failed", table, exc_info=True)
