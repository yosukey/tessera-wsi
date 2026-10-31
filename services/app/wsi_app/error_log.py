"""Best-effort DB capture of application error/warning logs.

A ``logging`` handler installed on the ``wsi_app`` parent logger copies
every WARNING+ record (level configurable via ``WSI_ERROR_LOG_LEVEL``)
into the ``error_logs`` table so an admin can triage bugs from the
dashboard without shell access to journald. journald remains the primary,
unabridged record; the DB row is the queryable convenience copy — the
same relationship ``audit_log.py`` has with the ``wsi_app.audit`` line.

Design notes (why this deviates slightly from ``audit_log``):

* **Attach to ``wsi_app``, not the root logger.** Every app logger is
  named ``wsi_app.<area>`` and propagates, so one handler on
  ``logging.getLogger("wsi_app")`` catches them all. Attaching here (not
  root) structurally excludes ``sqlalchemy.*`` and ``uvicorn.*`` records,
  which is the first line of defence against a write→log→write loop: the
  handler's own DB traffic goes through SQLAlchemy, whose logs are named
  ``sqlalchemy.*`` and never reach a ``wsi_app`` handler.

* **Write off the request thread (QueueHandler → QueueListener).**
  ``audit()`` fires after the endpoint's ``db.commit()``, so a synchronous
  INSERT there never contends for the SQLite write lock. A
  ``logger.exception(...)`` has no such guarantee — it usually fires from
  inside an ``except:`` while the request transaction is still open and
  holding the WAL writer lock. A synchronous INSERT from the same worker
  would then stall the request thread on ``busy_timeout`` (up to 5 s) and
  likely lose the row anyway. So the ``wsi_app`` logger gets a
  non-blocking ``QueueHandler``; a background ``QueueListener`` thread
  drains the queue and performs the INSERT, serialised per worker.

* **Bounded, and throttled per call site.** The queue has a fixed depth, and
  one call site (logger + source line + message template) may only write a
  few rows per window. A record repeated on every request — a broken slide
  viewed by a class, a misbehaving client — would otherwise push every older,
  unrelated error out of the row cap and keep the SQLite writer busy. What is
  held back is counted and noted on the next row that gets through; what a
  full queue turns away is reported in a row of its own. journald still has
  every line.

This module imports only ``db`` / ``models`` / ``config`` to stay free of
import cycles.
"""
from __future__ import annotations

import copy
import logging
import os
import logging.handlers
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import retention
from .config import settings
from .db import SessionLocal
from .models import ErrorLog

# Failures inside the capture path are logged HERE, on a logger name that is
# deliberately OUTSIDE the ``wsi_app.*`` tree so the failure record cannot
# propagate back into the QueueHandler and feed the loop. (This is the one
# thing audit_log doesn't need to worry about: no handler listens on its
# ``wsi_app.audit_log`` logger.)
_internal = logging.getLogger("error_log_internal")
_logger = logging.getLogger("wsi_app.error_log")

_CAPTURE_LOGGER = "wsi_app"

# A pathological traceback (deep recursion, huge locals repr) is truncated
# rather than rejected — the message/level/logger still carry the essentials
# and journald keeps the full text. Mirrors audit_log._DETAILS_MAX_BYTES.
_TRACEBACK_MAX_BYTES = 16 * 1024
_MESSAGE_MAX_BYTES = 8 * 1024

# Records waiting for the listener. A full queue drops the record (counted, and
# reported on the next row written) rather than blocking the request thread.
_QUEUE_MAX = 10_000

# Per call site: at most _REPEAT_BURST rows per _REPEAT_WINDOW_S seconds. The
# key table is bounded the same way dzi's tile-failure table is.
_REPEAT_WINDOW_S = 60.0
_REPEAT_BURST = 5
_REPEAT_MAX_KEYS = 1024

# Attribute the queue handler stores the pre-formatted traceback under.
_TRACEBACK_ATTR = "_wsi_error_log_traceback"

_prune_throttle = retention.PruneThrottle()

