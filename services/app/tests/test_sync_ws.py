"""WebSocket endpoint tests for the live-sync router.

Uses ``starlette.testclient.WebSocketTestSession`` (returned by
``client.websocket_connect``) which is a synchronous context manager but
runs the server inside the same process, so async manager state (rooms,
followers) is shared between the test and the FastAPI handler.
"""
from __future__ import annotations


import json

import anyio.from_thread
import pytest

from contextlib import contextmanager

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wsi_app import app_auth as _app_auth
from wsi_app.auth import create_token
from wsi_app.main import app
from wsi_app import sync_room as _sync_room


def live_manager():
    """Resolve the RoomManager at call time.

    ``reset_room_manager`` installs a fresh manager per test, so a
    module-level ``from ... import manager`` would pin the instance that
    existed at import and silently diverge from the one the endpoints use.
    """
    return _sync_room.manager


@pytest.fixture(autouse=True)
def _publish_slide(slide, db_session):
    """Publish the shared ``slide`` fixture so session creation succeeds.

    Live-sync session creation refuses unpublished slides (403); these WS
    tests open a session against the shared fixture, which defaults to a draft.
    """
    slide.is_published = True
    db_session.commit()


@pytest.fixture
def client():
    """A TestClient whose sockets and requests all run on ONE event loop.

    Production serves every socket of a room from the sync worker's single
    loop. A TestClient that is not entered instead starts a new blocking
    portal (a thread with its own loop) for each ``websocket_connect``, so a
    presenter frame fanned out to a follower would wake the follower's writer
    (``_WsBridge._wake``, an asyncio.Event of the other loop) from the wrong
    thread: the wakeup would sit unnoticed until that loop's next timer — the 15 s
    keepalive tick — and two such stalls in one test would close the silent
    follower with "ping timeout". One shared portal (what entering the client
    does, minus the lifespan run) restores the production threading model.
    """
    c = TestClient(app)
    with anyio.from_thread.start_blocking_portal(**c.async_backend) as portal:
        c.portal = portal
        try:
            yield c
        finally:
            c.portal = None


# ----------------------------------------------------- helpers

def _open_session(client, admin_headers, slide_id: int) -> str:
    return client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide_id},
        headers=admin_headers,
    ).json()["session_id"]


def _drain_until(ws, predicate, *, max_msgs: int = 10):
    """Receive frames until ``predicate(msg)`` is true or limit reached.

    Returns the matching frame, or raises AssertionError.
    """
    for _ in range(max_msgs):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("predicate never matched within drain limit")


# ----------------------------------------------------- WS auth/route

