"""Unit tests for the in-memory sync RoomManager."""
from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest

from wsi_app import sync_room as _sync_room_module
from wsi_app.sync_room import (
    DISCOVERY_MAX_SUBSCRIBERS,
    PRESENTER_GRACE_SECONDS,
    RoomFullError,
    RoomManager,
    sanitize_name,
)

from .sync_helpers import flush_audience


@pytest.fixture(autouse=True)
def _no_discovery_event_gate(monkeypatch):
    """These tests drive RoomManager in isolation with made-up slide ids; the
    router installs a published-slide gate on discovery events (it queries the
    DB), which would drop their events. The gate has its own test in
    test_sync_ws.py."""
    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", None)


class FakeWS:
    """Collects every JSON sent to it. ``close`` records the close code."""

    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.closed: List[Tuple[int, str]] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))

    def types_sent(self) -> List[str]:
        return [m.get("type") for m in self.sent]


# ----- sanitize_name -----------------------------------------------------

def test_sanitize_name_strips_control_chars_and_trims():
    assert sanitize_name("  Yamada\x00 Taro \n") == "Yamada Taro"


def test_sanitize_name_caps_length():
    assert sanitize_name("a" * 500) == "a" * 64


def test_sanitize_name_empty_returns_none():
    assert sanitize_name("") is None
    assert sanitize_name("   ") is None
    assert sanitize_name(None) is None


def test_sanitize_name_coerces_non_string():
    assert sanitize_name(42) == "42"


# ----- create / list / end ------------------------------------------------

async def test_create_returns_url_safe_session_id():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    assert isinstance(sid, str)
    assert len(sid) >= 20  # token_urlsafe(16) -> 22 chars
    assert mgr.get(sid) is not None
    assert mgr.get(sid).slide_id == 1


async def test_list_owned_lists_the_owners_rooms_with_presenter_state():
    """Resume must be offered while the server still holds the presenter's
    previous (possibly dead) socket, so an attached room is listed too —
    flagged — rather than hidden until the keepalive reaps that socket."""
    mgr = RoomManager()
    s1 = await mgr.create(slide_id=1, owner_user_id=1)
    s2 = await mgr.create(slide_id=2, owner_user_id=1)
    await mgr.create(slide_id=3, owner_user_id=2)
    p = FakeWS()
    await mgr.attach_presenter(s1, user_id=1, send=p.send, close=p.close)
    owned = {s.session_id: s for s in mgr.list_owned(1)}
    assert set(owned) == {s1, s2}
    assert owned[s1].presenter_connected is True
    assert owned[s2].presenter_connected is False
    assert owned[s1].slide_id == 1
    await mgr.detach_presenter(s1)
    assert {s.session_id: s.presenter_connected for s in mgr.list_owned(1)} == {
        s1: False, s2: False,
    }


async def test_end_closes_followers_and_removes_room():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="x", name="Alice",
        hostname=None, send=f.send, close=f.close,
    )
    assert await mgr.end(sid) is True
    assert mgr.get(sid) is None
    assert "end" in f.types_sent()
    assert f.closed and f.closed[0][0] == 1000


async def test_end_pushes_end_frame_to_presenter():
    """Presenter must receive an explicit ``end`` frame on teardown so the
    client can stop reconnecting. A bare 1000 close is indistinguishable
    from a transient network drop and the auto-reconnect would then loop
    against a now-empty room dict, producing 4404 churn forever.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    assert await mgr.end(sid, reason="ended_by_owner") is True
    assert "end" in p.types_sent()
    end_frame = next(m for m in p.sent if m.get("type") == "end")
    assert end_frame.get("reason") == "ended_by_owner"
    assert p.closed and p.closed[0][0] == 1000


async def test_end_unknown_returns_false():
    mgr = RoomManager()
    assert await mgr.end("nope") is False


# ----- presenter attach/detach -------------------------------------------

async def test_attach_presenter_requires_owner_match():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    bad = await mgr.attach_presenter(sid, user_id=99, send=p.send, close=p.close)
    assert bad is None
    good = await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    assert good is not None


async def test_attach_presenter_evicts_stale_same_owner():
    """Same-owner reattach replaces the previous presenter.

    The realistic trigger is a transient network drop on the client: the
    server's old WebSocket is still registered because it has not yet
    seen the half-close, and a fresh tab from the same admin would
    otherwise hit a 4409 conflict until ping-timeout reaped the dead
    socket. Eviction collapses that recovery window to a single round
    trip and closes the old socket with a private 4000-range code.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p1, p2 = FakeWS(), FakeWS()
    first = await mgr.attach_presenter(sid, user_id=10, send=p1.send, close=p1.close)
    assert first is not None
    second = await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    assert second is not None
    # The old presenter conn is closed with the "presenter replaced" code.
    assert p1.closed and p1.closed[0][0] == 4000
    # Old conn's detach must NOT clear the freshly attached successor.
    _room, first_conn = first
    await mgr.detach_presenter(sid, first_conn)
    assert mgr.get(sid).presenter is not None


async def test_attach_presenter_sends_audience_snapshot():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="x", name="Bob",
        hostname=None, send=f.send, close=f.close,
    )
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert audiences and audiences[-1]["count"] == 1
    assert audiences[-1]["followers"][0]["name"] == "Bob"


async def test_attach_presenter_records_the_client_id():
    """Every presenter attach records the attaching client's id, and a detach
    leaves it as-is. This is what lets a superseded tab's "end because I'm
    leaving" DELETE be ignored server-side (end_sync_session): its id no
    longer matches once another tab has taken over, while the last attacher's
    own DELETE still does after it detached.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    assert mgr.get(sid).presenter_client_id is None
    p1 = FakeWS()
    r1 = await mgr.attach_presenter(
        sid, user_id=10, send=p1.send, close=p1.close, client_id="p-one",
    )
    assert r1 is not None
    assert mgr.get(sid).presenter_client_id == "p-one"
    # A same-owner takeover evicts conn1 and records the new instance.
    p2 = FakeWS()
    r2 = await mgr.attach_presenter(
        sid, user_id=10, send=p2.send, close=p2.close, client_id="p-two",
    )
    assert r2 is not None
    _room2, conn2 = r2
    assert mgr.get(sid).presenter_client_id == "p-two"
    await mgr.detach_presenter(sid, conn2)
    assert mgr.get(sid).presenter_client_id == "p-two"


async def test_detach_presenter_sets_grace():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await mgr.detach_presenter(sid)
    room = mgr.get(sid)
    assert room is not None
    assert room.presenter is None
    assert room.presenter_grace_until is not None


# ----- follower attach/detach + replay -----------------------------------

async def test_follower_join_receives_last_view_and_cursor():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)

    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 1, "bounds": [0.0, 0.0, 1.0, 1.0]})
    await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 0.5, "y": 0.5, "visible": True})

    f = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name="late",
        hostname=None, send=f.send, close=f.close,
    )
    assert res is not None
    types = f.types_sent()
    assert "view" in types
    assert "cursor" in types


async def test_follower_join_unknown_session_returns_none():
    mgr = RoomManager()
    f = FakeWS()
    res = await mgr.attach_follower(
        "nope", ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close,
    )
    assert res is None


async def test_follower_detach_updates_audience():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name="X",
        hostname=None, send=f.send, close=f.close,
    )
    assert res is not None
    _room, meta = res
    p.sent.clear()
    await mgr.detach_follower(sid, meta.id)
    # The join's leading send opened a debounce window, so the leave lands
    # as a trailing flush.
    await flush_audience(mgr, sid)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert audiences and audiences[-1]["count"] == 0


async def test_follower_join_leave_churn_is_debounced():
    """One client joining and leaving in a tight loop must not queue an
    O(N) audience frame per event on the presenter (that would overflow its
    bounded outbound queue and close it with 1013). The burst collapses to a
    leading send plus one trailing flush carrying the final state."""
    mgr = RoomManager()
    mgr._audience_debounce = 60.0  # the whole loop runs inside one window
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await asyncio.sleep(0)
    room = mgr.get(sid)
    room._audience_last_sent = 0.0  # presenter attach does not open a window
    p.sent.clear()
    for _ in range(200):
        f = FakeWS()
        _, meta = await mgr.attach_follower(
            sid, ip="1.2.3.4", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        )
        await mgr.detach_follower(sid, meta.id)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert len(audiences) == 1  # the leading edge only
    await flush_audience(mgr, sid)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert len(audiences) == 2
    assert audiences[-1]["count"] == 0


async def test_follower_visibility_change_triggers_audience():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    _, meta = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close,
    )
    p.sent.clear()
    await mgr.update_follower_visibility(sid, meta.id, False)
    # Audience fan-out is debounced; flush the trailing frame deterministically.
    await flush_audience(mgr, sid)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert audiences and audiences[-1]["followers"][0]["visible"] is False
    # No-op when state unchanged (no schedule, so nothing to flush).
    p.sent.clear()
    await mgr.update_follower_visibility(sid, meta.id, False)
    await flush_audience(mgr, sid)
    assert not [m for m in p.sent if m.get("type") == "audience"]


# ----- broadcast ----------------------------------------------------------

async def test_broadcast_view_fans_out_and_caches():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    f1, f2 = FakeWS(), FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f1.send, close=f1.close)
    await mgr.attach_follower(sid, ip="2", ua="", name=None,
                              hostname=None, send=f2.send, close=f2.close)
    frame = {"type": "view", "slide_id": 1, "bounds": [0.0, 0.0, 0.5, 0.5]}
    assert await mgr.broadcast_view(sid, frame) is True
    assert frame in f1.sent
    assert frame in f2.sent
    assert mgr.get(sid).last_view == frame


async def test_broadcast_view_slide_change_updates_room_and_notifies_discovery():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    # Seat a presenter first: only an announced room emits slide_changed (the
    # index page adds an unknown sid on that event, see broadcast_view).
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s1", sub.send)
    sub.sent.clear()  # drop snapshot

    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 7, "bounds": [0.0, 0.0, 1.0, 1.0]})
    assert mgr.get(sid).slide_id == 7
    events = [m for m in sub.sent if m.get("type") == "slide_changed"]
    assert events and events[-1]["slide_id"] == 7


async def test_broadcast_view_unknown_session():
    mgr = RoomManager()
    assert await mgr.broadcast_view("nope", {"type": "view", "slide_id": 1}) is False


# ----- follower cap -------------------------------------------------------

async def test_attach_follower_raises_when_room_full(monkeypatch):
    """The per-room cap protects the audience fan-out from unbounded growth.
    Once FOLLOWERS_PER_ROOM_MAX is reached, the next attach must raise so
    the WS handler can map it to a dedicated close code instead of letting
    memory pressure mount silently."""
    import wsi_app.sync_room as sr
    monkeypatch.setattr(sr, "FOLLOWERS_PER_ROOM_MAX", 2)

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    for _ in range(2):
        f = FakeWS()
        await mgr.attach_follower(
            sid, ip="1.1.1.1", ua="", name=None,
            hostname=None, send=f.send, close=f.close,
        )

    overflow = FakeWS()
    with pytest.raises(RoomFullError):
        await mgr.attach_follower(
            sid, ip="2.2.2.2", ua="", name=None,
            hostname=None, send=overflow.send, close=overflow.close,
        )
    # The overflowed follower is never registered.
    assert len(mgr.get(sid).followers) == 2


# ----- hostname late binding ---------------------------------------------

async def test_update_follower_hostname_rebroadcasts_audience():
    """Reverse DNS resolves off the join path; the manager must accept
    a late hostname update and push a fresh audience frame so the presenter
    sees the resolved name even though the WS join completed without it."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    _, meta = await mgr.attach_follower(
        sid, ip="10.0.0.1", ua="ua", name="X",
        hostname=None, send=f.send, close=f.close,
    )
    p.sent.clear()
    await mgr.update_follower_hostname(sid, meta.id, "ws-laptop.example")
    # Debounced: the join's leading send opened the window, so this lands as
    # the trailing flush.
    await flush_audience(mgr, sid)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert audiences
    assert audiences[-1]["followers"][0]["hostname"] == "ws-laptop.example"


