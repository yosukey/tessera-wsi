"""Unit tests for ``_WsBridge`` — the non-blocking outbound queue that sits
between ``RoomManager``'s fan-out and a real WebSocket.

The manager-level high-load tests drive callbacks directly and never exercise
the bridge, so the property that actually protects a live room from one frozen
follower — *the broadcast fan-out never blocks on a single slow / stuck
socket* — is pinned here instead.
"""
from __future__ import annotations

import asyncio

from wsi_app.routers import sync as sync_mod
from wsi_app.routers.sync import _WsBridge


class _RecordingWS:
    """Fake Starlette WebSocket: records sends and closes, never blocks."""

    def __init__(self) -> None:
        self.sent: list = []
        self.closed: list = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _BlockingWS(_RecordingWS):
    """Fake WebSocket whose send blocks until released — models a peer whose
    TCP receive window is full (closed lid / dead Wi-Fi)."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.send_started = asyncio.Event()

    async def send_json(self, msg: dict) -> None:
        self.send_started.set()
        await self.release.wait()
        self.sent.append(msg)


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    """Spin the event loop until ``predicate()`` is true or the deadline hits."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)


async def test_writer_flushes_in_order():
    ws = _RecordingWS()
    bridge = _WsBridge(ws)
    bridge.start()
    for i in range(5):
        await bridge.send({"type": "view", "seq": i})
    # aclose drains the queue, then closes — a deterministic flush barrier that
    # doesn't depend on how many frames each event-loop tick happens to drain.
    await bridge.aclose()
    assert [m["seq"] for m in ws.sent] == [0, 1, 2, 3, 4]


async def test_send_is_nonblocking_when_socket_is_stuck():
    """``send`` must return immediately even when the socket's send is wedged.

    This is the whole point of the queue: the fan-out (called from the
    presenter's recv loop) cannot be held hostage by one frozen follower.
    """
    ws = _BlockingWS()
    bridge = _WsBridge(ws)
    bridge.start()
    try:
        # First send enters the writer and blocks inside ws.send_json.
        await bridge.send({"type": "cursor", "seq": 0})
        await asyncio.wait_for(ws.send_started.wait(), timeout=1.0)

        # Subsequent sends must not block on the stuck writer — each returns now.
        for i in range(1, 50):
            await asyncio.wait_for(bridge.send({"type": "cursor", "seq": i}), timeout=0.5)

        # The socket is still stuck on frame 0; nothing has been flushed past it.
        assert ws.sent == []
    finally:
        ws.release.set()
        await bridge.aclose()


async def test_overflow_drops_and_closes_stuck_client(monkeypatch):
    """A follower that backs up past MAX_OUTBOUND_QUEUE is dropped + closed."""
    monkeypatch.setattr(sync_mod, "MAX_OUTBOUND_QUEUE", 8)
    ws = _BlockingWS()
    bridge = _WsBridge(ws)
    bridge.start()
    try:
        await bridge.send({"type": "cursor", "seq": 0})
        await asyncio.wait_for(ws.send_started.wait(), timeout=1.0)

        # Pile frames on while the writer is stuck on frame 0.
        for i in range(1, 20):
            await bridge.send({"type": "cursor", "seq": i})

        # Releasing the stuck send lets the writer observe the overflow-close.
        ws.release.set()
        await _wait_for(lambda: bool(ws.closed))
        assert ws.closed, "stuck client should have been closed on overflow"
        assert ws.closed[0][0] == 1013
    finally:
        ws.release.set()
        await bridge.aclose()


async def test_send_timeout_closes_frozen_client(monkeypatch):
    """A single send that exceeds WS_SEND_TIMEOUT_SECONDS tears the conn down."""
    monkeypatch.setattr(sync_mod, "WS_SEND_TIMEOUT_SECONDS", 0.05)
    ws = _BlockingWS()  # never released → send_json blocks forever
    bridge = _WsBridge(ws)
    bridge.start()
    try:
        await bridge.send({"type": "view", "seq": 0})
        # Writer should hit the per-send timeout and close the socket.
        await _wait_for(lambda: bool(ws.closed))
        assert ws.closed, "frozen send should have been timed out and closed"
    finally:
        ws.release.set()
        await bridge.aclose()


async def test_writer_death_marks_closing_and_still_closes_socket(monkeypatch):
    """When the writer dies on a send timeout it must (a) mark the bridge
    closing so ``send`` stops queueing frames into a task that will never run
    again, and (b) leave a working close path: a later ``close``/``aclose``
    (the keepalive- and teardown-driven paths) must actually close the socket
    instead of silently no-opping because they delegated to the dead writer.
    """
    monkeypatch.setattr(sync_mod, "WS_SEND_TIMEOUT_SECONDS", 0.05)
    ws = _BlockingWS()  # send_json blocks until released → the writer times out
    bridge = _WsBridge(ws)
    bridge.start()
    try:
        await bridge.send({"type": "view", "seq": 0})
        # Writer hits the per-send timeout, marks the bridge closing, and exits.
        await _wait_for(lambda: bridge._writer_done)
        assert bridge._closing is True
        # send() now refuses to queue into the dead writer.
        await bridge.send({"type": "view", "seq": 1})
        assert ws.sent == []  # nothing was ever flushed
        # A keepalive/teardown close routed through the bridge after the writer
        # died still closes the socket and does not hang.
        await bridge.close(1000, "ping timeout")
        await bridge.aclose()
        assert ws.closed, "socket must be closed even though the writer already died"
    finally:
        ws.release.set()


async def test_close_flushes_pending_then_closes():
    """``close`` is ordered: frames already queued (e.g. the ``end`` frame)
    flush before the close frame goes out."""
    ws = _RecordingWS()
    bridge = _WsBridge(ws)
    bridge.start()
    await bridge.send({"type": "end", "reason": "ended"})
    await bridge.close(1000, "session ended")
    await bridge.aclose()
    assert ws.sent == [{"type": "end", "reason": "ended"}]
    assert ws.closed and ws.closed[0][0] == 1000