def test_ws_unknown_session_closes(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/sync/ws/no-such-sid") as ws:
            ws.receive_json()  # closed by server


def test_ws_unknown_session_delivers_4404_code(client):
    """The handler must accept() before close(code=4404) so the application-
    private close code survives the wire — otherwise the browser sees code
    1006 on a rejected handshake and the client's terminal-state branch
    (sync.ts: stop reconnecting on 4404) never engages, producing an
    infinite reconnect storm against a permanently-gone session."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/sync/ws/no-such-sid") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4404


def test_follower_cannot_join_after_the_slide_is_unpublished(
    client, admin_headers, admin_user, slide, db_session
):
    """Unpublishing mid-session must stop admitting new followers.

    The publish gate runs at session creation and on slide-*change* frames, so
    a slide pulled from view while its session runs would otherwise stay
    joinable until the room aged out (MAX_SESSION_AGE, 1.5 h). Refused with
    the retryable 1013, not a final 4404: the socket may be a follower of the
    session reconnecting, which must be let back in once the presenter is on a published slide again
    (a new joiner is turned away by the peek's 404 before it gets here).
    """
    from starlette.websockets import WebSocketDisconnect

    sid = _open_session(client, admin_headers, slide.id)
    slide.is_published = False
    db_session.commit()

    with client.websocket_connect(f"/api/sync/ws/{sid}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013
    assert client.get(f"/api/sync/sessions/{sid}").status_code == 404

    # The presenter is exempt: they still need to attach to stop the session.
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        assert _drain_until(ws, lambda m: m.get("type") == "audience") is not None

    slide.is_published = True
    db_session.commit()


def test_presenter_attach_and_audience_snapshot(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as ws:
        ws.send_json({"type": "hello", "token": token})
        # First non-ping frame should be an empty audience snapshot.
        msg = _drain_until(ws, lambda m: m.get("type") == "audience")
        assert msg["count"] == 0
        assert msg["followers"] == []


def test_owned_sessions_flag_an_attached_presenter(
    client, admin_headers, admin_user, slide
):
    """The owned-sessions probe (which backs the launcher's Resume button) also
    lists a session whose presenter socket is still attached, flagged. After a
    network drop or a crashed tab the server holds the dead socket until the
    keepalive notices; hiding the session meanwhile would leave the presenter
    only Start, which supersedes it and ends it for every follower. A Resume (a
    fresh attach) takes the session over and evicts the old socket instead."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as old:
        old.send_json({"type": "hello", "token": token, "client_id": "p-old"})
        _drain_until(old, lambda m: m.get("type") == "attached")
        owned = client.get("/api/admin/sync/sessions", headers=admin_headers).json()
        entry = {s["session_id"]: s for s in owned["sessions"]}[sid]
        assert entry["presenter_connected"] is True
        # The reloaded page Resumes: a fresh attach under a new client id.
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as new:
            new.send_json({"type": "hello", "token": token, "client_id": "p-new"})
            _drain_until(new, lambda m: m.get("type") == "attached")
            with pytest.raises(WebSocketDisconnect) as exc:
                _drain_until(old, lambda m: False)
            assert exc.value.code == 4000
            assert live_manager().get(sid).presenter_client_id == "p-new"
    # After detach the room lingers (grace) and is listed as not attached.
    owned_after = client.get("/api/admin/sync/sessions", headers=admin_headers).json()
    entry = {s["session_id"]: s for s in owned_after["sessions"]}[sid]
    assert entry["presenter_connected"] is False


def test_presenter_attach_rejects_bad_token(
    client, admin_headers, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/sync/ws/{sid}?role=presenter"
        ) as ws:
            ws.send_json({"type": "hello", "token": "not-a-jwt"})
            ws.receive_json()


def test_presenter_hello_non_object_closes_cleanly(
    client, admin_headers, slide
):
    """A well-formed but non-object JSON hello (e.g. a list) must close with
    the application 4400 code, not crash the handler with an AttributeError
    on ``.get`` (which would surface as a 1011/1006 and trip the client's
    reconnect storm). Mirrors the follower path's isinstance guard."""
    from starlette.websockets import WebSocketDisconnect

    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as ws:
        ws.send_json(["hello", "token"])
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_presenter_hello_nested_too_deeply_closes_cleanly(
    client, admin_headers, slide
):
    """A hello nested past the JSON decoder's recursion limit (a few KB of
    ``[``, far under MAX_WS_FRAME_BYTES) must be treated as a malformed hello
    and closed with 4400, not escape the handler as a RecursionError."""
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as ws:
        ws.send_text("[" * 20000)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_presenter_attach_rejects_non_owner(
    client, admin_headers, other_admin, slide
):
    """Another staff account's presenter socket gets 4409 (the room exists but
    is not theirs), which the client reports as such, not as a gone session."""
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as ws:
        ws.send_json({"type": "hello", "token": create_token(other_admin)})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4409
    assert live_manager().get(sid).presenter is None


def test_presenter_reauth_revocation_closes_with_4401(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """A presenter whose account is disabled/demoted mid-session is dropped by
    the periodic reauth loop with the 4401 (auth revoked) close code.

    Regression guard for the close path: the revocation close is routed
    through the _WsBridge writer task (``bridge.close``), not a bare
    ``ws.close`` — so it is ordered after any queued frames and can never race
    an in-flight ``send_json`` on the same socket.
    """
    import wsi_app.routers.sync as sync_mod

    # Tick the reauth loop fast so the revocation is observed within the test
    # window (default 60 s). The timeout is read fresh each iteration, so
    # patching before connect makes every tick fast.
    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_INTERVAL_SECONDS", 0.05)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        # Attach succeeded with the real resolver; drain the initial snapshot.
        _drain_until(ws, lambda m: m.get("type") == "audience")
        # Simulate the account being disabled/demoted: the next reauth check
        # resolves to "no longer an active presenter".
        monkeypatch.setattr(
            sync_mod, "_resolve_active_presenter", lambda _token: None
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(20):
                ws.receive_json()
        assert exc.value.code == 4401


def test_presenter_reauth_fails_closed_after_persistent_failures(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """A *persistent* reauth DB failure must fail closed: after
    PRESENTER_REAUTH_MAX_FAILURES consecutive raises the presenter is dropped
    rather than driving on a possibly-revoked JWT until its 24h TTL.
    Reauth is the only revocation path between connect and expiry, so a
    permanently-broken check that failed open would leave a demoted/disabled
    presenter live for a full day.

    Dropped with 1013, not 4401: an outage is no verdict on the token, and
    4401 is final for the client, which would strand a valid presenter until
    a reload. On 1013 it reconnects, and its hello is checked afresh (1013
    again while the DB is still down; see the hello test below).
    """
    import wsi_app.routers.sync as sync_mod

    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_MAX_FAILURES", 3)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        # Attach succeeded with the real resolver; drain the initial snapshot.
        _drain_until(ws, lambda m: m.get("type") == "audience")

        # Every subsequent reauth check now raises (persistent DB outage).
        def _boom(_token):
            raise RuntimeError("db down")

        monkeypatch.setattr(sync_mod, "_resolve_active_presenter", _boom)
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(50):
                ws.receive_json()
        assert exc.value.code == 1013


def test_presenter_reauth_tolerates_intermittent_failures(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """A transient reauth failure must NOT drop the presenter: the consecutive-
    failure streak resets on any check that completes. Even alternating
    fail/success indefinitely (never MAX_FAILURES *consecutive*) keeps the
    session live — proving the streak resets rather than accumulating totals.
    """
    import time as _time

    import wsi_app.routers.sync as sync_mod

    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_MAX_FAILURES", 3)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        _drain_until(ws, lambda m: m.get("type") == "audience")

        # Patch only after the initial attach so the handshake uses the real
        # resolver. Reauth then alternates fail/success: max 1 consecutive
        # failure, so a correct (streak-resetting) loop never closes. A broken
        # loop that summed total failures would cross MAX_FAILURES=3 and close.
        real = sync_mod._resolve_active_presenter
        state = {"n": 0}

        def _flaky(tok):
            state["n"] += 1
            if state["n"] % 2 == 1:
                raise RuntimeError("transient db blip")
            return real(tok)

        monkeypatch.setattr(sync_mod, "_resolve_active_presenter", _flaky)
        # The TestClient runs the app on a background event loop, so the reauth
        # task ticks in real time. Let several ticks elapse (>=5 checks => >=3
        # total failures, which would trip a broken total-based threshold).
        deadline = _time.monotonic() + 3.0
        while state["n"] < 5 and _time.monotonic() < deadline:
            _time.sleep(0.05)
        assert state["n"] >= 5, "reauth loop did not tick enough for a meaningful test"
        # Socket is still open: a ping round-trips.
        ws.send_json({"type": "ping"})
        pong = _drain_until(ws, lambda m: m.get("type") == "pong", max_msgs=20)
        assert pong["type"] == "pong"


def test_presenter_socket_closes_once_an_ip_rule_denies_it(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """The staff JWT exempts the presenter from the app-access gate, not from
    the IP filter (HTTP answers a denied address with 403 whatever the
    bearer). A deny rule added mid-session must drop the presenter socket with
    4403 like the follower's, instead of letting it drive the room until the
    session's maximum age."""
    import wsi_app.routers.sync as sync_mod

    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_INTERVAL_SECONDS", 0.05)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    seen = []
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        _drain_until(ws, lambda m: m.get("type") == "attached")

        def deny(ip):
            seen.append(ip)
            return False

        monkeypatch.setattr(sync_mod._net, "ip_allowed", deny)
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(20):
                ws.receive_json()
        assert exc.value.code == 4403
    # Judged on the same resolved client IP the follower re-check uses.
    assert seen and seen[0] == "testclient"
    assert live_manager().get(sid).presenter is None


# ------- staff presenters are admitted by their hello token, not app access
#
# A browser cannot put the staff JWT in an Authorization header on a WebSocket
# upgrade; SyncPresenter sends it in the hello frame. A guard demanding app
# access before that frame is read would, under ldap / google_oauth (or basic
# without cached credentials), give every staff presenter without an app
# session of its own 4401 on a session the dashboard had just created.

@pytest.mark.parametrize("mode", ["ldap", "google_oauth", "basic"])
def test_staff_presenter_needs_no_app_session(
    client, manager_user, manager_headers, slide, monkeypatch, mode
):
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode=mode))
    r = client.post(
        "/api/admin/sync/sessions", json={"slide_id": slide.id}, headers=manager_headers,
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": create_token(manager_user)})
        assert _drain_until(ws, lambda m: m.get("type") == "attached")


@pytest.mark.parametrize("hello", [
    {"type": "hello", "token": "not-a-jwt"},
    {"type": "hello"},
])
def test_presenter_without_a_staff_token_learns_nothing_about_the_room(
    client, admin_headers, slide, monkeypatch, hello
):
    """With no app session, a presenter socket is let past the gate only to
    have its hello checked, so the refusal must not depend on the room: a live
    sid and an unknown one both close 4401, never 4404."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap"))
    for target in (sid, "no-such-sid"):
        with client.websocket_connect(f"/api/sync/ws/{target}?role=presenter") as ws:
            ws.send_json(hello)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401
    assert live_manager().get(sid).presenter is None


def test_presenter_with_a_staff_token_still_gets_4404_for_a_gone_room(
    client, admin_user, monkeypatch
):
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap"))
    with client.websocket_connect("/api/sync/ws/no-such-sid?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": create_token(admin_user)})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4404


def test_presenter_hello_does_not_lift_the_ip_filter_or_a_bad_cf_assertion(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """Both refusals come before the hello is even read."""
    import wsi_app.routers.sync as sync_mod
    from wsi_app import cf_access as _cf_access

    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap"))
    token = create_token(admin_user)

    monkeypatch.setattr(sync_mod._net, "ip_allowed", lambda ip: False)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403
    monkeypatch.undo()

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap"))
    monkeypatch.setattr(
        _cf_access, "cf_jwt_status", lambda headers, client_host, **_kw: ("invalid", None)
    )
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter", headers={_cf_access.CF_JWT_HEADER: "x.y.z"},
    ) as ws:
        ws.send_json({"type": "hello", "token": token})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401
    assert live_manager().get(sid).presenter is None


def test_follower_still_needs_app_access(client, admin_headers, admin_user, slide, monkeypatch):
    """Only the presenter role defers to its hello: a follower whose hello
    carries a staff token is still refused without app access."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap"))
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as ws:
        ws.send_json({"type": "hello", "token": create_token(admin_user)})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


# ------------------- a peer gone before / while reading never spins the loop

class _GoneBeforeHelloWs:
    """A socket whose peer disconnected before sending anything: the first
    receive() yields the disconnect message, every later one raises the
    RuntimeError Starlette raises once a disconnect has been handed out."""

    headers: dict = {}
    client = None

    def __init__(self) -> None:
        self.receives = 0
        self.closed = []

    async def receive(self):
        self.receives += 1
        if self.receives == 1:
            return {"type": "websocket.disconnect", "code": 1001}
        raise RuntimeError('Cannot call "receive" once a disconnect message has been received.')

    async def send_json(self, msg):
        pass

    async def close(self, code=1000, reason=None):
        self.closed.append(code)


@pytest.mark.parametrize("role", ["follower", "presenter"])
def test_peer_gone_before_hello_is_not_attached(role):
    """A follower that disconnects before its hello must not be attached as an
    anonymous ghost (its audience slot held until the keepalive reaped it),
    with the recv loop spinning on the RuntimeError meanwhile."""
    import asyncio

    import wsi_app.routers.sync as sync_mod

    async def run():
        mgr = live_manager()
        sid = await mgr.create(slide_id=1, owner_user_id=1)
        ws = _GoneBeforeHelloWs()
        bridge = sync_mod._WsBridge(ws)
        bridge.start()
        try:
            run_role = sync_mod._run_follower if role == "follower" else sync_mod._run_presenter
            await asyncio.wait_for(run_role(ws, bridge, sid), timeout=5)
        finally:
            await bridge.aclose()
        return mgr.get(sid), ws

    room, ws = asyncio.run(run())
    assert room.followers == {} and room.presenter is None
    assert ws.receives == 1
    # Nothing to tell a peer that is already gone (no 4400 "missing hello").
    assert ws.closed in ([], [1000])


class _PresenterHelloWs:
    """A presenter socket that sends one hello; ``before_hello`` runs inside
    the first receive(), i.e. after the socket connected and before its first
    message arrives."""

    headers: dict = {}
    client = None

    def __init__(self, before_hello=None) -> None:
        self.before_hello = before_hello
        self.closed = []

    async def receive(self):
        if self.before_hello is not None:
            hook, self.before_hello = self.before_hello, None
            await hook()
        return {"type": "websocket.receive", "text": json.dumps({"type": "hello", "token": "tok"})}

    async def send_json(self, msg):
        pass

    async def close(self, code=1000, reason=None):
        self.closed.append(code)


@pytest.mark.parametrize("case, expected", [
    # The room was ended (or reaped) while the presenter's hello was on its
    # way: the session is over, which the client must word as "session ended".
    # Not 4409, which the presenter reads as an expired sign-in.
    ("ended", 4404),
    # The room is still live but owned by another staff account.
    ("other_owner", 4409),
])
def test_presenter_attach_refusal_names_its_cause(monkeypatch, case, expected):
    import asyncio

    import wsi_app.routers.sync as sync_mod

    owner = 1
    monkeypatch.setattr(
        sync_mod, "_resolve_active_presenter",
        lambda token: owner if case == "ended" else owner + 1,
    )

    async def run():
        mgr = live_manager()
        sid = await mgr.create(slide_id=1, owner_user_id=owner)

        async def end_room():
            await mgr.end(sid)

        ws = _PresenterHelloWs(before_hello=end_room if case == "ended" else None)
        bridge = sync_mod._WsBridge(ws)
        bridge.start()
        try:
            await asyncio.wait_for(sync_mod._run_presenter(ws, bridge, sid), timeout=5)
        finally:
            await bridge.aclose()
        return mgr.get(sid), ws

    room, ws = asyncio.run(run())
    assert ws.closed[:1] == [expected]
    if case == "ended":
        assert room is None
    else:
        assert room is not None and room.presenter is None


def _count_recv_calls(monkeypatch, exc: BaseException):
    """Make every receive in the room / discovery loops raise ``exc``; give up
    with a disconnect after 50 calls so a regressed loop cannot hang."""
    import wsi_app.routers.sync as sync_mod
    from starlette.websockets import WebSocketDisconnect as _Disc

    calls = {"n": 0}

    async def fake_recv(ws, bridge, bucket=None):
        calls["n"] += 1
        if calls["n"] >= 50:
            raise _Disc(code=1000)
        raise exc

    monkeypatch.setattr(sync_mod, "_recv_with_bridge_close", fake_recv)
    return calls


def test_room_loop_stops_on_a_transport_error(client, admin_headers, slide, monkeypatch):
    """Starlette's WebSocketDisconnected (a RuntimeError) means the socket is
    gone for good; ``continue`` on it re-raised at once, busy-spinning the
    single sync worker's event loop until the keepalive closed the bridge."""
    sid = _open_session(client, admin_headers, slide.id)
    calls = _count_recv_calls(monkeypatch, RuntimeError("disconnected"))
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as ws:
        ws.send_json({"type": "hello", "name": "x"})
        assert ws.receive_json()["type"] == "attached"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert calls["n"] == 1


def test_room_loop_skips_a_malformed_frame(client, admin_headers, slide, monkeypatch):
    """A bad frame (ValueError: bad JSON, oversized) is still just dropped."""
    sid = _open_session(client, admin_headers, slide.id)
    calls = _count_recv_calls(monkeypatch, ValueError("bad frame"))
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as ws:
        ws.send_json({"type": "hello", "name": "x"})
        assert ws.receive_json()["type"] == "attached"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert calls["n"] == 50


def test_discovery_loop_stops_on_a_transport_error(client, monkeypatch):
    calls = _count_recv_calls(monkeypatch, RuntimeError("disconnected"))
    with client.websocket_connect("/api/sync/discovery") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert calls["n"] == 1


# ------------------------------ raw-IP / LAN-direct WS exemptions
#
# The HTTP gate (main.app_auth_middleware) admits break-glass and direct-LAN-IP
# requests before the mode switch; _ws_guard must mirror that so live sync is
# not silently broken for exactly the documented raw-IP / LAN-direct access
# routes under an otherwise-gated mode.

_LAN_HEADERS = {"host": "192.168.1.50", "x-real-ip": "192.168.1.42"}


def _loopback_client() -> TestClient:
    # request.client.host == 127.0.0.1 so cf_access._is_trusted_hop passes,
    # mirroring the production loopback-peer invariant (see test_access_paths).
    return TestClient(app, client=("127.0.0.1", 12345))


def test_ws_follower_rejected_under_gated_mode_without_exemption(
    client, admin_headers, slide, monkeypatch
):
    """Baseline: a gated mode with no exemption rejects an unauthenticated
    follower WS with 4401 — proving the exemptions below are what admit it."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth")
    )
    lb = _loopback_client()
    with lb.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower", headers=_LAN_HEADERS
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_ws_follower_admitted_via_lan_direct_exemption(
    client, admin_headers, slide, monkeypatch
):
    """The WS guard honours the direct-LAN exemption: at the
    "lan" level, a bare private-IP-host request from a LAN client must open the
    follower WebSocket just as the HTTP gate admits its traffic."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(
        _app_auth,
        "_snapshot",
        _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan"),
    )
    lb = _loopback_client()
    with lb.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower", headers=_LAN_HEADERS
    ) as ws:
        ws.send_json({"type": "hello", "name": "x"})
        # A ping round-trip proves the socket was admitted past _ws_guard:
        # a rejected guard closes with 4401 before any inbound frame is
        # handled, so _drain_until would raise instead.
        ws.send_json({"type": "ping"})
        pong = _drain_until(ws, lambda m: m.get("type") == "pong")
        assert pong["type"] == "pong"


def test_ws_follower_admitted_via_lan_direct_hostname(
    client, admin_headers, slide, monkeypatch
):
    """The WS guard mirrors the HTTP gate at the "lan" level: a LAN-direct
    hostname/vhost request from a LAN client (no tunnel) opens the follower
    WebSocket too, not just a bare private-IP host."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(
        _app_auth,
        "_snapshot",
        _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan"),
    )
    lb = _loopback_client()
    with lb.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower",
        headers={"host": "lan.wsi.example.org", "x-real-ip": "192.168.1.42"},
    ) as ws:
        ws.send_json({"type": "hello", "name": "x"})
        ws.send_json({"type": "ping"})
        pong = _drain_until(ws, lambda m: m.get("type") == "pong")
        assert pong["type"] == "pong"


def test_ws_follower_admitted_via_breakglass_exemption(
    client, admin_headers, slide, monkeypatch
):
    """The WS guard honours the break-glass exemption: with
    WSI_BREAKGLASS_NOAUTH on, a plaintext non-tunnel request opens the
    follower WebSocket under an otherwise-gated mode."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth")
    )
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    lb = _loopback_client()
    with lb.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower",
        headers={"x-forwarded-proto": "http", "x-real-ip": "192.168.1.42"},
    ) as ws:
        ws.send_json({"type": "hello", "name": "x"})
        ws.send_json({"type": "ping"})
        pong = _drain_until(ws, lambda m: m.get("type") == "pong")
        assert pong["type"] == "pong"


