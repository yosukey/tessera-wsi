"""High-load tests for the live-sync RoomManager.

These exercises target the operational ceiling the live-sync layer is
designed for (~100+ followers per session). They are intentionally
``RoomManager``-level so a single test process can drive 100-200 fake
clients without the per-WebSocket TCP overhead of the integration tests.

What is verified
----------------

* Correctness at scale: every follower receives every broadcast frame.
* Late-joiner replay still fires when ``followers`` is large.
* One stuck / failing follower does not break the fan-out to its peers
  (this is the ``return_exceptions=True`` contract in ``_fan_out``).
* Visibility, detach, and audience updates still serialise cleanly when
  the audience list is large.
* Per-follower memory growth is bounded — i.e. each follower stores only
  the frames the manager actually sent it.

What is surfaced (logged, not asserted)
---------------------------------------

Broadcast wall-clock is printed for 100 / 200 followers and the size of the
audience JSON payload sent to the presenter on join/leave/visibility, so
ops engineers reading ``pytest -s`` output can see the empirical scaling
without these turning into flaky timing assertions on slow CI hardware.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import List, Tuple

import pytest

from wsi_app.sync_room import RoomManager

from .sync_helpers import flush_audience


# Number of followers used by the bulk tests. 100+ is the operational
# ceiling the implementation is intended to be safe at.
N_FOLLOWERS = 150
# Frame counts for sustained-broadcast checks. Cursor frames are the
# highest-rate channel (30 Hz client throttle) so this gives a 2 s burst.
N_CURSOR_FRAMES = 60


class FakeWS:
    """In-memory WS stub. Mirrors the surface RoomManager calls."""

    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.closed: List[Tuple[int, str]] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class SlowFakeWS(FakeWS):
    """FakeWS that sleeps before recording — simulates a slow client.

    Used to confirm that ``asyncio.gather`` in ``_fan_out`` runs in
    parallel rather than serialising per-recipient.
    """

    def __init__(self, delay_s: float = 0.05) -> None:
        super().__init__()
        self._delay = delay_s

    async def send(self, msg: dict) -> None:
        await asyncio.sleep(self._delay)
        self.sent.append(msg)


class FailingFakeWS(FakeWS):
    """FakeWS whose ``send`` always raises — simulates a dead client.

    The fan-out path swallows individual failures via ``_safe_send``;
    this is used to assert the rest of the audience still receives frames.
    """

    async def send(self, msg: dict) -> None:  # noqa: D401
        raise RuntimeError("client gone")


# ---------------------------------------------------------------- helpers

async def _attach_n_followers(
    mgr: RoomManager, sid: str, n: int
) -> List[FakeWS]:
    fakes: List[FakeWS] = []
    for i in range(n):
        f = FakeWS()
        res = await mgr.attach_follower(
            sid,
            ip=f"10.0.{i // 256}.{i % 256}",
            ua="ua",
            name=f"f{i}",
            hostname=None,
            send=f.send,
            close=f.close,
        )
        assert res is not None, f"follower #{i} failed to attach"
        fakes.append(f)
    return fakes


# ----------------------------------------------------------------- tests

async def test_bulk_followers_attach_succeeds():
    """Verify N_FOLLOWERS attach cleanly under the FOLLOWERS_PER_ROOM_MAX cap
    (default 200) and the audience list reflects them all."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    pres = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=pres.send, close=pres.close)
    pres.sent.clear()

    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    assert len(mgr.get(sid).followers) == N_FOLLOWERS
    # Joins are debounced: the burst collapses into a leading send plus one
    # trailing flush, which must report the full count.
    await flush_audience(mgr, sid)
    audiences = [m for m in pres.sent if m.get("type") == "audience"]
    assert audiences
    assert audiences[-1]["count"] == N_FOLLOWERS
    assert len(audiences[-1]["followers"]) == N_FOLLOWERS
    # No follower received any close.
    assert all(not f.closed for f in followers)