async def test_update_follower_hostname_no_op_when_unchanged():
    """An idempotent update mustn't spam the presenter — repeated PTRs that
    resolve to the same string should not re-broadcast."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    _, meta = await mgr.attach_follower(
        sid, ip="10.0.0.1", ua="ua", name=None,
        hostname="ws-laptop.example", send=f.send, close=f.close,
    )
    await flush_audience(mgr, sid)
    p.sent.clear()
    await mgr.update_follower_hostname(sid, meta.id, "ws-laptop.example")
    await flush_audience(mgr, sid)
    assert not [m for m in p.sent if m.get("type") == "audience"]


async def test_broadcast_view_rejects_non_int_slide_id():
    """Followers map slide_id straight into a navigation URL, so a stray
    string / float / bool must not propagate. ``isinstance(True, int)`` is
    True in Python, hence the explicit bool exclusion in the manager.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close,
    )
    f.sent.clear()
    for bad in ["7", 7.0, True, None, [7], {"id": 7}]:
        assert await mgr.broadcast_view(sid, {"type": "view", "slide_id": bad}) is False
    # No view frame leaked to followers despite the rejected payloads.
    assert not [m for m in f.sent if m.get("type") == "view"]
    assert mgr.get(sid).slide_id == 1
    assert mgr.get(sid).last_view is None


async def test_slide_change_clears_last_cursor():
    """Cursor coordinates are tied to the slide they were captured on. A
    presenter switching slides must wipe ``room.last_cursor`` so a late
    follower joining between the slide change and the next live cursor
    frame doesn't get a laser dot projected against the wrong image.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    # Pin a cursor on the old slide.
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 1, "bounds": [0, 0, 1, 1]})
    await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 50, "y": 60, "visible": True})
    assert mgr.get(sid).last_cursor is not None
    # Same-slide updates keep the cursor cached.
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 1, "bounds": [0, 0, 0.5, 0.5]})
    assert mgr.get(sid).last_cursor is not None
    # Slide change drops the stale cursor so the next-attaching follower
    # never receives it.
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "bounds": [0, 0, 1, 1]})
    assert mgr.get(sid).last_cursor is None
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close,
    )
    assert "cursor" not in f.types_sent()


async def test_attach_follower_honors_initial_visibility():
    """A client may surface its current document.visibilityState in the
    hello frame. Without honoring it, a follower joining while their tab
    is in the background would show as "active" to the presenter until
    they happen to flip visibility once.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    f = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name="Hidden",
        hostname=None, send=f.send, close=f.close, visible=False,
    )
    assert res is not None
    audience = next(m for m in p.sent if m.get("type") == "audience")
    assert audience["followers"][0]["visible"] is False


async def test_attach_follower_reports_viewport_size():
    """The follower's hello may carry its viewer-canvas size so the presenter
    can see who is on a differently-shaped screen. Both values ride through to
    the audience frame."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    f = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name="iPad",
        hostname=None, send=f.send, close=f.close, vw=1194, vh=834,
    )
    assert res is not None
    audience = next(m for m in p.sent if m.get("type") == "audience")
    assert audience["followers"][0]["vw"] == 1194
    assert audience["followers"][0]["vh"] == 834


async def test_attach_follower_without_viewport_reports_none():
    """A hello whose size is missing or malformed must still attach, with the
    fields present-but-null so the presenter UI can render "unknown"
    uniformly."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close,
    )
    audience = next(m for m in p.sent if m.get("type") == "audience")
    assert audience["followers"][0]["vw"] is None
    assert audience["followers"][0]["vh"] is None


@pytest.mark.parametrize(
    "vw,vh",
    [
        (0, 800),                      # zero
        (-1194, 834),                  # negative
        (1194, float("nan")),          # NaN survives int() as a crash otherwise
        (float("inf"), 834),           # infinite
        (10**9, 834),                  # past the sanity ceiling
        (10**400, 834),                # int too large for float() -> OverflowError
        ("1194", "834"),               # right shape, wrong type -> still usable
        ("wide", 834),                 # unparseable
        ({"nested": "object"}, 834),   # wrong JSON type entirely
        (True, 834),                   # bool is an int subclass; must not pass
        (1194, None),                  # half-reported pair
    ],
)
async def test_attach_follower_scrubs_hostile_viewport(vw, vh):
    """Viewport values are client-supplied and fanned back out to the presenter,
    so they get the numeric equivalent of the ua/hostname scrub. Anything
    unusable drops the *pair* to None rather than showing a plausible lie —
    except a numeric string, which is a legitimate JSON encoding of the size.
    """
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    f = FakeWS()
    res = await mgr.attach_follower(
        sid, ip="1.2.3.4", ua="ua", name=None,
        hostname=None, send=f.send, close=f.close, vw=vw, vh=vh,
    )
    assert res is not None
    audience = next(m for m in p.sent if m.get("type") == "audience")
    got = audience["followers"][0]
    if (vw, vh) == ("1194", "834"):
        assert (got["vw"], got["vh"]) == (1194, 834)
    else:
        assert got["vw"] is None and got["vh"] is None


# ----- discovery ----------------------------------------------------------

async def test_discovery_snapshot_on_subscribe():
    """The subscribe-time snapshot lists only *announced* rooms (a presenter has
    seated at least once). A room created but never attached is omitted, so a
    subscriber connecting during the create->attach window doesn't see a room
    that never surfaced a ``started`` event."""
    mgr = RoomManager()
    s1 = await mgr.create(slide_id=1, owner_user_id=1)
    s2 = await mgr.create(slide_id=2, owner_user_id=2)
    # A third room created but never attached must NOT appear in the snapshot.
    s3 = await mgr.create(slide_id=3, owner_user_id=3)
    p1, p2 = FakeWS(), FakeWS()
    await mgr.attach_presenter(s1, user_id=1, send=p1.send, close=p1.close)
    await mgr.attach_presenter(s2, user_id=2, send=p2.send, close=p2.close)
    sub = FakeWS()
    ok = await mgr.add_discovery_subscriber("s", sub.send)
    assert ok
    snaps = [m for m in sub.sent if m.get("type") == "snapshot"]
    assert snaps
    sids = {s["sid"] for s in snaps[-1]["sessions"]}
    assert sids == {s1, s2}
    assert s3 not in sids


async def test_discovery_started_event():
    """``started`` is deferred to the first presenter attach, not create(): a
    room whose presenter never completes the handshake must not surface a join
    banner."""
    mgr = RoomManager()
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    sub.sent.clear()
    sid = await mgr.create(slide_id=42, owner_user_id=1)
    # create() alone announces nothing.
    assert [m for m in sub.sent if m.get("type") == "started"] == []
    # The first presenter attach fires ``started``.
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    started = [m for m in sub.sent if m.get("type") == "started"]
    assert started and started[-1]["sid"] == sid and started[-1]["slide_id"] == 42


async def test_discovery_started_fires_once_across_reattach():
    """A reattach within the grace window does not re-fire ``started`` — the
    room is announced exactly once, on the first presenter attach."""
    mgr = RoomManager()
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p1 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p1.send, close=p1.close)
    await mgr.detach_presenter(sid)
    p2 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    started = [m for m in sub.sent if m.get("type") == "started" and m.get("sid") == sid]
    assert len(started) == 1


async def test_discovery_ended_event_on_end():
    mgr = RoomManager()
    sub = FakeWS()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    # Announce the room via a presenter attach so its teardown emits ``ended``.
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    await mgr.add_discovery_subscriber("s", sub.send)
    sub.sent.clear()
    await mgr.end(sid)
    ended = [m for m in sub.sent if m.get("type") == "ended"]
    assert ended and ended[-1]["sid"] == sid


async def test_discovery_no_ended_for_unannounced_room():
    """A room torn down before any presenter attached was never in a
    subscriber's snapshot or ``started`` stream, so its teardown must NOT emit a
    stray ``ended``. Keeps the started/ended discovery pair consistent."""
    mgr = RoomManager()
    sub = FakeWS()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    await mgr.add_discovery_subscriber("s", sub.send)
    sub.sent.clear()
    assert await mgr.end(sid) is True
    assert [m for m in sub.sent if m.get("type") == "ended"] == []


async def test_discovery_subscriber_cap(monkeypatch):
    mgr = RoomManager()
    monkeypatch.setattr("wsi_app.sync_room.DISCOVERY_MAX_SUBSCRIBERS", 2)
    # Re-import the constant locally — the manager checks the module attribute
    # directly so the monkeypatch is effective.
    import wsi_app.sync_room as sr
    sr.DISCOVERY_MAX_SUBSCRIBERS = 2

    a, b, c = FakeWS(), FakeWS(), FakeWS()
    assert await mgr.add_discovery_subscriber("a", a.send) is True
    assert await mgr.add_discovery_subscriber("b", b.send) is True
    assert await mgr.add_discovery_subscriber("c", c.send) is False

    sr.DISCOVERY_MAX_SUBSCRIBERS = DISCOVERY_MAX_SUBSCRIBERS  # restore default


# ----- GC ----------------------------------------------------------------

async def test_gc_expires_room_after_grace():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.detach_presenter(sid)

    # Just before expiry: room still there.
    grace = mgr.get(sid).presenter_grace_until
    expired = await mgr.gc_step(now=grace - 1)
    assert expired == []
    assert mgr.get(sid) is not None

    # After expiry: room gets reaped, follower receives end + close.
    expired = await mgr.gc_step(now=grace + 0.01)
    assert expired == [sid]
    assert mgr.get(sid) is None
    assert "end" in f.types_sent()
    assert f.closed