def test_ws_breakglass_does_not_admit_a_public_client(
    client, admin_headers, slide, monkeypatch
):
    """The break-glass exemption is a LAN fallback: a plaintext follower
    socket from a public address stays gated (4401) even with the flag on."""
    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="google_oauth")
    )
    monkeypatch.setattr(_app_auth, "BREAKGLASS_NOAUTH", True)
    lb = _loopback_client()
    with lb.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower",
        headers={"x-forwarded-proto": "http", "x-real-ip": "203.0.113.9"},
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_view_fanout_to_followers(client, admin_headers, admin_user, slide):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "Alice"})

        # Drain initial audience frames on the presenter so the next assertion
        # sees one with count == 1.
        snap = _drain_until(
            pres,
            lambda m: m.get("type") == "audience" and m.get("count") == 1,
            max_msgs=20,
        )
        assert snap["followers"][0]["name"] == "Alice"

        pres.send_json({
            "type": "view",
            "slide_id": slide.id,
            "bounds": [0.25, 0.25, 0.5, 0.5],
            "rotation_deg": 0,
        })
        view = _drain_until(fol, lambda m: m.get("type") == "view", max_msgs=20)
        assert view["bounds"] == [0.25, 0.25, 0.5, 0.5]
        assert view["rotation_deg"] == 0


def test_follower_hello_viewport_reaches_presenter(
    client, admin_headers, admin_user, slide
):
    """A follower reporting its viewer-canvas size in the hello frame shows up
    in the presenter's audience panel, so the presenter can tell who is on a
    differently-shaped screen (the follower's fitBounds pads the short axis,
    surrounding the presenter's field with extra margin)."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "iPad", "vw": 1194, "vh": 834})

        snap = _drain_until(
            pres,
            lambda m: m.get("type") == "audience" and m.get("count") == 1,
            max_msgs=20,
        )
        assert snap["followers"][0]["vw"] == 1194
        assert snap["followers"][0]["vh"] == 834


def test_follower_hello_without_viewport_still_joins(
    client, admin_headers, admin_user, slide
):
    """The fields are optional: a hello that omits them joins
    normally and is reported with a null size rather than being rejected."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "Old"})

        snap = _drain_until(
            pres,
            lambda m: m.get("type") == "audience" and m.get("count") == 1,
            max_msgs=20,
        )
        assert snap["followers"][0]["vw"] is None
        assert snap["followers"][0]["vh"] is None


def test_follower_hello_hostile_viewport_is_dropped(
    client, admin_headers, admin_user, slide
):
    """A hand-crafted hello can carry anything. The values must be scrubbed
    before they reach the presenter UI, and must not break the join.

    ``vw`` here is a 401-digit integer: JSON bounds neither the magnitude nor
    the type, and such a payload is only ~440 bytes, so it passes the frame
    cap and reaches the attach path. ``float()`` cannot represent it, so this
    is the case that turns a scrub miss into an unhandled exception rather
    than a merely wrong value."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({
            "type": "hello",
            "name": "Hostile",
            "vw": 10**400,
            "vh": 834,
        })

        snap = _drain_until(
            pres,
            lambda m: m.get("type") == "audience" and m.get("count") == 1,
            max_msgs=20,
        )
        assert snap["followers"][0]["name"] == "Hostile"
        assert snap["followers"][0]["vw"] is None
        assert snap["followers"][0]["vh"] is None


def test_late_follower_receives_last_view(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        pres.send_json({
            "type": "view",
            "slide_id": slide.id,
            "bounds": [0.1, 0.2, 0.3, 0.4],
            "bounds_deg": 270,
            "rotation_deg": 90,
        })
        # Wait briefly for the broadcast to land in last_view.
        # The test client is synchronous; sending a follower-side hello below
        # forces the event loop to flush the prior broadcast.
        with client.websocket_connect(
            f"/api/sync/ws/{sid}?role=follower"
        ) as fol:
            fol.send_json({"type": "hello", "name": "Late"})
            view = _drain_until(fol, lambda m: m.get("type") == "view")
            assert view["bounds"] == [0.1, 0.2, 0.3, 0.4]
            assert view["rotation_deg"] == 90
            # bounds_deg must survive the late-join replay: a follower rebuilds
            # OpenSeadragon.Rect(x, y, w, h, bounds_deg) and would land on the
            # wrong region for a rotated presenter if the server dropped it.
            assert view["bounds_deg"] == 270


def test_visibility_change_updates_audience(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "Bob"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1)

        fol.send_json({"type": "visibility", "visible": False})
        msg = _drain_until(
            pres,
            lambda m: (
                m.get("type") == "audience"
                and m.get("followers")
                and m["followers"][0].get("visible") is False
            ),
            max_msgs=20,
        )
        assert msg["followers"][0]["name"] == "Bob"


def test_follower_disconnect_updates_audience(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)

    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres:
        pres.send_json({"type": "hello", "token": token})
        with client.websocket_connect(
            f"/api/sync/ws/{sid}?role=follower"
        ) as fol:
            fol.send_json({"type": "hello", "name": "Eve"})
            _drain_until(
                pres,
                lambda m: m.get("type") == "audience" and m.get("count") == 1,
            )
        # Follower has now disconnected.
        gone = _drain_until(
            pres,
            lambda m: m.get("type") == "audience" and m.get("count") == 0,
            max_msgs=20,
        )
        assert gone["followers"] == []


def test_invalid_view_bounds_are_dropped(
    client, admin_headers, admin_user, slide
):
    """View frames with malformed bounds must not reach followers and must
    not poison ``room.last_view`` — otherwise late joiners would replay an
    unusable frame and stall on the default viewport.
    """
    import math

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1)

        bad_frames = [
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, math.nan, 1], "rotation_deg": 0},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, math.inf, 1], "rotation_deg": 0},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 0, 1], "rotation_deg": 0},  # zero width
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, -1], "rotation_deg": 0},  # negative height
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1], "rotation_deg": 0},  # short
            {"type": "view", "slide_id": slide.id, "bounds": "nope", "rotation_deg": 0},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, 1], "rotation_deg": "spin"},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, 1], "rotation_deg": math.nan},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, 1], "rotation_deg": math.inf},
            {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, 1], "bounds_deg": math.nan},
        ]
        for f in bad_frames:
            pres.send_json(f)

        # Now send a valid frame; the follower should only see this one.
        good = {"type": "view", "slide_id": slide.id, "bounds": [0.1, 0.2, 0.3, 0.4], "rotation_deg": 0}
        pres.send_json(good)
        view = _drain_until(fol, lambda m: m.get("type") == "view", max_msgs=30)
        assert view["bounds"] == [0.1, 0.2, 0.3, 0.4]


def test_view_rotation_beyond_360_is_normalised_not_dropped(
    client, admin_headers, admin_user, slide
):
    """OpenSeadragon's rotateTo() does not wrap, so rotating five times in a
    row reports 450°. Dropping those frames as out-of-range would freeze
    every follower for the rest of the session; they must be delivered
    with the angle folded into [0, 360).
    """
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1)

        for sent, expected in (
            (450, 90.0), (540, 180.0), (-90, 270.0), (360, 0.0),
            (-1e-17, 0.0),  # plain % would give exactly 360.0 here
        ):
            pres.send_json({
                "type": "view", "slide_id": slide.id,
                "bounds": [0.1, 0.2, 0.3, 0.4], "bounds_deg": sent,
                "rotation_deg": sent,
            })
            view = _drain_until(fol, lambda m: m.get("type") == "view", max_msgs=30)
            assert view["rotation_deg"] == expected
            assert view["bounds_deg"] == expected


def test_presenter_is_told_it_is_attached(
    client, admin_headers, admin_user, slide
):
    """The presenter learns it is seated (its later automatic reconnects then
    carry the resume marker). The audience snapshot still arrives first."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": token, "client_id": "p-a"})
        first = _drain_until(ws, lambda m: m.get("type") != "ping")
        assert first["type"] == "audience"
        assert _drain_until(ws, lambda m: m.get("type") == "attached") == {"type": "attached"}


def test_end_from_a_superseded_tab_is_ignored(
    client, admin_headers, admin_user, slide
):
    """A superseded presenter tab's unmount DELETE (carrying its client id)
    must NOT tear down the room another tab/device took over. The DELETE of the
    tab that attached last ends it.
    """
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws1:
        ws1.send_json({"type": "hello", "token": token, "client_id": "p-one"})
        _drain_until(ws1, lambda m: m.get("type") == "attached")
        # A second connection (another tab/device) takes over: its attach
        # evicts ws1.
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws2:
            ws2.send_json({"type": "hello", "token": token, "client_id": "p-two"})
            _drain_until(ws2, lambda m: m.get("type") == "attached")
            # ws1's unmount fires a DELETE scoped to its own client id.
            r = client.delete(
                f"/api/admin/sync/sessions/{sid}?client_id=p-one", headers=admin_headers
            )
            assert r.status_code == 200
            assert r.json().get("superseded") is True
            # The room the successor drives survives.
            assert live_manager().get(sid) is not None
            r2 = client.delete(
                f"/api/admin/sync/sessions/{sid}?client_id=p-two", headers=admin_headers
            )
            assert r2.status_code == 200
            assert r2.json().get("superseded") is None
    assert live_manager().get(sid) is None