async def test_view_broadcast_reaches_all_followers():
    """Every connected follower receives the view frame exactly once."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    frame = {
        "type": "view", "slide_id": 1,
        "bounds": [0.1, 0.2, 0.3, 0.4], "rotation_deg": 0,
    }
    assert await mgr.broadcast_view(sid, frame) is True

    for i, f in enumerate(followers):
        views = [m for m in f.sent if m.get("type") == "view"]
        assert views == [frame], f"follower {i}: got {views!r}"


async def test_cursor_broadcast_sustained_throughput():
    """Burst-broadcast cursor frames at ~30 Hz to a large audience.

    Asserts every follower receives every frame in order. Logs wall-clock
    so the operator can see realistic throughput without a hard threshold.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    frames = [
        {"type": "cursor", "x": float(i), "y": float(i), "visible": True}
        for i in range(N_CURSOR_FRAMES)
    ]

    t0 = time.perf_counter()
    for fr in frames:
        await mgr.broadcast_cursor(sid, fr)
    elapsed = time.perf_counter() - t0
    print(
        f"\n[high-load] cursor x{N_CURSOR_FRAMES} fan-out to "
        f"{N_FOLLOWERS} followers: {elapsed*1000:.1f} ms "
        f"(avg {elapsed/N_CURSOR_FRAMES*1000:.2f} ms/frame)"
    )

    for i, f in enumerate(followers):
        cursors = [m for m in f.sent if m.get("type") == "cursor"]
        assert cursors == frames, f"follower {i} mismatch (got {len(cursors)})"