async def test_gc_does_not_touch_active_presenter():
    import time as _time

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    # A normal GC tick a moment into the future must not reap an active-
    # presenter room: it's well within the age cap and the presenter's idle
    # clock was just armed on attach. (All GC arithmetic is monotonic, so
    # ``now`` is anchored to the real monotonic clock — a synthetic far-future
    # value would legitimately trip the age/idle caps.)
    expired = await mgr.gc_step(now=_time.monotonic() + 1)
    assert expired == []
    assert mgr.get(sid) is not None


async def test_gc_reaps_room_whose_presenter_never_connects():
    """A REST-created session whose presenter never completes the WS hello
    must still be reaped: create() arms the grace deadline so gc_step finds
    it, instead of the room lingering in _rooms forever."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    # Grace deadline is armed at creation even though no presenter attached.
    grace = mgr.get(sid).presenter_grace_until
    assert grace is not None
    # Before expiry: still present.
    assert await mgr.gc_step(now=grace - 1) == []
    assert mgr.get(sid) is not None
    # After expiry: reaped.
    assert await mgr.gc_step(now=grace + 0.01) == [sid]
    assert mgr.get(sid) is None


async def test_attached_presenter_clears_creation_grace():
    """attach_presenter must clear the creation-time grace so an active
    presenter's room is never reaped while connected."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    assert mgr.get(sid).presenter_grace_until is not None
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    assert mgr.get(sid).presenter_grace_until is None


# ----- presenter absence notifications -----------------------------------
#
# Absence notices and teardown are driven by gc_step (the GC tick), so these
# tests call it directly with an explicit ``now`` — deterministic, no sleeps.
# ``now`` is monotonic-based; the tests anchor it to the room's presenter_left_at.

from wsi_app.sync_room import (  # noqa: E402
    PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS,
)


async def _absent_frames(ws: FakeWS):
    return [m for m in ws.sent if m.get("type") == "presenter"]


async def test_detach_does_not_notify_until_debounce_elapses():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.detach_presenter(sid)
    left = mgr.get(sid).presenter_left_at

    # A GC tick still inside the debounce window sends nothing.
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS - 0.5)
    assert await _absent_frames(f) == []

    # Once the debounce has elapsed, the absent notice fires with the remaining
    # grace (grace - debounce, give or take rounding).
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 0.1)
    absent = await _absent_frames(f)
    assert len(absent) == 1 and absent[0]["present"] is False
    expected = round(PRESENTER_GRACE_SECONDS - PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS)
    assert abs(absent[0]["grace_seconds"] - expected) <= 1

    # Idempotent: a later tick (still within grace) does not re-notify.
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 1.0)
    assert len(await _absent_frames(f)) == 1


async def test_departed_presenters_pointer_is_not_left_on_screen():
    """A presenter whose tab closes (or whose network drops) never sends the
    pointerleave that hides the laser dot. Joiners during the absence must not
    get the last pointer replayed, and the followers already watching are told
    to hide it together with the absence notice."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    _room, conn = await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.broadcast_cursor(
        sid, {"type": "cursor", "x": 100.0, "y": 200.0, "visible": True}, presenter=conn,
    )
    await mgr.detach_presenter(sid, conn)

    late = FakeWS()
    await mgr.attach_follower(sid, ip="2", ua="", name=None,
                              hostname=None, send=late.send, close=late.close)
    assert "cursor" not in late.types_sent()

    left = mgr.get(sid).presenter_left_at
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 0.1)
    for ws in (f, late):
        # Hidden, then the "presenter left" banner.
        assert ws.types_sent()[-2:] == ["cursor", "presenter"]
        assert ws.sent[-2]["visible"] is False
        assert ws.sent[-1]["present"] is False


async def test_reattach_within_debounce_sends_no_notice():
    """A reconnect inside the debounce window produces NO follower notice at
    all — neither "left" nor "returned" — so a brief blip never flashes."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.detach_presenter(sid)
    left = mgr.get(sid).presenter_left_at
    # A tick inside the debounce: nothing sent yet.
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS - 0.5)
    # Presenter reconnects before the debounce elapses.
    p2 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    assert mgr.get(sid).presenter_grace_until is None
    # Even well past when the notice would have fired, the follower saw nothing.
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 5)
    assert await _absent_frames(f) == []


async def test_reattach_after_notice_announces_return():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.detach_presenter(sid)
    left = mgr.get(sid).presenter_left_at
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 0.1)
    assert any(m["present"] is False for m in await _absent_frames(f))

    p2 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    assert mgr.get(sid).presenter_grace_until is None
    present = await _absent_frames(f)
    assert present[-1]["present"] is True


async def test_follower_joining_after_notice_gets_absent_notice():
    """A follower joining once the absence has been announced is told up-front,
    with the remaining grace. (Joining inside the debounce stays quiet — the
    next GC tick's notice reaches them too.)"""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await mgr.detach_presenter(sid)
    left = mgr.get(sid).presenter_left_at
    await mgr.gc_step(now=left + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS + 0.1)

    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    absent = await _absent_frames(f)
    assert absent and absent[-1]["present"] is False
    assert 0 <= absent[-1]["grace_seconds"] <= round(PRESENTER_GRACE_SECONDS)


async def test_follower_joining_within_debounce_stays_quiet():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await mgr.detach_presenter(sid)
    # Joining before the absence has been announced: no notice on join.
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    assert await _absent_frames(f) == []


async def test_gc_step_tears_down_room_after_grace():
    """gc_step reaps the room once the grace deadline passes, delivering the
    same end+close to followers as an explicit end."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    await mgr.detach_presenter(sid)
    grace = mgr.get(sid).presenter_grace_until
    # Just before the deadline: still alive.
    assert await mgr.gc_step(now=grace - 1) == []
    assert mgr.get(sid) is not None
    # After the deadline: reaped, follower gets end + close.
    assert await mgr.gc_step(now=grace + 0.01) == [sid]
    assert mgr.get(sid) is None
    assert "end" in f.types_sent()
    assert f.closed


async def test_create_superseding_replaces_only_the_callers_rooms():
    """One live session per presenter: create_superseding reaps all of the
    caller's rooms (notifying their followers) once the new one is seated, but
    leaves other presenters' sessions untouched."""
    mgr = RoomManager()
    a = await mgr.create(slide_id=1, owner_user_id=10)
    b = await mgr.create(slide_id=2, owner_user_id=10)
    other = await mgr.create(slide_id=3, owner_user_id=20)
    f = FakeWS()
    await mgr.attach_follower(a, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)

    new, ended = await mgr.create_superseding(slide_id=4, owner_user_id=10)
    assert set(ended) == {a, b}
    assert mgr.get(a) is None and mgr.get(b) is None
    assert mgr.get(new) is not None and mgr.get(new).slide_id == 4
    assert mgr.get(other) is not None  # another presenter's session survives
    # The superseded session's follower is told why it ended.
    assert {"type": "end", "reason": "superseded"} in f.sent
    await mgr.end(other)


async def test_create_superseding_with_no_previous_session():
    mgr = RoomManager()
    new, ended = await mgr.create_superseding(slide_id=1, owner_user_id=10)
    assert ended == []
    room = mgr.get(new)
    # Same grace arming as a plain create: a presenter that never attaches
    # does not keep the room forever.
    assert room is not None and room.presenter_grace_until is not None


async def test_reattach_before_grace_keeps_room():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await mgr.detach_presenter(sid)
    grace = mgr.get(sid).presenter_grace_until
    p2 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    # Reattach cleared the deadline, so a tick past the original deadline is a
    # no-op and the room survives.
    assert await mgr.gc_step(now=grace + 1) == []
    assert mgr.get(sid) is not None


# ----- absolute max-session-age cap --------------------------------------

async def test_gc_max_age_reaps_room_with_attached_presenter():
    """The age cap reaps a session past MAX_SESSION_AGE_SECONDS even while a
    presenter is still attached — the grace path never touches a presenter-ful
    room, so this is the only backstop against a forgotten/zombie presenter
    tab keeping the room (and its discovery banner) alive forever."""
    import time as _time
    from wsi_app.sync_room import MAX_SESSION_AGE_SECONDS

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    # Presenter is attached, so grace is disarmed: only the age cap can reap.
    assert mgr.get(sid).presenter is not None
    assert mgr.get(sid).presenter_grace_until is None

    # Backdate creation just past the cap. A normal tick (no synthetic ``now``)
    # uses the real monotonic clock for the age check, so backdating the
    # monotonic creation stamp is enough. (``created_at`` stays wall-clock for
    # the admin display and does not drive GC.)
    mgr.get(sid).created_monotonic = _time.monotonic() - (MAX_SESSION_AGE_SECONDS + 5)
    reaped = await mgr.gc_step()
    assert reaped == [sid]
    assert mgr.get(sid) is None
    # Both ends are notified + closed, same as any other teardown.
    assert "end" in f.types_sent()
    assert f.closed
    assert "end" in p.types_sent()
    assert p.closed


async def test_gc_max_age_disabled_keeps_old_room(monkeypatch):
    """With the cap disabled (<= 0) an old session with a live presenter is
    left alone — operators who opt out keep unbounded session retention."""
    import time as _time
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "MAX_SESSION_AGE_SECONDS", 0.0)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    mgr.get(sid).created_monotonic = _time.monotonic() - 10_000_000
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None


# ----- list_all_detailed (staff management snapshot) ---------------------

async def test_list_all_detailed_reports_presenter_and_followers():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=7, owner_user_id=42)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=42, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name="Ann",
                              hostname=None, send=f.send, close=f.close)

    entries = mgr.list_all_detailed()
    assert len(entries) == 1
    e = entries[0]
    assert e.session_id == sid
    assert e.slide_id == 7
    assert e.owner_user_id == 42
    assert e.presenter_connected is True
    assert e.follower_count == 1
    # Presenter attached → not in grace, so no countdown is surfaced.
    assert e.grace_remaining is None
    assert e.created_at > 0