def test_end_after_own_reconnects_ends_the_room(
    client, admin_headers, admin_user, slide
):
    """Were the unmount DELETE scoped by a server-assigned attach counter the
    client learns from the ``attached`` frame, a tab whose reconnect was
    seated but whose ``attached`` frame never arrived would hold a stale
    value; its DELETE would read as "superseded" and the room stay up for the
    grace window after the presenter left. Scoped by the client id,
    the tab's own reconnects never make it stale."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    hello = {"type": "hello", "token": token, "client_id": "p-laptop"}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json(hello)
        _drain_until(ws, lambda m: m.get("type") == "attached")
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({**hello, "resume": True})
        # Seated (the audience snapshot comes first), but the client leaves
        # without ever reading this attach's ``attached`` frame.
        _drain_until(ws, lambda m: m.get("type") == "audience")
    r = client.delete(
        f"/api/admin/sync/sessions/{sid}?client_id=p-laptop", headers=admin_headers
    )
    assert r.json() == {"ok": True}
    assert live_manager().get(sid) is None


def test_session_end_pushes_end_to_followers(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        fol.send_json({"type": "hello", "name": "watcher"})
        # Wait until the follower is seated: the server answers a ping only
        # from the loop it enters after attaching. Otherwise the DELETE below
        # can reach the room before the hello is handled, and the follower is
        # refused with 4404 instead of being told the session ended.
        fol.send_json({"type": "ping"})
        _drain_until(fol, lambda m: m.get("type") == "pong")
        assert len(live_manager().get(sid).followers) == 1
        # Synchronously end the session via REST (admin).
        r = client.delete(
            f"/api/admin/sync/sessions/{sid}", headers=admin_headers,
        )
        assert r.status_code == 200
        end = _drain_until(fol, lambda m: m.get("type") == "end")
        assert end.get("reason") == "ended_by_owner"


# --------------------------------------------------- discovery channel

def test_discovery_snapshot_and_started(client, admin_headers, admin_user, slide):
    token = create_token(admin_user)
    with client.websocket_connect("/api/sync/discovery") as disc:
        snap = disc.receive_json()
        assert snap["type"] == "snapshot"
        # ``started`` is deferred to the first presenter attach, not the REST
        # create: a room whose presenter never seats must not surface a banner.
        # So creating the session emits nothing; attaching a presenter does.
        sid = _open_session(client, admin_headers, slide.id)
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
            pres.send_json({"type": "hello", "token": token})
            evt = _drain_until(disc, lambda m: m.get("type") == "started")
            assert evt["sid"] == sid
            assert evt["slide_id"] == slide.id


def test_discovery_slide_change_event(
    client, admin_headers, admin_user, db_session, slide
):
    from wsi_app.models import Slide

    # Must be published: a live session can only switch to a published slide
    # (followers can't fetch a draft's tiles): a draft target is absent from
    # _published_public_ids, so the frame is refused and no slide_changed
    # event would be emitted.
    other_slide = Slide(title="Other", catalog_id="OTHER-001", is_published=True)
    db_session.add(other_slide)
    db_session.commit()
    db_session.refresh(other_slide)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect("/api/sync/discovery") as disc, \
            client.websocket_connect(
                f"/api/sync/ws/{sid}?role=presenter"
            ) as pres:
        # Drain the snapshot first.
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        # A valid view frame carries ``bounds``; without it _normalize_view_frame
        # drops the frame before broadcast_view runs, so no slide_changed event
        # is ever emitted and this test would hang until the WS ping timeout.
        pres.send_json({
            "type": "view",
            "slide_id": other_slide.id,
            "bounds": [0, 0, 0.5, 0.5],
            "rotation_deg": 0,
        })
        evt = _drain_until(
            disc,
            lambda m: m.get("type") == "slide_changed",
            max_msgs=20,
        )
        assert evt["sid"] == sid
        assert evt["slide_id"] == other_slide.id


def test_discovery_ended_event(client, admin_headers, admin_user, slide):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    # Attach a presenter so the room is announced to discovery, then let it
    # detach — the room lingers in grace with discovery_announced still True, so
    # a later teardown still emits ``ended`` (only never-announced rooms don't).
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
    with client.websocket_connect("/api/sync/discovery") as disc:
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        client.delete(f"/api/admin/sync/sessions/{sid}", headers=admin_headers)
        evt = _drain_until(disc, lambda m: m.get("type") == "ended")
        assert evt["sid"] == sid





# ----------------------------------------------------- WS Basic throttle

@contextmanager
def _override_settings(**values):
    """Temporarily override fields on the frozen Settings singleton
    (same pattern as test_login_hardening)."""
    from wsi_app.config import settings

    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


def _basic_header(user: str, pw: str) -> dict:
    import base64
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
    return {"Authorization": f"Basic {creds}"}


def test_ws_basic_auth_failures_are_ip_throttled(client, monkeypatch):
    """In app-access mode="basic", the WS upgrade path must share the same
    per-IP login throttle as POST /api/auth/login and the HTTP middleware's
    Basic branch. Without it, repeated WS upgrades carrying Basic guesses
    are an unthrottled brute-force channel against the very credential
    space the HTTP surfaces meter — each attempt burning a full pbkdf2
    verify on the sync worker."""
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic")
    )

    calls = {"n": 0}

    def fake_verify(username, password, db):
        calls["n"] += 1
        return None

    monkeypatch.setattr(_app_auth, "verify_basic_credential", fake_verify)

    headers = _basic_header("alice", "wrong")
    with _override_settings(app_login_ip_max_failures=3):
        # Up to the limit every attempt reaches the verify and is rejected.
        # Distinct guesses, as a brute-forcer must send: a repeat of one
        # credential counts once (see the replay test below).
        for i in range(3):
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/api/sync/ws/no-such-sid", headers=_basic_header("alice", f"guess-{i}")
                ) as ws:
                    ws.receive_json()
        assert calls["n"] == 3

        # Past the limit the throttle rejects BEFORE the pbkdf2 verify runs,
        # and it must say so with 4429 rather than the guard's usual 4401:
        # sync.ts stops reconnecting for good on 4401, which would strand a
        # client whose credentials are fine once the window clears.
        with client.websocket_connect(
            "/api/sync/ws/no-such-sid", headers=headers
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4429
        assert calls["n"] == 3


def test_ws_basic_replayed_stale_credential_does_not_lock_out_the_ip(client, monkeypatch):
    """A tab replaying a rotated password re-sends it on every reconnect of
    its sockets. Like the HTTP Basic branch, the WS path counts a repeat of
    one (username, password) pair once per window, so that tab cannot lock
    everyone behind its address (a classroom NAT) out of the WS surface."""
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    monkeypatch.setattr(_app_auth, "verify_basic_credential", lambda u, p, db: None)

    with _override_settings(app_login_ip_max_failures=3):
        for _ in range(6):
            with client.websocket_connect(
                "/api/sync/ws/no-such-sid", headers=_basic_header("alice", "old-password"),
            ) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4401


def test_ws_basic_auth_success_clears_the_throttle(client, monkeypatch):
    """A successful Basic verify on the WS path clears the caller IP's
    failure history, mirroring the HTTP branch — a user who mistypes twice
    and then logs in must not carry the stale strikes forward."""
    from wsi_app import login_throttle
    from wsi_app.config import settings

    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic")
    )

    # verify_basic_credential: None on a miss, the credential fingerprint on
    # success (kept for the open socket's re-check).
    outcomes = iter([None, None, "fingerprint"])
    monkeypatch.setattr(
        _app_auth, "verify_basic_credential", lambda u, p, db: next(outcomes)
    )

    from wsi_app.routers.app_auth import app_login_throttle_key

    key = app_login_throttle_key("testclient")

    def strikes_recorded() -> bool:
        """True when the caller IP has at least one in-window failure.

        Probing with max_failures=1 turns "any strike at all" into a
        non-zero Retry-After without disturbing the stored history.
        """
        return login_throttle.retry_after_seconds(
            key,
            max_failures=1,
            window_seconds=settings.app_login_ip_window_seconds,
        ) > 0

    headers = _basic_header("alice", "pw")
    with _override_settings(app_login_ip_max_failures=3):
        for _ in range(2):
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/api/sync/ws/no-such-sid", headers=headers
                ) as ws:
                    ws.receive_json()
        # The WS path must record its failures at all — an unthrottled
        # guard would leave this history empty and the clear-on-success
        # assertion below would pass vacuously.
        assert strikes_recorded()

        # Third attempt succeeds: the guard admits it (4404 for the unknown
        # sid proves it got PAST auth into the handler) and clears the
        # strikes.
        with client.websocket_connect(
            "/api/sync/ws/no-such-sid", headers=headers
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4404

    assert not strikes_recorded()


# ------------------------------------ stale presenter reconnect is refused

def test_stale_presenter_reconnect_is_closed_4000(
    client, admin_headers, admin_user, slide
):
    """A laptop that slept through a Resume on a tablet auto-reconnects with its
    own client id and the resume marker. It is closed with 4000 (the client's
    terminal 'replaced'), and the tablet keeps the session."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as laptop:
        laptop.send_json({"type": "hello", "token": token, "client_id": "laptop"})
        _drain_until(laptop, lambda m: m.get("type") == "attached")
    # The laptop's socket is gone; the tablet Resumes (a fresh attach).
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as tablet:
        tablet.send_json({"type": "hello", "token": token, "client_id": "tablet"})
        _drain_until(tablet, lambda m: m.get("type") == "attached")
        tablet_conn = live_manager().get(sid).presenter
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as stale:
            stale.send_json({
                "type": "hello", "token": token, "client_id": "laptop", "resume": True,
            })
            with pytest.raises(WebSocketDisconnect) as exc:
                _drain_until(stale, lambda m: False)
            assert exc.value.code == 4000
        room = live_manager().get(sid)
        assert room is not None and room.presenter is tablet_conn
        assert room.presenter_client_id == "tablet"