# Reentrancy guard: belt-and-suspenders against any record generated while a
# DB write is in flight on the listener thread.
_in_emit = threading.local()

# Module-level handles so install/shutdown are idempotent across repeated
# `with TestClient(app):` lifecycle cycles (the test idiom in main.py).
_queue_handler: Optional[logging.handlers.QueueHandler] = None
_listener: Optional[logging.handlers.QueueListener] = None
# Remembers the ``wsi_app`` logger's own level from before install lowered it,
# so shutdown can restore it (keeps repeated TestClient lifecycles clean and
# avoids leaving the tree permanently more verbose after the feature is off).
# A separate flag is needed because the saved level can legitimately be
# NOTSET (0), which is indistinguishable from "unset" otherwise.
_capture_level_lowered = False
_prev_capture_level = logging.NOTSET
# Reused to format exc_info into traceback text; a bare Formatter is enough.
_formatter = logging.Formatter()


# SQLAlchemy renders bound values into exception text as one line,
# "[parameters: (...)]". The app engine sets hide_parameters=True, so this is
# a backstop for any other engine/connection whose error reaches the log: the
# values may be secrets (e.g. the LDAP bind password on a settings UPDATE).
# The optional prefix covers exception-group tracebacks, which indent every
# line of a member exception with "  | " (nested groups add more). It is
# spaces/tabs/'|' only — not \s — so the match can never reach back across a
# newline. The prefix is kept so the traceback's frame drawing stays intact.
_SQL_PARAMS_RE = re.compile(r"^([ \t|]*)\[parameters: .*$", re.MULTILINE)


def _scrub_sql_params(text: str) -> str:
    if not text or "[parameters: " not in text:
        return text
    return _SQL_PARAMS_RE.sub(r"\1[SQL parameters hidden]", text)


def _truncate(text: str, max_bytes: int) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore") + "\n… [truncated]"


_SUPPRESSED_ATTR = "_wsi_error_log_suppressed"

# Records dropped because the queue was full, reported in a row of their own
# by the listener once it gets to them.
_dropped_lock = threading.Lock()
_dropped = 0


def _note_dropped(n: int = 1) -> None:
    global _dropped
    with _dropped_lock:
        _dropped += n


def _take_dropped() -> int:
    global _dropped
    with _dropped_lock:
        n, _dropped = _dropped, 0
    return n


def _template_key(record: logging.LogRecord) -> str:
    try:
        return str(record.msg)[:200]
    except Exception:
        return type(record.msg).__name__


# This package's directory: frames under it are the app's own code.
_APP_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _exception_key(record: logging.LogRecord) -> Optional[tuple]:
    """The exception's type and the app line it came out of, so distinct
    failures logged from one shared line stay apart: every unhandled request
    exception is logged by the same ``_log_unhandled_exception`` call, and two
    different bugs must not share its burst.

    The app line is the innermost frame inside this package, not the
    innermost frame overall: an exception raised inside a library (every
    SQLAlchemy ``IntegrityError`` / ``OperationalError`` comes out of the same
    line of its engine) would otherwise make unrelated bugs one site. The
    innermost frame is the fallback when no app frame is on the stack.
    """
    exc_info = record.exc_info
    if not exc_info or exc_info[1] is None:
        return None
    exc_type, _exc, tb = exc_info
    innermost = app_frame = None
    while tb is not None:
        innermost = tb
        if tb.tb_frame.f_code.co_filename.startswith(_APP_DIR):
            app_frame = tb
        tb = tb.tb_next
    site = app_frame or innermost
    where = (site.tb_frame.f_code.co_filename, site.tb_lineno) if site is not None else None
    return (getattr(exc_type, "__qualname__", str(exc_type)), where)