async def test_list_all_detailed_surfaces_grace_for_orphan():
    """After the presenter leaves, the snapshot flags the room as orphaned
    (presenter_connected False) and exposes the remaining grace so the admin
    UI can show a countdown."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    await mgr.detach_presenter(sid)

    e = next(x for x in mgr.list_all_detailed() if x.session_id == sid)
    assert e.presenter_connected is False
    assert e.grace_remaining is not None
    assert 0 <= e.grace_remaining <= PRESENTER_GRACE_SECONDS


# ----- presenter inactivity (idle) timeout -------------------------------

async def test_gc_idle_warns_presenter_then_force_ends():
    """A connected-but-silent presenter is warned once at the warn threshold
    (presenter only) and force-ended at the idle deadline (both ends), without
    ever entering the absence/grace path."""
    import time as _time
    from wsi_app.sync_room import (
        PRESENTER_IDLE_SECONDS as IDLE,
        PRESENTER_IDLE_WARN_SECONDS as WARN,
    )

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    room = mgr.get(sid)

    # Backdate activity into the warning window (past IDLE-WARN, before IDLE).
    # presenter_last_activity is a monotonic stamp, so backdate off monotonic.
    room.presenter_last_activity = _time.monotonic() - (IDLE - WARN + 1)
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None
    warn_frames = [m for m in p.sent if m.get("type") == "idle_warning"]
    assert len(warn_frames) == 1
    assert 0 <= warn_frames[0]["seconds"] <= WARN
    assert room.presenter_idle_warned is True
    # The warning is presenter-only — followers are not told.
    assert "idle_warning" not in f.types_sent()
    # Idempotent: a second tick in the window does not re-warn.
    p.sent.clear()
    assert await mgr.gc_step() == []
    assert "idle_warning" not in p.types_sent()

    # Cross the deadline: force-end with reason presenter_idle, both notified.
    room.presenter_last_activity = _time.monotonic() - (IDLE + 1)
    assert await mgr.gc_step() == [sid]
    assert mgr.get(sid) is None
    assert "end" in f.types_sent() and f.closed
    p_end = [m for m in p.sent if m.get("type") == "end"]
    assert p_end and p_end[0].get("reason") == "presenter_idle"
    assert p.closed


async def test_presenter_activity_resets_idle_warning():
    """Any presenter frame (cursor here) clears the warning and re-arms the
    inactivity clock, so an interacting presenter is never force-ended."""
    import time as _time
    from wsi_app.sync_room import (
        PRESENTER_IDLE_SECONDS as IDLE,
        PRESENTER_IDLE_WARN_SECONDS as WARN,
    )

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    room = mgr.get(sid)
    room.presenter_last_activity = _time.monotonic() - (IDLE - WARN + 1)
    await mgr.gc_step()
    assert room.presenter_idle_warned is True

    await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 1.0, "y": 1.0, "visible": True})
    assert room.presenter_idle_warned is False
    # broadcast_cursor re-armed the monotonic activity clock.
    assert _time.monotonic() - room.presenter_last_activity < 1
    # Now a tick reaps nothing — the clock was reset by the activity.
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None


async def test_idle_warn_lead_longer_than_the_timeout_is_halved(monkeypatch):
    """A lead at or above the idle timeout would put the warning threshold
    at or below zero, so an *active* presenter would be warned again on every
    GC tick right after each interaction. Like the max-age lead it is clamped to
    half the timeout."""
    from wsi_app import sync_room as sr

    monkeypatch.setattr(sr, "PRESENTER_IDLE_SECONDS", 600.0)
    monkeypatch.setattr(sr, "PRESENTER_IDLE_WARN_SECONDS", 900.0)  # misconfigured
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    room = mgr.get(sid)
    for _ in range(5):
        # The presenter keeps interacting; a tick follows each frame.
        await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 1.0, "y": 1.0, "visible": True})
        await mgr.gc_step(now=room.presenter_last_activity + 1)
    assert "idle_warning" not in p.types_sent()
    # Warned once half the timeout has passed in silence. Just past it, not
    # exactly on it: (t + 300) - t can come out as 299.99999999999994 when
    # t + 300 crosses a power of two, as it does for the few-hundred-second
    # monotonic clock of a freshly booted CI runner.
    await mgr.gc_step(now=room.presenter_last_activity + 300.25)
    warnings = [m for m in p.sent if m.get("type") == "idle_warning"]
    assert warnings == [{"type": "idle_warning", "seconds": 300}]


async def test_gc_idle_disabled_keeps_silent_presenter(monkeypatch):
    import time as _time
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "PRESENTER_IDLE_SECONDS", 0.0)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    mgr.get(sid).presenter_last_activity = _time.monotonic() - 10_000_000
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None


# ----- GC clock is monotonic, immune to wall-clock jumps -------------------

async def test_gc_idle_uses_monotonic_not_wall_clock(monkeypatch):
    """A large FORWARD wall-clock jump (suspend/resume, NTP step) must not
    force-end an actively-driven session — all GC arithmetic is monotonic. A
    genuine monotonic idle still ends it, proving the timeout keys off the right
    clock: idle/age must not key off the wall clock while grace uses
    monotonic."""
    import time as _time
    import wsi_app.sync_room as sr
    from wsi_app.sync_room import PRESENTER_IDLE_SECONDS as IDLE

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    room = mgr.get(sid)
    # Presenter active as of now (attach armed the monotonic activity clock).
    room.presenter_last_activity = _time.monotonic()

    # Jump the WALL clock far past the idle deadline and the age cap. The module
    # reads the wall clock through ``sr.time`` (its imported ``time`` module), so
    # patching ``sr.time.time`` is the documented time-indirection seam.
    real_time = _time.time
    monkeypatch.setattr(sr.time, "time", lambda: real_time() + IDLE * 100)

    # A real (monotonic) GC tick must NOT reap the actively-driven session.
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None

    # But real monotonic silence past the deadline still force-ends it.
    room.presenter_last_activity = _time.monotonic() - (IDLE + 1)
    assert await mgr.gc_step() == [sid]
    assert mgr.get(sid) is None


async def test_gc_max_age_uses_monotonic_not_wall_clock(monkeypatch):
    """The absolute-age cap is monotonic too: a forward wall-clock jump doesn't
    prematurely reap a young room, and the wall-clock ``created_at`` shown to
    admins is left untouched."""
    import time as _time
    import wsi_app.sync_room as sr

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    wall_created = mgr.get(sid).created_at

    real_time = _time.time
    monkeypatch.setattr(
        sr.time, "time",
        lambda: real_time() + sr.MAX_SESSION_AGE_SECONDS * 100,
    )
    # Young room (monotonic), so a wall-clock jump must not trip the age cap.
    assert await mgr.gc_step() == []
    assert mgr.get(sid) is not None
    # The admin-display wall stamp is unchanged (not recomputed from GC math).
    assert mgr.get(sid).created_at == wall_created


# ----- visibility-toggle burst is coalesced --------------------------------

async def test_visibility_toggle_burst_is_coalesced():
    """A burst of visibility toggles from one follower must NOT push O(K)
    audience frames to the presenter — the debounce collapses them to a leading
    frame plus one trailing frame carrying the latest state. A per-toggle
    O(N) audience rebuild would be an amplification vector."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    followers = []
    for _ in range(20):
        f = FakeWS()
        await mgr.attach_follower(
            sid, ip="1", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        )
        followers.append(f)
    room = mgr.get(sid)
    target = next(iter(room.followers))
    p.sent.clear()

    # Hammer visibility, alternating so every flip is a genuine state change.
    K = 50
    for i in range(K):
        await mgr.update_follower_visibility(sid, target, i % 2 == 0)
    await flush_audience(mgr, sid)

    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert len(audiences) <= 2, (
        f"expected the toggle burst to coalesce to <=2 audience frames, "
        f"got {len(audiences)} (looks like one-per-toggle O(N) amplification)"
    )
    assert len(audiences) < K
    # Latest state wins: the final toggle (i=K-1) set visible=((K-1)%2==0).
    final_visible = ((K - 1) % 2 == 0)
    flagged = [
        fol for fol in audiences[-1]["followers"] if fol["id"] == target
    ]
    assert flagged and flagged[0]["visible"] is final_visible
    # Visibility events never fan out to followers, only presenter-ward.
    assert all(not f.sent for f in followers)


async def test_visibility_debounce_trailing_flush_fires_on_its_own():
    """Two rapid toggles inside the window: the first is the leading send, the
    second coalesces into a trailing flush the debounce timer fires by itself
    (no explicit flush_audience). Latest state wins."""
    mgr = RoomManager()
    mgr._audience_debounce = 0.02  # keep the test fast
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    _, meta = await mgr.attach_follower(
        sid, ip="1", ua="", name=None, hostname=None, send=f.send, close=f.close,
    )
    # Let the join's own leading send age out of the window first.
    await asyncio.sleep(0.03)
    p.sent.clear()
    await mgr.update_follower_visibility(sid, meta.id, False)  # leading (immediate)
    await mgr.update_follower_visibility(sid, meta.id, True)   # coalesced -> trailing
    # Wait past the window; the trailing timer must fire without an explicit flush.
    await asyncio.sleep(0.06)
    audiences = [m for m in p.sent if m.get("type") == "audience"]
    assert len(audiences) == 2  # leading (False) + trailing (True)
    assert audiences[-1]["followers"][0]["visible"] is True


# ----- MAX_ROOMS cap -------------------------------------------------------

async def test_create_rejects_over_max_rooms(monkeypatch):
    """``create`` refuses to grow ``_rooms`` past MAX_ROOMS with RoomLimitError
    so the REST caller can map it to a 503, instead of the dict growing
    unbounded."""
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "MAX_ROOMS", 3)
    mgr = RoomManager()
    for i in range(3):
        await mgr.create(slide_id=1, owner_user_id=i)
    with pytest.raises(sr.RoomLimitError):
        await mgr.create(slide_id=1, owner_user_id=99)
    # The rejected room was never inserted.
    assert len(mgr._rooms) == 3


async def test_create_max_rooms_disabled(monkeypatch):
    """MAX_ROOMS <= 0 disables the cap (unbounded)."""
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "MAX_ROOMS", 0)
    mgr = RoomManager()
    for i in range(5):
        await mgr.create(slide_id=1, owner_user_id=i)
    assert len(mgr._rooms) == 5


# ----- lock-free list_* are safe under concurrent mutation -----------------

async def test_list_methods_safe_under_concurrent_mutation():
    """The plain-sync ``list_*`` snapshots run in a threadpool concurrently with
    the event loop mutating ``_rooms`` (create / gc_step). They must snapshot
    the dict atomically (``list(...)``) so they never raise "dictionary changed
    size during iteration"."""
    import threading

    mgr = RoomManager()
    for i in range(50):
        await mgr.create(slide_id=i, owner_user_id=i)

    stop = threading.Event()
    errors: List[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                mgr.list_all_detailed()
                mgr.list_owned(1)
            except Exception as exc:  # pragma: no cover - the failure path
                errors.append(exc)
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        # Churn _rooms from the event-loop thread while the reader iterates.
        for _ in range(300):
            sid = await mgr.create(slide_id=1, owner_user_id=1)
            await mgr.end(sid)
    finally:
        stop.set()
        t.join()
    assert not errors, f"list_* raised under concurrent mutation: {errors!r}"


# ----- A stale presenter's auto-reconnect cannot reclaim the room -----------
# Staleness is keyed by the presenter client-instance id each hello carries
# (``client_id``) plus the ``resume`` marker of an automatic reconnect.

async def test_stale_presenter_reconnect_cannot_reclaim_after_resume():
    """Laptop (client A) drops; the presenter Resumes on a tablet (client B,
    fresh attach). When the laptop wakes and auto-reconnects it must be
    refused, leaving the tablet attached and un-evicted."""
    from wsi_app.sync_room import PresenterSupersededError

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    laptop, tablet, laptop2 = FakeWS(), FakeWS(), FakeWS()
    _room, laptop_conn = await mgr.attach_presenter(
        sid, user_id=10, send=laptop.send, close=laptop.close, client_id="A",
    )
    await mgr.detach_presenter(sid, laptop_conn)
    _room, tablet_conn = await mgr.attach_presenter(
        sid, user_id=10, send=tablet.send, close=tablet.close, client_id="B",
    )

    with pytest.raises(PresenterSupersededError):
        await mgr.attach_presenter(
            sid, user_id=10, send=laptop2.send, close=laptop2.close,
            client_id="A", resume=True,
        )
    room = mgr.get(sid)
    assert room.presenter is tablet_conn
    assert room.presenter_client_id == "B"
    assert tablet.closed == []


async def test_stale_reconnect_refused_even_after_successor_left():
    """attach(A) -> attach(B fresh) -> B detaches -> A's auto-reconnect is
    still refused: a different client attached since A last did."""
    from wsi_app.sync_room import PresenterSupersededError

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    a, b, a2 = FakeWS(), FakeWS(), FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=a.send, close=a.close, client_id="A")
    _room, cb = await mgr.attach_presenter(
        sid, user_id=10, send=b.send, close=b.close, client_id="B",
    )
    await mgr.detach_presenter(sid, cb)
    with pytest.raises(PresenterSupersededError):
        await mgr.attach_presenter(
            sid, user_id=10, send=a2.send, close=a2.close, client_id="A", resume=True,
        )
    assert mgr.get(sid).presenter is None