def test_presenter_own_reconnect_not_refused_after_lost_attached_frame(
    client, admin_headers, admin_user, slide
):
    """A presenter whose socket died before it read the
    ``attached`` frame reconnects (same client id, resume marker) and is
    seated again — twice in a row — rather than refused with 4000."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    hello = {"type": "hello", "token": token, "client_id": "laptop"}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json(hello)
        _drain_until(ws, lambda m: m.get("type") == "attached")
    for _ in range(2):
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
            ws.send_json({**hello, "resume": True})
            # Leave without reading ``attached`` beyond confirming the seat.
            assert _drain_until(ws, lambda m: m.get("type") == "attached")


# -------------------------- discovery snapshot hides unpublished sessions

def test_discovery_snapshot_omits_unpublished_slide(
    client, admin_headers, admin_user, db_session, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        with client.websocket_connect("/api/sync/discovery") as disc:
            snap = _drain_until(disc, lambda m: m.get("type") == "snapshot")
            assert [s["sid"] for s in snap["sessions"]] == [sid]
        slide.is_published = False
        db_session.commit()
        try:
            with client.websocket_connect("/api/sync/discovery") as disc:
                snap = _drain_until(disc, lambda m: m.get("type") == "snapshot")
                assert snap["sessions"] == []
            # Same answer as the public peek.
            assert client.get(f"/api/sync/sessions/{sid}").status_code == 404
        finally:
            slide.is_published = True
            db_session.commit()


def test_discovery_started_event_is_withheld_for_unpublished_slide(
    client, admin_headers, admin_user, db_session, slide
):
    """The snapshot already hides an unpublished slide; ``started`` must too, or
    its public_id is announced to every index page."""
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    slide.is_published = False
    db_session.commit()
    try:
        with client.websocket_connect("/api/sync/discovery") as disc:
            _drain_until(disc, lambda m: m.get("type") == "snapshot")
            with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
                pres.send_json({"type": "hello", "token": token})
                _drain_until(pres, lambda m: m.get("type") == "audience")
            # A never-shown room's end is not announced either, so the marker
            # that its traffic is out is a listed session's ``started``.
            client.delete(f"/api/admin/sync/sessions/{sid}", headers=admin_headers)
            slide.is_published = True
            db_session.commit()
            marker = _open_session(client, admin_headers, slide.id)
            with client.websocket_connect(f"/api/sync/ws/{marker}?role=presenter") as pres:
                pres.send_json({"type": "hello", "token": token})
                seen = []
                _drain_until(disc, lambda m: seen.append(m) or m.get("type") == "started")
            assert [m["type"] for m in seen if m["type"] != "ping"] == ["started"], seen
            assert seen[-1]["sid"] == marker
    finally:
        slide.is_published = True
        db_session.commit()


def _link_only_slide(db_session):
    from wsi_app.models import Slide

    s = Slide(title="Link only", catalog_id="LINK-001", is_published=True, unlisted=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _discovery_types_until_ended(disc) -> list:
    seen = []
    _drain_until(disc, lambda m: seen.append(m) or m.get("type") == "ended", max_msgs=30)
    return seen


def test_session_on_a_link_only_slide_is_not_announced(
    client, admin_headers, admin_user, db_session, slide
):
    """A link-only (unlisted) slide stays off every browse surface; a live
    session on it must not announce its public_id to every index page (nor
    the sid needed to join). Followers with the share link still join. Its
    end is not announced either: an ``ended`` would name the sid too."""
    hidden = _link_only_slide(db_session)
    sid = _open_session(client, admin_headers, hidden.id)
    token = create_token(admin_user)
    with client.websocket_connect("/api/sync/discovery") as disc:
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
            pres.send_json({"type": "hello", "token": token})
            _drain_until(pres, lambda m: m.get("type") == "attached")
            with client.websocket_connect("/api/sync/discovery") as late:
                assert _drain_until(late, lambda m: m.get("type") == "snapshot")["sessions"] == []
            # The share link (?sync=<sid>) still works.
            assert client.get(f"/api/sync/sessions/{sid}").status_code == 200
            with client.websocket_connect(f"/api/sync/ws/{sid}") as fol:
                fol.send_json({"type": "hello", "name": "x"})
                _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1)
        client.delete(f"/api/admin/sync/sessions/{sid}", headers=admin_headers)
        # Nothing should arrive for it; a listed session started afterwards is
        # the marker that the deletion's discovery traffic (if any) is out.
        listed = _open_session(client, admin_headers, slide.id)
        with client.websocket_connect(f"/api/sync/ws/{listed}?role=presenter") as pres:
            pres.send_json({"type": "hello", "token": token})
            seen = []
            _drain_until(disc, lambda m: seen.append(m) or m.get("type") == "started", max_msgs=30)
    assert [m["type"] for m in seen if m["type"] != "ping"] == ["started"]
    assert seen[-1]["sid"] == listed
    assert sid not in json.dumps(seen) and hidden.public_id not in json.dumps(seen)


def test_moving_onto_a_link_only_slide_withdraws_the_announcement(
    client, admin_headers, admin_user, db_session, slide
):
    """Moving the session onto a link-only slide takes it off the index pages
    (an ``ended`` for the sid, never a ``slide_changed`` naming the slide);
    moving back onto a listed slide announces it again."""
    hidden = _link_only_slide(db_session)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    view = {"type": "view", "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0}
    with client.websocket_connect("/api/sync/discovery") as disc, \
            client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        pres.send_json({"type": "hello", "token": token})
        assert _drain_until(disc, lambda m: m.get("type") == "started")["sid"] == sid

        pres.send_json({**view, "slide_id": hidden.id})
        seen = _discovery_types_until_ended(disc)
        assert seen[-1] == {"type": "ended", "sid": sid}
        assert hidden.public_id not in json.dumps(seen)
        with client.websocket_connect("/api/sync/discovery") as late:
            assert _drain_until(late, lambda m: m.get("type") == "snapshot")["sessions"] == []

        pres.send_json({**view, "slide_id": slide.id})
        evt = _drain_until(disc, lambda m: m.get("type") == "slide_changed", max_msgs=20)
        assert (evt["sid"], evt["slide_public_id"]) == (sid, slide.public_id)


def test_hidden_pointer_still_reaches_followers_while_cursor_is_suppressed(
    client, admin_headers, admin_user, db_session, slide
):
    """A pointer shown just before the presenter moved to a draft slide must be
    switched off on the followers' screens, not left frozen there."""
    from wsi_app.models import Slide

    draft = Slide(title="Draft2", catalog_id="DRAFT-002", is_published=False)
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(f"/api/sync/ws/{sid}") as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1, max_msgs=20)
        pres.send_json({"type": "cursor", "x": 50, "y": 60, "visible": True})
        _drain_until(fol, lambda m: m.get("type") == "cursor" and m.get("visible") is True, max_msgs=20)

        pres.send_json({"type": "view", "slide_id": draft.id,
                        "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0})
        _drain_until(pres, lambda m: m.get("type") == "notice")
        pres.send_json({"type": "cursor", "x": 70, "y": 80, "visible": False})
        hidden = _drain_until(fol, lambda m: m.get("type") == "cursor" and m.get("visible") is False, max_msgs=20)
        assert hidden["visible"] is False


def test_suppressed_cursor_frames_still_count_as_presenter_activity(
    client, admin_headers, admin_user, db_session, slide
):
    """Dropped (suppressed) cursor frames are plainly a presenter driving the
    session: they must keep it from being ended as idle."""
    from wsi_app import sync_room as _sr
    from wsi_app.models import Slide

    draft = Slide(title="Draft3", catalog_id="DRAFT-003", is_published=False)
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience", max_msgs=20)
        pres.send_json({"type": "view", "slide_id": draft.id,
                        "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0})
        _drain_until(pres, lambda m: m.get("type") == "notice")
        room = _sr.manager._rooms[sid]
        room.presenter_last_activity -= 500  # looks idle for a long while
        before = room.presenter_last_activity
        pres.send_json({"type": "cursor", "x": 1, "y": 2, "visible": True})
        pres.send_json({"type": "cursor", "x": 3, "y": 4, "visible": True})
        pres.send_json({"type": "ping"})
        _drain_until(pres, lambda m: m.get("type") == "pong", max_msgs=20)
        assert room.presenter_last_activity > before + 400


def test_refused_view_frames_count_as_presenter_activity(
    client, admin_headers, admin_user, db_session, slide
):
    """Panning/zooming on an unavailable (draft) slide sends only view frames; the
    server refuses them, but that is still a presenter at the controls - it must
    not be ended as idle after 10 minutes of it."""
    from wsi_app import sync_room as _sr
    from wsi_app.models import Slide

    draft = Slide(title="Draft4", catalog_id="DRAFT-004", is_published=False)
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience", max_msgs=20)
        pres.send_json({"type": "view", "slide_id": draft.id,
                        "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0})
        _drain_until(pres, lambda m: m.get("type") == "notice")
        room = _sr.manager._rooms[sid]
        room.presenter_last_activity -= 500
        before = room.presenter_last_activity
        for i in range(3):
            pres.send_json({"type": "view", "slide_id": draft.id,
                            "bounds": [0, 0, 0.5 + i / 100, 0.5], "rotation_deg": 0})
        pres.send_json({"type": "ping"})
        _drain_until(pres, lambda m: m.get("type") == "pong", max_msgs=20)
        assert room.presenter_last_activity > before + 400


def test_ws_basic_mode_accepts_form_login_session_cookie(client, monkeypatch):
    """In mode="basic" a browser that logged in via /api/auth/login carries only
    the session cookie; the WS gate must admit it (the cookie branch runs
    through run_in_threadpool and must not crash with NameError). Past the
    gate an unknown session id answers 4404, not the guard's 4401."""
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic")
    )
    monkeypatch.setattr(
        _app_auth, "decode_session_token", lambda token, **_kw: {"sub": "alice"}
    )
    client.cookies.set(_app_auth.SESSION_COOKIE, "cookie-from-form-login")
    try:
        with client.websocket_connect("/api/sync/ws/no-such-sid") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4404
    finally:
        client.cookies.clear()


def test_gates_skip_cf_jwt_check_without_assertion_header(client, monkeypatch):
    """With no Cf-Access-Jwt-Assertion header both the HTTP middleware and the
    WS gate treat the request as "absent" inline, never hopping to a thread
    for cf_jwt_status; with the header present the check still runs."""
    from wsi_app import cf_access as _cf_access

    calls = []

    def _fake_cf_jwt_status(headers, client_host, **_kw):
        calls.append(client_host)
        return ("absent", None)

    monkeypatch.setattr(_cf_access, "cf_jwt_status", _fake_cf_jwt_status)
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic")
    )
    monkeypatch.setattr(
        _app_auth, "decode_session_token", lambda token, **_kw: {"sub": "alice"}
    )
    client.cookies.set(_app_auth.SESSION_COOKIE, "cookie-from-form-login")
    try:
        client.get("/api/public/settings")
        with client.websocket_connect("/api/sync/ws/no-such-sid") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4404
        assert calls == []

        hdr = {_cf_access.CF_JWT_HEADER: "x.y.z"}
        client.get("/api/public/settings", headers=hdr)
        with client.websocket_connect("/api/sync/ws/no-such-sid", headers=hdr) as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert len(calls) == 2
    finally:
        client.cookies.clear()


# ------------------------------------------------- open sockets are re-checked

def _receive_until_closed(ws, max_msgs: int = 50) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        for _ in range(max_msgs):
            ws.receive_json()
    return exc.value.code


def test_discovery_socket_closes_once_its_app_session_is_revoked(client, monkeypatch):
    """The upgrade check alone is not enough: after every app session is
    revoked, an open discovery socket would keep receiving new live sessions'
    ids and slides (only a reconnect would be refused)."""
    from wsi_app.routers import sync as _sync_router

    monkeypatch.setattr(_sync_router, "VIEWER_REAUTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(
        _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap", session_generation=1)
    )
    cookie = _app_auth.create_session_token("ldap:alice")
    headers = {"cookie": f"{_app_auth.SESSION_COOKIE}={cookie}"}
    with client.websocket_connect("/api/sync/discovery", headers=headers) as disc:
        assert disc.receive_json()["type"] == "snapshot"
        # Still admitted after several re-checks while nothing changes.
        import time as _time
        _time.sleep(0.2)
        disc.send_json({"type": "ping"})
        assert _drain_until(disc, lambda m: m.get("type") == "pong")
        # Bulk revoke (as the settings refresh loop would install it).
        monkeypatch.setattr(
            _app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="ldap", session_generation=2)
        )
        assert _receive_until_closed(disc) == 4401


def test_follower_socket_closes_once_its_basic_user_is_deleted(
    client, admin_headers, slide, db_session, monkeypatch,
):
    """A follower admitted with a Basic header is re-checked through the
    credential fingerprint, so deleting the user ends the socket."""
    from sqlalchemy import delete as _delete

    from wsi_app.auth import hash_password
    from wsi_app.models import AuthUser
    from wsi_app.routers import sync as _sync_router

    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(_sync_router, "VIEWER_REAUTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    db_session.execute(_delete(AuthUser))
    db_session.add(AuthUser(username="bob", password_hash=hash_password("bob-pass-1")))
    db_session.commit()
    try:
        with client.websocket_connect(
            f"/api/sync/ws/{sid}?role=follower", headers=_basic_header("bob", "bob-pass-1"),
        ) as ws:
            ws.send_json({"type": "hello", "name": "bob"})
            ws.send_json({"type": "ping"})
            assert _drain_until(ws, lambda m: m.get("type") == "pong")
            db_session.execute(_delete(AuthUser))
            db_session.commit()
            _app_auth.forget_basic_credential("bob")
            assert _receive_until_closed(ws) == 4401
    finally:
        db_session.execute(_delete(AuthUser))
        db_session.commit()


async def test_viewer_recheck_verifies_a_basic_header_the_upgrade_never_needed(monkeypatch):
    """A socket admitted on a cookie that also carried a Basic header must
    not be dropped once the cookie lapses while the header is still good:
    the re-check verifies the header once (as a fresh upgrade would) and
    fingerprints it for later rounds."""
    from wsi_app.routers import sync as _sync_router

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    calls = []

    def fake_credential(u, p, db):
        calls.append(u)
        return "fp-1" if p == "good" else None

    monkeypatch.setattr(_app_auth, "verify_basic_credential", fake_credential)
    monkeypatch.setattr(
        _app_auth, "_basic_session_live", lambda user, fp, **_kw: user == "carol" and fp == "fp-1"
    )

    class _FakeWs:
        headers = {"authorization": _basic_header("carol", "good")["Authorization"]}
        cookies: dict = {}
        client = None

    adm = _sync_router._WsAdmission()
    assert await _sync_router._ws_admission(_FakeWs(), adm, recheck=True) is None
    assert calls == ["carol"] and adm.basic_cred == "fp-1"
    # Later rounds use the fingerprint, not another verify.
    assert await _sync_router._ws_admission(_FakeWs(), adm, recheck=True) is None
    assert calls == ["carol"]
    # A reset (fingerprint no longer live) closes the socket.
    monkeypatch.setattr(_app_auth, "_basic_session_live", lambda user, fp, **_kw: False)
    assert await _sync_router._ws_admission(_FakeWs(), adm, recheck=True) == 4401


@pytest.mark.asyncio
async def test_viewer_recheck_reports_a_failed_credential_read_instead_of_revoking(monkeypatch):
    """A DB blip during a follower's periodic re-check must reach
    _viewer_reauth_loop's consecutive-failure budget (it raises), not read as
    "revoked" (4401, which the clients treat as final). The upgrade check
    itself still fails closed."""
    from sqlalchemy.exc import OperationalError

    from wsi_app.routers import sync as _sync_router

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    _app_auth._basic_cred_cache.clear()

    class _LockedSession:
        def execute(self, *a, **kw):
            raise OperationalError("SELECT", {}, Exception("database is locked"))

        def close(self):
            pass

    monkeypatch.setattr(_app_auth, "SessionLocal", _LockedSession)

    class _FakeWs:
        headers = {"authorization": _basic_header("carol", "pw")["Authorization"]}
        cookies: dict = {}
        client = None

    adm = _sync_router._WsAdmission()
    adm.basic_user, adm.basic_cred = "carol", "fp-1"
    with pytest.raises(OperationalError):
        await _sync_router._ws_admission(_FakeWs(), adm, recheck=True)
    # Fail-closed default for every other caller.
    assert _app_auth._basic_session_live("carol", "fp-1") is False


@pytest.mark.asyncio
async def test_viewer_recheck_reports_a_jwks_outage_instead_of_invalid(monkeypatch):
    """A CF JWKS fetch failure on a re-check raises (and is retried by the
    loop) rather than being read as an invalid assertion."""
    from wsi_app import cf_access as _cf_access
    from wsi_app.routers import sync as _sync_router

    def unavailable(headers, client_host, *, raise_unavailable=False):
        if raise_unavailable:
            raise _cf_access.VerifierUnavailable()
        return ("invalid", None)

    monkeypatch.setattr(_cf_access, "cf_jwt_status", unavailable)

    class _FakeWs:
        headers = {_cf_access.CF_JWT_HEADER: "jwt"}
        cookies: dict = {}
        client = None

    adm = _sync_router._WsAdmission()
    with pytest.raises(_cf_access.VerifierUnavailable):
        await _sync_router._ws_admission(_FakeWs(), adm, recheck=True)
    # The upgrade check raises too (and _ws_guard turns it into a retry close:
    # see test_an_unreachable_auth_backend_at_upgrade_is_retryable).
    with pytest.raises(_cf_access.VerifierUnavailable):
        await _sync_router._ws_admission(_FakeWs(), adm, recheck=False)


# ------- an auth backend that is down at upgrade time is "retry", not "denied"
#
# A JWKS outage or a failed DB read at the upgrade must not close with 4401,
# which sync.ts / discovery.ts treat as final: follower sync and the index
# page's live banner would stay dead until a reload, over a blip.

def _jwks_down(monkeypatch):
    from wsi_app import cf_access as _cf_access

    def unavailable(headers, client_host, *, raise_unavailable=False):
        if raise_unavailable:
            raise _cf_access.VerifierUnavailable()
        return ("invalid", None)

    monkeypatch.setattr(_cf_access, "cf_jwt_status", unavailable)
    return {_cf_access.CF_JWT_HEADER: "x.y.z"}


def _credential_read_fails(monkeypatch):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))

    def locked(token, *, raise_errors=False):
        if raise_errors:
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return None

    monkeypatch.setattr(_app_auth, "decode_session_token", locked)
    return {"cookie": f"{_app_auth.SESSION_COOKIE}=basic-cookie"}


@pytest.mark.parametrize("outage", [_jwks_down, _credential_read_fails])
@pytest.mark.parametrize("path", ["/api/sync/ws/{sid}?role=follower", "/api/sync/discovery"])
def test_an_unreachable_auth_backend_at_upgrade_is_retryable(
    client, admin_headers, slide, monkeypatch, outage, path
):
    sid = _open_session(client, admin_headers, slide.id)
    headers = outage(monkeypatch)
    with client.websocket_connect(path.format(sid=sid), headers=headers) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


@pytest.mark.parametrize("path", ["/api/sync/ws/{sid}?role=follower", "/api/sync/discovery"])
def test_a_long_auth_outage_on_an_open_viewer_socket_is_retryable(
    client, admin_headers, slide, monkeypatch, path
):
    """The re-check loop's failure budget must not end in 4401 — final for
    both clients — or a DB / JWKS outage longer than the budget would leave
    follower sync and the index banner dead until a reload — the same failure
    the upgrade-time check retries. A budget spent on outages closes with 1013, so the
    client reconnects (and is checked afresh) once the backend is back."""
    from sqlalchemy.exc import OperationalError

    from wsi_app.routers import sync as _sync_router

    monkeypatch.setattr(_sync_router, "VIEWER_REAUTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(_sync_router, "PRESENTER_REAUTH_MAX_FAILURES", 3)
    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    monkeypatch.setattr(
        _app_auth, "decode_session_token", lambda token, *, raise_errors=False: "basic:alice"
    )
    sid = _open_session(client, admin_headers, slide.id)
    headers = {"cookie": f"{_app_auth.SESSION_COOKIE}=basic-cookie"}
    with client.websocket_connect(path.format(sid=sid), headers=headers) as ws:
        ws.send_json({"type": "hello", "name": "x"})  # ignored by discovery
        ws.send_json({"type": "ping"})
        assert _drain_until(ws, lambda m: m.get("type") == "pong")

        def locked(token, *, raise_errors=False):
            raise OperationalError("SELECT", {}, Exception("database is locked"))

        monkeypatch.setattr(_app_auth, "decode_session_token", locked)
        assert _receive_until_closed(ws) == 1013


def test_an_unreachable_users_table_at_presenter_hello_is_retryable(
    client, admin_headers, admin_user, slide, monkeypatch
):
    import wsi_app.routers.sync as sync_mod
    from sqlalchemy.exc import OperationalError

    sid = _open_session(client, admin_headers, slide.id)

    def locked(_token):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(sync_mod, "_resolve_active_presenter", locked)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "hello", "token": create_token(admin_user)})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


# ---------------------------------------- inbound flood is closed with 1008

def _flood_until_closed(ws, frame, *, limit=3000):
    """Send ``frame`` repeatedly; return the close code the server answers."""
    for _ in range(limit):
        ws.send_json(frame)
    for _ in range(limit + 10):
        try:
            ws.receive_json()
        except WebSocketDisconnect as exc:
            return exc.code
    return None


def test_flooding_follower_is_closed_with_1008(client, admin_headers, slide):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}") as ws:
        ws.send_json({"type": "hello"})
        assert _flood_until_closed(ws, {"type": "visibility", "visible": True}) == 1008