class _RepeatThrottle:
    """Admit at most ``burst`` records per ``window`` seconds per call site.

    A call site is the logger, level, source line and message *template*
    (not the rendered message), so "failed to open slide %s" for a hundred
    tile requests is one site whatever the arguments — plus, for a record
    carrying an exception, the exception's type and raising line (see
    :func:`_exception_key`). Counted per worker process.
    """

    def __init__(self, window: Optional[float] = None, burst: Optional[int] = None,
                 max_keys: Optional[int] = None) -> None:
        self._window = _REPEAT_WINDOW_S if window is None else window
        self._burst = _REPEAT_BURST if burst is None else burst
        self._max_keys = _REPEAT_MAX_KEYS if max_keys is None else max_keys
        self._lock = threading.Lock()
        # key -> [window start (monotonic), admitted in window, held back]
        self._slots: dict[tuple, list] = {}

    def admit(self, record: logging.LogRecord) -> Optional[int]:
        """None to drop the record; otherwise how many records of this site
        were held back since the last one admitted (normally 0)."""
        key = (record.name, record.levelno, record.pathname, record.lineno,
               _template_key(record), _exception_key(record))
        now = time.monotonic()
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None and now - slot[0] < self._window:
                if slot[1] < self._burst:
                    slot[1] += 1
                    held, slot[2] = slot[2], 0
                    return held
                slot[2] += 1
                return None
            held = slot[2] if slot is not None else 0
            # Re-inserted so the dict stays in least-recently-opened order.
            self._slots.pop(key, None)
            if len(self._slots) >= self._max_keys:
                for k in [k for k, s in self._slots.items() if now - s[0] >= self._window]:
                    del self._slots[k]
                while len(self._slots) >= self._max_keys:
                    del self._slots[next(iter(self._slots))]
            self._slots[key] = [now, 1, 0]
            return held


class _InProcessQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler feeding the in-process listener, throttled and bounded.

    The stdlib ``prepare()`` folds the traceback into the message so the
    record can be pickled onto a multiprocessing queue. Here the traceback is
    kept apart, for its own column. The record is still rendered *now*, on the
    logging thread, and a copy is queued: rendering on the listener later
    would read ``args`` after the caller may have changed them (so the row
    could disagree with the journald line), could run an ORM object's
    ``__str__`` against a Session from another thread, and would keep the
    traceback's frames (and their locals) alive for as long as the record
    waits in the queue.
    """

    def __init__(self, log_queue: "queue.Queue[logging.LogRecord]",
                 throttle: Optional[_RepeatThrottle] = None) -> None:
        super().__init__(log_queue)
        self._throttle = throttle or _RepeatThrottle()

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # A copy: the same record goes on to the stderr/journald handlers.
        prepared = copy.copy(record)
        message = _record_message(record)
        prepared.msg = message
        prepared.args = None
        prepared.message = message
        setattr(prepared, _TRACEBACK_ATTR, _record_traceback(record))
        prepared.exc_info = None
        prepared.exc_text = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            _note_dropped()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            held = self._throttle.admit(record)
            if held is None:
                return
            prepared = self.prepare(record)
            if held:
                setattr(prepared, _SUPPRESSED_ATTR, held)
            self.enqueue(prepared)
        except Exception:
            self.handleError(record)


class _QueueListener(logging.handlers.QueueListener):
    def enqueue_sentinel(self) -> None:
        # Blocking: on a full queue the stdlib put_nowait would make stop()
        # raise. The listener thread is draining, so a slot frees up.
        self.queue.put(self._sentinel)


class DBLogHandler(logging.Handler):
    """Terminal handler run inside the QueueListener thread: INSERT one row
    per record. Never raises (stdlib ``Handler.emit`` contract) and never
    logs into the captured namespace."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_in_emit, "active", False):
            return
        _in_emit.active = True
        try:
            persist_log_record(record)
            dropped = _take_dropped()
            if dropped and not _persist_dropped_note(dropped):
                # Not stored: keep the count for the next attempt.
                _note_dropped(dropped)
        except Exception:
            # Route through handleError → stderr; do NOT logger.* here.
            self.handleError(record)
        finally:
            _in_emit.active = False


def _record_message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:
        # Bad %-args: fall back to the raw template so something is still
        # captured.
        return str(record.msg)