async def test_presenter_reconnect_after_lost_attached_frame_succeeds():
    """A socket that dies before the ``attached`` frame reaches the
    client must not lock the legitimate presenter out of its own session:
    attach(A) -> reconnect(A) whose ``attached`` frame never arrived ->
    reconnect(A) again succeeds, each time evicting the half-dead socket."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p1, p2, p3 = FakeWS(), FakeWS(), FakeWS()
    await mgr.attach_presenter(
        sid, user_id=10, send=p1.send, close=p1.close, client_id="A",
    )
    # The client never saw this attach confirmed (socket lost before the
    # ``attached`` frame); the next hello carries only its instance id.
    res2 = await mgr.attach_presenter(
        sid, user_id=10, send=p2.send, close=p2.close, client_id="A", resume=True,
    )
    assert res2 is not None
    res3 = await mgr.attach_presenter(
        sid, user_id=10, send=p3.send, close=p3.close, client_id="A", resume=True,
    )
    assert res3 is not None
    assert p1.closed and p1.closed[0][0] == 4000
    assert p2.closed and p2.closed[0][0] == 4000
    assert mgr.get(sid).presenter is res3[1]


async def test_presenter_resume_before_any_attach_succeeds():
    """A reconnect that reaches a room nobody has attached to yet (the first
    hello was lost) is not stale."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    assert await mgr.attach_presenter(
        sid, user_id=10, send=p.send, close=p.close, client_id="A", resume=True,
    ) is not None


async def test_fresh_attach_still_takes_over_from_reconnected_presenter():
    """An explicit Resume (fresh instance, no resume marker) always wins, so
    the device the presenter is actually holding can take the session back."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    a, b = FakeWS(), FakeWS()
    await mgr.attach_presenter(
        sid, user_id=10, send=a.send, close=a.close, client_id="A",
    )
    res = await mgr.attach_presenter(
        sid, user_id=10, send=b.send, close=b.close, client_id="B",
    )
    assert res is not None and mgr.get(sid).presenter is res[1]
    assert a.closed and a.closed[0][0] == 4000


async def test_idless_attach_supersedes_id_carrying_client():
    """A hello without a client id (missing or malformed) is a fresh attach,
    and it counts as a different client for a later id-carrying reconnect."""
    from wsi_app.sync_room import PresenterSupersededError

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    a, idless, a2 = FakeWS(), FakeWS(), FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=a.send, close=a.close, client_id="A")
    assert await mgr.attach_presenter(
        sid, user_id=10, send=idless.send, close=idless.close,
    ) is not None
    with pytest.raises(PresenterSupersededError):
        await mgr.attach_presenter(
            sid, user_id=10, send=a2.send, close=a2.close, client_id="A", resume=True,
        )


def test_sanitize_presenter_client_id():
    from wsi_app.sync_room import sanitize_presenter_client_id as s
    assert s("0123abcd" * 4) == "0123abcd" * 4
    for bad in (None, "", 5, True, "x" * 65, "a\nb", "\u00e9t\u00e9"):
        assert s(bad) is None


# ----- One host cannot fill a room or the discovery channel -----------------

async def test_attach_follower_per_ip_cap(monkeypatch):
    import wsi_app.sync_room as sr
    monkeypatch.setattr(sr, "FOLLOWERS_PER_IP_MAX", 2)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    for _ in range(2):
        f = FakeWS()
        assert await mgr.attach_follower(
            sid, ip="6.6.6.6", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        ) is not None
    f = FakeWS()
    with pytest.raises(RoomFullError):
        await mgr.attach_follower(
            sid, ip="6.6.6.6", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        )
    # Another address still gets in.
    assert await mgr.attach_follower(
        sid, ip="7.7.7.7", ua="", name=None, hostname=None,
        send=f.send, close=f.close,
    ) is not None


async def test_discovery_per_ip_cap_and_release(monkeypatch):
    import wsi_app.sync_room as sr
    monkeypatch.setattr(sr, "DISCOVERY_MAX_SUBS_PER_IP", 2)
    mgr = RoomManager()
    subs = [FakeWS() for _ in range(4)]
    assert await mgr.add_discovery_subscriber("a", subs[0].send, ip="6.6.6.6")
    assert await mgr.add_discovery_subscriber("b", subs[1].send, ip="6.6.6.6")
    assert not await mgr.add_discovery_subscriber("c", subs[2].send, ip="6.6.6.6")
    assert await mgr.add_discovery_subscriber("d", subs[3].send, ip="7.7.7.7")
    # Closing one frees that address's slot.
    await mgr.remove_discovery_subscriber("a")
    assert await mgr.add_discovery_subscriber("c", subs[2].send, ip="6.6.6.6")
    await mgr.remove_discovery_subscriber("b")
    await mgr.remove_discovery_subscriber("c")
    await mgr.remove_discovery_subscriber("d")
    assert mgr._discovery_ip_counts == {}


# ----- The discovery snapshot honours the transport's filter ----------------

async def test_discovery_snapshot_filter_hides_rooms():
    mgr = RoomManager()
    s1 = await mgr.create(slide_id=1, owner_user_id=1)
    s2 = await mgr.create(slide_id=2, owner_user_id=2)
    for sid, uid in ((s1, 1), (s2, 2)):
        p = FakeWS()
        await mgr.attach_presenter(sid, user_id=uid, send=p.send, close=p.close)

    async def only_slide_1(sessions):
        return [s for s in sessions if s["slide_id"] == 1]

    sub = FakeWS()
    assert await mgr.add_discovery_subscriber(
        "s", sub.send, snapshot_filter=only_slide_1,
    )
    snap = [m for m in sub.sent if m.get("type") == "snapshot"][-1]
    assert [s["sid"] for s in snap["sessions"]] == [s1]

    # A failing filter fails closed (empty snapshot), never open.
    async def broken(_sessions):
        raise RuntimeError("db down")

    sub2 = FakeWS()
    assert await mgr.add_discovery_subscriber(
        "s2", sub2.send, snapshot_filter=broken,
    )
    snap2 = [m for m in sub2.sent if m.get("type") == "snapshot"][-1]
    assert snap2["sessions"] == []


# ----- Per-IP defaults suit a class behind one NAT ---------------------------

def test_per_ip_cap_defaults_do_not_block_a_natted_class():
    """A campus reaching the server through one address (NAT / Cloudflare
    tunnel) is a single client IP. The follower per-IP cap is off by default
    (the per-room cap bounds a room), and the discovery per-IP share admits a
    full room's worth of tabs while staying well below the global cap."""
    import os
    import wsi_app.sync_room as sr
    if "WSI_SYNC_FOLLOWERS_PER_IP_MAX" not in os.environ:
        assert sr.FOLLOWERS_PER_IP_MAX == 0
    if "WSI_SYNC_DISCOVERY_MAX_SUBS_PER_IP" not in os.environ:
        assert sr.DISCOVERY_MAX_SUBS_PER_IP == 200
        assert sr.FOLLOWERS_PER_ROOM_MAX <= sr.DISCOVERY_MAX_SUBS_PER_IP
        assert sr.DISCOVERY_MAX_SUBS_PER_IP < sr.DISCOVERY_MAX_SUBSCRIBERS


async def test_follower_per_ip_disabled_admits_whole_room_from_one_ip(monkeypatch):
    import wsi_app.sync_room as sr
    monkeypatch.setattr(sr, "FOLLOWERS_PER_IP_MAX", 0)
    monkeypatch.setattr(sr, "FOLLOWERS_PER_ROOM_MAX", 120)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    for _ in range(120):
        f = FakeWS()
        assert await mgr.attach_follower(
            sid, ip="10.0.0.1", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        ) is not None
    # The per-room cap still applies.
    f = FakeWS()
    with pytest.raises(RoomFullError):
        await mgr.attach_follower(
            sid, ip="10.0.0.1", ua="", name=None, hostname=None,
            send=f.send, close=f.close,
        )


async def test_per_ip_refusals_are_logged_rate_limited(monkeypatch, caplog):
    """A per-IP refusal is a WARNING (so operators can tell it from a full
    room), at most once per (cap, IP) per interval."""
    import logging
    import wsi_app.sync_room as sr
    monkeypatch.setattr(sr, "FOLLOWERS_PER_IP_MAX", 1)
    monkeypatch.setattr(sr, "DISCOVERY_MAX_SUBS_PER_IP", 1)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    f = FakeWS()
    await mgr.attach_follower(
        sid, ip="6.6.6.6", ua="", name=None, hostname=None, send=f.send, close=f.close,
    )
    s = FakeWS()
    assert await mgr.add_discovery_subscriber("a", s.send, ip="6.6.6.6")
    with caplog.at_level(logging.WARNING, logger="wsi_app.sync"):
        for _ in range(3):
            with pytest.raises(RoomFullError):
                await mgr.attach_follower(
                    sid, ip="6.6.6.6", ua="", name=None, hostname=None,
                    send=f.send, close=f.close,
                )
            assert not await mgr.add_discovery_subscriber("b", s.send, ip="6.6.6.6")
    msgs = [r.getMessage() for r in caplog.records if r.name == "wsi_app.sync"]
    assert len(msgs) == 2, msgs
    assert any("FOLLOWERS_PER_IP_MAX" in m and "6.6.6.6" in m for m in msgs)
    assert any("DISCOVERY_MAX_SUBS_PER_IP" in m for m in msgs)
    # A genuinely full room is not reported as a per-IP refusal.
    caplog.clear()
    monkeypatch.setattr(sr, "FOLLOWERS_PER_IP_MAX", 0)
    monkeypatch.setattr(sr, "FOLLOWERS_PER_ROOM_MAX", 1)
    with caplog.at_level(logging.WARNING, logger="wsi_app.sync"):
        with pytest.raises(RoomFullError):
            await mgr.attach_follower(
                sid, ip="7.7.7.7", ua="", name=None, hostname=None,
                send=f.send, close=f.close,
            )
    assert not [r for r in caplog.records if r.name == "wsi_app.sync"]


