"""Live ViewerPage sync — REST + WebSocket endpoints.

REST surface:

- ``POST   /api/admin/sync/sessions``         create (staff JWT)
- ``DELETE /api/admin/sync/sessions/{sid}``   end (staff JWT — any staff
  user may end any session)
- ``GET    /api/sync/sessions/{sid}``         peek session (app_auth gated)
- ``GET    /api/admin/sync/sessions``         list the caller's own live
  sessions (staff JWT — backs the "resume" affordance)
- ``GET    /api/admin/sync/sessions/all``     list every active session
  (staff JWT — backs the admin management panel)

WebSocket surface:

- ``WS     /api/sync/ws/{sid}?role=presenter|follower``  state fan-out
- ``WS     /api/sync/discovery``                          live-room presence

Starlette HTTP middleware (the IP filter and app-auth gates in ``main.py``)
does not run for WebSocket scopes, so the same checks are repeated here in
``_ws_guard``. Both endpoints ``accept`` the socket *before* calling it, so
that a rejection can deliver an application-private 4xxx code instead of the
bare 1006 a close-before-accept produces; see the comment on each endpoint.
Browsers cannot set custom Authorization headers on a WebSocket upgrade —
for app-auth modes the cookie path is the realistic one for followers and
discovery — but the handler still honours an explicit Bearer staff token for
parity with the HTTP middleware. A presenter's staff token travels in its
hello frame instead, and that is what admits a presenter socket past the
app-access gate (see _ws_admission).
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import json
import logging
import secrets
import time
from typing import Deque, Dict, List, Optional, Set
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import analytics as _analytics
from .. import app_auth as _app_auth
from .. import auth as _admin_auth
from .. import cf_access as _cf_access
from .. import login_throttle as _login_throttle
from .. import network as _net
from .. import sync_room as _sync_room_module
from ..admin_helpers import api_error, audit
from ..auth import STAFF_ROLES, require_staff
from ..config import settings, env_bool, env_int, env_int_min, env_float
from ..db import SessionLocal, get_db
from ..models import Slide, User
from ..sync_room import (
    PresenterConn,
    PresenterSupersededError,
    RoomFullError,
    RoomLimitError,
    SharedFrame,
    manager,
    sanitize_name,
    sanitize_presenter_client_id,
)
from .app_auth import app_login_throttle_key

router = APIRouter()
_log = logging.getLogger("wsi_app.sync")


# Tunables (overridable via env for ops experiments).
HELLO_TIMEOUT_SECONDS = env_float("WSI_SYNC_HELLO_TIMEOUT_SEC", 5)
# Keepalive. The server sends a ping every PING_INTERVAL_SECONDS, and closes a
# connection that has produced no inbound frame for PING_TIMEOUT_SECONDS —
# the only way to notice a peer that vanished without a TCP FIN (sleeping
# laptop, dead Wi-Fi), which would otherwise hold its room slot until the
# session ages out. Keep the timeout a comfortable multiple of the interval so
# one dropped ping never evicts a healthy client.
PING_INTERVAL_SECONDS = env_float("WSI_SYNC_PING_INTERVAL_SEC", 15)
PING_TIMEOUT_SECONDS = env_float("WSI_SYNC_PING_TIMEOUT_SEC", 30)
# Hard backstop on the recv loop's ``ws.receive()``. The keepalive closes the
# socket on PING_TIMEOUT_SECONDS of silence, but that close is routed through
# the _WsBridge writer task; if the writer already died on a send timeout (a
# frozen peer whose TCP window is full — the exact case), that close is a no-op
# and ``ws.receive()`` would block forever, leaking the recv task and the
# follower's audience slot until the room's absolute-age GC (~1.5 h) fires. A
# healthy peer answers the server's keepalive pings (and sends its own) so
# inbound arrives every <=PING_INTERVAL_SECONDS; total silence for this long
# means the peer is gone regardless of socket state, so bail and let the
# handler detach.
# Kept above PING_TIMEOUT_SECONDS so the graceful keepalive close is always the
# preferred path and this only catches the un-closable-socket tail.
WS_RECV_IDLE_TIMEOUT_SECONDS = env_float("WSI_SYNC_WS_RECV_IDLE_TIMEOUT_SEC", 45)
# Require every follower to supply a display name before joining. Off by
# default: a follower that sends no name (or no hello frame at all) joins as
# anonymous. Turning it on both surfaces the requirement to the join dialog
# (via the session peek's ``require_name``) and enforces it server-side — a
# nameless hello is closed with CLOSE_BAD_REQUEST — so an instructor who needs
# to see who is in the room can rely on it rather than on the client honouring
# the hint.
REQUIRE_NAME = env_bool("WSI_SYNC_REQUIRE_NAME", False)
# Re-validate the presenter's admin status (and the IP filter) periodically so
# a JWT minted before the account was disabled / demoted — or a client whose
# address a new IP rule denies — cannot keep driving a long WebSocket session
# past the next interval. The handshake check alone only binds policy at
# connect time.
PRESENTER_REAUTH_INTERVAL_SECONDS = env_float("WSI_SYNC_PRESENTER_REAUTH_SEC", 60)
# How many *consecutive* failed reauth checks (the DB lookup itself raising)
# before the reauth loop fails closed and drops the presenter with 4401. A
# single transient hiccup (SQLite write-lock storm, momentary I/O error) is
# tolerated — the streak resets on any check that completes, whatever its
# verdict — but a persistent failure must not leave a possibly-demoted /
# disabled presenter driving the session: reauth is the only revocation between
# connect and the JWT's 24h expiry, so a permanently-broken check would
# otherwise fail open for a full day. At the 60s interval, 5 covers ~5 minutes
# of continuous DB unavailability before erring on the side of safety. Set to
# <= 0 to disable the fail-closed backstop (the check then fails open).
PRESENTER_REAUTH_MAX_FAILURES = env_int("WSI_SYNC_PRESENTER_REAUTH_MAX_FAILURES", 5)
# The same periodic re-check for the app-access gate a follower / discovery
# socket was admitted through (see _viewer_reauth_loop): a session cookie that
# expires or is revoked (bulk revoke, mode / allowed-domain change), a deleted
# basic user, an IP rule that now denies the client. The check reads the
# settings snapshot every worker refreshes on its own (see
# main._settings_refresh_loop), so a revocation reaches an open socket within
# roughly this interval plus that refresh, without restarting wsi-sync. Its
# consecutive-failure backstop is PRESENTER_REAUTH_MAX_FAILURES.
VIEWER_REAUTH_INTERVAL_SECONDS = env_float("WSI_SYNC_VIEWER_REAUTH_SEC", 60)

# Reverse-DNS lookup for follower IPs is opt-in; on-LAN PTRs are often
# missing or misleading and the lookup is blocking.
ENABLE_REVERSE_DNS = env_bool("WSI_SYNC_REVERSE_DNS", False)
REVERSE_DNS_TIMEOUT = 0.5

# Reverse-DNS calls go through a dedicated, bounded executor instead of
# asyncio's default thread pool. asyncio.wait_for can cancel the awaiting
# Future on timeout but cannot kill the underlying thread that's still
# blocked inside libc's getaddrinfo/gethostbyaddr — under a slow or hostile
# resolver those threads stack up and would otherwise starve the default
# pool that backup extraction, DZI handle open, and other run_in_threadpool
# callers share. Capping it at a small size confines the damage.
_RDNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=env_int("WSI_SYNC_REVERSE_DNS_WORKERS", 4),
    thread_name_prefix="wsi-rdns",
)


# The HTTP Basic verify of a WS upgrade (a full pbkdf2 hash per attempt) gets a
# bounded pool of its own for a similar reason: on the default pool, a class
# reconnecting at once after a restart — every follower and index-page socket
# carrying cached Basic credentials — would queue ahead of the presenters'
# slide change lookups and the discovery gate, and live rooms stall behind it.
# Created on first use (and again after a shutdown, which a test's app
# lifespan triggers), see _basic_auth_executor.
_BASIC_AUTH_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _basic_auth_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _BASIC_AUTH_EXECUTOR
    if _BASIC_AUTH_EXECUTOR is None:
        _BASIC_AUTH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            # A pool of 0 would refuse every Basic upgrade (as a 1013 the
            # clients retry forever), so a value below 1 is raised to 1.
            max_workers=env_int_min("WSI_SYNC_BASIC_AUTH_WORKERS", 4, 1),
            thread_name_prefix="wsi-sync-basic",
        )
    return _BASIC_AUTH_EXECUTOR


def shutdown_rdns_executor() -> None:
    """Release the reverse-DNS and Basic-verify worker pools on app shutdown.

    Called from main.lifespan's finally clause. Threads blocked inside
    libc's gethostbyaddr cannot be cancelled, so ``wait=False`` lets the
    process exit promptly and ``cancel_futures=True`` drops queued work.
    """
    global _BASIC_AUTH_EXECUTOR
    _RDNS_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    if _BASIC_AUTH_EXECUTOR is not None:
        _BASIC_AUTH_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        _BASIC_AUTH_EXECUTOR = None

# Cap per-frame payload size at the application layer so a single oversized
# message can't pin the event loop in JSON parsing for every other room. The
# steady-state cursor/view JSON is well under 1 KiB; 64 KiB leaves room for
# unforeseen schema growth without exposing a DoS surface. Independently, the
# systemd unit passes --ws-max-size to uvicorn so the same bound is also
# enforced at the WebSocket protocol layer before any Python code runs.
MAX_WS_FRAME_BYTES = env_int("WSI_SYNC_WS_MAX_BYTES", 65536)

# Per-connection outbound queue depth. The broadcast fan-out enqueues frames
# here (non-blocking) instead of awaiting ``ws.send_json`` directly, so one
# follower whose TCP receive window is full — a closed laptop lid, a dead
# Wi-Fi link — cannot wedge the presenter's recv loop (and through it every
# other follower's fan-out) while the OS retransmit timer counts down for
# minutes. A client that falls this far behind is treated as gone and dropped.
MAX_OUTBOUND_QUEUE = env_int("WSI_SYNC_OUTBOUND_QUEUE_MAX", 512)
# Hard ceiling on a single ``ws.send_json``. A frozen peer can otherwise leave
# a send awaiting socket drain indefinitely; bound it so the writer task gives
# up and tears the connection down instead of pinning the slot forever.
WS_SEND_TIMEOUT_SECONDS = env_float("WSI_SYNC_WS_SEND_TIMEOUT_SEC", 10)

# Per-connection inbound-frame rate limit (token bucket). Without it a single
# hostile follower can spam frames — e.g. visibility toggles — fast enough to
# burn event-loop time parsing JSON and (pre-debounce) fan out an O(N) audience
# rebuild per toggle, saturating the presenter's outbound queue and evicting
# them. Tokens refill continuously at INBOUND_FRAME_RATE per second up to
# INBOUND_FRAME_BURST; a frame that arrives with no token is dropped
# (coalesced) rather than handled.
#
# Sized well above a legitimate presenter's steady trickle: the client sends
# cursor frames at 15 Hz (useSyncPresenterSession's minInterval) and view
# frames at most 5/s while the view moves (TARGET_FRAME_INTERVAL_MS) plus one
# when it settles, and a keepalive ping every 15 s — roughly 21 frames/s
# sustained against a 60/s refill. Keep that headroom in mind if the
# client-side rates are ever raised: a steady state at or above the refill rate would drain the burst reserve
# over a long session and start silently coalescing real frames.
INBOUND_FRAME_RATE = env_float("WSI_SYNC_INBOUND_FRAME_RATE", 60)
INBOUND_FRAME_BURST = env_float("WSI_SYNC_INBOUND_FRAME_BURST", 120)
# A peer that keeps hammering an empty bucket is not a slow client, it is a
# flood: dropping its frames still costs a recv + task wakeup each, which at
# tens of thousands of frames/s measurably slowed unrelated rooms. More than
# INBOUND_OVERLIMIT_MAX over-limit frames inside one INBOUND_OVERLIMIT_WINDOW_SEC
# window closes the connection with 1008 (policy violation). Legitimate
# traffic never goes over the limit, so it never accumulates here.
INBOUND_OVERLIMIT_MAX = env_int("WSI_SYNC_INBOUND_OVERLIMIT_MAX", 200)
INBOUND_OVERLIMIT_WINDOW_SECONDS = env_float("WSI_SYNC_INBOUND_OVERLIMIT_WINDOW_SEC", 10)
CLOSE_POLICY_VIOLATION = 1008

# How long a slide the presenter was refused (unpublished) stays refused
# without another publish lookup. A presenter panning around on it sends a view
# frame per animation step; within this window those cost no DB read, and
# once it passes the next frame looks again, so publishing the slide while the
# presenter is on it is noticed within about this long.
REFUSED_SLIDE_RECHECK_SECONDS = 2.0


class _InboundBucket:
    """Per-connection inbound-frame token bucket (see INBOUND_FRAME_RATE).

    ``admit()`` spends a token and returns False when none is left; the caller
    then drops the frame *without parsing it*. ``abusive()`` reports whether
    the rejected frames have piled up past INBOUND_OVERLIMIT_MAX in the
    current window, i.e. the connection should be closed.
    """

    def __init__(self) -> None:
        self._tokens = float(INBOUND_FRAME_BURST)
        self._last_refill = time.monotonic()
        self._over = 0
        self._window_start = self._last_refill

    def admit(self) -> bool:
        now = time.monotonic()
        self._tokens = min(
            float(INBOUND_FRAME_BURST),
            self._tokens + (now - self._last_refill) * INBOUND_FRAME_RATE,
        )
        self._last_refill = now
        if self._tokens < 1.0:
            if now - self._window_start > INBOUND_OVERLIMIT_WINDOW_SECONDS:
                self._window_start = now
                self._over = 0
            self._over += 1
            return False
        self._tokens -= 1.0
        return True

    def abusive(self) -> bool:
        return self._over > INBOUND_OVERLIMIT_MAX


# Upper bound on cursor coordinates (in level-0 image pixels). The largest
# real WSI scans top out at ~200k px per side, and SZI is capped at 2 M in
# szi_utils, so 4 M comfortably covers every legitimate payload while
# refusing the obvious nonsense (NaN, inf, MAX_FLOAT, off-by-billion
# typos) that a buggy or malicious presenter could otherwise broadcast.
_CURSOR_COORD_MAX = 4_000_000.0

# Upper bound on viewport-bounds coordinates. OSD viewport space is
# normalised so legitimate bounds sit roughly in [-2, 2], but pinch / fling
# transients can land outside that briefly. 10 000 is generous enough to
# absorb every realistic value while still rejecting NaN / Inf / off-by-
# galaxies floats from a buggy or malicious client.
_VIEW_COORD_MAX = 10_000.0
# SQLite binds integers as signed 64-bit; a slide_id at/above 2**63 raises
# OverflowError at bind time. Reject out-of-range ids in the frame validator
# so a hostile ``view`` frame can't crash the presenter's WS handler (the
# DB lookup runs unguarded inside _handle_inbound). Mirrors public._MAX_DB_ID.
_MAX_DB_ID = 2**63 - 1


def _coerce_finite_number(v) -> Optional[float]:
    """Return ``v`` as a finite float, or None if it isn't a real number.

    Rejects bool (which passes ``isinstance(int)`` in Python), strings, and
    NaN/inf — all of which can sneak through ``isinstance(int, float)``
    checks but break clients that treat the value as a pixel coordinate.
    """
    if isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        # JSON has no integer bound: a 400-digit literal arrives as a Python
        # int that float() cannot represent (a ``1e400`` float is inf instead,
        # caught below).
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _wrap_degrees(f: float) -> float:
    """Fold a finite angle into [0, 360).

    Python's ``%`` alone is not enough: a tiny negative (e.g. ``-1e-17``, which
    OSD's rotation spring can land on) rounds to exactly ``360.0``.
    """
    r = f % 360.0
    return 0.0 if r >= 360.0 else r


def _normalize_view_frame(raw: dict) -> Optional[dict]:
    """Validate a presenter ``view`` frame before fan-out.

    The frame is pinned as ``room.last_view`` and replayed verbatim to every
    late-joining follower, so a single invalid payload would otherwise wedge
    the session: followers' ``fitBounds`` would throw on NaN / Inf bounds
    and silently freeze on the previous viewport until the next valid frame
    arrives. ``bounds`` is required; ``rotation_deg`` is optional.
    Present-but-malformed values are rejected outright.

    Returns the normalised frame, or None if it must be dropped.
    """
    new_slide_id = raw.get("slide_id")
    if not isinstance(new_slide_id, int) or isinstance(new_slide_id, bool):
        return None
    # Bound to SQLite's signed-64-bit range so the downstream db.get() can't
    # raise OverflowError at bind time and tear down the WS handler.
    if not (0 < new_slide_id <= _MAX_DB_ID):
        return None
    out: dict = {"type": "view", "slide_id": new_slide_id}

    bounds = raw.get("bounds")
    # bounds is required: the client always sends it and applyViewFrame
    # destructures it unconditionally. A frame without bounds would crash
    # every follower's viewport handler.
    if bounds is None:
        return None
    if not isinstance(bounds, list) or len(bounds) != 4:
        return None
    coerced: List[float] = []
    for b in bounds:
        f = _coerce_finite_number(b)
        if f is None or abs(f) > _VIEW_COORD_MAX:
            return None
        coerced.append(f)
    # width / height must be strictly positive — a zero or negative box
    # is a malformed frame OSD's fitBounds would happily accept and then
    # render as an inverted / degenerate viewport on every follower.
    if coerced[2] <= 0 or coerced[3] <= 0:
        return None
    out["bounds"] = coerced

    # Angles are normalised into [0, 360) rather than range-checked.
    # OpenSeadragon's rotateTo() does not wrap, so a presenter who presses
    # the rotate button five times reports 450°; rejecting that would silently
    # freeze every follower for the rest of the session.
    rot = raw.get("rotation_deg")
    if rot is not None:
        f = _coerce_finite_number(rot)
        if f is None:
            return None
        out["rotation_deg"] = _wrap_degrees(f)

    # bounds_deg is the OSD ``Rect.degrees`` of the bounds rect. The follower
    # rebuilds ``new Rect(x, y, w, h, bounds_deg)`` to land on the exact view
    # the presenter framed; dropping it desyncs every follower whenever the
    # presenter rotates. Validate and normalise like rotation_deg (OSD's Rect
    # wraps its degrees the same way, so the value means the same thing).
    bdeg = raw.get("bounds_deg")
    if bdeg is not None:
        f = _coerce_finite_number(bdeg)
        if f is None:
            return None
        out["bounds_deg"] = _wrap_degrees(f)

    # Pass ``t`` (presenter-side monotonic timestamp) through when present,
    # coerced to a finite float the same way the cursor frame validates it.
    # Followers don't currently use it for ordering — TCP already delivers
    # frames in order on a single WS connection — but keeping the schema field
    # stable means a future follower-side stale-frame check can ship without a
    # server change. Coercing here (rather than forwarding verbatim) keeps a
    # NaN/Inf ``t`` out of the pinned ``room.last_view`` that future check would
    # read. Other unknown keys are intentionally dropped to keep the fan-out
    # payload small.
    t = _coerce_finite_number(raw.get("t"))
    if t is not None:
        out["t"] = t
    return out


def _normalize_cursor_frame(raw: dict) -> Optional[dict]:
    x = _coerce_finite_number(raw.get("x"))
    y = _coerce_finite_number(raw.get("y"))
    if x is None or y is None:
        return None
    if abs(x) > _CURSOR_COORD_MAX or abs(y) > _CURSOR_COORD_MAX:
        return None
    # A real boolean or nothing: ``bool()`` would read "false", {} and [0] as a
    # visible pointer, which would then be shown to every follower.
    visible = raw.get("visible", True)
    if not isinstance(visible, bool):
        return None
    out: dict = {"type": "cursor", "x": x, "y": y, "visible": visible}
    t = _coerce_finite_number(raw.get("t"))
    if t is not None:
        out["t"] = t
    return out


# Returned by _receive_json_capped instead of a parsed frame when the caller's
# token bucket was empty: the frame is discarded before any decode / JSON work.
_FRAME_DROPPED = object()


async def _receive_json_capped(ws: WebSocket, bucket: Optional["_InboundBucket"] = None):
    """receive_json() with a payload-size guard.

    With a ``bucket`` the rate check runs before the frame is decoded, so a
    flood costs one dict lookup per frame rather than a JSON parse; an
    over-limit frame yields ``_FRAME_DROPPED``.

    Raises ``WebSocketDisconnect`` on remote close, ``ValueError`` for any
    oversized or malformed frame so the existing recv loops can ``continue``
    past it the same way they handle bad JSON.
    """
    msg = await ws.receive()
    if msg.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(code=msg.get("code", 1005))
    if bucket is not None and not bucket.admit():
        return _FRAME_DROPPED
    text = msg.get("text")
    if text is None:
        raw = msg.get("bytes") or b""
        if len(raw) > MAX_WS_FRAME_BYTES:
            raise ValueError("ws frame too large")
        text = raw.decode("utf-8")
    elif len(text) > MAX_WS_FRAME_BYTES:
        raise ValueError("ws frame too large")
    try:
        return json.loads(text)
    except RecursionError:
        # A frame of a few thousand ``[`` nests deeper than the decoder's
        # recursion limit while staying far under MAX_WS_FRAME_BYTES. Surface
        # it as the ValueError every caller already treats as a malformed
        # frame (the hello path would otherwise let it escape the handler).
        raise ValueError("ws frame nested too deeply") from None


# WebSocket close codes follow the 4000-4999 application-private range.
CLOSE_IP_DENIED = 4403
# A browser upgrade from another site's page (see _ws_origin_allowed). The
# same "forbidden" code as the IP filter: both clients stop on it, which is
# right for a misconfigured proxy too — a retry would be refused the same way.
CLOSE_ORIGIN_DENIED = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_NOT_FOUND = 4404
# Presenter only: the room exists but belongs to another staff account. A room
# that is gone is CLOSE_NOT_FOUND, never this (see _run_presenter).
CLOSE_CONFLICT = 4409
CLOSE_TOO_BUSY = 4503
# Deliberately NOT in the terminal stop-lists of frontend/src/lib/sync.ts and
# discovery.ts. Unlike 4401 (bad credential — retrying cannot help) this says
# "correct credentials may well work, just not yet": the per-IP login window
# expires on its own, so the client should keep backing off and retrying,
# which is exactly what those clients do for any code they do not special-case.
# Named after HTTP 429, which the equivalent HTTP path returns.
CLOSE_RATE_LIMITED = 4429
# RFC 6455 "Try Again Later". The admission check could not reach a verdict —
# the Cloudflare Access JWKS endpoint or the DB was unreachable — so the socket
# is refused without judging its credential. Sending 4401 here would tell
# sync.ts / discovery.ts the credential was bad for good: follower sync and
# the index page's live banner would stay dead until a reload, over a blip.
# Like 4429 this code is on neither client's stop-list, so they back off and
# retry. The send-backlog overflow uses it too.
CLOSE_TRY_AGAIN_LATER = 1013


# ---------------------------------------------------------------- schemas

class SessionCreateIn(BaseModel):
    slide_id: int = Field(..., ge=1)


class SessionCreateOut(BaseModel):
    session_id: str
    slide_id: int
    # The slide's public_id, so the client can build a viewer URL if it ever
    # needs to fall back from the server-provided ``follower_url``.
    slide_public_id: str = ""
    follower_url: str


class SessionPeekOut(BaseModel):
    session_id: str
    slide_id: int
    # Slide's public_id: what the follower's browser navigates to / fetches
    # tiles by. The numeric slide_id is retained for internal correlation.
    slide_public_id: str = ""
    slide_title: Optional[str] = None
    slide_catalog_id: Optional[str] = None
    # Surfaces WSI_SYNC_REQUIRE_NAME so the follower's join dialog can hide
    # the "Join anonymously" button. Without this, an operator with the env
    # var enabled would see followers send anonymous hellos only to be closed
    # with 4400 "name required", which the client treats as final ("could not
    # join") — so the dialog asks for the name up front instead.
    require_name: bool = False
    # Populated only on the owner-scoped list (``/api/admin/sync/sessions``),
    # which backs the presenter's "resume" affordance. Carrying the
    # server-built share URL here keeps URL construction in one place
    # (``_follower_url``) so resume reuses the exact same canonicalisation as
    # create — including ``WSI_PUBLIC_BASE_URL`` — instead of rebuilding the
    # URL from the browser's current origin (which would pin the link to
    # whichever host the presenter happens to be on). Left ``None`` on the
    # public peek, which doesn't need it.
    follower_url: Optional[str] = None
    # Owner-scoped list only (None on the public peek): whether a presenter
    # socket is attached right now — another tab/device, or a dead socket the
    # server has not noticed yet (see sync_room.OwnedSession). Resume takes
    # the session over either way.
    presenter_connected: Optional[bool] = None


class SessionListOut(BaseModel):
    sessions: List[SessionPeekOut]


class AdminSessionOut(BaseModel):
    """A single row in the staff "active live-sync sessions" management view.

    Carries enough to triage and end an orphan an operator can no longer reach
    from a presenter tab: the slide it's bound to, who started it, whether a
    presenter socket is currently attached (a True here on a session nobody is
    actually driving is the zombie-tab case), the live follower count, when it
    started, and — for a presenter-less room — the seconds left before the
    grace GC reaps it on its own.
    """
    session_id: str
    slide_id: int
    slide_title: Optional[str] = None
    slide_catalog_id: Optional[str] = None
    owner_user_id: int
    owner_username: Optional[str] = None
    owner_role: Optional[str] = None
    presenter_connected: bool
    follower_count: int
    created_at: float
    grace_remaining: Optional[float] = None


class AdminSessionListOut(BaseModel):
    sessions: List[AdminSessionOut]


# ------------------------------------------------------------- helpers

def _follower_url(request: Request, slide_public_id: str, session_id: str) -> str:
    """Build the share URL handed to followers (QR + copy link).

    The viewer route is keyed by the slide's unguessable public_id (never the
    numeric PK), so the share link carries the token — the same identifier a
    follower's browser needs to fetch tiles.

    When ``WSI_PUBLIC_BASE_URL`` is set, it is used verbatim as the base so the
    link always points at the operator's declared canonical entry point — this
    matters when the server is reachable under more than one name/address (a
    public hostname, a LAN hostname, a raw IP) and the presenter happens to be
    on one that other participants can't reach. When it is unset the link falls
    back to the presenter's own request host, which is exactly right for a
    single-address deployment where every participant uses the same origin.

    Only ``Host`` is consulted, never ``X-Forwarded-Host``: nginx rewrites
    ``Host`` to ``$host`` on every proxied location but passes a client-set
    ``X-Forwarded-Host`` through untouched, so honouring it would let any
    caller that can reach the endpoint choose the host baked into the share
    link (and QR code) handed to every follower.
    """
    base = settings.public_base_url
    if base:
        return f"{base}/slides/{slide_public_id}?sync={session_id}"
    proto = _net.forwarded_proto(request.headers, request.url.scheme) or "http"
    host = request.headers.get("host")
    if not host:
        host = request.url.netloc or "localhost"
    return f"{proto}://{host}/slides/{slide_public_id}?sync={session_id}"


def _peek_visible(slide: Optional[Slide]) -> bool:
    """Whether a live session may be surfaced on the *public* sync endpoints.

    The publish gate in ``create_sync_session`` runs once, and the one in the
    slide-change path only fires when the presenter switches slides — nothing
    ends a running session when staff unpublish (or delete) the slide it is
    already on. Without this re-check, ``GET /api/sync/sessions/{sid}`` —
    reachable by anyone once the app-access mode is ``none`` — keeps serving
    the now-private slide's title and catalog_id, and keeps handing out the
    token to join it, for the rest of the session's life (up to
    MAX_SESSION_AGE, 1.5 h by default).

    The owner-scoped staff listing deliberately does NOT use this: a presenter
    still needs to see the session in order to stop it.

    A link-only slide passes: the share link carries the sid, and the sid is
    all a follower presents. So once a session has been announced on the index
    pages (on a listed slide), everyone who saw the banner holds a sid that
    follows it onto a link-only slide too — refusing them would also refuse
    every follower already in the room on its next reconnect. The presenter is
    told when that happens (``unlisted_announced``, see
    RoomManager._warn_unlisted_announced).
    """
    return slide is not None and bool(slide.is_published)


def _peek(
    slide: Optional[Slide],
    room_slide_id: int,
    sid: str,
    request: Optional[Request] = None,
) -> SessionPeekOut:
    slide_public_id = getattr(slide, "public_id", None) if slide else None
    return SessionPeekOut(
        session_id=sid,
        slide_id=room_slide_id,
        slide_public_id=slide_public_id or "",
        slide_title=getattr(slide, "title", None) if slide else None,
        slide_catalog_id=getattr(slide, "catalog_id", None) if slide else None,
        require_name=REQUIRE_NAME,
        # Only the owner-scoped list passes ``request`` so it can surface a
        # ready-to-use share URL for the resume flow; public peeks leave it None.
        follower_url=(
            _follower_url(request, slide_public_id or "", sid)
            if request is not None and slide_public_id else None
        ),
    )


# ------------------------------------------------------------ REST endpoints

@router.post("/api/admin/sync/sessions", response_model=SessionCreateOut)
async def create_sync_session(
    body: SessionCreateIn,
    request: Request,
    presenter: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    # Run the SQLite read off the event loop. This handler is ``async`` (it must
    # await the RoomManager), so a synchronous ``db.get`` here would block every
    # other room's broadcast/recv on this single worker while SQLite waits out a
    # write-lock storm (``busy_timeout``). The peek / list endpoints offload
    # their reads the same way.
    slide = await asyncio.to_thread(db.get, Slide, body.slide_id)
    if not slide:
        raise api_error(404, "sync.apiErr.slideNotFound")
    # Block live sync on unpublished (draft/private) slides. Without this gate
    # the discovery WebSocket broadcasts a "started" event for the draft to
    # every viewer, but followers who join can't fetch the slide's tiles (the
    # public/dzi endpoints filter unpublished slides), so the join fails. Refuse
    # at creation time so the session — and its notification — never exists.
    if not slide.is_published:
        raise api_error(403, "sync.apiErr.slideUnpublished")
    # One live session per presenter: starting a new one supersedes the
    # caller's existing session(s). This keeps a single presenter from leaving
    # an orphan running (e.g. closed the tab, then started a session on a
    # different slide) which would otherwise show a second join banner on the
    # index page until its grace window elapsed. Scoped to the caller, so a
    # colleague's concurrent session is untouched.
    #
    # Create and supersede are one atomic step (see create_superseding): done
    # as two calls, a create that failed on the room ceiling would still have
    # destroyed the presenter's running session — a 503 response that dropped
    # every follower it already had.
    try:
        sid, superseded = await manager.create_superseding(
            slide_id=body.slide_id, owner_user_id=presenter.id,
            slide_public_id=slide.public_id or "",
            # The superseded sessions' followers are offered the new one, but
            # only when it is on a slide the index page lists: their audience
            # was never given a link-only slide's link.
            announce_successor=not slide.unlisted,
        )
    except RoomLimitError:
        # The room table is at capacity. 503 (not 400/429) so the client treats
        # it as a transient server-side capacity limit and can retry later.
        raise api_error(503, "sync.apiErr.tooManySessions")
    # audit() does a synchronous INSERT; batch the supersede + create records
    # into one threadpool hop so the event loop is never blocked on the writes.
    def _write_audit() -> None:
        for old_sid in superseded:
            audit(presenter, "sync.supersede", target=old_sid)
        audit(presenter, "sync.create", target=sid, slide_id=body.slide_id)
    await asyncio.to_thread(_write_audit)
    return SessionCreateOut(
        session_id=sid,
        slide_id=body.slide_id,
        slide_public_id=slide.public_id or "",
        follower_url=_follower_url(request, slide.public_id or "", sid),
    )


@router.delete("/api/admin/sync/sessions/{sid}")
async def end_sync_session(
    sid: str,
    presenter: User = Depends(require_staff),
    client_id: Optional[str] = None,
):
    # No owner-ID check: per the role-permission matrix any staff user may
    # end a sync session regardless of who created it (e.g. a manager
    # closing out a session an instructor abandoned). The audit record
    # captures who closed which session for traceability.
    room = manager.get(sid)
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    # Guard for the presenter-unmount "end because I'm leaving" path. The
    # viewer sends its own presenter client-instance id (the one in its hello);
    # if a different instance — another tab or device — has attached since,
    # the leaving tab must NOT end the room that one is now driving. Skip the
    # teardown and report it as superseded. A room nobody has attached to yet
    # (presenter_client_id None) is still the caller's to end. Explicit ends —
    # the presenter's Stop button and the admin session-management End button —
    # send no ``client_id`` and remain unconditional.
    #
    # Keyed by the id the client minted, never by a value the server hands out
    # later: the client knows its own id even when its socket died before any
    # reply arrived, so the check cannot go stale and leave the room up for the
    # grace window after the presenter navigated away.
    if client_id is not None and room.presenter_client_id not in (None, client_id):
        # audit() does a synchronous INSERT; run it off the event loop so a
        # SQLite write-lock wait can't stall other rooms on this worker.
        await asyncio.to_thread(audit, presenter, "sync.end.superseded", target=sid)
        return {"ok": True, "superseded": True}
    await manager.end(sid, reason="ended_by_owner")
    await asyncio.to_thread(audit, presenter, "sync.end", target=sid)
    return {"ok": True}


# The three read endpoints below are ``async`` and read the RoomManager on the
# event loop, which is the only thread that mutates it; only their DB reads go
# to a thread. As plain ``def`` endpoints they would run in the threadpool and
# read room state while the loop changes it: the peek reads ``room.slide_id``
# twice, and a slide change between the reads would pair one slide's row with
# the other's id.

@router.get("/api/sync/sessions/{sid}", response_model=SessionPeekOut)
async def peek_sync_session(sid: str, db: Session = Depends(get_db)):
    room = manager.get(sid)
    if not room:
        raise HTTPException(status_code=404, detail="Session not found")
    slide_id = room.slide_id
    slide = await asyncio.to_thread(db.get, Slide, slide_id)
    if not _peek_visible(slide):
        raise HTTPException(status_code=404, detail="Session not found")
    return _peek(slide, slide_id, sid)


@router.get("/api/admin/sync/sessions", response_model=SessionListOut)
async def list_owned_sync_sessions(
    request: Request,
    presenter: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    # Owner-scoped list backing the presenter's "resume" affordance: after a
    # presenter leaves (reload / tab close / network drop) the room survives
    # the grace window, and the viewer page uses this to learn whether a
    # still-live session for the current slide is theirs to reattach to. Scoped
    # to the caller (owner_user_id) and behind staff auth so it can't be used
    # to enumerate other presenters' sessions. Sessions with a presenter still
    # attached are included and flagged (see RoomManager.list_owned).
    #
    # Each entry carries a server-built ``follower_url`` so the resume flow
    # reuses the same canonical share link as create instead of reconstructing
    # it from the browser origin.
    entries = manager.list_owned(presenter.id)
    slides = await asyncio.to_thread(_load_slides, db, [e.slide_id for e in entries])
    out: List[SessionPeekOut] = []
    for entry in entries:
        peek = _peek(slides.get(entry.slide_id), entry.slide_id, entry.session_id, request)
        peek.presenter_connected = entry.presenter_connected
        out.append(peek)
    return SessionListOut(sessions=out)


def _load_slides(db: Session, slide_ids: List[int]) -> Dict[int, Slide]:
    """The rows of ``slide_ids`` that exist, by id (for a to_thread hop)."""
    return {sid: s for sid in set(slide_ids) if (s := db.get(Slide, sid)) is not None}


@router.get("/api/admin/sync/sessions/all", response_model=AdminSessionListOut)
async def list_all_sync_sessions(
    presenter: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Every active live-sync session, for the staff management surface.

    Backs the admin "active live sync sessions" panel: the operator-facing way
    to find and force-end an orphan they can no longer reach from a presenter
    tab (closed / lost / on another device that's still holding the socket
    open). Distinct from the public peek ``GET /api/sync/sessions/{sid}`` (one
    known sid) and the owner-scoped ``GET /api/admin/sync/sessions`` (the
    caller's rooms only): this lists *every* room with the triage fields
    needed to decide what to end, then the operator ends it via
    ``DELETE /api/admin/sync/sessions/{sid}``.

    Staff-gated and visible to any staff role, mirroring the DELETE policy that
    already lets any staff user end any session (e.g. a manager closing out a
    session an instructor abandoned). Follower IPs/names are deliberately not
    surfaced here — only counts — so this stays a session-control view, not an
    audience-enumeration one.
    """
    entries = manager.list_all_detailed()
    # Resolve owner usernames in one pass instead of per row. The same presenter
    # commonly owns multiple rows mid-supersede, and a deployment rarely has
    # more than a handful of concurrent sessions, so a single IN-style lookup
    # keeps this O(1) queries.
    owner_ids = {e.owner_user_id for e in entries}

    def _load() -> tuple:
        owners = {
            u.id: u
            for u in (db.query(User).filter(User.id.in_(owner_ids)).all() if owner_ids else [])
        }
        return owners, _load_slides(db, [e.slide_id for e in entries])

    owners, slides = await asyncio.to_thread(_load)
    out: List[AdminSessionOut] = []
    for e in entries:
        slide = slides.get(e.slide_id)
        owner = owners.get(e.owner_user_id)
        out.append(
            AdminSessionOut(
                session_id=e.session_id,
                slide_id=e.slide_id,
                slide_title=getattr(slide, "title", None) if slide else None,
                slide_catalog_id=getattr(slide, "catalog_id", None) if slide else None,
                owner_user_id=e.owner_user_id,
                owner_username=getattr(owner, "username", None) if owner else None,
                owner_role=getattr(owner, "role", None) if owner else None,
                presenter_connected=e.presenter_connected,
                follower_count=e.follower_count,
                created_at=e.created_at,
                grace_remaining=e.grace_remaining,
            )
        )
    # Stable, useful ordering: newest sessions first so a just-spawned orphan is
    # easy to spot at the top.
    out.sort(key=lambda s: s.created_at, reverse=True)
    return AdminSessionListOut(sessions=out)