def _record_traceback(record: logging.LogRecord) -> str:
    pre = getattr(record, _TRACEBACK_ATTR, None)
    if pre is not None:
        return pre
    if not record.exc_info:
        return ""
    try:
        return _formatter.formatException(record.exc_info)
    except Exception:
        return ""


def _record_time(record: logging.LogRecord) -> Optional[datetime]:
    """When the record was logged, as the naive-UTC second the column stores.

    Not the INSERT time: the row is written later, on the listener thread —
    after a busy_timeout wait, or (for the boot backlog) after the schema
    phase — and the listing must line up with journald and the audit history.
    """
    try:
        return (
            datetime.fromtimestamp(record.created, timezone.utc)
            .replace(tzinfo=None, microsecond=0)
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _with_notes(message: str, record: logging.LogRecord) -> str:
    held = getattr(record, _SUPPRESSED_ATTR, 0)
    if held:
        message += (
            f"\n[{held} more record(s) from this call site were not stored since "
            "the previous one; journald has every line]"
        )
    return message


def _persist_dropped_note(n: int) -> bool:
    """A row of its own saying ``n`` records were turned away by the full
    queue. Its own row because the records written around it were queued
    before the drops, so a note on one of them would date the gap wrongly.
    True when stored."""
    try:
        db = SessionLocal()
        try:
            db.add(ErrorLog(
                level="WARNING",
                logger=_logger.name,
                message=(
                    f"{n} log record(s) were not stored: the error-log queue was "
                    "full. journald has every line."
                ),
                traceback="",
            ))
            db.commit()
        finally:
            db.close()
        return True
    except Exception:
        _internal.warning("error-log dropped-count note failed", exc_info=True)
        return False


def persist_log_record(record: logging.LogRecord) -> None:
    """INSERT one error_logs row in a short-lived session. Never raises.

    Runs on the QueueListener thread, so stalling on the SQLite write lock
    here does not block any request thread; the worst case is a
    ``busy_timeout`` wait against another worker.
    """
    try:
        stamped = {}
        occurred_at = _record_time(record)
        if occurred_at is not None:
            stamped["occurred_at"] = occurred_at
        row = ErrorLog(
            **stamped,
            level=record.levelname[:16],
            logger=record.name[:255],
            # Notes after truncation, so a long message cannot cut them off.
            message=_with_notes(
                _truncate(_scrub_sql_params(_record_message(record)), _MESSAGE_MAX_BYTES),
                record,
            ),
            module=record.module or None,
            func=record.funcName or None,
            line=record.lineno,
            traceback=_truncate(
                _scrub_sql_params(_record_traceback(record)), _TRACEBACK_MAX_BYTES
            ),
            request_path=_str_or_none(getattr(record, "request_path", None), 512),
            request_method=_str_or_none(getattr(record, "request_method", None), 8),
        )
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:
        _internal.warning(
            "error-log persist failed; the journald line is the only record "
            "— logger=%s level=%s", record.name, record.levelname, exc_info=True,
        )
    _maybe_prune()


def _str_or_none(value, max_len: int) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:max_len]


def _resolve_level() -> int:
    name = (settings.error_log_level or "WARNING").upper()
    level = logging.getLevelName(name)
    # getLevelName returns the string "Level <name>" for unknown names.
    return level if isinstance(level, int) else logging.WARNING