# ----- The snapshot always precedes events -----------------------------------

async def _announced_room(mgr, *, slide_id, owner):
    sid = await mgr.create(slide_id=slide_id, owner_user_id=owner)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=owner, send=p.send, close=p.close)
    return sid


async def test_discovery_snapshot_precedes_events_raised_during_filter():
    """A session started (and another's slide switched) while the snapshot
    filter's DB query is in flight must be in the snapshot, and no event may
    reach the subscriber before it — the client replaces its list with the
    snapshot, so an earlier event would be lost."""
    mgr = RoomManager()
    s_old = await _announced_room(mgr, slide_id=1, owner=1)
    started_during_filter = {}

    async def filt(sessions):
        # Events broadcast while the filter "does DB I/O".
        started_during_filter["sid"] = await _announced_room(mgr, slide_id=2, owner=2)
        await mgr.broadcast_view(
            s_old, {"type": "view", "slide_id": 3, "bounds": [0.0, 0.0, 1.0, 1.0]},
        )
        return sessions

    sub = FakeWS()
    assert await mgr.add_discovery_subscriber("s", sub.send, snapshot_filter=filt)
    assert sub.sent[0]["type"] == "snapshot"
    snap = {s["sid"]: s["slide_id"] for s in sub.sent[0]["sessions"]}
    assert snap == {s_old: 3, started_during_filter["sid"]: 2}


async def test_discovery_snapshot_precedes_events_raised_during_send():
    """Events broadcast while the snapshot send itself is awaiting are held
    back and delivered right after it, in order."""
    mgr = RoomManager()
    await _announced_room(mgr, slide_id=1, owner=1)
    gate = asyncio.Event()
    received: List[dict] = []

    async def slow_send(msg):
        if msg["type"] == "snapshot":
            await gate.wait()
        received.append(msg)

    task = asyncio.create_task(mgr.add_discovery_subscriber("s", slow_send))
    await asyncio.sleep(0)
    for _ in range(5):
        if "s" in mgr._discovery_subs:
            break
        await asyncio.sleep(0)
    s_new = await _announced_room(mgr, slide_id=2, owner=2)
    await mgr.end(s_new)
    gate.set()
    assert await task is True
    assert [m["type"] for m in received] == ["snapshot", "started", "ended"]
    # Direct delivery afterwards.
    s3 = await _announced_room(mgr, slide_id=3, owner=3)
    assert received[-1] == {
        "type": "started", "sid": s3, "slide_id": 3, "slide_public_id": "",
    }
    assert mgr._discovery_pending == {}


async def test_discovery_snapshot_excludes_room_ended_during_filter():
    mgr = RoomManager()
    sid = await _announced_room(mgr, slide_id=1, owner=1)

    async def filt(sessions):
        await mgr.end(sid)
        return sessions

    sub = FakeWS()
    assert await mgr.add_discovery_subscriber("s", sub.send, snapshot_filter=filt)
    assert sub.sent == [{"type": "snapshot", "sessions": []}]


async def test_discovery_hidden_room_reappears_via_slide_changed():
    """A room filtered out of the snapshot (slide unpublished) surfaces again
    through ``slide_changed`` once the presenter moves to another slide (the
    WS handler only allows published targets); the index page adds unknown
    sids on that event."""
    mgr = RoomManager()
    sid = await _announced_room(mgr, slide_id=1, owner=1)

    async def hide_slide_1(sessions):
        return [s for s in sessions if s["slide_id"] != 1]

    sub = FakeWS()
    assert await mgr.add_discovery_subscriber(
        "s", sub.send, snapshot_filter=hide_slide_1,
    )
    assert sub.sent[0] == {"type": "snapshot", "sessions": []}
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "bounds": [0.0, 0.0, 1.0, 1.0]})
    assert sub.sent[-1]["type"] == "slide_changed" and sub.sent[-1]["sid"] == sid


async def test_no_slide_changed_after_room_ended():
    """A slide_changed racing the room's teardown must not follow its
    ``ended`` (it would resurrect the banner on the index page)."""
    mgr = RoomManager()
    sid = await _announced_room(mgr, slide_id=1, owner=1)
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)

    async def ending_send(_msg):
        # The follower fan-out awaits; the room ends meanwhile.
        await mgr.end(sid)

    async def noop_close(code=1000, reason=""):
        return None

    await mgr.attach_follower(
        sid, ip="1.1.1.1", ua="", name=None, hostname=None,
        send=ending_send, close=noop_close,
    )
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "bounds": [0.0, 0.0, 1.0, 1.0]})
    types = sub.types_sent()
    assert "ended" in types
    assert "slide_changed" not in types[types.index("ended"):]


# ----- discovery slot is released when the subscribe is cancelled ------------

async def test_cancelled_discovery_subscribe_does_not_leak_slot():
    mgr = RoomManager()
    await _announced_room(mgr, slide_id=1, owner=1)

    # Cancelled while the filter runs (before registration).
    blocker = asyncio.Event()

    async def blocking_filter(sessions):
        await blocker.wait()
        return sessions

    sub = FakeWS()
    t = asyncio.create_task(mgr.add_discovery_subscriber(
        "a", sub.send, ip="6.6.6.6", snapshot_filter=blocking_filter,
    ))
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    # Cancelled while the snapshot send is in flight (after registration).
    async def blocking_send(_msg):
        await blocker.wait()

    t = asyncio.create_task(mgr.add_discovery_subscriber(
        "b", blocking_send, ip="6.6.6.6",
    ))
    await asyncio.sleep(0.01)
    assert len(mgr._discovery_subs) == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert mgr._discovery_subs == {}
    assert mgr._discovery_ip_counts == {}
    assert mgr._discovery_pending == {}


async def test_superseded_presenter_frames_do_not_overwrite_the_room():
    """A slide-change frame whose published-slide lookup is still in flight
    when another tab takes the session over must not be applied afterwards,
    rolling the room's slide and last_view back to the old presenter's."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10, slide_public_id="s1")
    old_ws, new_ws, follower = FakeWS(), FakeWS(), FakeWS()
    old = await mgr.attach_presenter(sid, user_id=10, send=old_ws.send, close=old_ws.close)
    new = await mgr.attach_presenter(sid, user_id=10, send=new_ws.send, close=new_ws.close)
    _room, old_conn = old
    _room, new_conn = new
    await mgr.attach_follower(sid, ip="1.1.1.1", ua="", name="f", hostname=None,
                              send=follower.send, close=follower.close)

    frame3 = {"type": "view", "slide_id": 3, "slide_public_id": "s3", "bounds": [0, 0, 1, 1]}
    assert await mgr.broadcast_view(sid, dict(frame3), presenter=new_conn) is True
    frame2 = {"type": "view", "slide_id": 2, "slide_public_id": "s2", "bounds": [0, 0, 1, 1]}
    assert await mgr.broadcast_view(sid, dict(frame2), presenter=old_conn) is False
    assert await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 1, "y": 1}, presenter=old_conn) is False

    room = mgr.get(sid)
    assert room.slide_id == 3 and room.slide_public_id == "s3"
    assert room.last_view["slide_id"] == 3
    assert [m["slide_id"] for m in follower.sent if m.get("type") == "view"] == [3]
    assert room.presenter is new_conn


# ----- Presenter is warned shortly before the max-age cap -----------------

async def test_gc_warns_presenter_once_before_max_age():
    """Activity cannot postpone the age cap, so the presenter gets a single
    ``max_age_warning`` (presenter only) when the warning window opens."""
    import time as _time
    from wsi_app.sync_room import (
        MAX_SESSION_AGE_SECONDS as CAP,
        MAX_SESSION_AGE_WARN_SECONDS as WARN,
    )

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None,
                              hostname=None, send=f.send, close=f.close)
    room = mgr.get(sid)
    p.sent.clear()

    # Before the window: nothing.
    room.created_monotonic = _time.monotonic() - (CAP - WARN - 30)
    assert await mgr.gc_step() == []
    assert "max_age_warning" not in p.types_sent()

    # Inside the window: warned once, presenter only.
    room.created_monotonic = _time.monotonic() - (CAP - WARN + 1)
    assert await mgr.gc_step() == []
    frames = [m for m in p.sent if m.get("type") == "max_age_warning"]
    assert len(frames) == 1
    assert 0 <= frames[0]["seconds"] <= WARN
    assert "max_age_warning" not in f.types_sent()
    p.sent.clear()
    assert await mgr.gc_step() == []
    assert "max_age_warning" not in p.types_sent()


async def test_max_age_warning_reaches_a_presenter_that_reattaches_after_it(monkeypatch):
    """The one-shot frame is per attached presenter: a tab reloaded after it was
    sent must still learn the session is about to end."""
    import time as _time
    from wsi_app.sync_room import (
        MAX_SESSION_AGE_SECONDS as CAP,
        MAX_SESSION_AGE_WARN_SECONDS as WARN,
    )

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p1 = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p1.send, close=p1.close)
    room = mgr.get(sid)
    room.created_monotonic = _time.monotonic() - (CAP - WARN + 1)
    await mgr.gc_step()
    assert "max_age_warning" in p1.types_sent()

    p2 = FakeWS()  # the reloaded tab
    await mgr.attach_presenter(sid, user_id=10, send=p2.send, close=p2.close)
    p2.sent.clear()
    await mgr.gc_step()
    assert "max_age_warning" in p2.types_sent()


async def test_max_age_warning_is_not_lost_when_the_presenter_leaves_before_it_is_sent():
    import time as _time
    from wsi_app.sync_room import (
        MAX_SESSION_AGE_SECONDS as CAP,
        MAX_SESSION_AGE_WARN_SECONDS as WARN,
    )

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    room = mgr.get(sid)
    room.created_monotonic = _time.monotonic() - (CAP - WARN + 1)
    real_warn = mgr._warn_presenter_max_age

    async def presenter_leaves_first(r, *, seconds):
        r.presenter = None  # detached between the GC tick and the send
        await real_warn(r, seconds=seconds)

    mgr._warn_presenter_max_age = presenter_leaves_first
    await mgr.gc_step()
    assert room.max_age_warned is False  # so the next attach is warned


async def test_warn_lead_longer_than_the_cap_does_not_warn_a_new_room(monkeypatch):
    from wsi_app import sync_room as sr

    monkeypatch.setattr(sr, "MAX_SESSION_AGE_SECONDS", 200.0)
    monkeypatch.setattr(sr, "MAX_SESSION_AGE_WARN_SECONDS", 300.0)  # misconfigured
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=10)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=10, send=p.send, close=p.close)
    p.sent.clear()
    await mgr.gc_step()
    assert "max_age_warning" not in p.types_sent()  # brand-new room: not warned yet


async def test_discovery_started_is_not_announced_after_the_room_ended(monkeypatch):
    """The discovery gate awaits a DB lookup; an end() landing in that window
    broadcasts ``ended`` first, and announcing ``started`` afterwards would
    leave a dead join banner on every index page."""
    mgr = RoomManager()
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    sub.sent.clear()
    sid = await mgr.create(slide_id=42, owner_user_id=1)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_gate(event):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", slow_gate)
    p = FakeWS()
    attach = asyncio.create_task(mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close))
    await entered.wait()
    assert await mgr.end(sid, reason="ended_by_owner") is True
    release.set()
    await attach
    kinds = [m.get("type") for m in sub.sent]
    assert "started" not in kinds, kinds


# ----- link-only slides: the snapshot follows the gate's verdict -------------

async def test_snapshot_omits_a_room_the_gate_withheld_while_the_filter_ran(monkeypatch):
    """A room announced while the snapshot filter's query was in flight is
    not judged by it. Including it regardless would assume a presenter can
    only be on an announceable slide; a link-only slide is presentable but
    must not be announced, so the gate's verdict decides."""
    async def gate(event):
        return event["slide_id"] != 2  # slide 2 is link-only

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    before = await _announced_room(mgr, slide_id=1, owner=1)  # so the filter runs
    during = {}

    async def filt(sessions):
        during["hidden"] = await _announced_room(mgr, slide_id=2, owner=2)
        during["listed"] = await _announced_room(mgr, slide_id=3, owner=3)
        return sessions

    sub = FakeWS()
    assert await mgr.add_discovery_subscriber("s", sub.send, snapshot_filter=filt)
    assert sorted(s["sid"] for s in sub.sent[0]["sessions"]) == sorted([before, during["listed"]])
    assert during["hidden"] not in str(sub.sent)


