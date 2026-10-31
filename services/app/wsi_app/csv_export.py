"""Shared CSV rendering helpers for admin exports.

The analytics export (`/api/admin/analytics/export.csv`), the
audit-history export (`/api/admin/history/export.csv`) and the error-log
export (`/api/admin/logs/export.csv`) all need the same two things: a
UTF-8 BOM so Excel on Windows doesn't mis-detect the file as the system's
legacy code page (e.g. Shift-JIS), and a defence against spreadsheet formula injection on
externally controlled cell values. Keeping the logic here means there is a
single place to audit that behaviour.
"""
from __future__ import annotations

import csv
import io
import threading
from typing import Iterable, Iterator, Optional

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from .admin_helpers import api_error

_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

# Concurrent audit-history / error-log exports per worker. Each one in flight
# costs a DB read per batch and a thread hop per chunk, so a few parallel
# clicks (or a script) are turned away rather than allowed to pile up. Behind
# nginx (proxy_buffering on for /api/admin/) the app-side stream ends once
# nginx has buffered the body, usually long before a slow browser finishes;
# a client talking to the app directly holds its slot for the whole download. 409, not 429: the caller is not sending too
# fast, a resource is busy — and the UI shows its own message for a coded 409,
# where every 429 is reported as generic rate limiting.
EXPORT_MAX_CONCURRENT = 2
_export_slots = threading.BoundedSemaphore(EXPORT_MAX_CONCURRENT)


def claim_export_slot() -> None:
    """Take an export slot or raise 409 ``adminExport.busy``.

    Pair with :func:`slot_csv_response`, which gives the slot back when the
    download ends.
    """
    if not _export_slots.acquire(blocking=False):
        raise api_error(409, "adminExport.busy")


class _SlotReleasingRows:
    """Iterator over ``rows`` that gives the export slot back exactly once:
    when they run out or fail, when :meth:`close` is called (by
    :class:`_ClosingStreamingResponse` once the response is over, however it
    ended), or — the last resort — when it is collected.

    A class rather than a generator with ``finally``: a client that goes away
    before the body starts leaves the iterator never started, and a generator
    that never started does not run its ``finally`` when it is closed or
    collected. And not left to collection alone: a cancelled stream's frames
    sit in reference cycles, so the slot would come back only whenever the
    cyclic GC next ran, and the batches would keep being read until then.
    """

    def __init__(self, rows: Iterable[list]) -> None:
        self._rows = iter(rows)
        self._lock = threading.Lock()
        self._released = False

    def __iter__(self) -> "_SlotReleasingRows":
        return self

    def __next__(self) -> list:
        try:
            return next(self._rows)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        _export_slots.release()
        close = getattr(self._rows, "close", None)
        if close is not None:
            try:
                close()
            except ValueError:
                # "generator already executing": a batch read is still running
                # on a worker thread; it ends on its own when that returns.
                pass

    def __del__(self) -> None:
        self.close()


def release_on_exhaustion(rows: Iterable[list]) -> "_SlotReleasingRows":
    """``rows``, releasing the claimed export slot once the download is over."""
    return _SlotReleasingRows(rows)


class _ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that calls ``on_close`` when it is over: sent in
    full, cut off by a disconnect, failed, or cancelled.

    Starlette's own ``background`` task is not enough: it runs after a full
    send and after a disconnect it notices itself (ASGI spec < 2.4), but not
    when the send raises (spec >= 2.4 servers) or the task is cancelled.
    """

    def __init__(self, *args, on_close, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def slot_csv_response(
    filename: str,
    header: list[str],
    rows: Iterable[list],
    auth_db: Optional[Session] = None,
) -> StreamingResponse:
    """:func:`csv_response` for an export that holds a slot
    (:func:`claim_export_slot`): the slot is released, and the row source
    closed, as soon as the response is over — sent in full or abandoned.

    ``auth_db`` is the request's session (the one the auth dependency read the
    user through); it is closed here, before the body streams, so its pooled
    connection is not held for the length of the download — a dependency's
    teardown would only run after the last byte.
    """
    guarded = release_on_exhaustion(rows)
    if auth_db is not None:
        auth_db.close()
    return _ClosingStreamingResponse(
        _iter_csv(header, guarded),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers(filename),
        on_close=guarded.close,
    )


def _csv_headers(filename: str) -> dict:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }

# One export request must never stream more than this many rows, whatever the
# retention config says. It is a hard stop so a *disabled* retention row cap
# (WSI_*_LOG_MAX_ROWS=0 ⇒ unbounded table) cannot load an unbounded result set
# into the worker and OOM it. Chosen well above the default retention caps
# (audit 100k / error 50k) so normal operation is never truncated; an operator
# who keeps more than this and needs the full set should query the DB directly.
EXPORT_ABSOLUTE_MAX_ROWS = 1_000_000


def resolve_export_limit(requested: Optional[int], retention_max_rows: int) -> int:
    """Effective row ceiling for a CSV export.

    Follows the table's retention row cap (so the download never claims more
    than the table is allowed to keep) but is always bounded by
    ``EXPORT_ABSOLUTE_MAX_ROWS`` so a disabled retention cap (0 = unbounded
    table) can't stream an unbounded result set into memory. An explicit
    ``?limit`` only ever narrows the result further; it can never lift either
    ceiling. Shared by the audit-history and error-log exports so the two can
    never drift on how the cap is derived.
    """
    if retention_max_rows > 0:
        cap = min(retention_max_rows, EXPORT_ABSOLUTE_MAX_ROWS)
    else:
        cap = EXPORT_ABSOLUTE_MAX_ROWS
    if requested is not None and requested < cap:
        return requested
    return cap


def csv_safe(v):
    """Defang spreadsheet formula injection on a single cell value.

    Fields such as `referrer_host`, `user_agent`, audit `details`, and (in
    some auth flows) `user_email` are externally controlled. Excel /
    LibreOffice / Numbers will evaluate a cell beginning with `=`, `+`,
    `-`, `@`, TAB, or CR as a formula, which is enough for
    `=HYPERLINK("…attacker…", "click")` style data exfiltration when an
    admin opens the exported CSV. Prefix any such string with a leading
    single quote so the cell is treated as text. Non-string values pass
    through untouched.
    """
    if isinstance(v, str) and v.startswith(_CSV_FORMULA_LEAD):
        return "'" + v
    return v


def _iter_csv(header: list[str], rows: Iterable[list]):
    """Yield the CSV one row at a time (BOM+header first, then a chunk per row).

    Streaming instead of building the whole file in one ``StringIO`` keeps peak
    memory bounded to a single row even for a large export, and — crucially —
    when ``rows`` is a lazy iterator over the DB cursor, the ORM rows are never
    all held at once either. The concatenated bytes are identical to rendering
    the whole file as one string (same BOM, CRLF terminator, and quoting).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    buf.write("﻿")
    writer.writerow(header)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    for row in rows:
        writer.writerow(["" if v is None else csv_safe(v) for v in row])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def csv_response(filename: str, header: list[str], rows: Iterable[list]) -> StreamingResponse:
    """Stream `rows` as RFC 4180 CSV with a UTF-8 BOM.

    The BOM keeps Excel on Windows from mis-detecting a UTF-8 file as the
    system's legacy code page (e.g. Shift-JIS) — without it, multibyte titles render as mojibake. Browsers / pandas / Numbers
    all strip the BOM transparently.

    ``rows`` may be any iterable (a materialised list or a lazy generator over
    a DB cursor); it is consumed once, lazily, so passing a generator keeps the
    whole result set out of memory.
    """
    return StreamingResponse(
        _iter_csv(header, rows),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers(filename),
    )