def test_flooding_discovery_socket_is_closed_with_1008(client):
    with client.websocket_connect("/api/sync/discovery") as disc:
        assert disc.receive_json()["type"] == "snapshot"
        assert _flood_until_closed(disc, {"type": "noise"}) == 1008


def test_burst_within_the_bucket_is_not_closed(client, admin_headers, slide):
    """A burst inside INBOUND_FRAME_BURST must keep the socket open and be
    answered in full."""
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}") as ws:
        ws.send_json({"type": "hello"})
        for _ in range(100):
            ws.send_json({"type": "ping"})
        got = 0
        while got < 100:
            if ws.receive_json().get("type") == "pong":
                got += 1


def test_over_limit_frames_are_dropped_before_json_parsing(monkeypatch):
    """The bucket is consulted before the payload is decoded."""
    import asyncio
    from wsi_app.routers import sync as _sync_router

    parsed = []
    real_loads = _sync_router.json.loads
    monkeypatch.setattr(
        _sync_router.json, "loads", lambda t: parsed.append(t) or real_loads(t)
    )

    class _Ws:
        async def receive(self):
            return {"type": "websocket.receive", "text": '{"type": "x"}'}

    bucket = _sync_router._InboundBucket()
    bucket._tokens = 0.0
    bucket._last_refill = _sync_router.time.monotonic() + 1000  # no refill
    out = asyncio.run(_sync_router._receive_json_capped(_Ws(), bucket))
    assert out is _sync_router._FRAME_DROPPED
    assert parsed == []


# ------------------ cursors are held back while the presenter is on a draft