def install_error_log_capture(
    backlog: Iterable[logging.LogRecord] = (),
) -> None:
    """Attach the QueueHandler to ``wsi_app`` and start the listener thread.

    ``backlog`` is what the caller collected before ``error_logs`` could take
    it (the lifespan's schema phase); records at or above the capture level
    are queued ahead of anything logged from here on.

    Idempotent: a second call while already installed is a no-op (the
    backlog included — the installed handler saw those records itself), so
    repeated lifespan cycles in tests don't stack duplicate handlers.
    """
    global _queue_handler, _listener, _capture_level_lowered, _prev_capture_level
    if not settings.error_log_enabled:
        return
    if _queue_handler is not None:
        return
    level = _resolve_level()
    log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=_QUEUE_MAX)
    qh = _InProcessQueueHandler(log_queue)
    qh.setLevel(level)
    db_handler = DBLogHandler()
    listener = _QueueListener(
        log_queue, db_handler, respect_handler_level=False
    )
    listener.start()
    for record in backlog:
        if record.levelno >= level:
            qh.handle(record)
    capture_logger = logging.getLogger(_CAPTURE_LOGGER)
    # Setting the handler level is not enough: Logger.isEnabledFor() gates a
    # record on the logger's *effective* level (walking up to root, which
    # defaults to WARNING) BEFORE any handler sees it. So a configured
    # WSI_ERROR_LOG_LEVEL=INFO/DEBUG would be dropped at the logger before
    # reaching the queue handler. Lower the ``wsi_app`` logger itself to the
    # capture level — but only ever *lower* it, never raise: if something else
    # already set a more verbose level, leave that alone so its records keep
    # flowing to journald. Remember the prior level so shutdown can restore it.
    if capture_logger.level == logging.NOTSET or capture_logger.level > level:
        _prev_capture_level = capture_logger.level
        _capture_level_lowered = True
        capture_logger.setLevel(level)
    capture_logger.addHandler(qh)
    _queue_handler = qh
    _listener = listener


def shutdown_error_log_capture() -> None:
    """Detach the handler and stop the listener thread. Idempotent."""
    global _queue_handler, _listener, _capture_level_lowered, _prev_capture_level
    if _queue_handler is not None:
        logging.getLogger(_CAPTURE_LOGGER).removeHandler(_queue_handler)
        _queue_handler = None
    if _capture_level_lowered:
        logging.getLogger(_CAPTURE_LOGGER).setLevel(_prev_capture_level)
        _capture_level_lowered = False
        _prev_capture_level = logging.NOTSET
    if _listener is not None:
        try:
            _listener.stop()
        except Exception:
            _internal.warning("error-log listener stop failed", exc_info=True)
        _listener = None


def prune_error_logs(
    db: Session,
    max_rows: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> int:
    """Apply the row/age caps to error_logs. Returns rows deleted.

    Caller commits. Parameters default to the configured caps; tests pass
    explicit values. A cap of 0 disables that dimension. Mirrors
    ``audit_log.prune_audit_logs``.
    """
    if max_rows is None:
        max_rows = settings.error_log_max_rows
    if max_age_days is None:
        max_age_days = settings.error_log_max_age_days
    deleted = 0
    if max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        # Never the newest row: the id is a plain INTEGER PRIMARY KEY, which
        # SQLite reissues from max(id)+1 — an emptied table would start over
        # at 1, and the listing's snapshots (max_id), its "newer" count and
        # the NEW badge all rely on ids only ever growing.
        result = db.execute(delete(ErrorLog).where(
            ErrorLog.occurred_at < cutoff,
            ErrorLog.id < select(func.max(ErrorLog.id)).scalar_subquery(),
        ))
        deleted += result.rowcount or 0
    if max_rows > 0:
        threshold = db.execute(
            select(ErrorLog.id).order_by(ErrorLog.id.desc()).offset(max_rows).limit(1)
        ).scalar_one_or_none()
        if threshold is not None:
            result = db.execute(delete(ErrorLog).where(ErrorLog.id <= threshold))
            deleted += result.rowcount or 0
    return deleted


def prune_at_boot() -> None:
    """Boot-time prune; best-effort (called from the lifespan janitor block).

    Runs on the lifespan thread, not the listener, so the summary line can go
    to the ``wsi_app`` tree (journald) like the audit / analytics ones.
    """
    retention.prune_at_boot(prune_error_logs, _logger, "error_logs")


def _maybe_prune() -> None:
    """Opportunistic prune, throttled per worker (see ``retention``).

    Runs on the listener thread, so a failure is reported on ``_internal``,
    outside the captured tree."""
    retention.maybe_prune(_prune_throttle, prune_error_logs, _internal, "error_logs")