async def test_refused_slide_change_withdraws_only_a_shown_room(monkeypatch):
    async def gate(event):
        return event["slide_id"] not in (2, 5)  # link-only slides

    async def filt(sessions):  # the router's snapshot filter, same rule
        return [s for s in sessions if s["slide_id"] not in (2, 5)]

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    shown = await _announced_room(mgr, slide_id=1, owner=1)
    never_shown = await _announced_room(mgr, slide_id=2, owner=2)
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send, snapshot_filter=filt)
    assert [s["sid"] for s in sub.sent[0]["sessions"]] == [shown]
    frame = {"type": "view", "bounds": [0.0, 0.0, 1.0, 1.0]}
    await mgr.broadcast_view(shown, {**frame, "slide_id": 2})
    # A session that was never listed must not have its sid handed out by a
    # withdrawal when it moves between link-only slides.
    await mgr.broadcast_view(never_shown, {**frame, "slide_id": 5})
    events = [m for m in sub.sent if m["type"] != "snapshot"]
    assert events == [{"type": "ended", "sid": shown}]
    # Back onto a listed slide: announced again.
    await mgr.broadcast_view(shown, {**frame, "slide_id": 3})
    assert sub.sent[-1]["type"] == "slide_changed" and sub.sent[-1]["sid"] == shown


async def test_ending_a_never_shown_room_does_not_name_it(monkeypatch):
    """Sending ``ended`` for any announced room on teardown would hand the sid
    of a session kept on link-only slides to every index page. Only a room
    that was shown (and not withdrawn since) gets one."""
    async def gate(event):
        return event["slide_id"] != 2  # link-only

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    never_shown = await _announced_room(mgr, slide_id=2, owner=1)
    withdrawn = await _announced_room(mgr, slide_id=1, owner=2)
    shown = await _announced_room(mgr, slide_id=3, owner=3)
    frame = {"type": "view", "bounds": [0.0, 0.0, 1.0, 1.0]}
    await mgr.broadcast_view(withdrawn, {**frame, "slide_id": 2})
    sub.sent.clear()
    for sid in (never_shown, withdrawn, shown):
        assert await mgr.end(sid) is True
    # ``withdrawn`` already got its ``ended`` from the refused slide change.
    assert sub.sent == [{"type": "ended", "sid": shown}]


# ----- the audience privacy hook fails closed --------------------------------

async def test_audience_withholds_addresses_when_the_privacy_check_fails(monkeypatch):
    def broken():
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(_sync_room_module, "audience_ip_pseudonymiser", broken)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p, f = FakeWS(), FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    await mgr.attach_follower(
        sid, ip="203.0.113.5", ua="Firefox", name="x", hostname="pc1.example",
        send=f.send, close=f.close,
    )
    frame = [m for m in p.sent if m.get("type") == "audience"][-1]
    assert frame["pseudonymous"] is True
    entry = frame["followers"][0]
    assert entry["ip"] == "" and entry["ua"] == "" and entry["hostname"] is None


# ----- Names, shared frames, discovery gate, teardown, takeover, reconnect ----

async def _until(pred, *, timeout: float = 2.0) -> None:
    """Let the loop run until ``pred()`` holds (or fail after ``timeout``),
    rather than a fixed number of turns, which holds only while every step
    happens to start eagerly."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition never held"
        await asyncio.sleep(0.001)

def test_sanitize_name_keeps_wide_spaces_and_joiners():
    """isprintable() would drop every space but ASCII's and every format
    character: an ideographic space would vanish from "山田\u3000太郎" and joined emoji
    would fall apart. Bidi overrides still go."""
    assert sanitize_name("山田\u3000太郎") == "山田\u3000太郎"
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    assert sanitize_name(family) == family
    assert sanitize_name("abc\u202edef") == "abcdef"
    assert sanitize_name("a\u2028b") == "a b"


def test_sanitize_name_of_only_invisible_characters_is_none():
    """The join dialog applies the same rule, so a name it accepts is never
    empty here (a required name would then 4400 a follower who had typed one)."""
    assert sanitize_name("\u200b\u200b") is None
    assert sanitize_name("\u3000\u200d") is None


def test_checked_timing_replaces_out_of_range_values(caplog):
    from wsi_app.sync_room import _checked_timing

    assert _checked_timing("X", 3.0, 1.0) == 3.0
    assert _checked_timing("X", -1.0, 1.0) == 1.0
    assert _checked_timing("X", 0.0, 1.0, strict=True) == 1.0
    assert _checked_timing("X", float("nan"), 1.0) == 1.0
    assert "out of range" in caplog.text


async def test_shared_frame_is_encoded_once():
    from wsi_app.sync_room import SharedFrame

    frame = SharedFrame({"type": "view", "x": "あ"})
    text = frame.encoded()
    assert text == '{"type":"view","x":"あ"}'
    assert frame.encoded() is text


async def test_fan_out_hands_every_follower_the_same_shared_frame():
    from wsi_app.sync_room import SharedFrame

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    a, b = FakeWS(), FakeWS()
    for ws in (a, b):
        await mgr.attach_follower(sid, ip="1", ua="", name=None, hostname=None,
                                  send=ws.send, close=ws.close)
    await mgr.broadcast_cursor(sid, {"type": "cursor", "x": 1.0, "y": 2.0, "visible": True})
    fa = [m for m in a.sent if m.get("type") == "cursor"][-1]
    fb = [m for m in b.sent if m.get("type") == "cursor"][-1]
    assert isinstance(fa, SharedFrame) and fa is fb


async def test_stale_discovery_verdict_does_not_overwrite_a_newer_one(monkeypatch):
    """A slide change whose gate answers late must not undo a later change's
    verdict: the room ended on a link-only slide with ``discovery_shown`` True
    and a banner for the earlier slide on every index page."""
    release = asyncio.Event()

    async def gate(event):
        if event["slide_id"] == 2:
            await release.wait()  # the slow lookup
            return True
        return event["slide_id"] != 3  # slide 3 is link-only

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    room = mgr.get(sid)
    assert room.discovery_shown is True
    sub.sent.clear()

    # Bounded: the slow gate must not hold the slide changes up (if it did,
    # this would wait on ``release`` below forever instead of failing).
    await asyncio.wait_for(mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "slide_public_id": "p2"}), 1)
    await asyncio.wait_for(mgr.broadcast_view(sid, {"type": "view", "slide_id": 3, "slide_public_id": "p3"}), 1)
    release.set()
    # Until the slow gate's task has finished too: the stale verdict is
    # applied (or, rightly, dropped) only then, after the newer one went out.
    await _until(lambda: not mgr._bg_tasks)
    await _until(lambda: [m["type"] for m in sub.sent] == ["ended"]
                 and any(m.get("code") == "unlisted_announced" for m in p.sent))

    assert room.slide_id == 3
    assert room.discovery_shown is False
    assert [m["type"] for m in sub.sent] == ["ended"]
    # And the presenter is told the sid it moved onto a link-only slide was out.
    assert any(m.get("code") == "unlisted_announced" for m in p.sent)


async def test_failed_gate_does_not_warn_the_presenter(monkeypatch):
    calls = {"n": 0}

    async def gate(event):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db down")
        return True

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "slide_public_id": "p2"})
    assert not any(m.get("code") == "unlisted_announced" for m in p.sent)


async def test_discovery_events_and_snapshot_carry_gate_labels(monkeypatch):
    async def gate(event):
        return {"slide_title": f"T{event['slide_id']}", "slide_catalog_id": "C"}

    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", gate)
    mgr = RoomManager()
    sub = FakeWS()
    await mgr.add_discovery_subscriber("s", sub.send)
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    started = [m for m in sub.sent if m.get("type") == "started"][-1]
    assert started["slide_title"] == "T1" and started["sid"] == sid
    # A later subscriber whose filter never judged the room (no filter here)
    # gets the labels recorded with the verdict.
    sub2 = FakeWS()
    await mgr.add_discovery_subscriber("s2", sub2.send)
    snap = [m for m in sub2.sent if m.get("type") == "snapshot"][-1]
    assert snap["sessions"][0]["slide_title"] == "T1"


async def test_cancelled_end_still_tears_the_room_down():
    """The room is out of the table before its teardown awaits anything; a
    cancelled caller must not leave its followers with no ``end`` at all."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    gate = asyncio.Event()
    f = FakeWS()

    async def slow_send(msg):
        if msg.get("type") == "end":
            await gate.wait()
        f.sent.append(msg)

    await mgr.attach_follower(sid, ip="1", ua="", name=None, hostname=None,
                              send=slow_send, close=f.close)
    task = asyncio.ensure_future(mgr.end(sid))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr.get(sid) is None
    gate.set()
    await _until(lambda: bool(f.closed))
    assert "end" in f.types_sent()
    assert f.closed