def test_cursor_is_suppressed_after_view_to_unpublished_slide(
    client, admin_headers, admin_user, db_session, slide
):
    """A refused (draft) slide change leaves followers on the old slide, so the
    presenter's cursor — coordinates on the *draft* — must not reach them, and
    the presenter is told once. The next accepted view lifts the hold."""
    from wsi_app.models import Slide

    draft = Slide(title="Draft", catalog_id="DRAFT-001", is_published=False)
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(f"/api/sync/ws/{sid}") as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1, max_msgs=20)

        pres.send_json({"type": "view", "slide_id": draft.id,
                        "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0})
        notice = _drain_until(pres, lambda m: m.get("type") == "notice")
        assert notice["code"] == "slide_unavailable"

        pres.send_json({"type": "cursor", "x": 111, "y": 5, "visible": True})
        # Back on the published slide: accepted, cursor flows again.
        pres.send_json({"type": "view", "slide_id": slide.id,
                        "bounds": [0, 0, 0.5, 0.5], "rotation_deg": 0})
        pres.send_json({"type": "cursor", "x": 222, "y": 5, "visible": True})
        cur = _drain_until(fol, lambda m: m.get("type") == "cursor", max_msgs=20)
        assert cur["x"] == 222


# ------------- the audience panel honours the analytics privacy mode
#
# Under "hashed" the admin dashboard itself sees no raw address or UA, so the
# presenter's audience panel (and its CSV export) must not show every
# follower's either.

def _audience_with_one_follower(client, admin_headers, admin_user, slide):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres, \
            client.websocket_connect(
                f"/api/sync/ws/{sid}", headers={"user-agent": "Mozilla/5.0 (iPad) Safari/604"},
            ) as fol:
        pres.send_json({"type": "hello", "token": create_token(admin_user)})
        fol.send_json({"type": "hello", "name": "Hanako"})
        return _drain_until(
            pres, lambda m: m.get("type") == "audience" and m.get("count") == 1, max_msgs=20,
        )


def test_audience_is_pseudonymous_in_hashed_analytics_mode(
    client, admin_headers, admin_user, slide, monkeypatch
):
    from wsi_app import analytics as _analytics
    from wsi_app import network as _net

    monkeypatch.setattr(_net, "_snapshot", _net._Snapshot(analytics_mode="hashed"))
    frame = _audience_with_one_follower(client, admin_headers, admin_user, slide)
    assert frame["pseudonymous"] is True
    (entry,) = frame["followers"]
    assert entry["name"] == "Hanako"
    # A prefix of the keyed hash hashed-mode analytics stores for the address.
    assert entry["ip"] == _analytics.pseudonymise_client_ip("testclient")[:12]
    assert entry["ua"] == "" and entry["hostname"] is None


@pytest.mark.parametrize("mode", ["off", "plain"])
def test_audience_shows_the_follower_outside_hashed_mode(
    client, admin_headers, admin_user, slide, monkeypatch, mode
):
    from wsi_app import network as _net

    monkeypatch.setattr(_net, "_snapshot", _net._Snapshot(analytics_mode=mode))
    frame = _audience_with_one_follower(client, admin_headers, admin_user, slide)
    assert frame["pseudonymous"] is False
    (entry,) = frame["followers"]
    assert entry["ip"] == "testclient"
    assert entry["ua"] == "Mozilla/5.0 (iPad) Safari/604"


def test_network_snapshot_carries_the_analytics_mode(db_session):
    """The sync worker learns the mode from the snapshot it already refreshes."""
    from wsi_app import network as _net
    from wsi_app.models import NetworkSettings

    ns = db_session.get(NetworkSettings, 1)
    if ns is None:
        ns = NetworkSettings(id=1)
        db_session.add(ns)
    saved = ns.analytics_mode
    try:
        ns.analytics_mode = "Hashed"
        db_session.commit()
        _net.refresh(db_session)
        assert _net.analytics_mode() == "hashed"
    finally:
        ns.analytics_mode = saved
        db_session.commit()
        _net.refresh(db_session)


# --------------------- WS upgrades from another site's page are refused
#
# The same-origin policy does not cover WebSockets. Under cached Basic
# credentials or the break-glass / LAN-direct exemptions, a malicious page in a
# LAN user's browser could open the discovery socket and read every live
# session id (and a presented link-only slide's public_id).

def _first_close_or_frame(ws):
    try:
        return ws.receive_json()
    except WebSocketDisconnect as exc:
        return exc.code


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "null"},
    # A sibling subdomain is refused too: the SPA never connects cross-origin.
    {"origin": "http://testserver", "sec-fetch-site": "same-site"},
    {"origin": "http://testserver", "sec-fetch-site": "cross-site"},
])
def test_cross_site_upgrades_are_refused(client, admin_headers, admin_user, slide, headers):
    sid = _open_session(client, admin_headers, slide.id)
    for path in ("/api/sync/discovery", f"/api/sync/ws/{sid}?role=follower"):
        with client.websocket_connect(path, headers=headers) as ws:
            assert _first_close_or_frame(ws) == 4403
    # Before the hello is read: no staff token makes it same-site.
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter", headers=headers) as ws:
        ws.send_json({"type": "hello", "token": create_token(admin_user)})
        assert _first_close_or_frame(ws) == 4403
    assert live_manager().get(sid).presenter is None


@pytest.mark.parametrize("headers", [
    {},  # not a browser
    {"origin": "http://testserver"},
    # nginx forwards Host without the port the browser's Origin carries.
    {"origin": "https://testserver:8443"},
    {"origin": "https://wsi.example.org", "host": "wsi.example.org"},
    {"origin": "http://[2001:db8::1]:8080", "host": "[2001:db8::1]"},
    # A fully-qualified name with its trailing dot: nginx's $host drops the
    # dot, the browser's Origin keeps it (and the reverse, straight to uvicorn).
    {"origin": "https://wsi.example.org.", "host": "wsi.example.org"},
    {"origin": "https://wsi.example.org:8443", "host": "wsi.example.org."},
    # Fetch metadata is trusted over a Host a proxy hop rewrote (the vite dev
    # proxy's changeOrigin, say).
    {"origin": "http://localhost:5173", "host": "127.0.0.1:8000", "sec-fetch-site": "same-origin"},
])
def test_same_site_and_non_browser_upgrades_are_admitted(client, headers):
    with client.websocket_connect("/api/sync/discovery", headers=headers) as ws:
        assert _first_close_or_frame(ws)["type"] == "snapshot"


def test_canonical_public_host_is_an_accepted_origin(client):
    """A hop that rewrites Host must not lock out the operator's canonical
    entry point (WSI_PUBLIC_BASE_URL); anything else is still refused."""
    headers = {"host": "127.0.0.1"}
    with _override_settings(public_base_url="https://wsi.example.org"):
        with client.websocket_connect(
            "/api/sync/discovery", headers={**headers, "origin": "https://wsi.example.org"},
        ) as ws:
            assert _first_close_or_frame(ws)["type"] == "snapshot"
        with client.websocket_connect(
            "/api/sync/discovery", headers={**headers, "origin": "https://other.example.org"},
        ) as ws:
            assert _first_close_or_frame(ws) == 4403
    # The trailing-dot form of the canonical name is the same host, either way
    # round; only one dot is dropped, so "org.." is still a different name.
    for base, origin, code in (
        ("https://wsi.example.org", "https://wsi.example.org.", None),
        ("https://wsi.example.org./", "https://wsi.example.org", None),
        ("https://wsi.example.org", "https://wsi.example.org..", 4403),
        ("https://wsi.example.org", "https://other.example.org.", 4403),
    ):
        with _override_settings(public_base_url=base):
            with client.websocket_connect(
                "/api/sync/discovery", headers={**headers, "origin": origin},
            ) as ws:
                got = _first_close_or_frame(ws)
                assert (got if code else got["type"]) == (code or "snapshot"), (base, origin)


def test_trailing_dot_host_does_not_admit_a_different_name(client):
    with client.websocket_connect(
        "/api/sync/discovery",
        headers={"host": "wsi.example.org", "origin": "https://evil.example.org."},
    ) as ws:
        assert _first_close_or_frame(ws) == 4403


def test_cross_site_upgrade_is_refused_under_the_lan_exemption(
    client, admin_headers, slide, monkeypatch
):
    """The case the check exists for: the exemption admits a same-site LAN
    page without a credential, but not another site's page in that browser."""
    monkeypatch.setattr(
        _app_auth,
        "_snapshot",
        _app_auth._AuthSnapshot(mode="google_oauth", lan_direct_noauth_mode="lan"),
    )
    lb = _loopback_client()
    with lb.websocket_connect(
        "/api/sync/discovery", headers={**_LAN_HEADERS, "origin": "http://192.168.1.50"},
    ) as ws:
        assert _first_close_or_frame(ws)["type"] == "snapshot"
    with lb.websocket_connect(
        "/api/sync/discovery", headers={**_LAN_HEADERS, "origin": "https://evil.example"},
    ) as ws:
        assert _first_close_or_frame(ws) == 4403


# ------- hello timing, slide refusal / unpublish, discovery labels, activity

def _sync_ping(ws):
    """Round-trip a ping: every frame sent before it has been handled."""
    ws.send_json({"type": "ping"})
    _drain_until(ws, lambda m: m.get("type") == "pong", max_msgs=50)


def test_follower_is_told_once_it_is_seated(client, admin_headers, slide):
    """The client reports itself connected only on a frame from the server (a
    refused upgrade opens the socket too), so a seated follower gets one even
    in a room with nothing to replay."""
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
        fol.send_json({"type": "hello", "name": "x"})
        assert _drain_until(fol, lambda m: m.get("type") == "attached")


def test_late_follower_hello_is_still_applied(
    client, admin_headers, admin_user, slide, monkeypatch
):
    import wsi_app.routers.sync as sync_mod

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        monkeypatch.setattr(sync_mod, "HELLO_TIMEOUT_SECONDS", 0.05)
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
            # Attached anonymously once the hello is overdue...
            _drain_until(fol, lambda m: m.get("type") == "attached")
            # ...and named once it arrives.
            fol.send_json({"type": "hello", "name": "Slow", "visible": False})
            snap = _drain_until(
                pres,
                lambda m: m.get("type") == "audience"
                and m["followers"] and m["followers"][0]["name"] == "Slow",
                max_msgs=30,
            )
            assert snap["followers"][0]["visible"] is False