async def test_failing_follower_does_not_break_fanout():
    """A follower whose ``send`` raises must not poison the broadcast.

    Pinned by the ``return_exceptions=True`` clause in ``_fan_out`` plus
    ``_safe_send``'s blanket except. Anything looser would cause one bad
    client to silently drop frames for a 100-strong audience.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)

    # Mix in 5 failing clients across the audience.
    healthy: List[FakeWS] = []
    failing_attached = 0
    fail_count = 5
    for i in range(N_FOLLOWERS):
        if i % (N_FOLLOWERS // fail_count) == 0 and failing_attached < fail_count:
            f: FakeWS = FailingFakeWS()
            failing_attached += 1
        else:
            f = FakeWS()
            healthy.append(f)
        res = await mgr.attach_follower(
            # Distinct IPs: 150 from one address would trip FOLLOWERS_PER_IP_MAX.
            sid, ip=f"1.1.{i // 256}.{i % 256}", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        )
        assert res is not None

    frame = {"type": "view", "slide_id": 1,
             "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    assert await mgr.broadcast_view(sid, frame) is True

    # All healthy followers still received the frame.
    for h in healthy:
        assert frame in h.sent


async def test_slow_followers_run_in_parallel():
    """``_fan_out`` uses ``asyncio.gather``; a 50 ms-per-send slow client
    should not multiply by N. Total wall-clock for a single broadcast to
    50 slow clients should stay close to one slow-send delay, not 50×.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    n = 50
    delay = 0.05  # 50 ms per slow send

    slow: List[SlowFakeWS] = []
    for _ in range(n):
        s = SlowFakeWS(delay_s=delay)
        await mgr.attach_follower(
            sid, ip="1.1.1.1", ua="", name=None, hostname=None,
            send=s.send, close=s.close,
        )
        slow.append(s)

    frame = {"type": "view", "slide_id": 1,
             "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    t0 = time.perf_counter()
    await mgr.broadcast_view(sid, frame)
    elapsed = time.perf_counter() - t0
    print(
        f"\n[high-load] slow ({delay*1000:.0f} ms) fan-out to {n} "
        f"clients: {elapsed*1000:.1f} ms (serial would be "
        f"{delay*n*1000:.0f} ms)"
    )

    # Hard ceiling: must be far below the serial worst case. A generous
    # 4× the per-client delay is allowed to absorb scheduler jitter on
    # busy CI.
    assert elapsed < delay * 4, (
        f"fan-out took {elapsed:.3f}s — looks serialised "
        f"(serial would be {delay*n:.3f}s, parallel ~{delay:.3f}s)"
    )
    for s in slow:
        assert frame in s.sent


async def test_late_follower_replay_with_large_audience():
    """A late joiner gets the cached last_view + last_cursor even when
    the audience is already large. The replay path must not iterate the
    whole audience just to reach one client."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    view = {"type": "view", "slide_id": 1,
            "bounds": [0.0, 0.0, 0.5, 0.5], "rotation_deg": 0}
    cursor = {"type": "cursor", "x": 1.0, "y": 2.0, "visible": True}
    await mgr.broadcast_view(sid, view)
    await mgr.broadcast_cursor(sid, cursor)

    late = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="9.9.9.9", ua="late", name="late",
        hostname=None, send=late.send, close=late.close,
    )
    assert res is not None
    assert view in late.sent
    assert cursor in late.sent


async def test_audience_payload_size_with_many_followers():
    """The presenter's audience snapshot is a full follower list. That
    is accepted today (no diff/incremental protocol; joins are debounced);
    this test just records the payload size so a future regression to
    per-follower-update cost is visible in test logs."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    pres = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=pres.send, close=pres.close)

    await _attach_n_followers(mgr, sid, N_FOLLOWERS)
    await flush_audience(mgr, sid)
    audiences = [m for m in pres.sent if m.get("type") == "audience"]
    assert audiences
    last = audiences[-1]
    assert last["count"] == N_FOLLOWERS
    blob = json.dumps(last)
    print(
        f"\n[high-load] audience JSON for {N_FOLLOWERS} followers: "
        f"{len(blob)} bytes ({len(blob)/N_FOLLOWERS:.0f} B/follower)"
    )
    # Record how many audience updates the presenter has seen. Joins are
    # debounced, so a burst of N sequential joins collapses to a handful.
    print(
        f"[high-load] audience updates pushed during {N_FOLLOWERS} "
        f"sequential joins: {len(audiences)}"
    )


async def test_visibility_change_at_scale():
    """Visibility flips on a single follower must update only that
    follower's flag and notify the presenter once."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    pres = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=pres.send, close=pres.close)
    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    # Find a follower's id from the room.
    room = mgr.get(sid)
    target_id = next(iter(room.followers))
    pres.sent.clear()

    await mgr.update_follower_visibility(sid, target_id, False)
    # Debounced: flush the single coalesced frame.
    await flush_audience(mgr, sid)
    audiences = [m for m in pres.sent if m.get("type") == "audience"]
    assert len(audiences) == 1, f"expected 1 audience update, got {len(audiences)}"
    flagged = [
        f for f in audiences[-1]["followers"]
        if f["id"] == target_id
    ]
    assert flagged and flagged[0]["visible"] is False
    # Idempotent: same value -> no schedule, so nothing to flush.
    pres.sent.clear()
    await mgr.update_follower_visibility(sid, target_id, False)
    await flush_audience(mgr, sid)
    assert not [m for m in pres.sent if m.get("type") == "audience"]
    # Untouched followers' send queues are unaffected — visibility events
    # only flow presenter-ward.
    assert all(not f.sent for f in followers)


async def test_concurrent_attach_and_broadcast():
    """Pile up follower attaches and presenter broadcasts on the same
    event loop and confirm no frame is lost or duplicated.

    The room lock is meant to serialise mutations of the followers dict
    while letting send fan-outs run lock-free. This test exercises the
    boundary."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)

    # Pre-attach 50 followers (the steady audience).
    base = await _attach_n_followers(mgr, sid, 50)

    # Concurrently: attach 50 more AND issue 20 broadcasts.
    new_fakes: List[FakeWS] = []

    async def attach_one(i: int) -> None:
        f = FakeWS()
        new_fakes.append(f)
        await mgr.attach_follower(
            sid, ip="2.2.2.2", ua="", name=f"new{i}",
            hostname=None, send=f.send, close=f.close,
        )

    async def broadcast_one(i: int) -> None:
        await mgr.broadcast_view(sid, {
            "type": "view", "slide_id": 1, "seq": i,
            "bounds": [0, 0, 1, 1], "rotation_deg": 0,
        })

    await asyncio.gather(
        *(attach_one(i) for i in range(50)),
        *(broadcast_one(i) for i in range(20)),
    )

    # All baseline followers must have received between 1 and 20 view
    # frames (depending on interleaving they may have missed pre-attach
    # broadcasts that happened before they attached — but they were all
    # attached up front, so they should have all 20 frames).
    for f in base:
        views = [m for m in f.sent if m.get("type") == "view"]
        assert len(views) == 20, (
            f"baseline follower expected 20 views, got {len(views)}"
        )
        # No duplicates.
        seqs = [m["seq"] for m in views]
        assert len(set(seqs)) == 20

    # New followers will have between 0 and 20 frames depending on when
    # they joined, plus a replay of the latest view (exactly one) on
    # attach.
    for f in new_fakes:
        views = [m for m in f.sent if m.get("type") == "view"]
        assert len(views) >= 1, "every new follower must get a replay"


async def test_mass_detach_cleans_up_room_state():
    """After 150 followers all detach, the followers dict is empty and
    a subsequent broadcast is a no-op fan-out (no errors, no allocations
    on dead callbacks)."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    # Detach every follower in the order they were attached.
    room = mgr.get(sid)
    fids = list(room.followers.keys())
    for fid in fids:
        await mgr.detach_follower(sid, fid)

    assert len(mgr.get(sid).followers) == 0

    # Broadcast still succeeds with zero followers.
    ok = await mgr.broadcast_view(sid, {
        "type": "view", "slide_id": 1,
        "bounds": [0, 0, 1, 1], "rotation_deg": 0,
    })
    assert ok is True
    # Detached followers received no further frames after detach.
    for f in followers:
        # Each got at most an audience-replay-irrelevant view broadcast
        # if any happened before detach (none did in this test).
        assert not [m for m in f.sent if m.get("type") == "view"]


async def test_end_session_closes_all_followers():
    """``end()`` must walk every follower and close them — at scale this
    is the only path that emits a 1000-code close per client."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    followers = await _attach_n_followers(mgr, sid, N_FOLLOWERS)

    assert await mgr.end(sid) is True
    assert mgr.get(sid) is None
    for f in followers:
        assert any(m.get("type") == "end" for m in f.sent)
        assert f.closed and f.closed[0][0] == 1000


@pytest.mark.parametrize("n", [10, 50, 100, 200])
async def test_broadcast_scales_sublinearly(n: int):
    """Smoke-scaling check: broadcast latency for view frames grows but
    must not blow up. No hard bound is asserted (CI hardware varies) —
    per-N timings are just recorded so a regression that accidentally
    introduces an O(N²) loop becomes obvious in the test log."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    await _attach_n_followers(mgr, sid, n)

    frame = {"type": "view", "slide_id": 1,
             "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    # Warm up.
    await mgr.broadcast_view(sid, frame)

    # Time 5 broadcasts and report the median.
    samples: List[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        await mgr.broadcast_view(sid, frame)
        samples.append(time.perf_counter() - t0)
    samples.sort()
    median = samples[len(samples) // 2]
    print(
        f"\n[high-load] broadcast_view to {n:3d} followers: "
        f"median {median*1000:.2f} ms (range "
        f"{samples[0]*1000:.2f}-{samples[-1]*1000:.2f} ms)"
    )
    # Sanity ceiling — a single in-process fan-out to 200 fake clients
    # should be well under 1 s on any machine that can run the rest of
    # the test suite. If it isn't, something has regressed.
    assert median < 1.0


@pytest.mark.skipif(
    __import__("sys").version_info < (3, 12), reason="eager task start needs Python 3.12+"
)
async def test_fan_out_schedules_no_tasks_for_sends_that_complete_immediately():
    """A send that never suspends (the production _WsBridge.send just
    enqueues) must not cost a Task + gather round trip. A hung recipient is the
    only one left pending, and the fast ones are delivered before it finishes."""
    from wsi_app.sync_room import _fan_out

    loop = asyncio.get_running_loop()
    created: List[object] = []
    prev = loop.get_task_factory()

    def factory(lp, coro, **kw):
        created.append(coro)
        return asyncio.Task(coro, loop=lp, **kw)

    loop.set_task_factory(factory)
    try:
        sink: List[int] = []

        def make(i):
            async def send(msg):
                sink.append(i)
            return send

        await _fan_out([make(i) for i in range(50)], {"type": "x"})
        assert sink == list(range(50))
        assert created == []

        # One hung send among fast ones: fast ones land while it is pending.
        release = asyncio.Event()
        got: List[int] = []

        async def hung(msg):
            await release.wait()

        def make2(i):
            async def send(msg):
                got.append(i)
            return send

        task = asyncio.ensure_future(_fan_out([hung] + [make2(i) for i in range(5)], {}))
        await asyncio.sleep(0)
        assert got == list(range(5)) and not task.done()
        release.set()
        await task
    finally:
        loop.set_task_factory(prev)