# ----------------------------------------------------------- WS auth guard

def _client_ip(ws: WebSocket) -> str:
    return _net.get_client_ip(ws.headers, ws.client.host if ws.client else "unknown")


def _url_hostname(url: str) -> str:
    """Lower-cased hostname of ``url`` ("" when it has none or won't parse),
    without the one trailing dot a fully-qualified name may carry.

    ``wsi.example.org.`` is the same host as ``wsi.example.org``, but nginx
    strips the dot from ``$host`` (so the forwarded ``Host`` lacks it) while
    the browser keeps it in ``Origin``: compared as-is, every upgrade from a
    page opened by its FQDN-with-dot would be refused with a final 4403. Only one
    dot goes, so ``name..`` stays a different (invalid) name.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced "[" in an IPv6 literal
        return ""
    return host[:-1] if host.endswith(".") else host


def _ws_origin_allowed(headers) -> bool:
    """Whether a WS upgrade may come from where it says it comes from.

    The same-origin policy does not cover WebSockets: any page can open one to
    any host, and the browser attaches whatever ambient credentials it holds
    for that host. A SameSite=Lax session cookie is not among them (it stays
    off cross-site subresource requests), but cached HTTP Basic credentials
    are, and the break-glass / LAN-direct exemptions need no credential at all
    — so under those a malicious page in a LAN user's browser could open the
    discovery socket and read every live session id and the public_id of a
    link-only slide being presented, or join a session as a follower.

    * No ``Origin``: not a browser (curl, a script). There is no ambient
      credential to abuse, so it is judged by the usual gate alone, as the
      app's other cross-site checks do (see _reject_cross_site_post).
    * ``Sec-Fetch-Site`` present (browsers send it on WebSocket handshakes
      too, and script cannot set it): only ``same-origin``. The browser
      computes it against the URL it actually connected to, so it holds
      whatever a proxy hop did to ``Host``. The SPA always opens its sockets
      on ``location.host`` — the LAN-direct probe moves the whole page to the
      LAN host, it never connects cross-origin — so ``same-site`` (a sibling
      subdomain) is refused like ``cross-site``.
    * Otherwise the ``Origin`` hostname must equal the ``Host`` hostname —
      hostnames only, as in _reject_cross_site_post, because nginx forwards
      ``Host: $host`` without the port the browser's ``Origin`` carries, and
      with one trailing dot dropped on each side (see _url_hostname) — or
      the operator's canonical ``WSI_PUBLIC_BASE_URL`` host, in case a hop
      ahead of the app rewrites ``Host``. An opaque ``Origin: null``
      (sandboxed frame, file: page) is refused: the app never runs there.

    This is not a DNS-rebinding defence (a rebound name is same-origin with
    itself); the IP filter and the app-access gate still apply after it.
    """
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        return site == "same-origin"
    origin_host = _url_hostname(origin)
    if not origin_host:
        return False
    if origin_host == _url_hostname(f"//{headers.get('host') or ''}"):
        return True
    base = settings.public_base_url
    return bool(base) and origin_host == _url_hostname(base)


class _WsAdmission:
    """What a socket was admitted with, kept so _viewer_reauth_loop can
    re-check the same grounds for as long as the socket stays open."""

    __slots__ = ("basic_user", "basic_cred")

    def __init__(self) -> None:
        # Set when admitted through a verified Basic header: the username and
        # the fingerprint of the password hash it was verified against.
        self.basic_user = ""
        self.basic_cred = ""


async def _ws_guard(
    ws: WebSocket,
    admission: Optional[_WsAdmission] = None,
    *,
    presenter: bool = False,
) -> bool:
    """Admission check for a WS upgrade (see _ws_admission).

    Returns True iff the connection is permitted. On rejection the socket is
    closed with the appropriate 4xxx code and the caller should bail out.
    Pass ``admission`` to keep what the check admitted with, for
    _viewer_reauth_loop. ``presenter`` admits the socket without app access,
    leaving it to the caller to authenticate the staff token in its hello
    (see _ws_admission).

    A check that raises (a JWKS outage, a failed DB read — _ws_admission lets
    those escape rather than read them as a bad credential) still refuses the
    socket, but with CLOSE_TRY_AGAIN_LATER, which the clients retry.
    """
    try:
        code = await _ws_admission(
            ws, admission or _WsAdmission(), recheck=False, presenter=presenter
        )
    except Exception:
        _log.warning("sync: WS admission check failed; asking the client to retry", exc_info=True)
        code = CLOSE_TRY_AGAIN_LATER
    if code is None:
        return True
    await ws.close(code=code)
    return False


async def _viewer_reauth_loop(
    ws: WebSocket,
    bridge: "_WsBridge",
    admission: _WsAdmission,
    stop: asyncio.Event,
) -> None:
    """Keep re-checking the app-access gate a follower / discovery socket was
    admitted through, and close it once that no longer admits it.

    The upgrade check alone binds policy at connect time only: a socket opened
    with a session cookie would keep receiving live-session announcements (and
    room frames) after the cookie expired or every app session was revoked —
    only a reconnect would be refused. The presenter has the equivalent loop for its staff
    JWT and the IP filter (_presenter_reauth_loop); this one is for the viewer
    side. Closes go
    through ``bridge`` for the same single-writer reason, and a check that
    keeps raising fails closed after PRESENTER_REAUTH_MAX_FAILURES in a row —
    with CLOSE_TRY_AGAIN_LATER, since a raise is no verdict on the credential.
    """
    consecutive_failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=VIEWER_REAUTH_INTERVAL_SECONDS)
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        try:
            code = await _ws_admission(ws, admission, recheck=True)
        except Exception:
            consecutive_failures += 1
            _log.exception(
                "viewer reauth check raised (%d/%d consecutive); will retry",
                consecutive_failures,
                PRESENTER_REAUTH_MAX_FAILURES,
            )
            if (
                PRESENTER_REAUTH_MAX_FAILURES > 0
                and consecutive_failures >= PRESENTER_REAUTH_MAX_FAILURES
            ):
                # Still fail closed, but with the retryable code: _ws_admission
                # raises only when it could not reach a verdict (JWKS / DB
                # down), and a definitive refusal returns a code (handled
                # below, 4401 / 4403). 4401 here would be final for both
                # clients, stranding follower sync and the index banner until a reload
                # after any outage longer than the budget — what _ws_guard's
                # 1013 at upgrade avoids. On 1013 the client reconnects, and
                # the upgrade check (1013 again while the backend is down)
                # decides afresh.
                await bridge.close(CLOSE_TRY_AGAIN_LATER, "auth check unavailable")
                return
            continue
        consecutive_failures = 0
        if code is not None:
            await bridge.close(code, "access revoked")
            return


def _start_viewer_reauth(ws: WebSocket, bridge: "_WsBridge", admission: _WsAdmission):
    """Run _viewer_reauth_loop for a socket; returns an async stopper."""
    stop = asyncio.Event()
    task = asyncio.create_task(_viewer_reauth_loop(ws, bridge, admission, stop))

    async def _stop() -> None:
        stop.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return _stop


async def _ws_admission(
    ws: WebSocket, admission: "_WsAdmission", *, recheck: bool, presenter: bool = False,
) -> Optional[int]:
    """Mirror the HTTP-middleware checks (IP filter + CF Access + app_auth)
    on a WS upgrade.

    Returns None when the connection is permitted, else the 4xxx code to
    close it with. ``recheck`` re-evaluates an already-open socket (see
    _viewer_reauth_loop): the same grounds, against the current settings
    snapshot, except that a Basic credential already verified on this socket
    is re-checked through its fingerprint (``admission``) instead of another
    pbkdf2 verify (and so without the login throttle).

    A check that cannot be completed — the CF JWKS endpoint unreachable, a
    failed DB read — raises instead of returning a code, on the upgrade as on
    a re-check: _ws_guard turns it into CLOSE_TRY_AGAIN_LATER and
    _viewer_reauth_loop counts it toward its failure budget. Either way the
    socket is not admitted on it; only the close code differs from a refusal.

    Order matches main.app_auth_middleware:
      1. IP filter
      1a. Origin (upgrade only; see _ws_origin_allowed). No HTTP counterpart:
         the same-origin policy already keeps another site from reading an
         HTTP response, but not a WebSocket's frames.
      2. Admin Bearer JWT (admin overrides every other gate, parity with HTTP)
      3. Cloudflare Access JWT (valid → admit; invalid → 4401; absent → fall
         through). Without this, a CF-Access-fronted deployment with mode!=none
         either rejects external CF users at the cookie check (mode!=none) or
         silently admits invalid-JWT requests on WS while the matching HTTP
         path returns 401 (mode==none).
      3a. ``presenter``: admit here, without app access. Every staff token
         bypasses the app-access gate on HTTP, but the one a presenter holds
         cannot reach step 2: browsers cannot set Authorization on a
         WebSocket upgrade, so the client sends it in its hello frame instead
         — after this check has run. Gating a presenter on app access
         would therefore 4401 every staff presenter with no app session of
         its own (ldap / google_oauth, basic without cached credentials), while
         the dashboard had just let them create the session. sync_ws instead
         authenticates the hello's staff token (_presenter_hello) BEFORE it
         looks the room up, so a client this step let through learns nothing
         about the room — not even whether it exists (4404 vs 4401) — without
         a valid staff token. Steps 1, 1a and 3 still apply first: the IP
         filter binds staff too, as does the Origin check, and an invalid CF
         assertion is refused whoever sends it. What an unauthenticated client gains by getting this far
         is a socket held for at most HELLO_TIMEOUT_SECONDS (the handshake
         rate is metered by nginx's wsi_sync_ws zone) and one JWT signature
         check — a forged token never reaches the users table.
      4. Break-glass / direct-LAN-IP exemptions (opt-in). The HTTP gate admits
         these before the mode switch; mirror it here so a follower reaching
         the app over the documented raw-IP / LAN-direct route can still open
         the live-sync WebSocket instead of being rejected with 4401 while its
         HTTP traffic is allowed through. Without this, enabling
         WSI_BREAKGLASS_NOAUTH / lan_direct_noauth_mode under a gated mode
         silently breaks live sync for exactly those clients.
      5. Mode-based auth (basic / ldap / google_oauth)
    """
    if not _net.ip_allowed(_client_ip(ws)):
        return CLOSE_IP_DENIED
    # The headers of an open socket never change, so a re-check skips this.
    if not recheck and not _ws_origin_allowed(ws.headers):
        return CLOSE_ORIGIN_DENIED

    auth_header = ws.headers.get("authorization", "")
    # Any staff bearer (admin / manager / instructor) bypasses the gate, matching
    # the HTTP middleware: a manager presenting a live-sync session under a gated
    # mode must not be 4401'd here when the same token is honoured everywhere
    # else. staff_token_valid opens a SessionLocal + sync DB read; cf_jwt_status
    # may do a synchronous JWKS HTTPS fetch; the Basic verify (below) burns pbkdf2
    # CPU. All three would block this worker's event loop — and a burst of WS
    # upgrades would serialise on it, freezing every live sync room — so each
    # is offloaded to a thread, matching the rest of this module.
    if auth_header and await asyncio.to_thread(_app_auth.staff_token_valid, auth_header):
        return None

    client_host = ws.client.host if ws.client else "127.0.0.1"
    # No assertion header (the common LAN / bypass case) short-circuits to
    # "absent" without the thread hop, as in main.app_auth_middleware.
    if ws.headers.get(_cf_access.CF_JWT_HEADER):
        # A JWKS outage raises (see the docstring) instead of reading as an
        # invalid assertion and closing with 4401, which the clients treat as
        # final — on the upgrade too, where it would strand a CF user's
        # follower sync and live banner until a reload.
        cf_status, _cf_claims = await asyncio.to_thread(
            _cf_access.cf_jwt_status, ws.headers, client_host, raise_unavailable=True
        )
    else:
        cf_status, _cf_claims = "absent", None
    if cf_status == "valid":
        return None
    if cf_status == "invalid":
        return CLOSE_UNAUTHORIZED
    if presenter:
        # Step 3a: the staff token in the hello frame decides (see above).
        return None
    # cf_status == "absent": fall through to the opt-in exemptions and then
    # mode-based auth, matching main.app_auth_middleware's order.

    # Break-glass (plaintext, non-tunnel raw-IP) and direct-LAN-IP exemptions.
    # Both are no-ops unless the operator opted in, and both gate on
    # nginx-set, non-forgeable signals (see app_auth.breakglass_exempt /
    # lan_direct_exempt). Mirrored from the HTTP middleware so the same
    # raw-IP / LAN-direct request that is admitted for HTTP can also open the
    # live-sync WebSocket. These run inline (no DB / network I/O, like the
    # HTTP path) before the mode switch.
    if _app_auth.breakglass_exempt(ws.headers, client_host):
        return None
    if _app_auth.lan_direct_exempt(ws.headers, client_host):
        return None

    mode = _app_auth.current_mode()
    if mode == "none":
        return None

    if mode == "basic":
        # Mirror app_auth_middleware: accept either the form-login session
        # cookie or a fresh HTTP Basic header. Browsers that authenticated
        # via /api/auth/login would otherwise be unable to open the
        # follower / discovery WebSockets even though their HTTP traffic
        # works.
        cookie = ws.cookies.get(_app_auth.SESSION_COOKIE)
        # raise_errors: a failed credential read must reach _ws_guard's retry
        # close or _viewer_reauth_loop's failure counter, not read as
        # "revoked" (see the docstring).
        if cookie and await asyncio.to_thread(
            _app_auth.decode_session_token, cookie, raise_errors=True
        ):
            return None
        creds = _app_auth.parse_basic_header(auth_header)
        if creds and recheck and admission.basic_cred and admission.basic_user == creds[0]:
            # Re-checked through the fingerprint of the hash the password was
            # verified against earlier: deleting the user or resetting the
            # password ends the socket, at the cost of a cached indexed read
            # rather than a pbkdf2 verify per socket per interval. (A header
            # never verified yet — the socket got in on a cookie that has
            # since lapsed — takes the full verify below once, as a fresh
            # upgrade would, and is fingerprinted from then on.)
            if await asyncio.to_thread(
                _app_auth._basic_session_live, admission.basic_user, admission.basic_cred,
                raise_errors=True,
            ):
                return None
            return CLOSE_UNAUTHORIZED
        if creds:
            # Per-IP throttle on the WS Basic surface, using the same "app:"
            # keyspace and limits as the HTTP Basic branch in
            # main.app_auth_middleware and POST /api/auth/login. The counter
            # itself is process-local (see login_throttle) and nginx routes
            # these upgrades to the separate wsi-sync worker, so in production
            # this is the sync process's own bucket rather than a shared one —
            # the policy is mirrored, the tally is not.
            #
            # Without it this path would have no application-level bound at
            # all: only nginx's generic wsi_api zone (120 r/m, vs 10 r/m on the
            # login zones) would stand between an attacker and one full pbkdf2
            # verify per upgrade, against the same credential space the HTTP
            # surfaces deliberately meter. Pre-check before the verify so a
            # throttled IP pays nothing.
            #
            # The rejection uses CLOSE_RATE_LIMITED rather than the
            # CLOSE_UNAUTHORIZED the rest of this guard sends. Both endpoints
            # accept() before calling the guard precisely so these codes reach
            # the client, and sync.ts treats 4401 as terminal — it stops the
            # reconnect loop and reports "unauthorized" for good. Applying
            # that to a window that clears itself in
            # app_login_ip_window_seconds would strand a client holding
            # correct credentials until it was re-instantiated. 4429 is not on
            # either client's stop-list, so they back off and retry and
            # recover on their own. Retries stay cheap: a throttled attempt
            # returns above without touching pbkdf2 and records no new
            # failure, so reconnecting cannot extend the window.
            throttle_key = app_login_throttle_key(
                _net.get_client_ip(ws.headers, client_host)
            )
            retry_after = _login_throttle.retry_after_seconds(
                throttle_key,
                max_failures=settings.app_login_ip_max_failures,
                window_seconds=settings.app_login_ip_window_seconds,
            )
            if retry_after > 0:
                return CLOSE_RATE_LIMITED

            def _verify_basic() -> Optional[str]:
                db = SessionLocal()
                try:
                    return _app_auth.verify_basic_credential(creds[0], creds[1], db)
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
            credential = await asyncio.get_running_loop().run_in_executor(
                _basic_auth_executor(), _verify_basic
            )
            if credential:
                _login_throttle.clear_user(throttle_key, creds[0])
                admission.basic_user, admission.basic_cred = creds[0], credential
                return None
            # With the secret, as in the HTTP Basic branch: a tab still
            # replaying a rotated password re-sends it on every reconnect of
            # its follower and discovery sockets, and counting each one would
            # lock the whole address (a classroom behind NAT) out of the WS
            # surface, correct passwords included. A repeat of an
            # already-counted (username, password) pair is counted once per
            # window; every distinct guess still counts.
            _login_throttle.record_failure(
                throttle_key,
                window_seconds=settings.app_login_ip_window_seconds,
                max_failures=settings.app_login_ip_max_failures,
                username=creds[0],
                secret=creds[1],
            )
        return CLOSE_UNAUTHORIZED

    # ldap / google_oauth: cookie session. Off the loop and with
    # raise_errors, like the basic branch: decoding reads the settings
    # snapshot only today, but a check that could not be completed must reach
    # the retry close rather than read as a bad cookie (a final 4401).
    cookie = ws.cookies.get(_app_auth.SESSION_COOKIE)
    if cookie and await asyncio.to_thread(
        _app_auth.decode_session_token, cookie, raise_errors=True
    ):
        return None
    return CLOSE_UNAUTHORIZED


# ----------------------------------------------------------- token decoding
# Presenter authentication shares its rules with admin-API endpoints (staff
# JWT, no `aud`, signature check, exp, token_version). The single source of
# truth for those rules lives in auth.resolve_staff_user, used directly by
# _resolve_active_presenter so the WS handlers don't need to know the
# algorithm details.
def _resolve_active_presenter(token: str) -> Optional[int]:
    """Decode a presenter token and confirm the user is still active staff.

    Returns the user_id on success, or None if the token is invalid, the
    user no longer exists, has been disabled, or no longer holds a staff
    role. Mirrors the checks in ``auth.get_current_user`` + ``auth.require_staff``
    so a JWT minted before the account was disabled / demoted cannot keep
    driving a live session until expiry. Any of admin / manager /
    instructor may present — the matrix in the role docs explicitly allows
    instructors to open sync sessions.
    """
    db = SessionLocal()
    try:
        # resolve_staff_user covers signature/exp, existence, is_active, AND
        # the token_version generation, so a stale JWT can't keep driving a
        # live session after the account is disabled / demoted / reset.
        user = _admin_auth.resolve_staff_user(token, db)
        if user is None or user.role not in STAFF_ROLES:
            return None
        return user.id
    finally:
        db.close()


async def _resolve_hostname(ip: str) -> Optional[str]:
    if not ENABLE_REVERSE_DNS:
        return None
    import socket
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_RDNS_EXECUTOR, socket.gethostbyaddr, ip)
    try:
        host = await asyncio.wait_for(fut, timeout=REVERSE_DNS_TIMEOUT)
        return host[0] if host else None
    except Exception:
        # On timeout the executor thread keeps running until libc returns,
        # but it's bounded by _RDNS_EXECUTOR's max_workers and doesn't
        # contend with the default thread pool.
        return None


# ------------------------------------------------------------ WS plumbing

class _WsBridge:
    """Adapter exposing a Starlette WebSocket as the (send, close) callbacks
    expected by ``RoomManager``, backed by a non-blocking outbound queue.

    ``send`` appends to a bounded per-connection deque and returns at once; a
    single dedicated writer task drains it onto the socket. This decouples the
    broadcast fan-out (driven from the presenter's recv loop) from any one
    slow / stuck follower: a follower whose receive window is full does not
    block ``ws.send_json`` on the fan-out path, so it cannot stall the whole
    room while the OS retransmit timer counts down. A follower that falls
    ``MAX_OUTBOUND_QUEUE`` frames behind, or whose individual send exceeds
    ``WS_SEND_TIMEOUT_SECONDS``, is dropped and its socket closed.

    The lone writer task is also the only thing that touches ``ws.send_json``
    / ``ws.send_text`` (a pre-encoded SharedFrame) / ``ws.close``, so sends
    and the close frame can never interleave —
    mutual exclusion is structural here, not enforced by a per-socket send lock.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._queue: Deque[dict] = collections.deque()
        self._wake = asyncio.Event()
        self._closing = False
        # Set the instant the bridge starts closing (teardown-driven close,
        # keepalive/ reauth close, overflow, or writer death). The recv loop
        # races ``ws.receive()`` against this so a frozen peer's recv side is
        # released promptly instead of blocking on an un-closable socket until
        # WS_RECV_IDLE_TIMEOUT. Purely a recv-side wakeup — the writer still owns
        # the actual close frame.
        self.closing_event = asyncio.Event()
        self._close_code = 1000
        self._close_reason = ""
        self._writer: Optional[asyncio.Task] = None
        # True once the writer task has exited (any path). After this, the
        # writer will never observe a wake, so close()/aclose() must close the
        # socket directly instead of delegating to it — otherwise a writer that
        # died on a send timeout leaves the socket (and its recv loop) leaked,
        # since the keepalive and teardown both route their close through the
        # now-dead writer.
        self._writer_done = False
        # Guards _safe_close so the underlying ws.close() is attempted at most
        # once no matter how many paths race to close (writer error path,
        # keepalive, teardown, aclose).
        self._socket_closed = False

    def start(self) -> None:
        """Spin up the writer task. Call once, after ``accept``."""
        if self._writer is None:
            self._writer = asyncio.create_task(self._run())

    async def send(self, msg: dict) -> None:
        # Non-blocking: enqueue and wake the writer. Never awaits the socket,
        # so a slow peer cannot back-pressure the broadcast fan-out.
        if self._closing:
            return
        self._queue.append(msg)
        if len(self._queue) > MAX_OUTBOUND_QUEUE:
            # Hopelessly behind: drop the backlog and schedule a close. Keeping
            # the queue would let a frozen receiver grow it without bound. 1013
            # ("try again later") is the closest standard code; the client's
            # reconnect loop treats it as a transient close and backs off.
            self._queue.clear()
            self._closing = True
            self._close_code = CLOSE_TRY_AGAIN_LATER
            self._close_reason = "send backlog overflow"
            self.closing_event.set()
        self._wake.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        # Queue an ordered close so frames already enqueued (e.g. the ``end``
        # frame _teardown_room sends right before this) still flush first.
        if not self._closing:
            self._closing = True
            self._close_code = code
            self._close_reason = reason
            self._wake.set()
        # Wake the recv loop regardless of who first flipped ``_closing`` so a
        # second close path (teardown after keepalive, say) still releases it.
        self.closing_event.set()
        # If the writer already exited it will never act on the wake above, so
        # the socket would never be closed (the keepalive/teardown that called
        # close() would silently no-op). Close directly as a last resort — safe
        # because a finished writer cannot be mid-send_json, so there is no
        # interleave for the single-writer discipline to protect against, and
        # _safe_close is idempotent.
        if self._writer_done:
            await self._safe_close()

    async def _run(self) -> None:
        try:
            while True:
                if not self._queue and not self._closing:
                    self._wake.clear()
                    await self._wake.wait()
                while self._queue:
                    msg = self._queue.popleft()
                    # A fanned-out frame carries its encoding, made once for
                    # every recipient (see sync_room.SharedFrame).
                    try:
                        send = (
                            self.ws.send_text(msg.encoded())
                            if isinstance(msg, SharedFrame) else self.ws.send_json(msg)
                        )
                        await asyncio.wait_for(send, timeout=WS_SEND_TIMEOUT_SECONDS)
                    except Exception:
                        # Timeout or transport error: the peer is gone. Mark the
                        # bridge closing (so send() stops accepting frames into a
                        # dead writer and the state stays consistent), best-effort
                        # close the socket, and exit.
                        self._closing = True
                        self.closing_event.set()
                        await self._safe_close()
                        return
                if self._closing:
                    await self._safe_close()
                    return
        finally:
            # Record that the writer has exited so close()/aclose() know to
            # close the socket themselves rather than delegating to a task that
            # will never run again.
            self._writer_done = True

    async def _safe_close(self) -> None:
        # Idempotent: only the first caller actually issues ws.close(); the rest
        # (writer error path, keepalive-triggered close, teardown, aclose) no-op.
        if self._socket_closed:
            return
        self._socket_closed = True
        try:
            await asyncio.wait_for(
                self.ws.close(code=self._close_code, reason=self._close_reason),
                timeout=2.0,
            )
        except Exception:
            pass

    async def aclose(self) -> None:
        """Stop the writer task. Called from the WS handler's finally clause.

        Signals a drain-then-close and waits briefly; if the writer is stuck
        on a frozen socket, ``wait_for`` cancels it so the handler can unwind.
        """
        self._closing = True
        self.closing_event.set()
        self._wake.set()
        if self._writer is None:
            return
        try:
            await asyncio.wait_for(self._writer, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        # Backstop: if the writer died on a send error (or was cancelled above)
        # without closing the socket, close it here so the socket and its recv
        # loop are not leaked. Idempotent when the writer already closed cleanly.
        await self._safe_close()


# Returned by _hello_with_timeout when no frame arrived in time, as opposed to
# None for a frame that is not a usable hello.
_HELLO_LATE = object()


async def _hello_with_timeout(ws: WebSocket):
    """The client's hello frame (a dict whose ``type`` is ``hello``), None
    when its first frame is anything else, or ``_HELLO_LATE`` when none
    arrived within HELLO_TIMEOUT_SECONDS.

    Late and malformed are told apart because a slow link is not the client's
    fault: the presenter is asked to retry (1013) instead of being refused for
    good (4400), and a follower is attached anonymously, its hello still
    applied when it does arrive (see _run_follower).

    A peer that disconnects instead raises ``WebSocketDisconnect``: the
    caller must give up rather than treat it like a silent client. Attaching
    it anyway (a follower without a hello joins as anonymous) would register a
    ghost follower, and every further ``ws.receive()`` on the consumed
    disconnect raises a RuntimeError that the recv loop would spin on.
    """
    try:
        msg = await asyncio.wait_for(
            _receive_json_capped(ws), timeout=HELLO_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return _HELLO_LATE
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "hello":
        return None
    return msg


def _published_public_ids(slide_ids: List[int]) -> Dict[int, str]:
    """Map each *published* slide among ``slide_ids`` to its public_id.

    The publish-gate lookup of the WS join and slide-change paths, mirroring
    the gate ``create_sync_session`` applies at session creation. A slide that
    is missing or unpublished is absent from the result. It backs:

    * the follower join check: a slide pulled from view mid-session stops
      accepting new followers;
    * the presenter slide change: a live room must not be re-bound onto a
      draft via a ``view`` frame, since followers can't fetch its tiles (the
      public/dzi endpoints filter unpublished slides); one round-trip also
      yields the token followers navigate by.

    The discovery channel's stricter rule (published *and* listed) is
    _listed_slide_labels; the periodic re-check of a running room is
    _slide_status.

    A plain function the callers run through ``asyncio.to_thread``, so the
    SQLite query never blocks the event loop for other rooms / sockets.
    """
    if not slide_ids:
        return {}
    db = SessionLocal()
    try:
        query = db.query(Slide.id, Slide.public_id).filter(
            Slide.id.in_(set(slide_ids)), Slide.is_published.is_(True)
        )
        return {r[0]: r[1] or "" for r in query.all()}
    finally:
        db.close()


# --------------------------------------------------------- sync WS endpoint

@router.websocket("/api/sync/ws/{sid}")
async def sync_ws(websocket: WebSocket, sid: str, role: str = "follower"):
    # Accept first so subsequent close() calls can deliver an application-
    # private 4xxx code (CLOSE_NOT_FOUND, CLOSE_UNAUTHORIZED, ...) over the
    # wire. ASGI's contract for close-before-accept is "deny the handshake
    # with HTTP 403" — the 4xxx code is silently dropped and the browser
    # only sees CloseEvent.code == 1006. That defeats the dedicated
    # 4401/4404/4409 branches in sync.ts's reconnect loop and turns a
    # permanent failure (revoked auth, room reaped by GC, ownership
    # mismatch) into an infinite reconnect storm.
    await websocket.accept()

    is_presenter = role == "presenter"
    admission = _WsAdmission()
    if not await _ws_guard(websocket, admission, presenter=is_presenter):
        return

    # A presenter got past the guard without app access (see _ws_admission,
    # step 3a), so its staff token is checked here, before the room lookup
    # below: a client without one must not learn whether the room exists.
    authed = None
    if is_presenter:
        authed = await _presenter_hello(websocket)
        if authed is None:
            return

    room = manager.get(sid)
    if not room:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    # Re-run the publish gate for joiners (see _peek_visible): a slide pulled
    # from view mid-session must stop accepting new followers rather than stay
    # joinable until the room ages out. Presenters are exempt so they can still
    # reattach and stop the session cleanly.
    #
    # Refused with the retryable 1013, not 4404: the socket may be a follower
    # already in the session reconnecting after a blip, which 4404 (final for
    # the client) would drop out of it for good although the session goes on and
    # follows the presenter onto the next published slide. Retrying, it is
    # admitted once the room is on one again. A new joiner is stopped earlier
    # anyway, by the peek's 404 (the join dialog never opens); what a sid holder
    # learns here is only that the session is still running.
    if not is_presenter:
        slide_id = room.slide_id
        try:
            published = await asyncio.to_thread(_published_public_ids, [slide_id])
        except Exception:
            # No verdict (the DB read failed): ask the client to retry, as the
            # admission checks do, rather than fail with a bare 1011.
            _log.warning("sync: follower publish check failed; asking the client to retry", exc_info=True)
            await websocket.close(code=CLOSE_TRY_AGAIN_LATER, reason="check unavailable")
            return
        if slide_id not in published:
            await websocket.close(code=CLOSE_TRY_AGAIN_LATER, reason="slide unavailable")
            return

    bridge = _WsBridge(websocket)
    bridge.start()
    try:
        if is_presenter:
            await _run_presenter(websocket, bridge, sid, authed)
        else:
            await _run_follower(websocket, bridge, sid, admission)
    finally:
        await bridge.aclose()


async def _presenter_hello(ws: WebSocket):
    """Read a presenter's hello and authenticate the staff token it carries.

    Returns ``(hello, token, user_id)``, or None once the socket has been
    closed (or the peer is gone). This is the presenter's whole admission
    beyond the IP filter and the CF check (see _ws_admission, step 3a), so it
    must run before anything about the room is revealed.
    """
    try:
        hello = await _hello_with_timeout(ws)
    except WebSocketDisconnect:
        return None
    if hello is _HELLO_LATE:
        # A slow link, not a bad client: 4400 is final for the presenter
        # client, which would then drop a live session over a congested network.
        # On 1013 it reconnects, and the room's grace window holds meanwhile.
        await ws.close(code=CLOSE_TRY_AGAIN_LATER, reason="hello timeout")
        return None
    if hello is None:
        await ws.close(code=CLOSE_BAD_REQUEST, reason="missing hello")
        return None
    token = hello.get("token")
    if not isinstance(token, str):
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return None
    # Run the sync DB lookup (SessionLocal + db.get(User, ...)) off-loop, as
    # the reauth loop does. Calling _resolve_active_presenter synchronously
    # here would block every other room's event loop work for the duration of
    # the SQLite query.
    try:
        user_id = await asyncio.to_thread(_resolve_active_presenter, token)
    except Exception:
        # The users-table read failed: no verdict on the token, so ask the
        # client to retry rather than call it invalid (see _ws_guard).
        _log.warning("sync: presenter token check failed; asking the client to retry", exc_info=True)
        await ws.close(code=CLOSE_TRY_AGAIN_LATER, reason="auth check unavailable")
        return None
    if user_id is None:
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return None
    return hello, token, user_id


async def _run_presenter(
    ws: WebSocket, bridge: _WsBridge, sid: str, authed=None,
) -> None:
    # ``authed`` is what sync_ws's _presenter_hello already read and verified;
    # without it (direct callers) the hello is read and checked here.
    if authed is None:
        authed = await _presenter_hello(ws)
        if authed is None:
            return
    hello, token, user_id = authed

    # Every hello carries the presenter client-instance id (``client_id``, a
    # random token each SyncPresenter mints once); an automatic reconnect adds
    # ``resume: true``. A first connect or an explicit Resume sends no marker
    # and always takes over. A missing / malformed id, or any ``resume`` other
    # than a literal true, is treated as a fresh attach, so a malformed value
    # can never lock a presenter out of their own session.
    client_id = sanitize_presenter_client_id(hello.get("client_id"))
    resume = hello.get("resume") is True and client_id is not None
    try:
        attached = await manager.attach_presenter(
            sid, user_id=user_id, send=bridge.send, close=bridge.close,
            client_id=client_id, resume=resume,
        )
    except PresenterSupersededError:
        # Another tab/device took the session over while this one was away
        # (e.g. a laptop that slept through a Resume on a tablet). Same code the
        # evicted socket would have received, so the client stops reconnecting
        # instead of stealing the session back. Closed through the bridge,
        # whose writer owns the socket from here on (the single-writer rule).
        await bridge.close(4000, "presenter replaced")
        return
    if attached is None:
        # Ownership mismatch (the room belongs to a different admin) or the
        # room was ended / reaped between the WS upgrade and the attach.
        # Owner-collision recovery (transient drop, fast reconnect) is
        # handled inside attach_presenter via stale-conn eviction, so any
        # None return here is a permanent reject. The two causes get their own
        # codes, because the client words them differently: a room that is gone
        # is 4404 like the lookup in sync_ws ("session ended"), and only a room
        # that still exists under another owner is 4409. Sending 4409 for both
        # would tell a presenter whose session had just ended that it had no
        # right to it.
        if manager.get(sid) is None:
            await bridge.close(CLOSE_NOT_FOUND, "session not found")
        else:
            await bridge.close(CLOSE_CONFLICT, "cannot attach presenter")
        return
    _room, conn = attached
    # Tell the client it is seated: from then on its automatic reconnects carry
    # the resume marker. Sent after the audience snapshot attach_presenter
    # already queued, so the first non-ping frame the client sees is still that
    # snapshot.
    await bridge.send({"type": "attached"})

    stop_reauth = asyncio.Event()
    reauth_task = asyncio.create_task(
        _presenter_reauth_loop(ws, bridge, token, user_id, stop_reauth, sid=sid)
    )
    try:
        await _ws_main_loop(ws, bridge, sid, role="presenter", presenter=conn)
    finally:
        stop_reauth.set()
        if not reauth_task.done():
            reauth_task.cancel()
            try:
                await reauth_task
            except (asyncio.CancelledError, Exception):
                pass
        # Pass this conn explicitly so a same-owner reconnect that already
        # replaced it in attach_presenter is not undone by this finally clause.
        await manager.detach_presenter(sid, conn)


async def _presenter_reauth_loop(
    ws: WebSocket,
    bridge: _WsBridge,
    token: str,
    user_id: int,
    stop: asyncio.Event,
    *,
    sid: Optional[str] = None,
) -> None:
    """Periodically confirm the presenter is still an active admin, reaching
    the server from an address the IP filter still allows — and, with
    ``sid``, bring the room in line with what staff have done to the slide it
    is on since (unpublished, made link-only or listed again; see
    RoomManager.refresh_slide_status). The publish gate otherwise only runs on
    a slide change.

    The staff JWT exempts the presenter from the app-access gate (as on every
    other surface), but not from the IP filter: the HTTP middleware refuses a
    denied address with 403 whatever the bearer. So a deny rule added
    mid-session closes the presenter socket with 4403 within one interval,
    exactly like the follower / discovery re-check (_viewer_reauth_loop), and
    the client IP is resolved the same way (_client_ip).

    Closes the socket on revocation. SessionLocal is sync, so the DB lookup
    runs in a worker thread to avoid blocking the event loop. The close is
    routed through ``bridge`` (never a bare ``ws.close``) so it is ordered
    after any queued frames by the bridge's lone writer task and cannot
    interleave with an in-flight ``send_json`` on the same socket — the same
    single-writer discipline the keepalive close follows. The recv loop in
    ``_ws_main_loop`` then observes the close as a ``WebSocketDisconnect``
    and exits, triggering presenter detach.

    A single transient DB failure is tolerated (the streak resets on any check
    that completes), but after ``PRESENTER_REAUTH_MAX_FAILURES`` *consecutive*
    failures the loop fails closed and drops the presenter (with the retryable
    CLOSE_TRY_AGAIN_LATER; a revocation is 4401) — a persistently
    unreachable DB must not let a possibly-revoked presenter keep driving until
    the JWT's 24h expiry.
    """
    consecutive_failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=PRESENTER_REAUTH_INTERVAL_SECONDS
            )
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        # In-memory settings snapshot, no I/O: checked inline, before the DB
        # lookup, in the same order as _ws_admission.
        if not _net.ip_allowed(_client_ip(ws)):
            await bridge.close(CLOSE_IP_DENIED, "access revoked")
            return
        try:
            still_active = await asyncio.to_thread(_resolve_active_presenter, token)
        except Exception:
            # DB hiccup. Tolerate a transient one (prefer the live session over
            # a false-positive disconnect), but count consecutive failures so a
            # *persistent* outage can't fail open: reauth is the only revocation
            # path between connect and the JWT's 24h expiry.
            consecutive_failures += 1
            _log.exception(
                "presenter reauth check raised (%d/%d consecutive); will retry",
                consecutive_failures,
                PRESENTER_REAUTH_MAX_FAILURES,
            )
            if (
                PRESENTER_REAUTH_MAX_FAILURES > 0
                and consecutive_failures >= PRESENTER_REAUTH_MAX_FAILURES
            ):
                # Can no longer confirm the presenter is still authorized; fail
                # closed. Routed through the bridge like the revocation close so
                # it can't race an in-flight send_json. 1013, not 4401: the DB
                # gave no verdict on the token, and 4401 is final for the
                # client, which would leave a valid presenter stopped until a
                # reload. On 1013 it reconnects; its hello is checked afresh
                # (_presenter_hello answers 1013 again while the DB is down)
                # and the room's grace window holds the session meanwhile.
                await bridge.close(CLOSE_TRY_AGAIN_LATER, "auth check unavailable")
                return
            continue
        # A check that completed clears the streak — one transient failure must
        # never accumulate toward the fail-closed threshold across quiet ticks.
        consecutive_failures = 0
        if still_active != user_id:
            # Route the revocation close through the bridge so it can never
            # race an in-flight send_json from the writer task (bridge.close
            # only flips flags + wakes the writer, so it cannot raise).
            await bridge.close(CLOSE_UNAUTHORIZED, "auth revoked")
            return
        if sid is not None:
            await _refresh_room_slide(sid)


def _slide_status(slide_id: int) -> tuple:
    """``(published, listed, labels)`` for one slide (a missing row is
    unpublished), for a to_thread hop; ``labels`` are the discovery fields."""
    db = SessionLocal()
    try:
        slide = db.get(Slide, slide_id)
        if slide is None or not slide.is_published:
            return False, False, {}
        return True, not slide.unlisted, _discovery_labels(slide.title, slide.catalog_id)
    finally:
        db.close()


async def _refresh_room_slide(sid: str) -> None:
    """One slide-status re-check of a room (see _presenter_reauth_loop). A
    failed read is logged and left to the next interval."""
    room = manager.get(sid)
    if room is None:
        return
    slide_id = room.slide_id
    try:
        published, listed, labels = await asyncio.to_thread(_slide_status, slide_id)
    except Exception:
        _log.warning("sync: slide status re-check failed for %s", sid, exc_info=True)
        return
    await manager.refresh_slide_status(
        sid, slide_id, published=published, listed=listed, labels=labels,
    )


async def _run_follower(
    ws: WebSocket, bridge: _WsBridge, sid: str, admission: Optional[_WsAdmission] = None,
) -> None:
    try:
        hello = await _hello_with_timeout(ws)
    except WebSocketDisconnect:
        # Gone before saying hello: nothing to attach.
        return
    late = hello is _HELLO_LATE
    if late and REQUIRE_NAME:
        # A slow link, not a nameless follower: 4400 is final for the client,
        # which would then show "could not join" to someone who had typed a name.
        # On 1013 it reconnects and sends its hello again.
        await bridge.close(CLOSE_TRY_AGAIN_LATER, "hello timeout")
        return
    if not isinstance(hello, dict):
        # A late hello is not fatal: the follower joins as anonymous now and
        # its hello is applied when it arrives (_handle_inbound). A first
        # frame that is not a hello at all joins as anonymous for good.
        hello = {}

    name = sanitize_name(hello.get("name"))
    if REQUIRE_NAME and not name:
        await bridge.close(CLOSE_BAD_REQUEST, "name required")
        return
    initial_visible = _hello_visible(hello)
    # Optional viewer-canvas size in CSS pixels, so the presenter's audience
    # panel can see which followers are on a differently-shaped screen (the
    # follower's fitBounds pads the short axis, so a mismatched aspect ratio
    # shows the presenter's field surrounded by extra margin). Forwarded raw —
    # FollowerMeta scrubs both values and drops the pair if either is
    # unusable, so an absent or hostile field just yields None/None.
    hello_vw = hello.get("vw")
    hello_vh = hello.get("vh")

    ip = _client_ip(ws)
    ua = ws.headers.get("user-agent", "")

    # Reverse-DNS is opt-in and runs *after* the attach, not before it, so it
    # can't add REVERSE_DNS_TIMEOUT seconds of join latency to every follower.
    # Attach first with hostname=None and let a background task fill it in
    # via manager.update_follower_hostname when the PTR result arrives, so
    # the audience frame the presenter sees only updates from placeholder
    # to resolved name (never blocks the join handshake).
    try:
        res = await manager.attach_follower(
            sid,
            ip=ip,
            ua=ua,
            name=name,
            hostname=None,
            send=bridge.send,
            close=bridge.close,
            visible=initial_visible,
            vw=hello_vw,
            vh=hello_vh,
            hello_received=not late,
        )
    except RoomFullError:
        await bridge.close(CLOSE_TOO_BUSY, "room full")
        return
    if res is None:
        await bridge.close(CLOSE_NOT_FOUND, "session not found")
        return
    _room, meta = res
    # Tell the client it is seated. The server accepts every upgrade before
    # its checks, so a refusal (4503, 1013, ...) still opens the socket first:
    # the client reports itself connected only on a frame like this one, which
    # a refused socket never gets. Queued after the replayed view / pointer /
    # absence notice attach_follower sent, which are proof of it just as well.
    await bridge.send({"type": "attached"})

    # Not while the panel is pseudonymous: the name would be withheld anyway
    # (see _audience_ip_pseudonymiser), so the PTR lookup of the address
    # would be pure cost.
    if ENABLE_REVERSE_DNS and _audience_ip_pseudonymiser() is None:
        _spawn_background(_resolve_and_update_hostname(sid, meta.id, ip))

    # Not for the presenter: _presenter_reauth_loop re-checks its staff JWT
    # (which bypasses the app-access gate on every other surface too) and the
    # IP filter.
    stop_reauth = _start_viewer_reauth(ws, bridge, admission or _WsAdmission())
    try:
        await _ws_main_loop(ws, bridge, sid, role="follower", follower_id=meta.id)
    finally:
        await stop_reauth()
        await manager.detach_follower(sid, meta.id)


# Strong references to fire-and-forget background tasks (the per-follower
# reverse-DNS lookups). asyncio only holds a weak reference to a task, so a
# bare ``create_task`` whose result is discarded can be garbage-collected
# mid-flight — silently dropping the hostname update. Hold the task here and
# let it remove itself on completion. Mirrors how every other create_task in
# this module keeps its handle (_writer, reauth_task, keepalive, _gc_task).
_background_tasks: Set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _resolve_and_update_hostname(sid: str, follower_id: str, ip: str) -> None:
    """Resolve PTR for ``ip`` off the join critical path."""
    try:
        hostname = await _resolve_hostname(ip)
    except Exception:
        return
    if hostname:
        try:
            await manager.update_follower_hostname(sid, follower_id, hostname)
        except Exception:
            _log.exception("update_follower_hostname failed")


def _start_ws_keepalive(bridge: "_WsBridge"):
    """Start a keepalive ping task on a WebSocket.

    Returns ``(mark_inbound, stop)``:

    * ``mark_inbound()`` resets the liveness clock — call it whenever the
      caller's recv loop sees any inbound frame.
    * ``await stop()`` cancels the keepalive cleanly. Always pair this with
      a finally-block in the caller.

    Shared by ``_ws_main_loop`` (presenter / follower) and ``discovery_ws``
    so the timeout and ping cadence stay in lockstep across all WS surfaces.

    Pings go through ``bridge.send`` rather than ``ws.send_json`` directly
    so they are serialised by the bridge's lone writer task: otherwise a
    keepalive ping racing with a broadcast could interleave at the
    transport level.
    """
    last_inbound = time.monotonic()
    stop_event = asyncio.Event()

    async def loop() -> None:
        nonlocal last_inbound
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=PING_INTERVAL_SECONDS)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last_inbound > PING_TIMEOUT_SECONDS:
                # Close through the bridge so the close frame is ordered after
                # any queued frames by the lone writer task and cannot
                # interleave with an in-flight broadcast send on the same
                # socket. A bare ws.close() here would bypass the very
                # serialization _WsBridge exists to provide.
                await bridge.close(1000, "ping timeout")
                stop_event.set()
                return
            try:
                await bridge.send({"type": "ping"})
            except Exception:
                stop_event.set()
                return

    task = asyncio.create_task(loop())

    def mark_inbound() -> None:
        nonlocal last_inbound
        last_inbound = time.monotonic()

    async def stop() -> None:
        stop_event.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return mark_inbound, stop


# Sentinel returned by _recv_with_bridge_close when the bridge starts closing
# (teardown / keepalive / reauth revocation) instead of a real inbound frame.
_RECV_BRIDGE_CLOSED = object()


async def _recv_with_bridge_close(
    ws: WebSocket, bridge: "_WsBridge", bucket: Optional[_InboundBucket] = None
):
    """Await one inbound frame, but bail promptly if the bridge starts closing.

    Races ``ws.receive()`` against ``bridge.closing_event`` (set by every bridge
    close path) so a recv loop is not left blocked on an un-closable frozen
    socket after the room is torn down: it returns ``_RECV_BRIDGE_CLOSED``
    and the caller unwinds instead of waiting out WS_RECV_IDLE_TIMEOUT. With a
    ``bucket`` an over-limit frame comes back as ``_FRAME_DROPPED``. Raises
    ``WebSocketDisconnect`` / ``ValueError`` exactly like ``_receive_json_capped``,
    or ``asyncio.TimeoutError`` on the idle backstop.
    """
    if bridge.closing_event.is_set():
        return _RECV_BRIDGE_CLOSED
    recv = asyncio.ensure_future(_receive_json_capped(ws, bucket))
    closing = asyncio.ensure_future(bridge.closing_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {recv, closing},
            timeout=WS_RECV_IDLE_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if recv in done:
            return recv.result()  # re-raises WebSocketDisconnect / ValueError
        if not done:
            raise asyncio.TimeoutError
        return _RECV_BRIDGE_CLOSED
    finally:
        # Cancel whichever waiter didn't finish. Cancelling ``recv`` cancels
        # only this inner ws.receive() sub-task — never the handler task — so it
        # can't surface as a spurious cancellation on the caller (the TestClient
        # portal future, in particular).
        for t in (recv, closing):
            if not t.done():
                t.cancel()
        # Retrieve a real recv exception about to be discarded (closing won the
        # race but recv also finished with an error) so it isn't flagged as a
        # never-retrieved task exception.
        if recv.done() and not recv.cancelled() and recv.exception() is not None:
            pass


async def _ws_main_loop(
    ws: WebSocket,
    bridge: "_WsBridge",
    sid: str,
    *,
    role: str,
    follower_id: Optional[str] = None,
    presenter: Optional[PresenterConn] = None,
) -> None:
    """Common per-socket loop: routes inbound frames + drives keepalive ping.

    The keepalive runs in a sibling task so a slow / silent peer cannot
    starve the recv loop. Any inbound frame counts as proof of life.

    A per-connection token bucket rate-limits inbound frames: a peer flooding
    frames (a hostile follower toggling visibility fast, say) has its
    over-rate frames dropped before ``_handle_inbound`` runs, so it cannot
    burn event-loop time or amplify into per-frame audience work for every
    other room. The bucket is sized above any legitimate presenter trickle,
    and a peer that stays over the limit (see INBOUND_OVERLIMIT_MAX) is
    closed with 1008 rather than being drained forever.
    """
    mark_inbound, stop_keepalive = _start_ws_keepalive(bridge)
    bucket = _InboundBucket()
    try:
        while True:
            try:
                msg = await _recv_with_bridge_close(ws, bridge, bucket)
            except WebSocketDisconnect:
                return
            except asyncio.TimeoutError:
                # No inbound frame — not even a keepalive pong — for the idle
                # window. The peer is gone but its socket may be un-closable, so
                # ws.receive() would block forever; bail so the handler's finally
                # detaches this connection instead of leaking it.
                return
            except ValueError:
                # Malformed JSON, undecodable bytes, or an oversized payload
                # past MAX_WS_FRAME_BYTES: drop the frame and continue.
                continue
            except Exception:
                # Not the frame but the transport. Once Starlette has handed
                # out the disconnect message every further receive() raises
                # a RuntimeError (WebSocketDisconnected), so retrying would
                # spin the event loop; treat it like the disconnect it is.
                return
            if msg is _RECV_BRIDGE_CLOSED:
                # The bridge started closing (room teardown, keepalive/reauth
                # close, or overflow). Stop reading so the handler's finally
                # detaches promptly instead of waiting out the idle backstop on
                # a possibly-un-closable socket.
                return
            mark_inbound()
            if msg is _FRAME_DROPPED:
                # Over-rate frame, dropped (coalesced) before it was parsed:
                # the peer stays marked live, but a sustained flood is closed.
                if bucket.abusive():
                    await bridge.close(CLOSE_POLICY_VIOLATION, "rate limit")
                    return
                continue
            try:
                await _handle_inbound(bridge, sid, role, follower_id, msg, presenter=presenter)
            except WebSocketDisconnect:
                return
            except Exception:
                # A single malformed/hostile frame must never tear down the
                # socket (which would flash "presenter left" to followers and
                # log a traceback). Drop it and keep serving; a genuinely dead
                # socket surfaces on the next _receive_json_capped instead.
                _log.exception("sync: error handling inbound frame on %s", sid)
                continue
    finally:
        await stop_keepalive()


async def _handle_inbound(
    bridge: "_WsBridge",
    sid: str,
    role: str,
    follower_id: Optional[str],
    msg: dict,
    *,
    presenter: Optional[PresenterConn] = None,
) -> None:
    if not isinstance(msg, dict):
        return
    t = msg.get("type")
    if t == "ping":
        try:
            await bridge.send({"type": "pong"})
        except Exception:
            pass
        return
    if t == "pong":
        return  # liveness already recorded by recv loop

    if role == "presenter":
        if t == "view":
            # Rebuild the view frame against the strict ViewFrame schema
            # before pinning + fan-out. Followers replay ``room.last_view``
            # verbatim on join, so a single NaN/Inf/wrong-shape bounds value
            # would otherwise wedge every late joiner against a frame their
            # ``fitBounds`` can't apply. ``_normalize_view_frame`` also
            # enforces the same slide_id contract that ``broadcast_view``
            # checks, so the DB-existence gate below operates on a trusted
            # value.
            normalized = _normalize_view_frame(msg)
            if normalized is None:
                return
            # The view and pointer a reconnecting client replays right after its
            # hello carry ``replay``: relayed as usual, but not counted as the
            # presenter interacting (see RoomManager.attach_presenter).
            activity = msg.get("replay") is not True
            new_slide_id = normalized["slide_id"]
            room = manager.get(sid)
            if room is not None and new_slide_id != room.slide_id:
                if (
                    presenter is not None
                    and presenter.refused_slide_id == new_slide_id
                    and time.monotonic() - presenter.refused_at < REFUSED_SLIDE_RECHECK_SECONDS
                ):
                    # Refused a moment ago: otherwise every pan frame on that slide
                    # would repeat the DB lookup (up to INBOUND_FRAME_RATE a second, in
                    # series on this loop). Looked up again once the window is
                    # over, so a slide published meanwhile is picked up.
                    if activity:
                        await manager.touch_presenter(sid, presenter)
                    return
                # Run the SQLite lookup off the event loop so a slow DB
                # does not stall every other room's broadcast/recv loop.
                # Drafts are rejected here too (not just at session creation)
                # so a presenter can't re-bind the room onto an unpublished
                # slide every follower would 404 on. The same lookup returns
                # the new slide's public_id, which is stamped onto the frame so
                # followers can navigate by token (broadcast_view then pins it
                # onto room.slide_public_id + last_view).
                try:
                    published = await asyncio.to_thread(
                        _published_public_ids, [new_slide_id]
                    )
                except Exception:
                    # No verdict. Treated like a refusal for the pointer — the
                    # followers stay on the old slide, so a cursor for the new
                    # one must not reach them — but neither the presenter nor
                    # the refusal cache hears of it: the next frame tries again.
                    _log.warning("sync: slide-change lookup failed on %s", sid, exc_info=True)
                    if presenter is not None:
                        presenter.cursor_suppressed = True
                    if activity:
                        await manager.touch_presenter(sid, presenter)
                    return
                new_public_id = published.get(new_slide_id)
                if not new_public_id:
                    # Followers stay on the old slide, so this presenter's
                    # cursor (coords on the unavailable slide) would land on
                    # the wrong image: hold cursors back until a view is
                    # accepted again, and tell the presenter once per episode.
                    if presenter is not None:
                        presenter.refused_slide_id = new_slide_id
                        presenter.refused_at = time.monotonic()
                    if presenter is not None:
                        presenter.cursor_suppressed = True
                    # Its own flag, not cursor_suppressed: a failed lookup
                    # (above) suppresses the pointer without telling anyone,
                    # and keying the notice on that flag would skip it for the
                    # whole episode once the next lookup did refuse the slide.
                    if presenter is not None and not presenter.refusal_notified:
                        presenter.refusal_notified = True
                        try:
                            await bridge.send({
                                "type": "notice", "code": "slide_unavailable",
                            })
                        except Exception:
                            pass
                    # A refused view is still the presenter driving the session:
                    # without this, panning around on an unavailable slide
                    # (no cursor frames) ended the session as idle.
                    if activity:
                        await manager.touch_presenter(sid, presenter)
                    return
                normalized["slide_public_id"] = new_public_id
            # Scoped to this connection: the lookup above awaits a thread, and
            # a takeover by another tab / device can land meanwhile. The room
            # checks under its lock that this is still its presenter, so the
            # superseded connection's frame can't roll the room (and its
            # replayed last_view) back over what the new presenter sent.
            if presenter is not None:
                presenter.cursor_suppressed = False
                presenter.refusal_notified = False
                presenter.refused_slide_id = None
            await manager.broadcast_view(
                sid, normalized, presenter=presenter, activity=activity,
            )
        elif t == "cursor":
            # Rebuild the cursor frame from scratch instead of forwarding the
            # presenter's raw JSON: the broadcast path fans the same dict out
            # to every follower, so an unvalidated payload could push NaN /
            # infinite coordinates (or arbitrary extra fields) into every
            # client's UI in lock-step. Coerce numeric fields, cap them to a
            # sane image-pixel range, and drop anything else.
            normalized = _normalize_cursor_frame(msg)
            if normalized is None:
                return
            activity = msg.get("replay") is not True
            if presenter is not None and presenter.cursor_suppressed and normalized["visible"]:
                # The presenter is on a slide followers are not shown, so a
                # visible pointer would be drawn at another image's
                # coordinates: drop it - but it is still presenter activity.
                # (A hidden-pointer frame is let through below so a pointer
                # shown just before the switch is switched off, not left
                # frozen on the followers' screens.)
                if activity:
                    await manager.touch_presenter(sid, presenter)
                return
            await manager.broadcast_cursor(
                sid, normalized, presenter=presenter, activity=activity,
            )
        # Unknown frames are ignored to keep the protocol forward-compatible.
        return

    # role == "follower"
    if follower_id is None:
        return
    if t == "visibility":
        # A real boolean only (see _normalize_cursor_frame).
        visible = msg.get("visible")
        if isinstance(visible, bool):
            await manager.update_follower_visibility(sid, follower_id, visible)
    elif t == "hello":
        # A hello that missed the timeout (the follower was attached
        # anonymously meanwhile). Applied once; ignored for a follower that
        # joined on its hello.
        await manager.update_follower_hello(
            sid, follower_id,
            name=msg.get("name"), visible=_hello_visible(msg),
            vw=msg.get("vw"), vh=msg.get("vh"),
        )


def _hello_visible(hello: dict) -> bool:
    """The hello's ``visible``: the follower's document.visibilityState, so the
    presenter's panel does not show a background-tab joiner as active until it
    happens to flip once. Optional; anything but a literal False is visible."""
    return hello.get("visible") is not False


# ------------------------------------------------------- discovery WS endpoint

async def _filter_listed_sessions(sessions: List[dict]) -> List[dict]:
    """Discovery-snapshot counterpart of ``_peek_visible``, plus the
    link-only rule.

    A session keeps running after staff unpublish its slide, but it must stop
    being advertised: the peek already 404s it, and the index page's join
    banner is fed by this snapshot. A session on a link-only (unlisted) slide
    is not advertised either — that slide is kept off every other browse
    surface, and the index page is one; its audience joins through the share
    link, whose sid the peek still answers. One query, off-loop.
    """
    listed = await asyncio.to_thread(_listed_slide_labels, [s["slide_id"] for s in sessions])
    return [{**s, **listed[s["slide_id"]]} for s in sessions if s["slide_id"] in listed]


def _discovery_labels(title: Optional[str], catalog_id: Optional[str]) -> dict:
    """The fields a discovery entry / event labels its slide with: the index
    page's banner shows them for a slide its loaded listing does not hold,
    which otherwise left it nothing better than the bare numeric id."""
    return {"slide_title": title or None, "slide_catalog_id": catalog_id or None}


def _listed_slide_labels(slide_ids: List[int]) -> Dict[int, dict]:
    """The listed (published, not link-only) slides among ``slide_ids``, each
    with its discovery labels (the slide's title and catalog id) — the gate of
    the discovery snapshot and of its ``started`` / ``slide_changed`` events,
    which reach every index page, the one browse surface that must not list a
    link-only slide. Joining and presenting are unaffected: the share link
    carries the sid. For a to_thread hop."""
    if not slide_ids:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(Slide.id, Slide.title, Slide.catalog_id).filter(
            Slide.id.in_(set(slide_ids)),
            Slide.is_published.is_(True),
            Slide.unlisted.is_(False),
        ).all()
        return {r[0]: _discovery_labels(r[1], r[2]) for r in rows}
    finally:
        db.close()


async def _discovery_event_is_listed(event: dict):
    """Gate for ``started`` / ``slide_changed`` discovery events (see
    ``sync_room.discovery_event_gate``): same rule as the snapshot, so neither
    an unpublished nor a link-only slide's public_id is ever announced. A
    listed slide's verdict is its labels (a dict, truthy even when empty)."""
    slide_id = event.get("slide_id")
    if not isinstance(slide_id, int):
        return True
    labels = (await asyncio.to_thread(_listed_slide_labels, [slide_id])).get(slide_id)
    return labels if labels is not None else False


_sync_room_module.discovery_event_gate = _discovery_event_is_listed


# Characters of the keyed IP hash the presenter is shown: plenty to tell 200
# followers apart, and a prefix of the digest hashed-mode analytics stores
# for the same address (analytics.pseudonymise_client_ip), so an operator can
# still correlate the two.
_AUDIENCE_IP_PSEUDONYM_LEN = 12


def _audience_ip_pseudonym(ip: str) -> str:
    # "" when the salt is gone: the address is withheld, never shown raw.
    return _analytics.pseudonymise_client_ip(ip)[:_AUDIENCE_IP_PSEUDONYM_LEN]


def _audience_ip_pseudonymiser():
    """sync_room's ``audience_ip_pseudonymiser`` hook.

    Without it the presenter's audience panel would show every follower's raw
    IP, User-Agent and reverse-DNS name whatever the analytics privacy mode,
    so under "hashed" — where the operator chose that the admin dashboard
    itself sees no raw address or UA — any instructor running a session would
    still get them, and could export them as CSV. Under "hashed" the panel
    shows the same keyed pseudonym of the address instead and withholds the
    rest.

    "off" keeps the raw values, as "plain" does. It is the default of every
    fresh install and means "record no analytics" — nothing is stored either
    way, the panel is a live view for the person running the session — so
    reading it as a privacy choice would strip the panel on every install
    that simply never opted into analytics.
    """
    if _net.analytics_mode() == _analytics.ANALYTICS_HASHED:
        return _audience_ip_pseudonym
    return None


_sync_room_module.audience_ip_pseudonymiser = _audience_ip_pseudonymiser


@router.websocket("/api/sync/discovery")
async def discovery_ws(websocket: WebSocket):
    # See sync_ws: accept before any guard close() so 4xxx codes survive.
    await websocket.accept()

    admission = _WsAdmission()
    if not await _ws_guard(websocket, admission):
        return

    bridge = _WsBridge(websocket)
    bridge.start()
    sub_id = secrets.token_urlsafe(8)
    stop_keepalive = None
    stop_reauth = None
    # Everything from the registration on sits inside this try/finally: the
    # subscriber holds a capped slot, and a handler cancelled between the
    # registration and the recv loop (client gone while the snapshot filter's
    # DB query ran, server shutdown) must still release it. Removing an
    # unregistered sub_id (the refused path) is a no-op.
    try:
        accepted = await manager.add_discovery_subscriber(
            sub_id, bridge.send,
            ip=_client_ip(websocket),
            snapshot_filter=_filter_listed_sessions,
        )
        if not accepted:
            # Route the reject through the bridge rather than closing the
            # socket directly: a bare ws.close() here doesn't set the bridge's
            # ``_socket_closed`` flag, so the writer's own ``_safe_close`` would
            # attempt a second close and raise Starlette's "Cannot call close
            # once a close message has been sent" into the swallow-all handler.
            await bridge.close(code=CLOSE_TOO_BUSY, reason="discovery full")
            return

        mark_inbound, stop_keepalive = _start_ws_keepalive(bridge)
        # Without it the socket would outlive its app session: it would keep
        # receiving new live sessions' ids and slides after the session was
        # revoked.
        stop_reauth = _start_viewer_reauth(websocket, bridge, admission)
        bucket = _InboundBucket()
        while True:
            try:
                # Races the bridge's closing signal like the room loop does. A
                # plain wait_for only notices the peer, so when the bridge
                # itself closed (send-queue overflow, keepalive timeout) a
                # frozen client would keep its discovery subscriber slot for the
                # whole idle window — and that slot is capped, so live viewers
                # would be turned away with CLOSE_TOO_BUSY meanwhile.
                msg = await _recv_with_bridge_close(websocket, bridge, bucket)
            except WebSocketDisconnect:
                return
            except asyncio.TimeoutError:
                # See _ws_main_loop: bail on a dead-but-un-closable peer so the
                # discovery subscriber is removed instead of leaking.
                return
            except ValueError:
                continue  # a bad frame, dropped (see _ws_main_loop)
            except Exception:
                return  # the transport is gone (see _ws_main_loop)
            if msg is _RECV_BRIDGE_CLOSED:
                return
            mark_inbound()
            if msg is _FRAME_DROPPED:
                # Same flood policy as _ws_main_loop: drop pre-parse, then close.
                if bucket.abusive():
                    await bridge.close(CLOSE_POLICY_VIOLATION, "rate limit")
                    return
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                # Through the bridge so this pong is serialised against the
                # keepalive task's outbound ping on the same socket.
                try:
                    await bridge.send({"type": "pong"})
                except Exception:
                    pass
    finally:
        if stop_reauth is not None:
            await stop_reauth()
        if stop_keepalive is not None:
            await stop_keepalive()
        await manager.remove_discovery_subscriber(sub_id)
        await bridge.aclose()