async def test_gc_tears_reaped_rooms_down_side_by_side():
    """One room's wedged close must not hold up another room's teardown."""
    import time as _time

    mgr = RoomManager()
    stuck_sid = await mgr.create(slide_id=1, owner_user_id=1)
    ok_sid = await mgr.create(slide_id=2, owner_user_id=2)
    never = asyncio.Event()
    stuck, ok = FakeWS(), FakeWS()

    async def wedged_close(code, reason):
        await never.wait()

    await mgr.attach_follower(stuck_sid, ip="1", ua="", name=None, hostname=None,
                              send=stuck.send, close=wedged_close)
    await mgr.attach_follower(ok_sid, ip="1", ua="", name=None, hostname=None,
                              send=ok.send, close=ok.close)
    gc = asyncio.ensure_future(mgr.gc_step(now=_time.monotonic() + PRESENTER_GRACE_SECONDS + 1))
    await _until(lambda: bool(ok.closed))
    assert "end" in ok.types_sent()
    assert not gc.done()
    never.set()
    assert set(await gc) == {stuck_sid, ok_sid}


async def test_takeover_does_not_wait_on_the_evicted_close():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    never = asyncio.Event()
    old, new = FakeWS(), FakeWS()

    async def wedged_close(code, reason):
        old.closed.append((code, reason))
        await never.wait()

    await mgr.attach_presenter(sid, user_id=1, send=old.send, close=wedged_close)
    await asyncio.wait_for(
        mgr.attach_presenter(sid, user_id=1, send=new.send, close=new.close),
        timeout=1,
    )
    assert old.closed == [(4000, "presenter replaced")]
    assert "audience" in new.types_sent()
    never.set()


async def test_automatic_reconnect_keeps_the_idle_clock():
    """A client stuck reconnecting would reset the inactivity clock on every attach
    (and with its replayed frames), so only the age cap would ever end it."""
    import time as _time
    from wsi_app.sync_room import PRESENTER_IDLE_SECONDS as IDLE

    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close, client_id="p-a")
    room = mgr.get(sid)
    stale = _time.monotonic() - (IDLE - 30)
    room.presenter_last_activity = stale

    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close,
                               client_id="p-a", resume=True)
    assert room.presenter_last_activity == stale
    # The reconnect's replayed view and pointer are relayed but not activity.
    assert await mgr.broadcast_view(
        sid, {"type": "view", "slide_id": 1}, activity=False,
    )
    assert await mgr.broadcast_cursor(
        sid, {"type": "cursor", "x": 0.0, "y": 0.0, "visible": False}, activity=False,
    )
    assert room.presenter_last_activity == stale
    # A deliberate attach (Resume) is the presenter acting.
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close, client_id="p-b")
    assert room.presenter_last_activity > stale


async def test_superseded_followers_are_pointed_at_the_new_session():
    mgr = RoomManager()
    old = await mgr.create(slide_id=1, owner_user_id=1)
    f = FakeWS()
    await mgr.attach_follower(old, ip="1", ua="", name=None, hostname=None,
                              send=f.send, close=f.close)
    new, superseded = await mgr.create_superseding(
        slide_id=2, owner_user_id=1, slide_public_id="pub2", announce_successor=True,
    )
    assert superseded == [old]
    end = [m for m in f.sent if m.get("type") == "end"][-1]
    assert end["reason"] == "superseded"
    assert end["next"] == {"sid": new, "slide_public_id": "pub2"}


async def test_superseded_end_names_no_successor_unless_asked():
    mgr = RoomManager()
    old = await mgr.create(slide_id=1, owner_user_id=1)
    f = FakeWS()
    await mgr.attach_follower(old, ip="1", ua="", name=None, hostname=None,
                              send=f.send, close=f.close)
    await mgr.create_superseding(slide_id=2, owner_user_id=1, slide_public_id="pub2")
    end = [m for m in f.sent if m.get("type") == "end"][-1]
    assert "next" not in end


async def test_refresh_slide_status_tells_both_ends_and_the_index_pages():
    """A slide unpublished (or made link-only) while it is being presented
    must change things for the room: otherwise the followers would sit on
    404ing tiles and the index pages keep the banner."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=7, owner_user_id=1, slide_public_id="pub7")
    p, f, sub = FakeWS(), FakeWS(), FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    await mgr.attach_follower(sid, ip="1", ua="", name=None, hostname=None,
                              send=f.send, close=f.close)
    await mgr.add_discovery_subscriber("s", sub.send)
    sub.sent.clear()

    await mgr.refresh_slide_status(sid, 7, published=False, listed=False)
    assert {"type": "notice", "code": "slide_unavailable"} in f.sent
    assert {"type": "notice", "code": "slide_unpublished"} in p.sent
    assert sub.sent == [{"type": "ended", "sid": sid}]
    # Unchanged status: nothing more.
    f.sent.clear(); sub.sent.clear()
    await mgr.refresh_slide_status(sid, 7, published=False, listed=False)
    assert f.sent == [] and sub.sent == []
    # Published and listed again: followers told, banner back.
    await mgr.refresh_slide_status(sid, 7, published=True, listed=True,
                                   labels={"slide_title": "T"})
    assert {"type": "notice", "code": "slide_available"} in f.sent
    assert sub.sent and sub.sent[-1]["type"] == "slide_changed"
    assert sub.sent[-1]["slide_title"] == "T"
    # Made link-only while announced: banner down, presenter warned.
    p.sent.clear()
    await mgr.refresh_slide_status(sid, 7, published=True, listed=False)
    assert sub.sent[-1] == {"type": "ended", "sid": sid}
    await _until(lambda: any(m.get("code") == "unlisted_announced" for m in p.sent))


async def test_refresh_slide_status_ignores_a_room_that_moved_on():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=7, owner_user_id=1)
    f = FakeWS()
    await mgr.attach_follower(sid, ip="1", ua="", name=None, hostname=None,
                              send=f.send, close=f.close)
    await mgr.refresh_slide_status(sid, 8, published=False, listed=False)
    assert f.sent == [] and mgr.get(sid).slide_unavailable is False


async def test_late_hello_fills_in_an_anonymous_follower_once():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    f = FakeWS()
    _room, meta = await mgr.attach_follower(
        sid, ip="1", ua="", name=None, hostname=None, send=f.send, close=f.close,
        hello_received=False,
    )
    assert await mgr.update_follower_hello(
        sid, meta.id, name="Late", visible=False, vw=800, vh=600,
    ) is True
    assert (meta.name, meta.visible, meta.vw, meta.vh) == ("Late", False, 800, 600)
    # Only once: no renaming mid-session.
    assert await mgr.update_follower_hello(sid, meta.id, name="Other", visible=True) is False
    assert meta.name == "Late"


async def test_hello_cannot_rename_a_follower_that_joined_on_its_hello():
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    f = FakeWS()
    _room, meta = await mgr.attach_follower(
        sid, ip="1", ua="", name="Alice", hostname=None, send=f.send, close=f.close,
    )
    assert await mgr.update_follower_hello(sid, meta.id, name="Bob", visible=True) is False
    assert meta.name == "Alice"


def test_sanitize_name_cuts_before_judging_visibility():
    """Leading joiners can push the one visible character past the 64 cut: a
    check passed on the whole string would let an invisible name come out."""
    assert sanitize_name("\u200d" * 64 + "a") is None
    assert sanitize_name("a" + "\u200d" * 100) == "a" + "\u200d" * 63


def test_sanitize_name_keeps_code_points_newer_than_this_python():
    """Unassigned to this interpreter's Unicode tables may be an emoji the
    browser knows (U+1FAE9 is new in Unicode 16); dropping it would empty a
    name the join dialog had accepted."""
    assert sanitize_name("\U0001FAE9") == "\U0001FAE9"
    assert sanitize_name("\ue000x") == "x"  # private use still goes


async def test_a_failed_teardown_is_logged(caplog):
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)

    async def broken(room, reason, *, extra=None):
        raise RuntimeError("boom")

    mgr._teardown_room = broken
    with caplog.at_level("ERROR", logger="wsi_app.sync"):
        assert await mgr.end(sid) is True
    assert "tearing down room" in caplog.text and "boom" in caplog.text


async def test_a_failed_teardown_is_logged_even_if_the_caller_was_cancelled(caplog):
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    release = asyncio.Event()

    async def broken(room, reason, *, extra=None):
        await release.wait()
        raise RuntimeError("late boom")

    mgr._teardown_room = broken
    with caplog.at_level("ERROR", logger="wsi_app.sync"):
        task = asyncio.ensure_future(mgr.end(sid))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await _until(lambda: "late boom" in caplog.text)



# ----- Slow discovery gate, repeated unpublish notices, notice delay -----------

def _never_answering_gate():
    async def gate(event):
        await asyncio.Event().wait()
    return gate


async def test_a_slow_discovery_gate_does_not_hold_up_a_slide_change(monkeypatch):
    """The gate is a DB lookup; awaited inline it would hold up the
    presenter's receive loop (and the next frames) for as long as it took."""
    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", _never_answering_gate())
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    await asyncio.wait_for(mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close), 1)
    assert await asyncio.wait_for(
        mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "slide_public_id": "p2"}), 1,
    )
    for t in list(mgr._bg_tasks):
        t.cancel()


async def test_a_slow_discovery_gate_does_not_hold_up_the_first_attach(monkeypatch):
    monkeypatch.setattr(_sync_room_module, "discovery_event_gate", _never_answering_gate())
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1)
    p = FakeWS()
    assert await asyncio.wait_for(
        mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close), 1,
    ) is not None
    assert "audience" in p.types_sent()
    for t in list(mgr._bg_tasks):
        t.cancel()


async def test_each_unpublished_slide_is_announced_to_followers():
    """Moving on clears the unavailable state: the next slide unpublished
    under the session is announced again."""
    mgr = RoomManager()
    sid = await mgr.create(slide_id=1, owner_user_id=1, slide_public_id="p1")
    p, f = FakeWS(), FakeWS()
    await mgr.attach_presenter(sid, user_id=1, send=p.send, close=p.close)
    await mgr.attach_follower(sid, ip="1", ua="", name=None, hostname=None,
                              send=f.send, close=f.close)
    await mgr.refresh_slide_status(sid, 1, published=False, listed=False)
    await mgr.broadcast_view(sid, {"type": "view", "slide_id": 2, "slide_public_id": "p2"})
    f.sent.clear()
    await mgr.refresh_slide_status(sid, 2, published=False, listed=False)
    assert {"type": "notice", "code": "slide_unavailable"} in f.sent


def test_a_notice_delay_past_the_grace_is_halved(caplog):
    from wsi_app.sync_room import _checked_notify_delay

    assert _checked_notify_delay(5.0, 45.0) == 5.0
    assert _checked_notify_delay(60.0, 45.0) == 22.5
    assert "not below" in caplog.text
    assert _checked_notify_delay(60.0, 0.0) == 60.0  # no grace: nothing to keep inside