@pytest.mark.parametrize("role", ["follower", "presenter"])
def test_overdue_hello_is_asked_to_retry_not_refused(
    client, admin_headers, slide, monkeypatch, role
):
    """4400 is final for both clients; a slow link is not a bad hello."""
    import wsi_app.routers.sync as sync_mod

    sid = _open_session(client, admin_headers, slide.id)
    monkeypatch.setattr(sync_mod, "HELLO_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(sync_mod, "REQUIRE_NAME", True)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role={role}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


def test_non_boolean_pointer_visibility_is_dropped(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(pres, lambda m: m.get("type") == "audience" and m.get("count") == 1, max_msgs=20)
        pres.send_json({"type": "cursor", "x": 1, "y": 1, "visible": "false"})
        pres.send_json({"type": "cursor", "x": 2, "y": 2, "visible": True})
        cursor = _drain_until(fol, lambda m: m.get("type") == "cursor", max_msgs=20)
        assert cursor["x"] == 2


def test_follower_publish_check_failure_asks_to_retry(
    client, admin_headers, slide, monkeypatch
):
    import wsi_app.routers.sync as sync_mod

    sid = _open_session(client, admin_headers, slide.id)

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sync_mod, "_published_public_ids", broken)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


def test_refused_slide_is_not_looked_up_on_every_frame(
    client, admin_headers, admin_user, slide, db_session, monkeypatch
):
    import wsi_app.routers.sync as sync_mod
    from wsi_app.models import Slide

    hidden = Slide(title="Draft", catalog_id="D-1", is_published=False)
    db_session.add(hidden)
    db_session.commit()
    db_session.refresh(hidden)
    calls = {"n": 0}
    real = sync_mod._published_public_ids

    def counting(ids, **kw):
        calls["n"] += 1
        return real(ids, **kw)

    monkeypatch.setattr(sync_mod, "_published_public_ids", counting)
    # Wide enough that the five frames below fall inside it on any machine.
    monkeypatch.setattr(sync_mod, "REFUSED_SLIDE_RECHECK_SECONDS", 60.0)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    view = {"type": "view", "slide_id": hidden.id, "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        for _ in range(5):
            pres.send_json(view)
        _sync_ping(pres)
        assert calls["n"] == 1


def test_failed_slide_change_lookup_holds_the_pointer_back(
    client, admin_headers, admin_user, slide, db_session, monkeypatch
):
    """A DB error on a slide change must not skip the pointer suppression, or
    the next cursor (the new slide's coordinates) would reach followers still
    on the old slide."""
    import wsi_app.routers.sync as sync_mod
    from wsi_app.models import Slide

    other = Slide(title="Other", catalog_id="O-1", is_published=True)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")

        def broken(*_a, **_k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(sync_mod, "_published_public_ids", broken)
        pres.send_json({"type": "view", "slide_id": other.id, "bounds": [0, 0, 1, 1], "rotation_deg": 0})
        _sync_ping(pres)
        conn = live_manager().get(sid).presenter
        assert conn.cursor_suppressed is True
        # No verdict, so nothing cached: the next frame looks again.
        assert conn.refused_slide_id is None


def test_mid_session_unpublish_reaches_both_ends(
    client, admin_headers, admin_user, slide, db_session
):
    import wsi_app.routers.sync as sync_mod

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(fol, lambda m: m.get("type") == "attached")
        slide.is_published = False
        db_session.commit()
        client.portal.call(sync_mod._refresh_room_slide, sid)
        assert _drain_until(fol, lambda m: m.get("code") == "slide_unavailable", max_msgs=20)
        assert _drain_until(pres, lambda m: m.get("code") == "slide_unpublished", max_msgs=20)


def test_the_presenter_reauth_tick_runs_the_slide_recheck(
    client, admin_headers, admin_user, slide, db_session, monkeypatch
):
    """Through the loop that drives it, not a direct call: dropping the
    loop's ``sid`` would leave every other test passing with the feature off."""
    import wsi_app.routers.sync as sync_mod

    monkeypatch.setattr(sync_mod, "PRESENTER_REAUTH_INTERVAL_SECONDS", 0.05)
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect("/api/sync/discovery") as disc, \
            client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres, \
            client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(disc, lambda m: m.get("type") == "started", max_msgs=20)
        _drain_until(fol, lambda m: m.get("type") == "attached", max_msgs=20)
        # Made link-only while announced: the banner goes, the presenter is told.
        slide.unlisted = True
        db_session.commit()
        assert _drain_until(disc, lambda m: m == {"type": "ended", "sid": sid}, max_msgs=50)
        assert _drain_until(pres, lambda m: m.get("code") == "unlisted_announced", max_msgs=50)
        # Unpublished: followers and the presenter are told.
        slide.is_published = False
        db_session.commit()
        assert _drain_until(fol, lambda m: m.get("code") == "slide_unavailable", max_msgs=50)
        assert _drain_until(pres, lambda m: m.get("code") == "slide_unpublished", max_msgs=50)
        # Published and listed again: the banner is back.
        slide.is_published = True
        slide.unlisted = False
        db_session.commit()
        assert _drain_until(fol, lambda m: m.get("code") == "slide_available", max_msgs=50)
        assert _drain_until(disc, lambda m: m.get("type") == "slide_changed" and m["sid"] == sid, max_msgs=50)


def test_discovery_events_name_the_slide(client, admin_headers, admin_user, slide):
    token = create_token(admin_user)
    with client.websocket_connect("/api/sync/discovery") as disc:
        _drain_until(disc, lambda m: m.get("type") == "snapshot")
        sid = _open_session(client, admin_headers, slide.id)
        with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
            pres.send_json({"type": "hello", "token": token})
            evt = _drain_until(disc, lambda m: m.get("type") == "started")
            assert evt["slide_title"] == (slide.title or None)
            assert evt["slide_catalog_id"] == (slide.catalog_id or None)
            with client.websocket_connect("/api/sync/discovery") as disc2:
                snap = _drain_until(disc2, lambda m: m.get("type") == "snapshot")
                assert snap["sessions"][0]["slide_title"] == (slide.title or None)


@pytest.mark.parametrize("unlisted", [False, True])
def test_superseded_followers_learn_the_new_session_on_listed_slides(
    client, admin_headers, slide, db_session, unlisted
):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(fol, lambda m: m.get("type") == "attached")
        slide.unlisted = unlisted
        db_session.commit()
        new_sid = _open_session(client, admin_headers, slide.id)
        end = _drain_until(fol, lambda m: m.get("type") == "end", max_msgs=20)
    assert end["reason"] == "superseded"
    if unlisted:
        assert "next" not in end
    else:
        assert end["next"] == {"sid": new_sid, "slide_public_id": slide.public_id}


def test_replayed_frames_are_not_presenter_activity(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        room = live_manager().get(sid)
        before = room.presenter_last_activity
        view = {"type": "view", "slide_id": slide.id, "bounds": [0, 0, 1, 1], "rotation_deg": 0}
        pres.send_json({**view, "replay": True})
        pres.send_json({"type": "cursor", "x": 1, "y": 1, "visible": False, "replay": True})
        _sync_ping(pres)
        assert room.presenter_last_activity == before
        assert room.last_view is not None
        pres.send_json(view)
        _sync_ping(pres)
        assert room.presenter_last_activity > before


def test_refusal_is_announced_even_after_a_failed_lookup(
    client, admin_headers, admin_user, slide, db_session, monkeypatch
):
    """A failed lookup suppresses the pointer silently; the notice must not be
    keyed on that flag, or it would be skipped for the whole episode once the
    slide is refused: followers would stay behind and the presenter never be
    told."""
    import wsi_app.routers.sync as sync_mod
    from wsi_app.models import Slide

    hidden = Slide(title="Draft", catalog_id="D-2", is_published=False)
    db_session.add(hidden)
    db_session.commit()
    db_session.refresh(hidden)
    real = sync_mod._published_public_ids
    calls = {"n": 0}

    def flaky(ids, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(ids, **kw)

    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    view = {"type": "view", "slide_id": hidden.id, "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        monkeypatch.setattr(sync_mod, "_published_public_ids", flaky)
        pres.send_json(view)  # the lookup fails
        pres.send_json(view)  # refused
        assert _drain_until(pres, lambda m: m.get("code") == "slide_unavailable", max_msgs=20)


def test_basic_auth_pool_is_never_empty(monkeypatch):
    import wsi_app.routers.sync as sync_mod

    monkeypatch.setenv("WSI_SYNC_BASIC_AUTH_WORKERS", "0")
    monkeypatch.setattr(sync_mod, "_BASIC_AUTH_EXECUTOR", None)
    pool = sync_mod._basic_auth_executor()
    try:
        assert pool._max_workers == 1
    finally:
        pool.shutdown(wait=False)
        monkeypatch.setattr(sync_mod, "_BASIC_AUTH_EXECUTOR", None)



# ------- refusal episodes, lookup window, visibility frames, first-frame hello

def _hidden_slide(db_session, **kw):
    from wsi_app.models import Slide

    s = Slide(title=kw.get("title", "Draft"), catalog_id=kw.get("catalog_id", "D"),
              is_published=kw.get("is_published", False))
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_every_refusal_episode_is_announced(
    client, admin_headers, admin_user, slide, db_session
):
    """Once per episode, and again for the next one once a view was accepted."""
    a = _hidden_slide(db_session, catalog_id="D-A")
    c = _hidden_slide(db_session, catalog_id="D-C")
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    v = lambda sid_: {"type": "view", "slide_id": sid_, "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        for frame in (v(a.id), v(a.id), v(slide.id), v(c.id)):
            pres.send_json(frame)
        pres.send_json({"type": "ping"})
        notices = 0
        while True:
            m = pres.receive_json()
            if m.get("type") == "pong":
                break
            notices += m.get("code") == "slide_unavailable"
        assert notices == 2


def test_the_refused_slide_is_looked_up_again_once_the_window_passes(
    client, admin_headers, admin_user, slide, db_session, monkeypatch
):
    """Published while the presenter is on it: the next frame after the window
    is accepted, not refused from the cache for good."""
    import wsi_app.routers.sync as sync_mod

    monkeypatch.setattr(sync_mod, "REFUSED_SLIDE_RECHECK_SECONDS", 0.0)
    x = _hidden_slide(db_session, catalog_id="D-X")
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    v = {"type": "view", "slide_id": x.id, "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as pres:
        pres.send_json({"type": "hello", "token": token})
        _drain_until(pres, lambda m: m.get("type") == "audience")
        pres.send_json(v)
        _sync_ping(pres)
        assert live_manager().get(sid).slide_id == slide.id
        x.is_published = True
        db_session.commit()
        pres.send_json(v)
        _sync_ping(pres)
        assert live_manager().get(sid).slide_id == x.id


@pytest.mark.parametrize("bad", ["false", None, 0, {}])
def test_a_non_boolean_visibility_frame_is_ignored(
    client, admin_headers, admin_user, slide, bad
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(
        f"/api/sync/ws/{sid}?role=presenter"
    ) as pres, client.websocket_connect(
        f"/api/sync/ws/{sid}?role=follower"
    ) as fol:
        pres.send_json({"type": "hello", "token": token})
        fol.send_json({"type": "hello", "name": "x"})
        _drain_until(fol, lambda m: m.get("type") == "attached")
        frame = {"type": "visibility"} if bad is None else {"type": "visibility", "visible": bad}
        fol.send_json(frame)
        _sync_ping(fol)
        room = live_manager().get(sid)
        assert [f.meta.visible for f in room.followers.values()] == [True]


@pytest.mark.parametrize("value", [None, 0, "false", "no"])
def test_a_hello_visible_other_than_false_joins_as_visible(
    client, admin_headers, slide, value
):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
        fol.send_json({"type": "hello", "name": "x", "visible": value})
        _drain_until(fol, lambda m: m.get("type") == "attached")
        room = live_manager().get(sid)
        assert [f.meta.visible for f in room.followers.values()] == [True]


def test_a_presenter_whose_first_frame_is_not_a_hello_is_refused(
    client, admin_headers, admin_user, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    token = create_token(admin_user)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=presenter") as ws:
        ws.send_json({"type": "view", "token": token})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_a_follower_whose_first_frame_is_not_a_hello_joins_anonymously(
    client, admin_headers, slide
):
    sid = _open_session(client, admin_headers, slide.id)
    with client.websocket_connect(f"/api/sync/ws/{sid}?role=follower") as fol:
        fol.send_json({"type": "visibility", "visible": False, "name": "x"})
        _drain_until(fol, lambda m: m.get("type") == "attached")
        room = live_manager().get(sid)
        metas = [f.meta for f in room.followers.values()]
        assert [(m.name, m.visible) for m in metas] == [(None, True)]
