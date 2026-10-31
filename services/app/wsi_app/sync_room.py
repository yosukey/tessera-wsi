"""In-memory live-view sync coordination.

Single-process room manager for the live ViewerPage sync feature. The
transport layer (FastAPI WebSocket) is deliberately abstracted behind two
minimal coroutine callbacks (``send``/``close``; a discovery subscriber only
needs ``send``) so this module is trivially unit-testable without a running
ASGI server.

Concurrency model: everything runs on the one event loop of the single sync
worker, and what keeps room state consistent is that no critical section
awaits: every check-then-mutate (is the room still registered, is this still
its presenter, is there a free slot) happens with no ``await`` in between.
A single ``RoomManager`` also keeps one ``asyncio.Lock`` per room plus one for
the global rooms dict / discovery subscribers, but while that rule holds they
are never contended; they stay as a guard should a section ever need to
await, and code added inside one must keep to the rule rather than rely on
the lock. All public coroutines never call back into user code while holding a
lock — they take a copy of the relevant callback list, release the lock, then
dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from .config import env_int, env_float

_log = logging.getLogger("wsi_app.sync")

# Contract for both callbacks: they must not suspend before the frame is
# queued (the transport's _WsBridge.send only appends to a deque). The
# fan-out starts every recipient's send inline and the manager sends a
# connection its frames in order (a joiner's replayed last_view, then live
# frames; ``present: false`` before ``present: true``) relying on each send
# having taken its place in that connection's queue before the next one
# starts. A callback that awaited first could let a later frame overtake an
# earlier one on the same connection.
SendCallback = Callable[[dict], Awaitable[None]]
CloseCallback = Callable[[int, str], Awaitable[None]]


class SharedFrame(dict):
    """A frame fanned out to many connections, JSON-encoded at most once.

    Without the cache every follower's writer would ``json.dumps`` the same
    dict again: at 200 followers and the presenter's frame rate that is thousands of
    encodings a second on the single event loop. The transport asks
    :meth:`encoded` instead and gets the cached text. Frames are never
    mutated once they are fanned out, which is what makes the cache safe.
    """

    __slots__ = ("_encoded",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._encoded: Optional[str] = None

    def encoded(self) -> str:
        if self._encoded is None:
            # Same encoding as Starlette's send_json.
            self._encoded = json.dumps(self, separators=(",", ":"), ensure_ascii=False)
        return self._encoded

# How long a room outlives its presenter. A reload, a tab close or a network
# drop disconnects the presenter socket without ending the session; the room is
# kept alive for this long so they can reattach (the "resume" affordance backed
# by GET /api/admin/sync/sessions) and the audience is not scattered by a blip.
# The GC reaps the room once the deadline passes with no presenter back.
PRESENTER_GRACE_SECONDS = env_float("WSI_SYNC_PRESENTER_GRACE_SEC", 45)
# Debounce before telling followers the presenter left. A genuine brief drop
# (Wi-Fi blip, network handoff) closes the socket and reconnects within ~1s;
# broadcasting "presenter left" on the raw detach would show followers a
# pointless "left → returned" flash. Holding the notice for a few seconds means
# a presenter who reconnects inside the window is never announced as absent at
# all. Must stay well below PRESENTER_GRACE_SECONDS (the teardown deadline).
PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS = env_float(
    "WSI_SYNC_PRESENTER_ABSENT_NOTIFY_DELAY_SEC", 5
)
# GC tick cadence. ~1s because the same loop drives the presenter-absence
# notice and the grace teardown: a coarse tick would delay the "presenter
# left" banner and let the follower countdown drift seconds past the actual
# session end. Iterating the handful of live rooms once a second is negligible.
GC_INTERVAL_SECONDS = env_float("WSI_SYNC_GC_INTERVAL_SEC", 1)
# Cap on concurrent /api/sync/discovery subscribers. That socket is open to
# any viewer-page visitor (it only reports whether a session is live), so
# without a ceiling a crowd — or a script — could hold unbounded sockets on the
# single-worker sync process. Sized well above one classroom's worth of tabs.
DISCOVERY_MAX_SUBSCRIBERS = env_int("WSI_SYNC_DISCOVERY_MAX_SUBS", 500)
# Cap concurrent followers per room. The audience frame fans out the full
# follower list to the presenter on every join/leave/visibility change, so
# the cost is roughly O(N) per event; with no cap a misconfigured public
# link could let arbitrary clients pile up until the room's send buffers
# exhaust process memory.
FOLLOWERS_PER_ROOM_MAX = env_int("WSI_SYNC_FOLLOWERS_PER_ROOM_MAX", 200)
# Per-client-IP shares of the two caps above, meant to stop one host opening a
# few hundred sockets from filling a room (or the discovery channel) so every
# real student is turned away with 4503. The IP is the resolved client IP (the
# CF-Connecting-IP / X-Real-IP a trusted proxy supplies, see
# network.get_client_ip), so distinct browsers behind nginx / Cloudflare are
# counted separately — but a whole class behind ONE address (a campus NAT, or
# every student reaching a Cloudflare tunnel through the same egress) is one
# "client" here, so any small per-IP number would lock that class out.
#
# * Followers: disabled by default (0). FOLLOWERS_PER_ROOM_MAX already bounds a
#   room, and no per-IP number both admits a NAT'd class and meaningfully
#   limits one host within a single room. Set it only when every follower has
#   its own address.
# * Discovery: 200 by default — at or above FOLLOWERS_PER_ROOM_MAX, so a class
#   that fits in a room also fits here even from one address, yet well below
#   DISCOVERY_MAX_SUBSCRIBERS (500), so one host still cannot take every slot.
#
# <= 0 disables the respective check. A refusal by either per-IP cap is logged
# at WARNING (see _warn_per_ip_refusal) so operators can tell it from a
# genuinely full room.
FOLLOWERS_PER_IP_MAX = env_int("WSI_SYNC_FOLLOWERS_PER_IP_MAX", 0)
DISCOVERY_MAX_SUBS_PER_IP = env_int("WSI_SYNC_DISCOVERY_MAX_SUBS_PER_IP", 200)
# Minimum seconds between two per-IP-refusal warnings for the same (cap, IP),
# so a client retrying with backoff cannot flood the journal.
PER_IP_REFUSAL_LOG_INTERVAL_SECONDS = 60.0
NAME_MAX_LEN = 64

# Absolute upper bound on how long a single live-sync room may exist, in
# seconds, regardless of whether a presenter is attached. The presenter-grace
# path (PRESENTER_GRACE_SECONDS) only reaps rooms whose presenter has *left*;
# a room with a still-attached — or silently reconnecting — presenter is never
# reaped by it. So a forgotten browser tab, a kiosk left on the slide, or any
# zombie client that keeps the presenter WebSocket alive (or re-establishes it
# on every drop) would otherwise keep the room, its discovery banner, and its
# "join" affordance alive indefinitely. That is exactly the orphan an operator
# can no longer reach: they lost the presenter tab but some device is still
# holding the socket open. This cap is the backstop that bounds that lifetime.
# 1.5h comfortably covers a normal teaching session while guaranteeing a
# forgotten one is collected without operator intervention; a presenter who
# needs longer just starts a fresh session. Set to <= 0 to disable (rooms then
# live until ended, superseded, or grace-reaped).
MAX_SESSION_AGE_SECONDS = env_float("WSI_SYNC_MAX_SESSION_AGE_SEC", 5400)
# How long before the age cap the presenter gets a single "this session will
# end" warning frame. Unlike the idle warning, activity cannot postpone the cap,
# so the frame only tells the presenter to wrap up / start a fresh session.
# Set to <= 0 to end with no advance warning. A lead longer than half the cap is
# treated as half the cap, so a misconfigured value cannot warn a brand-new room.
MAX_SESSION_AGE_WARN_SECONDS = env_float("WSI_SYNC_MAX_SESSION_AGE_WARN_SEC", 300)

# Presenter inactivity timeout. A room whose presenter is still connected but
# has sent no view/cursor frame for this long is force-ended — the common
# "zombie tab" shape where the WebSocket stays alive on app-level ping/pong
# (which does NOT count as activity) while nobody is actually driving. A live
# presentation produces a steady trickle of viewport/cursor frames, so 10
# minutes of complete silence reliably means the session was abandoned. Set to
# <= 0 to disable inactivity teardown.
PRESENTER_IDLE_SECONDS = env_float("WSI_SYNC_PRESENTER_IDLE_SEC", 600)
# How many seconds before the inactivity deadline the presenter gets a single
# "ending soon — interact to keep it alive" warning frame, so an idle-but-
# present presenter can keep the session by moving/zooming. Any presenter
# activity clears the warning and resets the clock. Set to <= 0 to end with no
# advance warning. A lead longer than half the timeout is treated as half the
# timeout (like the max-age lead), so a misconfigured value cannot warn a
# presenter who has just interacted — on every GC tick.
PRESENTER_IDLE_WARN_SECONDS = env_float("WSI_SYNC_PRESENTER_IDLE_WARN_SEC", 60)

# Absolute ceiling on the number of concurrent live-sync rooms. Per-room
# followers (FOLLOWERS_PER_ROOM_MAX) and discovery subscribers
# (DISCOVERY_MAX_SUBSCRIBERS) are already capped, but ``_rooms`` itself needs
# its own ceiling: a script that repeatedly hits the create endpoint (each
# call under a distinct staff token, so create_superseding doesn't supersede)
# could grow the dict — and its per-room locks / discovery banners — without
# limit. 200 comfortably exceeds any realistic number of simultaneous
# presenters while bounding the blast radius. Set to <= 0 to disable the cap.
MAX_ROOMS = env_int("WSI_SYNC_MAX_ROOMS", 200)

# Debounce window for presenter-bound audience frames. Every follower
# join / leave / visibility flip changes the audience the presenter sees, and
# emitting the full O(N) follower list synchronously on each one lets a single
# follower toggling visibility fast saturate the presenter's bounded outbound
# queue (and burn event-loop time re-serialising the list for every other
# room). Coalescing to at most one audience frame per window collapses a burst
# into O(1) sends while still delivering the latest state.
AUDIENCE_DEBOUNCE_SECONDS = env_float("WSI_SYNC_AUDIENCE_DEBOUNCE_SEC", 0.2)


def _checked_timing(
    name: str, value: float, default: float, *, minimum: float = 0.0, strict: bool = False,
) -> float:
    """``value`` when usable, else ``default`` with a warning in the journal.

    The timings above come straight from the environment, and a negative (or,
    where ``strict``, a zero) value would silently break the feature it tunes — a GC
    interval of 0 spins the loop, a negative grace reaps every room at once.
    """
    if math.isfinite(value) and (value > minimum if strict else value >= minimum):
        return value
    _log.warning(
        "live sync: %s=%r is out of range; using the default %r", name, value, default,
    )
    return default


GC_INTERVAL_SECONDS = _checked_timing(
    "WSI_SYNC_GC_INTERVAL_SEC", GC_INTERVAL_SECONDS, 1.0, strict=True,
)
PRESENTER_GRACE_SECONDS = _checked_timing(
    "WSI_SYNC_PRESENTER_GRACE_SEC", PRESENTER_GRACE_SECONDS, 45.0,
)
PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS = _checked_timing(
    "WSI_SYNC_PRESENTER_ABSENT_NOTIFY_DELAY_SEC", PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS, 5.0,
)
AUDIENCE_DEBOUNCE_SECONDS = _checked_timing(
    "WSI_SYNC_AUDIENCE_DEBOUNCE_SEC", AUDIENCE_DEBOUNCE_SECONDS, 0.2,
)
def _checked_notify_delay(delay: float, grace: float) -> float:
    """The "presenter left" notice delay, kept inside the grace window.

    A delay not below the grace would make the notice due only once the room
    is already gone, so followers would never be told the presenter left: it
    becomes half the grace, with a warning.
    """
    if delay >= grace > 0:
        _log.warning(
            "live sync: WSI_SYNC_PRESENTER_ABSENT_NOTIFY_DELAY_SEC (%s) is not below "
            "WSI_SYNC_PRESENTER_GRACE_SEC (%s); using half the grace instead",
            delay, grace,
        )
        return grace / 2
    return delay


PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS = _checked_notify_delay(
    PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS, PRESENTER_GRACE_SECONDS,
)
if PRESENTER_GRACE_SECONDS <= 2 * GC_INTERVAL_SECONDS:
    # Warned, not changed: a short grace may be wanted in tests. In production
    # it reaps a REST-created room before its presenter's hello can arrive, and
    # ends a session on any reconnect.
    _log.warning(
        "live sync: WSI_SYNC_PRESENTER_GRACE_SEC (%s) is not well above "
        "WSI_SYNC_GC_INTERVAL_SEC (%s); a presenter may lose the session on a "
        "reconnect, or before their first connect lands",
        PRESENTER_GRACE_SECONDS, GC_INTERVAL_SECONDS,
    )


class RoomFullError(Exception):
    """Raised by ``attach_follower`` when a room is at FOLLOWERS_PER_ROOM_MAX.

    Distinct from the ``return None`` ("session not found") path so the WS
    handler can map it to a dedicated 4503 close code and let the client
    surface a "room full" message instead of a generic "session ended".
    """


class RoomLimitError(Exception):
    """Raised by ``create`` when ``_rooms`` is already at MAX_ROOMS.

    The REST caller maps this to a 503 so a capacity exhaustion is reported
    distinctly from a bad request.
    """


class PresenterSupersededError(Exception):
    """Raised by ``attach_presenter`` when an auto-reconnect is stale.

    Every presenter client instance (one ``SyncPresenter`` object: a page
    load, a Start or a Resume) mints a random id and sends it in each hello;
    its automatic reconnects additionally carry a resume marker. If a
    *different* client id has attached since, this client was superseded while
    it was away — e.g. a laptop that slept through the hand-over to a tablet.
    Letting it attach would evict the live presenter and replay its old view
    to every follower, so the WS handler closes it with 4000 ("presenter
    replaced") instead, which the client treats as terminal.

    Keyed by an id the client mints rather than by a counter the server
    assigns on purpose: a server-assigned value reaches the client only in the
    later ``attached`` frame, so a socket that died in between would leave the
    client holding a stale value and get its own next reconnect refused
    although no other device had attached.
    """


# Upper bound on the client-supplied presenter instance id (see
# PresenterSupersededError). The frontend sends "p-" + 12 random hex chars;
# anything longer, empty, non-ASCII or non-printable is treated as absent (a
# fresh attach) rather than stored, so a malformed value can never lock a
# presenter out.
PRESENTER_CLIENT_ID_MAX_LEN = 64


def sanitize_presenter_client_id(raw: Any) -> Optional[str]:
    """The hello's presenter client id, or None when absent / malformed."""
    if not isinstance(raw, str) or not raw or len(raw) > PRESENTER_CLIENT_ID_MAX_LEN:
        return None
    if not raw.isascii() or not raw.isprintable():
        return None
    return raw


# Format characters a name may keep: the zero-width (non-)joiner that emoji
# sequences and several scripts are built with. Every other format character
# goes — the bidi overrides and isolates among them, which could make a name
# read differently in the presenter's panel than it was typed.
_NAME_JOINERS = frozenset("\u200c\u200d")


def sanitize_name(raw: Any) -> Optional[str]:
    """A follower's display name, or None when nothing visible is left.

    ``str.isprintable`` is too blunt here: it rejects every space but the
    ASCII one and every format character, so a name like "山田 太郎" typed
    with an ideographic space (U+3000) would reach the presenter as
    "山田太郎", and joined emoji would fall apart.
    Controls, surrogates and private-use code points are dropped, format
    characters but the joiners too; line / paragraph separators become a
    space. Unassigned code points are kept: this interpreter's Unicode tables
    can be older than the browser's (an emoji new in Unicode 16 is unassigned
    to Python 3.12), and dropping them would empty a name the join dialog had
    accepted. The cut to NAME_MAX_LEN comes before the "anything visible?"
    check, so leading joiners cannot push the visible part past the cut. The
    join dialog (FollowerJoinDialog.tsx) applies the same rule, so a name it
    accepts is not empty here.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return None
    out: List[str] = []
    for ch in raw:
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat == "Cf":
            if ch in _NAME_JOINERS:
                out.append(ch)
        elif cat in ("Cc", "Cs", "Co"):
            continue
        else:
            out.append(ch)
    cleaned = "".join(out).strip()[:NAME_MAX_LEN].strip()
    if not any(not ch.isspace() and ch not in _NAME_JOINERS for ch in cleaned):
        return None
    return cleaned


# UA + reverse-DNS hostname come from untrusted sources (the client's
# User-Agent header for UA, the PTR record at the follower's IP for
# hostname) and are then broadcast back to every presenter in the room
# via FollowerMeta.to_dict. Strip non-printable characters and cap the
# length so a hostile PTR record or a hand-crafted User-Agent string with
# ANSI escape sequences cannot influence the presenter UI in unexpected
# ways. Empty / sanitization-collapsed values are coerced to "" instead
# of None so the FollowerMeta.ua/hostname field shapes stay stable.
_UA_MAX_LEN = 256
_HOSTNAME_MAX_LEN = 253  # RFC 1123 §2.1


def _scrub_metadata(raw: Any, *, max_len: int) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return ""
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    return cleaned.strip()[:max_len]


# Follower viewport size (CSS pixels of the follower's OpenSeadragon canvas),
# reported once in the hello frame. Like ua/hostname it is client-supplied and
# fanned back out to the presenter, so it gets the numeric equivalent of
# _scrub_metadata: anything non-finite, non-positive, or absurdly large is
# dropped to None rather than clamped, so the presenter panel shows "unknown"
# instead of a plausible-looking lie. The ceiling is well past any real display
# (8K is 7680) while still bounding the rendered string.
_VIEWPORT_PX_MAX = 32768


def _scrub_viewport_px(raw: Any) -> Optional[int]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is the non-obvious one: JSON has no integer bound, so
        # ``{"vw": 1e400}`` arrives as a float inf (caught below) but a literal
        # 401-digit integer arrives as a Python int that float() cannot
        # represent. That payload is only ~440 bytes, far under
        # MAX_WS_FRAME_BYTES, so without this it is a trivially reachable
        # unhandled exception on the follower attach path.
        return None
    # Rejects NaN and ±Inf, which would otherwise survive int() as a crash or
    # an arbitrary value.
    if not math.isfinite(val):
        return None
    px = int(round(val))
    if px < 1 or px > _VIEWPORT_PX_MAX:
        return None
    return px


@dataclass
class FollowerMeta:
    id: str
    name: Optional[str]
    ip: str
    ua: str
    hostname: Optional[str]
    joined_at: float  # epoch seconds (wall clock)
    visible: bool = True
    # Follower's viewer-canvas size in CSS pixels, or None when the client
    # did not report it (a hello whose fields are missing or malformed).
    # Reported once at hello time and never updated: a follower that resizes
    # or rotates mid-session keeps its join-time value.
    vw: Optional[int] = None
    vh: Optional[int] = None
    # False while the follower was attached without its hello (it missed the
    # hello timeout): the late hello may then still fill in the fields above,
    # once (see RoomManager.update_follower_hello). Not part of to_dict.
    hello_received: bool = True

    def __post_init__(self) -> None:
        # Coerce visible to a real bool so a buggy client passing 0/1 or a
        # truthy string can't smuggle non-boolean values into the audience
        # frame the presenter UI consumes.
        self.visible = bool(self.visible)
        # Same reasoning for the viewport pair, which arrives straight from
        # client JSON. Scrub both independently, then drop the pair unless
        # both survived — a half-known size is not something the presenter
        # panel can render meaningfully.
        vw = _scrub_viewport_px(self.vw)
        vh = _scrub_viewport_px(self.vh)
        if vw is None or vh is None:
            vw = vh = None
        self.vw = vw
        self.vh = vh

    def to_dict(self, *, ip_pseudonym: Optional[Callable[[str], str]] = None) -> dict:
        """The presenter's audience-panel entry for this follower.

        With ``ip_pseudonym`` (the analytics privacy mode is "hashed", see
        ``audience_ip_pseudonymiser``) the address is shown only as that
        pseudonym and the User-Agent and reverse-DNS name are left out: each
        would re-identify the follower as well as the address does. ``ip``
        itself stays raw on the object, for the per-IP caps.
        """
        pseudonymous = ip_pseudonym is not None
        return {
            "id": self.id,
            "name": self.name,
            "ip": ip_pseudonym(self.ip) if pseudonymous else self.ip,
            "ua": "" if pseudonymous else self.ua,
            "hostname": None if pseudonymous else self.hostname,
            "joined_at": self.joined_at,
            "visible": self.visible,
            "vw": self.vw,
            "vh": self.vh,
        }


@dataclass
class FollowerConn:
    meta: FollowerMeta
    send: SendCallback
    close: CloseCallback


@dataclass
class PresenterConn:
    send: SendCallback
    close: CloseCallback
    # Set by the WS handler when this presenter's view frame was refused
    # because the target slide is unpublished/unavailable: followers are still
    # on the previous slide, so the presenter's cursor (image-pixel coords of
    # the *new* slide) must not reach them. Cleared by the next accepted view.
    cursor_suppressed: bool = False
    # True once the presenter has been sent the ``slide_unavailable`` notice
    # for the current refusal episode (once per episode; cleared with
    # cursor_suppressed). Separate from it: a failed lookup suppresses the
    # pointer too, but tells no one.
    refusal_notified: bool = False
    # The slide this presenter's last refused view frame named, and when
    # (monotonic): the WS handler skips the publish lookup for that slide's
    # further frames for a short while instead of repeating it per frame.
    refused_slide_id: Optional[int] = None
    refused_at: float = 0.0


@dataclass
class Room:
    session_id: str
    slide_id: int
    owner_user_id: int
    # The current slide's unguessable public_id. Followers navigate by this
    # token (never the numeric slide_id), so every outgoing view frame is
    # stamped with it and it's updated in lock-step with slide_id on a
    # presenter slide-change. Empty only for a room created before a public_id
    # was known (should not happen in practice).
    slide_public_id: str = ""
    presenter: Optional[PresenterConn] = None
    followers: Dict[str, FollowerConn] = field(default_factory=dict)
    last_view: Optional[dict] = None
    last_cursor: Optional[dict] = None
    # Wall-clock epoch of creation. Kept ONLY for the admin snapshot display
    # (AdminSessionEntry.created_at is shown to operators as epoch seconds).
    # All GC arithmetic uses created_monotonic instead, so a wall-clock jump
    # (suspend/resume, NTP step) can't skew the absolute-age cap.
    created_at: float = field(default_factory=time.time)
    # Monotonic-clock stamp of creation. Drives the MAX_SESSION_AGE_SECONDS cap
    # in gc_step; immune to wall-clock jumps.
    created_monotonic: float = field(default_factory=time.monotonic)
    presenter_grace_until: Optional[float] = None  # monotonic deadline
    # Monotonic time the presenter last detached, or None if a presenter is /
    # was never connected since creation. Set only on a genuine detach (not the
    # creation grace), so gc_step can debounce the follower-facing "presenter
    # left" notice by PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS from this instant.
    presenter_left_at: Optional[float] = None
    # True once followers have actually been told the presenter is absent. Gates
    # the "presenter returned" broadcast (skip it if they were never told) and
    # what a follower joining mid-absence is shown.
    presenter_absent_notified: bool = False
    # Monotonic-clock stamp of the presenter's last *meaningful* frame
    # (view/cursor). Set on attach and refreshed on every broadcast_view /
    # broadcast_cursor; ping/pong keepalive deliberately does NOT touch it. None
    # while no presenter has ever attached. Drives the inactivity timeout in
    # gc_step — monotonic so a wall-clock jump can't force-end an active
    # presenter or postpone a legitimate idle teardown.
    presenter_last_activity: Optional[float] = None
    # True once the single pre-teardown inactivity warning has been sent for the
    # current idle stretch. Reset to False by any presenter activity (and on
    # attach), so a presenter who interacts after a warning is warned afresh the
    # next time they go idle.
    presenter_idle_warned: bool = False
    # True once the presenter has been sent the one-shot pre-max-age warning.
    # Only set when a presenter was attached to receive it, so a presenter who
    # reattaches inside the warning window is still warned.
    max_age_warned: bool = False
    # The presenter client-instance id (see PresenterSupersededError) of the
    # most recent successful attach, or None while nobody has attached yet.
    # An automatic reconnect is refused when this names a different client,
    # and an "end because I'm leaving" DELETE from a presenter tab is ignored
    # (see end_sync_session), so closing a superseded tab can't tear down the
    # room the tab/device that took over is now driving. A detach leaves it
    # as-is, so it keeps naming the last attacher until another one replaces it.
    # A hello without a client id (missing or malformed) records a fresh
    # server-minted placeholder instead of None, so it still counts as
    # "a different client attached" for any id-carrying reconnect after it.
    presenter_client_id: Optional[str] = None
    # True once this room has been announced to discovery subscribers (the
    # ``started`` event). Deliberately NOT set at create(): a REST-created room
    # whose presenter never completes the WS handshake would otherwise flash a
    # "join" banner on every index page for the whole grace window before GC
    # reaps it. Set on the *first* successful attach_presenter instead, so the
    # banner appears only once a presenter is actually seated. Gates three
    # discovery paths so they stay consistent: the ``started`` broadcast (fired
    # once here), the subscribe-time snapshot (un-announced rooms omitted), and
    # the teardown ``ended`` broadcast (which also requires discovery_shown, so
    # a room no subscriber was ever told about stays unnamed). A reattach within grace leaves it True, so no duplicate ``started``.
    discovery_announced: bool = False
    # The discovery gate's latest verdict on this room, as (slide_id, may be
    # listed), recorded by _notify_discovery the moment a ``started`` /
    # ``slide_changed`` for the room's current slide is let through or
    # withheld. The subscribe-time snapshot falls back on it for a room its
    # filter never judged (see add_discovery_subscriber).
    discovery_verdict: Optional[Tuple[int, bool]] = None
    # True while some index page may be showing this room: an event for it
    # went out, or a snapshot included it. A slide change the gate refuses
    # then sends ``ended`` to take the banner down, and so does teardown — but
    # only then, since an ``ended`` for a room never shown would hand every
    # index page the sid of a session on a link-only slide.
    discovery_shown: bool = False
    # (slide_id, fields) the discovery gate last let through for this room —
    # the slide's title and catalog id — so a subscribe-time snapshot that
    # includes the room on its recorded verdict labels it like the event did.
    discovery_labels: Optional[Tuple[int, Dict[str, Any]]] = None
    # True while the slide the room is on has been unpublished (or deleted)
    # mid-session, as last seen by refresh_slide_status. Followers were told
    # (a ``slide_unavailable`` notice) and are told again when it is back.
    slide_unavailable: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # --- audience-fan-out debounce state (see RoomManager._schedule_audience) ---
    # Loop-clock time of the last audience frame actually sent to the presenter.
    _audience_last_sent: float = 0.0
    # True while a trailing audience flush is armed. Guards against arming more
    # than one timer per window; the flush always sends the latest state.
    _audience_pending: bool = False
    # The pending trailing-flush timer, or None. A TimerHandle (not a Task) so
    # an un-fired window at loop shutdown is dropped silently.
    _audience_timer: Optional[asyncio.TimerHandle] = None


@dataclass
class OwnedSession:
    """One of a presenter's own live rooms, for the "resume" affordance."""
    session_id: str
    slide_id: int
    # The slide's public_id token, from which the share URL is built (viewer
    # URLs are keyed by the token, not the numeric id).
    slide_public_id: str = ""
    # Whether a presenter socket is attached right now: another tab or device
    # of the same owner, or this browser's previous socket that the server has
    # not yet seen die (a network drop or a crashed tab is only noticed by the
    # keepalive, WSI_SYNC_PING_TIMEOUT_SEC later). Either way a Resume is a
    # fresh attach that takes the session over.
    presenter_connected: bool = False


@dataclass
class AdminSessionEntry:
    """Rich per-room snapshot for the staff session-management UI.

    Carries the signals an operator needs to triage an orphan they can no
    longer reach from a presenter tab: who started it (``owner_user_id``),
    whether a presenter socket is *currently* attached (``presenter_connected``
    — a True here on a session nobody is actually driving is the zombie-tab
    case the absolute age cap eventually reaps), how many followers are live,
    when it started (``created_at``, epoch seconds), and — for a presenter-less
    room — the seconds left before the grace GC collects it on its own.
    """
    session_id: str
    slide_id: int
    owner_user_id: int
    presenter_connected: bool
    follower_count: int
    created_at: float
    grace_remaining: Optional[float]


async def _safe_send(cb: SendCallback, msg: dict) -> None:
    try:
        await cb(msg)
    except Exception:
        # Disconnects are reaped via the WS handler's exception path; swallow
        # here so one dead client does not break the broadcast.
        pass


async def _safe_close(cb: CloseCallback, code: int, reason: str) -> None:
    try:
        await cb(code, reason)
    except Exception:
        pass


async def _fan_out(callbacks: List[SendCallback], msg: dict) -> None:
    # Each send is started inline (an eager task; the installer requires
    # Python 3.12+) and only the ones that actually suspend (a peer that
    # really awaits its socket) are left running as tasks. The production
    # callback (_WsBridge.send) just enqueues and never suspends, so the common
    # case completes without any task being scheduled — a Task plus gather
    # bookkeeping per follower cost ~0.9 ms/frame at 200 followers versus
    # ~0.04 ms for a plain loop. A slow/hung recipient still cannot delay the
    # others: it is the only one left pending and is awaited concurrently.
    if not callbacks:
        return
    frame = msg if isinstance(msg, SharedFrame) else SharedFrame(msg)
    loop = asyncio.get_running_loop()
    pending = []
    for cb in callbacks:
        task = asyncio.Task(_safe_send(cb, frame), loop=loop, eager_start=True)
        if not task.done():
            pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# Optional async predicate set by the sync router: truthy when a discovery event
# may be announced (the slide it names is published and not link-only). A dict
# verdict also carries fields to add to the event (the router's: the slide's
# title and catalog id, which the index page's banner shows for a slide its
# listing has not loaded). A refused ``slide_changed`` withdraws a room the
# index pages show (see _notify_discovery). None = allow all.
discovery_event_gate: Optional[Callable[[dict], Awaitable[Any]]] = None


def _gate_labels(verdict: Any) -> Dict[str, Any]:
    """The event fields a discovery-gate verdict carries (see above)."""
    return dict(verdict) if isinstance(verdict, dict) else {}

# Optional hook set by the sync router, asked once per presenter audience
# frame: the function mapping a follower's IP to the pseudonym the presenter
# is shown instead (with the UA and hostname withheld, see
# FollowerMeta.to_dict), or None to show the follower as is. The router
# returns one while the analytics privacy mode is "hashed". None = show as is.
audience_ip_pseudonymiser: Optional[
    Callable[[], Optional[Callable[[str], str]]]
] = None


class RoomManager:
    def __init__(self) -> None:
        self._rooms: Dict[str, Room] = {}
        self._global_lock = asyncio.Lock()
        self._discovery_subs: Dict[str, SendCallback] = {}
        # sub_id -> client IP, and the per-IP tally DISCOVERY_MAX_SUBS_PER_IP
        # is checked against. Both mutated only under _global_lock.
        self._discovery_sub_ip: Dict[str, str] = {}
        self._discovery_ip_counts: Dict[str, int] = {}
        # sub_id -> events raised for a subscriber whose snapshot has not been
        # sent yet; see add_discovery_subscriber's ordering guarantee. Present
        # only during that short window.
        self._discovery_pending: Dict[str, List[dict]] = {}
        # (cap name, ip) -> monotonic time of the last per-IP-refusal warning,
        # for _warn_per_ip_refusal's rate limit.
        self._per_ip_warned_at: Dict[Tuple[str, str], float] = {}
        self._gc_task: Optional[asyncio.Task] = None
        # Debounce window for audience fan-out; instance attribute so tests can
        # tighten it without touching the module default.
        self._audience_debounce = AUDIENCE_DEBOUNCE_SECONDS
        # Strong refs to the manager's background tasks (trailing audience
        # flushes, and everything _spawn starts: teardowns, discovery
        # notifications, an evicted presenter's close, presenter notices) so
        # they aren't GC'd mid-flight; each removes itself on completion.
        self._bg_tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------ rooms
    async def create(self, *, slide_id: int, owner_user_id: int, slide_public_id: str = "") -> str:
        """Register a room *next to* any the owner already has.

        The REST endpoint goes through ``create_superseding`` (one live session
        per presenter); this plain form is what tests build rooms with. Both
        share ``_create``, so the room ceiling and the grace arming are the same.
        """
        session_id, _ = await self._create(
            slide_id=slide_id, owner_user_id=owner_user_id,
            slide_public_id=slide_public_id, supersede=False,
        )
        return session_id

    async def create_superseding(
        self,
        *,
        slide_id: int,
        owner_user_id: int,
        slide_public_id: str = "",
        reason: str = "superseded",
        announce_successor: bool = False,
    ) -> Tuple[str, List[str]]:
        """Create a session for ``owner_user_id``, replacing any they already own.

        Enforces the "one live session per presenter" rule as a single step.
        Doing it as two (end the owner's rooms, then create) would make a
        *rejected* request destructive: if the create then hit the room
        ceiling, the presenter's previous session would already be torn down —
        followers and all — while the caller got a 503 and no replacement.
        Swapping the order alone does not work either, since ending the owner's
        rooms would immediately reap the room just created. So the registry
        swap happens atomically under the global lock, and the old rooms are
        torn down only once the new one is definitely seated.

        With ``announce_successor`` the superseded rooms' ``end`` frame names
        the new session (``next``: its sid and slide token), so their followers
        can be offered to join it. The caller passes it only for a slide the
        index page may list: a link-only slide's token is not handed to an
        audience that was never given its link.

        Returns ``(new_session_id, superseded_session_ids)``.
        """
        return await self._create(
            slide_id=slide_id, owner_user_id=owner_user_id,
            slide_public_id=slide_public_id, supersede=True, reason=reason,
            announce_successor=announce_successor,
        )

    async def _create(
        self,
        *,
        slide_id: int,
        owner_user_id: int,
        slide_public_id: str,
        supersede: bool,
        reason: str = "superseded",
        announce_successor: bool = False,
    ) -> Tuple[str, List[str]]:
        session_id = secrets.token_urlsafe(16)
        room = Room(
            session_id=session_id,
            slide_id=slide_id,
            owner_user_id=owner_user_id,
            slide_public_id=slide_public_id,
            # Arm the grace deadline at creation so a session whose presenter
            # never completes the WS hello (tab closed, network failure
            # between the REST create and the upgrade) is reaped by gc_step
            # instead of lingering in _rooms forever. attach_presenter clears
            # this back to None once a presenter actually connects.
            presenter_grace_until=time.monotonic() + PRESENTER_GRACE_SECONDS,
        )
        async with self._global_lock:
            superseded = [
                r for r in self._rooms.values() if r.owner_user_id == owner_user_id
            ] if supersede else []
            # Enforce the absolute room ceiling atomically under the lock so two
            # concurrent creates can't both slip past a len() check. Judged
            # against what the table holds once the caller's superseded rooms
            # are gone, since those are being replaced rather than added to:
            # without the subtraction a presenter at a 1-room cap could never
            # restart their own session.
            if MAX_ROOMS > 0 and (len(self._rooms) - len(superseded)) >= MAX_ROOMS:
                raise RoomLimitError()
            for r in superseded:
                self._rooms.pop(r.session_id, None)
            self._rooms[session_id] = room
        # Outside the lock: teardown sends/closes over the network. The rooms
        # are already unreachable via _rooms, so nothing can join them meanwhile.
        extra = (
            {"next": {"sid": session_id, "slide_public_id": slide_public_id}}
            if announce_successor and slide_public_id else None
        )
        await self._teardown_rooms([(r, reason) for r in superseded], extra=extra)
        # The discovery ``started`` event is deliberately NOT emitted here.
        # A room whose presenter never completes the WS handshake would flash a
        # "join" banner on every index page for the grace window; instead
        # attach_presenter announces the room on the first successful attach
        # (see Room.discovery_announced), so the banner only appears once a
        # presenter is actually seated.
        return session_id, [r.session_id for r in superseded]

    def get(self, session_id: str) -> Optional[Room]:
        return self._rooms.get(session_id)

    def list_owned(self, owner_user_id: int) -> List[OwnedSession]:
        """Live sessions created by ``owner_user_id``.

        Backs the presenter-side "resume" affordance: a presenter who left a
        session (tab closed / reload / transient network drop) can still
        reattach within the grace window, but the client first needs to learn
        which still-live session is theirs. Ownership is server-side state
        (``Room.owner_user_id``), so this owner-scoped view is exposed only
        behind staff auth.

        Rooms with a presenter still attached are listed too, flagged
        ``presenter_connected``. After a network drop or a crashed tab the
        server keeps the dead socket registered until the keepalive notices
        (up to WSI_SYNC_PING_TIMEOUT_SEC); hiding the room meanwhile would leave the
        presenter only "Start", which supersedes the session and ends it for
        every follower, where a Resume (a fresh attach) takes it over cleanly.
        """
        # Call on the event loop (the sync router's list endpoints are async for
        # this): the loop is the only thread that mutates rooms, so the entries
        # are one consistent snapshot. The ``list(...)`` copy is a backstop for
        # a caller on another thread, where iterating ``_rooms.values()`` with
        # per-item Python work could raise "dictionary changed size during
        # iteration".
        rooms = list(self._rooms.values())
        return [
            OwnedSession(
                session_id=r.session_id, slide_id=r.slide_id,
                slide_public_id=r.slide_public_id,
                presenter_connected=r.presenter is not None,
            )
            for r in rooms
            if r.owner_user_id == owner_user_id
        ]

    def list_all_detailed(self) -> List[AdminSessionEntry]:
        """Snapshot of every live room for the staff management surface.

        Unlike ``list_owned`` (caller-scoped), this returns every room with the
        triage fields in ``AdminSessionEntry`` so a staff user can see — and
        end — an orphan regardless of who started it or whether a presenter is
        still attached. Read without the per-room lock, mirroring
        ``list_owned``: a momentarily stale count or flag is harmless for a
        read-only triage view, and the DELETE that follows re-checks room state
        under the lock anyway.
        """
        now = time.monotonic()
        out: List[AdminSessionEntry] = []
        # Call on the event loop, as list_owned.
        rooms = list(self._rooms.values())
        for r in rooms:
            grace_remaining: Optional[float] = None
            if r.presenter is None and r.presenter_grace_until is not None:
                grace_remaining = max(0.0, r.presenter_grace_until - now)
            out.append(
                AdminSessionEntry(
                    session_id=r.session_id,
                    slide_id=r.slide_id,
                    owner_user_id=r.owner_user_id,
                    presenter_connected=r.presenter is not None,
                    follower_count=len(r.followers),
                    created_at=r.created_at,
                    grace_remaining=grace_remaining,
                )
            )
        return out

    async def end(self, session_id: str, *, reason: str = "ended") -> bool:
        async with self._global_lock:
            room = self._rooms.pop(session_id, None)
        if not room:
            return False
        await self._teardown_rooms([(room, reason)])
        return True

    def _spawn(self, coro: Awaitable[Any]) -> "asyncio.Task[Any]":
        """Run ``coro`` as a task the manager keeps a strong reference to.

        Started eagerly, so whatever it does before its first suspension has
        happened by the time this returns (a fake callback in a test, or the
        transport's non-blocking send, completes on the spot).
        """
        task = asyncio.Task(coro, loop=asyncio.get_running_loop(), eager_start=True)
        if not task.done():
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _teardown_rooms(
        self, rooms: List[Tuple[Room, str]], *, extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Tear down rooms already removed from ``_rooms``, all at once.

        Each teardown runs as a task of its own that the caller's cancellation
        does not reach. By now the room is unreachable — popped from the table
        — so a teardown cut short (a REST handler cancelled mid-request, a
        shutdown) would leave its followers watching a dead session with no
        ``end``, its presenter's frames silently dropped and the index pages'
        banner up. And concurrently, so a wedged socket in one room (whose close
        can wait out the transport's timeout) holds up no other room's.
        """
        if not rooms:
            return
        tasks = [self._spawn(self._teardown_room(room, reason, extra=extra)) for room, reason in rooms]
        gathered = asyncio.gather(*tasks, return_exceptions=True)

        def _log_failures(results: List[Any]) -> None:
            # Gathered rather than raised (one room's failure must not stop the
            # others'), but not silenced: a raising teardown is logged here, as
            # it would be by the GC loop / the endpoint if it propagated.
            for (room, _reason), result in zip(rooms, results):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    _log.error(
                        "sync: tearing down room %s failed", room.session_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )

        try:
            results = await asyncio.shield(gathered)
        except asyncio.CancelledError:
            # The caller is gone but the teardowns go on: report their
            # failures once they finish.
            gathered.add_done_callback(
                lambda fut: None if fut.cancelled() else _log_failures(fut.result())
            )
            raise
        _log_failures(results)

    async def _teardown_room(
        self, room: Room, reason: str, *, extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Drop any armed audience-flush timer: the room is going away, and the
        # trailing send would either no-op (presenter cleared below) or fire
        # against a torn-down room.
        if room._audience_timer is not None:
            room._audience_timer.cancel()
            room._audience_timer = None
        room._audience_pending = False
        async with room.lock:
            followers = list(room.followers.values())
            presenter = room.presenter
            # Only a room some index page may be showing gets a matching
            # ``ended``. Being announced (first presenter attach) is not
            # enough: a session kept on link-only slides is announced but was
            # never let through the discovery gate, and an ``ended`` for it
            # would hand every index page the sid of a session they must not
            # learn — the leak the refused-slide-change withdrawal avoids too
            # (see Room.discovery_shown). A room torn down before any presenter
            # seated was never shown either.
            shown = room.discovery_announced and room.discovery_shown
            room.followers.clear()
            room.presenter = None

        end_frame = SharedFrame({"type": "end", "reason": reason, **(extra or {})})

        async def _end_and_close(conn) -> None:
            await _safe_send(conn.send, end_frame)
            try:
                await conn.close(1000, "session ended")
            except Exception:
                pass

        # The presenter gets an explicit ``end`` too, mirroring the follower
        # path. Without it the client-side close handler cannot distinguish
        # "session is gone for good" from a transient network drop and would
        # reconnect indefinitely against an empty _rooms dict.
        #
        # Fanned out rather than awaited one at a time (same shape as
        # _fan_out): a room can hold up to FOLLOWERS_PER_ROOM_MAX connections,
        # and a single stalled close on a wedged socket would otherwise hold up
        # every remaining teardown behind it (rooms are torn down alongside
        # each other for the same reason, see _teardown_rooms).
        targets = list(followers)
        if presenter is not None:
            targets.append(presenter)
        if targets:
            await asyncio.gather(
                *[_end_and_close(c) for c in targets],
                return_exceptions=True,
            )
        # Each ``close`` above trips the bridge's ``closing`` signal, which the
        # WS recv loop (_ws_main_loop) races against ``ws.receive()`` — so a
        # frozen peer's recv side is released as soon as teardown closes it
        # rather than lingering on an un-closable socket until the
        # WS_RECV_IDLE_TIMEOUT (~45s) backstop. See _WsBridge.closing_event.
        if shown:
            await self._notify_discovery({"type": "ended", "sid": room.session_id})

    # -------------------------------------------------------------- presenter
    async def attach_presenter(
        self,
        session_id: str,
        *,
        user_id: int,
        send: SendCallback,
        close: CloseCallback,
        client_id: Optional[str] = None,
        resume: bool = False,
    ) -> Optional[Tuple[Room, PresenterConn]]:
        """Attach a presenter to ``session_id``.

        Returns ``(room, conn)`` so the caller can identify *its* connection
        when it later calls ``detach_presenter`` — important because a
        same-owner reconnect can replace ``room.presenter`` while the old
        recv loop is still unwinding through its ``finally`` block.

        ``client_id`` identifies the presenter client instance (see
        :class:`PresenterSupersededError`); ``resume`` is True only for that
        client's automatic reconnect. A resume is refused with
        PresenterSupersededError when a *different* client id attached since
        — the only thing that makes this client stale. Whether this client's
        own last attach landed (or its ``attached`` frame was lost) does not
        matter. A fresh attach (``resume`` False: first connect, an explicit
        Resume, or a hello without a client id) always takes over.
        """
        room = self._rooms.get(session_id)
        if not room or room.owner_user_id != user_id:
            return None
        evicted: Optional[PresenterConn] = None
        conn = PresenterConn(send=send, close=close)
        async with room.lock:
            # Re-check after acquiring the lock: end() / GC may have removed
            # the room from _rooms between the lookup above and here.
            if self._rooms.get(session_id) is not room:
                return None
            if (
                resume
                and client_id is not None
                and room.presenter_client_id is not None
                and room.presenter_client_id != client_id
            ):
                raise PresenterSupersededError()
            # An id-less hello (id missing or malformed) gets a unique
            # placeholder: it is a different client from every id-carrying
            # one, and must supersede them just as a fresh attach from a new
            # instance would.
            room.presenter_client_id = (
                client_id if client_id is not None else "anon:" + secrets.token_urlsafe(8)
            )
            if room.presenter is not None:
                # A presenter is already attached. The most common reason a
                # legitimate owner reconnects while the old socket is still
                # registered is a transient network drop on the client: the
                # server hasn't observed the half-closed TCP yet, so the old
                # PresenterConn is stale. Evict it so the fresh attach
                # succeeds instead of churning on 4409 until ping-timeout
                # finally reaps the dead one ~30 s later.
                evicted = room.presenter
            # Whether followers were actually told the presenter left. Only then
            # do they need a "returned" frame — a reconnect inside the debounce
            # window (notice never sent) clears silently, producing no flash.
            notified = room.presenter_absent_notified
            room.presenter = conn
            # A (re)attached presenter is a new audience for the one-shot
            # "ends soon" frame: a tab reloaded after the warning went out
            # would otherwise never learn the session is about to end.
            room.max_age_warned = False
            room.presenter_grace_until = None
            room.presenter_left_at = None
            room.presenter_absent_notified = False
            # A first attach or a deliberate one (Start, Resume) is the
            # presenter acting, so it arms the inactivity clock afresh. An
            # automatic reconnect is not: resetting on it would let a client stuck
            # in a reconnect loop — a zombie tab on a flaky link — hold the
            # room forever, short of the absolute age cap. Its idle stretch
            # carries on from its last real frame (the reconnect's replayed
            # view and pointer do not count either, see broadcast_view). The
            # warning is re-armed both ways, so a new socket inside the
            # warning window is warned again on the next GC tick.
            if not resume or room.presenter_last_activity is None:
                room.presenter_last_activity = time.monotonic()
            room.presenter_idle_warned = False
            follower_sends = (
                [f.send for f in room.followers.values()] if notified else []
            )
            # Announce the room to discovery on the FIRST successful attach only.
            # create() intentionally stays silent so a presenter who never
            # completes the handshake never surfaces a join banner; a reattach
            # within grace already announced, so it must not re-fire ``started``.
            announce = not room.discovery_announced
            if announce:
                room.discovery_announced = True
        if evicted is not None:
            # Not awaited: closing a socket whose writer already died waits out
            # the transport's close timeout, and the presenter taking over must
            # not be held up behind the one it replaces.
            self._spawn(_safe_close(evicted.close, 4000, "presenter replaced"))
        await _fan_out(follower_sends, {"type": "presenter", "present": True})
        await self._send_audience_to_presenter(room)
        if announce and self._rooms.get(room.session_id) is room:
            # Liveness re-check, because the awaits above (evicting a stale
            # connection, fanning out to followers) all yield the loop: a
            # concurrent end() — an admin's End button, or GC reaping the room
            # — pops it and broadcasts ``ended`` in that window. Announcing
            # ``started`` afterwards leaves every index page showing a join
            # banner for a session that no longer exists until the subscriber
            # reconnects and gets a fresh snapshot. Same identity guard
            # detach_presenter uses. Its own task, like broadcast_view's
            # ``slide_changed``, so the gate's DB lookup does not delay the
            # presenter's first frames.
            self._spawn(self._notify_discovery({
                "type": "started",
                "sid": room.session_id,
                "slide_id": room.slide_id,
                "slide_public_id": room.slide_public_id,
            }))
        return room, conn

    async def detach_presenter(
        self,
        session_id: str,
        conn: Optional[PresenterConn] = None,
    ) -> None:
        """Detach the presenter, but only if ``conn`` is still the live one.

        ``conn`` is optional for callers that have no identity to assert
        (tests, manual teardown). When supplied, the detach is a no-op if a
        newer attach has already replaced the presenter — without that
        guard, the old recv loop's finally clause could clobber the freshly
        attached successor.
        """
        room = self._rooms.get(session_id)
        if not room:
            return
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return
            if conn is not None and room.presenter is not conn:
                return
            now = time.monotonic()
            room.presenter = None
            room.presenter_grace_until = now + PRESENTER_GRACE_SECONDS
            room.presenter_left_at = now
            room.presenter_absent_notified = False
            # A presenter who left took their pointer with them: a closed tab or
            # a dropped network never sends the pointerleave that hides it. Stop
            # replaying it to followers who join during the absence; the
            # followers already watching are told to hide it along with the
            # (debounced) absence notice. A reconnect of the same client
            # re-sends its current pointer right after its hello.
            room.last_cursor = None
        # No broadcast here. The "presenter left" notice is deferred to the GC
        # tick (gc_step), which fires it only once the presenter has stayed gone
        # past PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS — so a brief
        # disconnect+reconnect inside that window goes entirely unannounced.

    async def _notify_absent(self, room: Room, *, now: float) -> None:
        """Broadcast the debounced "presenter left" notice to followers.

        Re-checks room state under the lock (the presenter may have reconnected,
        or the room been torn down, since gc_step picked it as a candidate) so a
        racing reattach can't be papered over with a stale absence notice.
        """
        async with room.lock:
            if self._rooms.get(room.session_id) is not room:
                return
            if room.presenter is not None or room.presenter_grace_until is None:
                return
            if room.presenter_absent_notified:
                return
            remaining = max(0.0, room.presenter_grace_until - now)
            room.presenter_absent_notified = True
            follower_sends = [f.send for f in room.followers.values()]
        # Take down the laser dot the departed presenter left on every screen
        # (detach_presenter already dropped it from room.last_cursor).
        await _fan_out(
            follower_sends,
            {"type": "cursor", "x": 0.0, "y": 0.0, "visible": False},
        )
        await _fan_out(
            follower_sends,
            {
                "type": "presenter",
                "present": False,
                "grace_seconds": int(round(remaining)),
            },
        )

    # --------------------------------------------------------------- follower
    async def attach_follower(
        self,
        session_id: str,
        *,
        ip: str,
        ua: str,
        name: Optional[str],
        hostname: Optional[str],
        send: SendCallback,
        close: CloseCallback,
        visible: bool = True,
        vw: Any = None,
        vh: Any = None,
        hello_received: bool = True,
    ) -> Optional[Tuple[Room, FollowerMeta]]:
        room = self._rooms.get(session_id)
        if not room:
            return None
        meta = FollowerMeta(
            hello_received=hello_received,
            id=secrets.token_urlsafe(8),
            name=sanitize_name(name),
            ip=ip,
            ua=_scrub_metadata(ua, max_len=_UA_MAX_LEN),
            hostname=(
                _scrub_metadata(hostname, max_len=_HOSTNAME_MAX_LEN)
                or None
            ),
            joined_at=time.time(),
            visible=bool(visible),
            # Passed through raw: FollowerMeta.__post_init__ scrubs them.
            vw=vw,
            vh=vh,
        )
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return None
            if len(room.followers) >= FOLLOWERS_PER_ROOM_MAX:
                # Raise inside the lock so the cap is enforced atomically.
                # Returning a sentinel would race the next attach across the
                # lock release.
                raise RoomFullError()
            if FOLLOWERS_PER_IP_MAX > 0 and sum(
                1 for f in room.followers.values() if f.meta.ip == ip
            ) >= FOLLOWERS_PER_IP_MAX:
                # Same 4503 as a full room: from this client's point of view
                # there is no slot for it, and it retries with backoff either way.
                # Logged so operators can tell this from a genuinely full room.
                self._warn_per_ip_refusal("follower", ip, FOLLOWERS_PER_IP_MAX)
                raise RoomFullError()
            room.followers[meta.id] = FollowerConn(meta=meta, send=send, close=close)
            last_view = room.last_view
            last_cursor = room.last_cursor
            # If this follower joins while the presenter's absence has already
            # been announced (past the debounce), carry the remaining grace so
            # their countdown starts accurately. Within the debounce window
            # nothing is sent — matching what existing followers see — and the
            # pending _notify_absent will reach this follower too.
            grace_until = (
                room.presenter_grace_until
                if room.presenter is None and room.presenter_absent_notified
                else None
            )
        if last_view is not None:
            await _safe_send(send, last_view)
        if last_cursor is not None:
            await _safe_send(send, last_cursor)
        if grace_until is not None:
            remaining = max(0.0, grace_until - time.monotonic())
            await _safe_send(send, {
                "type": "presenter",
                "present": False,
                "grace_seconds": int(round(remaining)),
            })
        # Join goes through the debounce too: one client can join and leave a
        # room hundreds of times within the nginx upgrade burst, and an O(N)
        # audience frame per event would overflow the presenter's bounded
        # outbound queue (closing it with 1013). The leading edge still sends
        # at once after a quiet window, so a lone join shows up immediately.
        await self._schedule_audience(room)
        return room, meta

    async def detach_follower(self, session_id: str, follower_id: str) -> None:
        room = self._rooms.get(session_id)
        if not room:
            return
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return
            room.followers.pop(follower_id, None)
        # Debounced like join (see attach_follower). The trailing flush runs on
        # the manager's loop, not this connection, so it still fires after the
        # departing follower's recv loop has unwound.
        await self._schedule_audience(room)

    async def update_follower_hostname(
        self, session_id: str, follower_id: str, hostname: Optional[str]
    ) -> None:
        """Late-bind the reverse-DNS hostname for an already-attached follower.

        Reverse-DNS resolution runs in a background task so it doesn't add
        its latency (up to ~500 ms) to the WebSocket join. The follower
        is attached with ``hostname=None`` first; when the PTR lookup
        completes (or times out), the WS handler calls this to fill in the
        value and re-broadcast the audience frame so the presenter UI
        updates from the placeholder.
        """
        room = self._rooms.get(session_id)
        if not room:
            return
        cleaned = _scrub_metadata(hostname, max_len=_HOSTNAME_MAX_LEN) or None
        changed = False
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return
            f = room.followers.get(follower_id)
            if f and f.meta.hostname != cleaned:
                f.meta.hostname = cleaned
                changed = True
        if changed:
            # One bind per follower, but one per join too: a client that joins,
            # waits for its PTR, and leaves in a loop would get an undebounced
            # O(N) frame per cycle, so this goes through the debounce as well.
            await self._schedule_audience(room)

    async def update_follower_visibility(
        self, session_id: str, follower_id: str, visible: bool
    ) -> None:
        room = self._rooms.get(session_id)
        if not room:
            return
        changed = False
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return
            f = room.followers.get(follower_id)
            if f and f.meta.visible != bool(visible):
                f.meta.visible = bool(visible)
                changed = True
        if changed:
            # Visibility is the one audience-affecting event a single persistent
            # follower can emit without reconnecting, so it is THE amplification
            # vector: route it through the debounce so a fast toggle storm
            # collapses to at most one audience frame per window instead of an
            # O(N) rebuild+send per flip.
            await self._schedule_audience(room)

    async def update_follower_hello(
        self,
        session_id: str,
        follower_id: str,
        *,
        name: Any,
        visible: bool,
        vw: Any = None,
        vh: Any = None,
    ) -> bool:
        """Apply a hello that arrived after its follower was attached.

        A follower whose hello misses WSI_SYNC_HELLO_TIMEOUT_SEC (a slow link)
        is attached anonymously rather than refused; when its hello does
        arrive it fills in the name, visibility and viewport the follower would
        have joined with. Only once: a follower attached on its hello, or one
        already completed, is left alone (returns False), so a client cannot
        rename itself mid-session.
        """
        room = self._rooms.get(session_id)
        if not room:
            return False
        probe = FollowerMeta(
            id="", name=None, ip="", ua="", hostname=None, joined_at=0.0, vw=vw, vh=vh,
        )
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return False
            f = room.followers.get(follower_id)
            if f is None or f.meta.hello_received:
                return False
            f.meta.hello_received = True
            f.meta.name = sanitize_name(name)
            f.meta.visible = bool(visible)
            f.meta.vw, f.meta.vh = probe.vw, probe.vh
        await self._schedule_audience(room)
        return True

    # ------------------------------------------------------------- broadcast
    async def broadcast_view(
        self,
        session_id: str,
        frame: dict,
        *,
        presenter: Optional["PresenterConn"] = None,
        activity: bool = True,
    ) -> bool:
        """Pin and fan out a presenter view frame.

        ``presenter`` is the connection the frame arrived on. It must still
        be the room's presenter when the frame is applied: the WS handler
        awaits a DB lookup between receiving a slide change and calling this,
        and a takeover in that window would otherwise let the superseded
        connection's late frame overwrite the room's slide and last_view
        (replayed to every joiner) with state the new presenter has moved on
        from. Checked under ``room.lock``, the same lock attach_presenter
        swaps the presenter under, so no check-then-act gap remains.

        ``activity`` False is for the frame a reconnecting client replays
        after its hello: relayed like any other, but it is not the presenter
        interacting, so it neither refreshes the inactivity clock nor clears
        its warning (see attach_presenter).
        """
        room = self._rooms.get(session_id)
        if not room:
            return False
        # Reject view frames with a non-int / bool slide_id outright. Bool
        # is excluded explicitly because ``isinstance(True, int)`` is True
        # in Python and a stray ``slide_id: true`` would otherwise be
        # accepted and forwarded. Followers map slide_id straight into a
        # navigation URL (`/slides/<id>`), so a malformed value would send
        # every viewer to a 404.
        new_slide_id = frame.get("slide_id")
        if not isinstance(new_slide_id, int) or isinstance(new_slide_id, bool):
            return False
        slide_changed = False
        current_slide_id = None
        async with room.lock:
            # Re-check the room is still registered after acquiring the lock:
            # end() / gc_step() may have torn it down (and begun closing its
            # followers, which they do outside the lock) between the lookup
            # above and here. Every other room-mutating method performs this
            # guard; without it a stray view frame can reach a follower after
            # its `end`/close during a teardown race.
            if self._rooms.get(session_id) is not room:
                return False
            if presenter is not None and room.presenter is not presenter:
                return False
            if new_slide_id != room.slide_id:
                room.slide_id = new_slide_id
                # The WS handler stamps the new slide's public_id onto the frame
                # (after its published-gate lookup); adopt it so the room's
                # token tracks the numeric id in lock-step.
                frame_public_id = frame.get("slide_public_id")
                if isinstance(frame_public_id, str) and frame_public_id:
                    room.slide_public_id = frame_public_id
                slide_changed = True
                # Cursor coordinates are in image-pixel space (level 0) and
                # are tied to the slide that was active when the presenter
                # sent them. Pinning the old cursor across a slide change
                # would replay it to late followers against a different
                # image, projecting the laser dot to an unrelated location
                # (or off-canvas) until the next live cursor frame arrives.
                room.last_cursor = None
            current_slide_id = room.slide_id
            # Stamp every outgoing frame (and the pinned last_view replayed to
            # late joiners) with the room's current slide token so followers
            # can navigate by public_id without a per-frame DB lookup.
            frame["slide_public_id"] = room.slide_public_id
            room.last_view = frame
            if slide_changed:
                # The slide the session left was the one unpublished, if any:
                # the new one has just passed the WS handler's publish gate.
                room.slide_unavailable = False
            # A view frame is presenter activity: refresh the inactivity clock
            # and clear any pending idle warning. ping/pong does not reach here,
            # so a silent zombie tab never resets this.
            if activity:
                room.presenter_last_activity = time.monotonic()
                room.presenter_idle_warned = False
            targets = [f.send for f in room.followers.values()]
        await _fan_out(targets, frame)
        if (
            slide_changed
            and room.discovery_announced
            and self._rooms.get(session_id) is room
        ):
            # Liveness re-check after the fan-out await (same guard as the
            # ``started`` path in attach_presenter): the index page adds a
            # session it does not know on ``slide_changed`` (so a session hidden
            # from its snapshot because the slide was unpublished reappears once
            # the presenter moves to a published slide), so a stray event after
            # the room's ``ended`` would resurrect a dead banner.
            #
            # A task of its own: the discovery gate is a DB lookup, and awaiting
            # it here would hold up the presenter's next frames on its receive loop.
            # _notify_discovery re-checks the room and its slide once the gate
            # answers, so a verdict overtaken by a later change is dropped.
            self._spawn(self._notify_discovery({
                "type": "slide_changed",
                "sid": session_id,
                "slide_id": current_slide_id,
                "slide_public_id": room.slide_public_id,
            }))
        return True

    async def broadcast_cursor(
        self,
        session_id: str,
        frame: dict,
        *,
        presenter: Optional["PresenterConn"] = None,
        activity: bool = True,
    ) -> bool:
        room = self._rooms.get(session_id)
        if not room:
            return False
        async with room.lock:
            # Re-check after acquiring the lock (see broadcast_view): a
            # concurrent teardown can pop+clear this room and start closing
            # its followers outside the lock, so without this guard a cursor
            # frame could be fanned out after those followers' `end`/close.
            if self._rooms.get(session_id) is not room:
                return False
            # A superseded presenter's frame is dropped (see broadcast_view).
            if presenter is not None and room.presenter is not presenter:
                return False
            room.last_cursor = frame
            # Cursor frames are presenter activity too, a reconnect's replayed
            # one aside (see broadcast_view).
            if activity:
                room.presenter_last_activity = time.monotonic()
                room.presenter_idle_warned = False
            targets = [f.send for f in room.followers.values()]
        await _fan_out(targets, frame)
        return True

    async def touch_presenter(
        self, session_id: str, presenter: Optional["PresenterConn"] = None,
    ) -> bool:
        """Count a presenter frame that is NOT relayed (e.g. a cursor suppressed
        because the presenter is on an unavailable slide) as activity, so a
        presenter who is plainly driving the session is not ended as idle."""
        room = self._rooms.get(session_id)
        if not room:
            return False
        async with room.lock:
            if self._rooms.get(session_id) is not room:
                return False
            if presenter is not None and room.presenter is not presenter:
                return False
            room.presenter_last_activity = time.monotonic()
            room.presenter_idle_warned = False
        return True

    # -------------------------------------------------------------- audience
    async def _send_audience_to_presenter(self, room: Room) -> None:
        # Asked per frame, so a privacy-mode change reaches the next frame.
        hook = audience_ip_pseudonymiser
        try:
            ip_pseudonym = hook() if hook is not None else None
        except Exception:
            # Fail closed: withhold the addresses rather than show them raw
            # when it cannot be told whether they may be shown.
            _log.exception("audience privacy check failed; withholding follower addresses")
            ip_pseudonym = lambda _ip: ""  # noqa: E731
        async with room.lock:
            presenter = room.presenter
            followers = [
                f.meta.to_dict(ip_pseudonym=ip_pseudonym) for f in room.followers.values()
            ]
        if presenter is None:
            return
        await _safe_send(presenter.send, {
            "type": "audience",
            "count": len(followers),
            "followers": followers,
            # Lets the panel say why addresses are pseudonyms and UAs absent.
            "pseudonymous": ip_pseudonym is not None,
        })

    async def _schedule_audience(self, room: Room) -> None:
        """Coalesce presenter-bound audience frames on a short debounce.

        Used by the follower join / leave / hostname / visibility paths —
        events a single client can repeat rapidly (by toggling, or by
        reconnecting in a loop), and thus amplification vectors. Sending the
        full O(N) follower list on every event would let one hostile client
        flood the presenter's bounded outbound queue and re-serialise the list
        for every other room on the shared loop. Instead: the first change after a quiet period goes out
        immediately (leading edge, so the presenter UI stays responsive), and
        any further changes inside the window collapse into ONE trailing send.
        ``_send_audience_to_presenter`` always reads the live follower list, so
        the latest state wins.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        if room._audience_pending:
            # A trailing flush is already armed; it will pick up the latest
            # state, so nothing more to do.
            return
        if now - room._audience_last_sent >= self._audience_debounce:
            # Quiet window: send immediately, no timer needed.
            room._audience_last_sent = now
            await self._send_audience_to_presenter(room)
            return
        # Too soon after the last send: arm a single trailing flush at the end
        # of the current window.
        room._audience_pending = True
        delay = self._audience_debounce - (now - room._audience_last_sent)
        room._audience_timer = loop.call_later(
            max(0.0, delay), self._fire_audience_timer, room
        )

    def _fire_audience_timer(self, room: Room) -> None:
        """Trailing-edge callback: flush the coalesced audience frame."""
        room._audience_timer = None
        if not room._audience_pending:
            return
        room._audience_pending = False
        room._audience_last_sent = asyncio.get_running_loop().time()
        # The send is a coroutine; spawn it and hold a strong ref so it isn't
        # GC'd mid-flight.
        task = asyncio.ensure_future(self._send_audience_to_presenter(room))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _warn_per_ip_refusal(self, cap: str, ip: str, limit: int) -> None:
        """Log a per-IP cap refusal at WARNING, at most once a minute per IP.

        Both per-IP caps answer with the same 4503 as a genuinely full room /
        channel, so without this an operator facing "students see 'room full'"
        cannot tell a NAT'd class tripping the per-IP share (fix: raise or
        disable it) from real capacity exhaustion. Rate-limited per (cap, IP)
        because a refused client keeps retrying with backoff.
        """
        now = time.monotonic()
        key = (cap, ip)
        last = self._per_ip_warned_at.get(key)
        if last is not None and now - last < PER_IP_REFUSAL_LOG_INTERVAL_SECONDS:
            return
        # Keep the table bounded: drop entries whose window has passed once it
        # grows (a scan of many distinct addresses would otherwise grow it).
        if len(self._per_ip_warned_at) >= 1024:
            self._per_ip_warned_at = {
                k: t for k, t in self._per_ip_warned_at.items()
                if now - t < PER_IP_REFUSAL_LOG_INTERVAL_SECONDS
            }
        self._per_ip_warned_at[key] = now
        _log.warning(
            "live sync: refused a %s connection from %s: per-IP cap of %d reached "
            "(WSI_SYNC_%s). If many users share this address (NAT / tunnel), "
            "raise the cap or set it to 0.",
            cap, ip, limit,
            "FOLLOWERS_PER_IP_MAX" if cap == "follower" else "DISCOVERY_MAX_SUBS_PER_IP",
        )

    # -------------------------------------------------------------- discovery
    def _discovery_refused(self, ip: Optional[str]) -> bool:
        """True when the global or the per-IP subscriber cap is reached.

        Call with ``_global_lock`` held. A per-IP refusal is logged (rate-
        limited) so operators can tell it from a genuinely full channel.
        """
        if len(self._discovery_subs) >= DISCOVERY_MAX_SUBSCRIBERS:
            return True
        if (
            ip is not None
            and DISCOVERY_MAX_SUBS_PER_IP > 0
            and self._discovery_ip_counts.get(ip, 0) >= DISCOVERY_MAX_SUBS_PER_IP
        ):
            self._warn_per_ip_refusal("discovery", ip, DISCOVERY_MAX_SUBS_PER_IP)
            return True
        return False

    async def add_discovery_subscriber(
        self,
        sub_id: str,
        send: SendCallback,
        *,
        ip: Optional[str] = None,
        snapshot_filter: Optional[
            Callable[[List[dict]], Awaitable[List[dict]]]
        ] = None,
    ) -> bool:
        """Register a discovery subscriber and send it the live-room snapshot.

        Returns False (caller closes with 4503) when the global or the per-IP
        (``ip``) subscriber cap is reached. ``snapshot_filter`` lets the
        transport drop entries this module cannot judge — the WS handler uses
        it to hide rooms whose slide has since been unpublished, matching the
        session peek (``GET /api/sync/sessions/{sid}``), or is link-only. It
        runs outside the lock (it does DB I/O).

        Ordering guarantee: the snapshot is the first frame the subscriber
        receives; no ``started`` / ``ended`` / ``slide_changed`` event can
        overtake it. The client *replaces* its list with the snapshot, so an
        event delivered first would be silently lost (a session started in
        that window would never show its banner). Hence:

        1. the filter runs BEFORE registration, on a candidate list;
        2. registration and the final snapshot are then computed in one
           critical section, so every event broadcast before it is already
           reflected in the snapshot and every event after it is for a
           registered subscriber;
        3. until the snapshot has been handed to ``send``, events for this
           subscriber are buffered (see ``_notify_discovery``) and flushed
           right after it, so the order holds whatever ``send`` awaits on.
        """
        async with self._global_lock:
            # Fast fail before paying for the filter's DB round-trip. Re-checked
            # below, since slots may be taken while the filter runs.
            if self._discovery_refused(ip):
                return False
            # Only announced rooms (a presenter has seated at least once) appear
            # in the snapshot, mirroring the deferred ``started`` broadcast so a
            # subscriber connecting during the create->attach window doesn't see
            # a room that never surfaced a ``started`` event.
            candidates = [
                {"sid": r.session_id, "slide_id": r.slide_id, "slide_public_id": r.slide_public_id}
                for r in self._rooms.values()
                if r.discovery_announced
            ]
        # (sid, slide_id) pairs the filter judged, and those it let through.
        judged = {(c["sid"], c["slide_id"]) for c in candidates}
        allowed = set(judged)
        # (sid, slide_id) -> the fields the filter added to a kept entry (the
        # slide's title and catalog id), carried into the snapshot.
        labels: Dict[Tuple[str, int], Dict[str, Any]] = {}
        if snapshot_filter is not None and candidates:
            try:
                kept = await snapshot_filter(candidates)
                allowed = {(s["sid"], s["slide_id"]) for s in kept}
                labels = {
                    (s["sid"], s["slide_id"]): {
                        k: v for k, v in s.items()
                        if k not in ("sid", "slide_id", "slide_public_id")
                    }
                    for s in kept
                }
            except Exception:
                # Fail closed: an empty snapshot only hides a banner until the
                # next ``started`` event or reconnect, whereas failing open
                # would advertise sessions on slides that may be private.
                _log.exception("discovery snapshot filter failed")
                allowed = set()
        async with self._global_lock:
            if self._discovery_refused(ip):
                return False
            self._discovery_subs[sub_id] = send
            self._discovery_pending[sub_id] = []
            if ip is not None:
                self._discovery_sub_ip[sub_id] = ip
                self._discovery_ip_counts[ip] = self._discovery_ip_counts.get(ip, 0) + 1
            # Design tradeoff: this snapshot hands every live session id to
            # any app-authenticated discovery subscriber, so possession of a sid
            # is effectively the capability to *follow* that session. This is
            # intentional — a follower can only ever fetch published slides (the
            # public/dzi endpoints gate on is_published, and create refuses
            # unpublished slides), so the id leaks presence, not content. Session
            # content stays bounded to published material regardless of who
            # holds the id.
            #
            # Built from the rooms as they are NOW, not from the candidates: a
            # room that ended while the filter ran is gone (its ``ended`` went
            # out before this subscriber registered, so it must not be
            # resurrected), and a room announced meanwhile is included (its
            # ``started`` went out before registration too). A (sid, slide_id)
            # the filter never judged — a new room, or one whose presenter
            # switched slides meanwhile — follows the ``started`` /
            # ``slide_changed`` event it stands in for: included iff the
            # discovery gate already let that event through (the room's
            # recorded verdict). It cannot be included unjudged: a presenter
            # can start on / switch to a link-only slide, which is
            # presentable but must not be listed.
            # A verdict not yet recorded means the event is still in the gate,
            # and will reach this subscriber once it is registered here.
            visible = [
                r for r in self._rooms.values()
                if r.discovery_announced
                and (
                    (r.session_id, r.slide_id) in allowed
                    if (r.session_id, r.slide_id) in judged
                    else (
                        discovery_event_gate is None
                        or r.discovery_verdict == (r.slide_id, True)
                    )
                )
            ]
            for r in visible:
                r.discovery_shown = True

            def _labels_for(r: Room) -> Dict[str, Any]:
                key = (r.session_id, r.slide_id)
                if key in labels:
                    return labels[key]
                if r.discovery_labels is not None and r.discovery_labels[0] == r.slide_id:
                    return r.discovery_labels[1]
                return {}

            sessions = [
                {
                    **_labels_for(r),
                    "sid": r.session_id,
                    "slide_id": r.slide_id,
                    "slide_public_id": r.slide_public_id,
                }
                for r in visible
            ]
        try:
            await _safe_send(send, {"type": "snapshot", "sessions": sessions})
            # Flush what arrived while the snapshot was being sent. Events
            # raised during these sends keep appending to the same buffer, so
            # loop until it is empty, then (synchronously, no await in between)
            # switch the subscriber to direct delivery.
            while True:
                buf = self._discovery_pending.get(sub_id)
                if buf is None:
                    break  # removed concurrently
                if not buf:
                    del self._discovery_pending[sub_id]
                    break
                await _safe_send(send, buf.pop(0))
        except BaseException:
            # Cancelled mid-send (the handler task was torn down): don't leave
            # a registered subscriber — it holds a capped slot — behind.
            # Synchronous, so a second cancellation cannot interrupt it.
            self._drop_discovery_subscriber(sub_id)
            raise
        return True

    def _drop_discovery_subscriber(self, sub_id: str) -> None:
        """Forget ``sub_id`` and release its per-IP slot. Idempotent.

        Plain dict operations with no await, so it is atomic on the event loop
        (the lock-holding sections of add_discovery_subscriber never await
        either) and safe to call from a cancellation handler.
        """
        self._discovery_subs.pop(sub_id, None)
        self._discovery_pending.pop(sub_id, None)
        ip = self._discovery_sub_ip.pop(sub_id, None)
        if ip is not None:
            left = self._discovery_ip_counts.get(ip, 0) - 1
            if left > 0:
                self._discovery_ip_counts[ip] = left
            else:
                self._discovery_ip_counts.pop(ip, None)

    async def remove_discovery_subscriber(self, sub_id: str) -> None:
        async with self._global_lock:
            self._drop_discovery_subscriber(sub_id)

    async def _notify_discovery(self, event: dict) -> None:
        # ``started`` / ``slide_changed`` name the slide being presented. Gate
        # them like the snapshot so a slide staff unpublished, or one kept
        # link-only, never has its public_id announced to every index page.
        gate = discovery_event_gate
        if event.get("type") in ("started", "slide_changed"):
            listed = True
            # Whether the gate actually judged the slide unlisted, as opposed
            # to failing: only a real verdict warrants telling the presenter.
            refused = False
            fields: Dict[str, Any] = {}
            if gate is not None:
                try:
                    verdict = await gate(event)
                    listed = isinstance(verdict, dict) or bool(verdict)
                    refused = not listed
                    fields = _gate_labels(verdict)
                except Exception:
                    _log.exception("discovery event gate failed; dropping event")
                    listed = False
            # The gate awaits a thread (DB lookup), and an end() can land in
            # that window and broadcast ``ended`` first. Announcing the session
            # afterwards would leave a dead banner on every index page, so look
            # again (same liveness guard attach_presenter applies).
            sid = event.get("sid")
            room = self._rooms.get(sid) if sid is not None else None
            if sid is not None and room is None:
                return
            if room is not None and room.slide_id != event.get("slide_id"):
                # Overtaken while the gate ran: the room is on another slide
                # now (the presenter moved on, or another tab took over and
                # did), and that change raised an event of its own, which
                # decides what the index pages show. Applying this stale one —
                # even just its ``discovery_shown`` — could leave a banner up for
                # a session now on a link-only slide, or take a listed one down.
                return
            if room is not None:
                # Recorded with no await before the targets are picked below,
                # so a subscriber registering concurrently either gets this
                # event or finds the verdict (see add_discovery_subscriber).
                room.discovery_verdict = (room.slide_id, listed)
                if listed:
                    room.discovery_labels = (room.slide_id, fields)
                was_shown = room.discovery_shown
                room.discovery_shown = listed
            else:
                was_shown = False
            if not listed:
                if event.get("type") != "slide_changed" or not was_shown:
                    return
                # The presenter moved a session the index pages show onto a
                # slide they must not: take the banner down rather than leave
                # it pointing at the previous slide. A later move back onto a
                # listed slide re-adds it (the index page adds an unknown sid
                # on ``slide_changed``).
                event = {"type": "ended", "sid": sid}
                if refused and room is not None:
                    self._warn_unlisted_announced(room)
            elif fields:
                event = {**fields, **event}
        await self._deliver_discovery(event)

    async def _deliver_discovery(self, event: dict) -> None:
        targets: List[SendCallback] = []
        for sub_id, send in self._discovery_subs.items():
            pending = self._discovery_pending.get(sub_id)
            if pending is not None:
                # Its snapshot is not out yet; add_discovery_subscriber
                # delivers this right after it (see the ordering guarantee).
                pending.append(event)
            else:
                targets.append(send)
        await _fan_out(targets, event)

    def _warn_unlisted_announced(self, room: Room) -> None:
        """Tell the presenter their announced session is now on a link-only slide.

        The index pages' banner is taken down, but every index page that showed
        it was handed the sid, and the sid is what joins (the presenter's share
        link carries nothing more). So anyone who saw the banner can still
        follow the session onto this slide — which a link-only slide is
        otherwise not shown to. Withholding the slide from them would also lock
        out the followers already watching (each reconnect is a fresh join), so
        the presenter is told instead, to decide whether to show it here.
        """
        presenter = room.presenter
        if presenter is not None:
            self._spawn(_safe_send(presenter.send, {
                "type": "notice", "code": "unlisted_announced",
            }))

    async def refresh_slide_status(
        self,
        session_id: str,
        slide_id: int,
        *,
        published: bool,
        listed: bool,
        labels: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Bring a running room in line with what staff did to its slide since.

        The publish gate runs when a session starts and when the presenter
        switches slides, so a slide unpublished (or made link-only, or listed
        again) *while it is being presented* would change nothing for the
        room on its own: followers would sit on a viewer whose tiles 404, and
        the index pages would keep — or never get — the banner. The sync
        router calls this periodically (with the presenter's re-auth) with the
        slide's current status; ``slide_id`` is the slide that status is for,
        and a room that has moved on since is left alone.

        * Unpublished: the followers get a ``slide_unavailable`` notice (they
          stay in the session, and follow the presenter onto the next
          published slide) and the presenter a ``slide_unpublished`` one; once
          published again, the followers get ``slide_available``.
        * The discovery banner follows ``listed``: taken down from the index
          pages that show it, or added (as the ``slide_changed`` for an
          unknown sid that the index page adds) once the slide is listed.
        """
        room = self._rooms.get(session_id)
        if not room or room.slide_id != slide_id:
            return
        notice: Optional[str] = None
        warn_unlisted = False
        async with room.lock:
            if self._rooms.get(session_id) is not room or room.slide_id != slide_id:
                return
            if not published and not room.slide_unavailable:
                room.slide_unavailable = True
                notice = "slide_unavailable"
            elif published and room.slide_unavailable:
                room.slide_unavailable = False
                notice = "slide_available"
            presenter = room.presenter
            follower_sends = [f.send for f in room.followers.values()]
            event: Optional[dict] = None
            if room.discovery_announced:
                room.discovery_verdict = (slide_id, listed)
                if listed:
                    room.discovery_labels = (slide_id, dict(labels or {}))
                if listed and not room.discovery_shown:
                    event = {
                        **(labels or {}),
                        "type": "slide_changed",
                        "sid": session_id,
                        "slide_id": slide_id,
                        "slide_public_id": room.slide_public_id,
                    }
                elif not listed and room.discovery_shown:
                    event = {"type": "ended", "sid": session_id}
                    # Made link-only while announced (an unpublished slide has
                    # its own notice).
                    warn_unlisted = published
                room.discovery_shown = listed
        if warn_unlisted:
            self._warn_unlisted_announced(room)
        if notice is not None:
            if presenter is not None and notice == "slide_unavailable":
                # Its own code: the presenter's ``slide_unavailable`` means the
                # slide they moved to was refused, followers staying behind.
                await _safe_send(presenter.send, {"type": "notice", "code": "slide_unpublished"})
            await _fan_out(follower_sends, {"type": "notice", "code": notice})
        if event is not None:
            await self._deliver_discovery(event)

    # ------------------------------------------------------------------- GC
    async def gc_step(self, *, now: Optional[float] = None) -> List[str]:
        """Drive presenter-absence state for every room.

        Runs on the GC tick (and is called directly in tests with an explicit
        ``now``). For every room it first applies the absolute age cap
        (MAX_SESSION_AGE_SECONDS) — reaping it regardless of presenter state so
        a zombie/forgotten presenter connection can't keep it alive forever.
        Then, for a room whose presenter is *connected*, it drives the
        inactivity timeout: warn once at PRESENTER_IDLE_WARN_SECONDS before the
        deadline and force-end at PRESENTER_IDLE_SECONDS of silence. For a
        presenter-*less* room it either:

        * tears the room down once the grace deadline has passed (returning the
          reaped session ids), or
        * fires the debounced "presenter left" follower notice once the
          presenter has stayed gone past PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS
          (and the notice hasn't already been sent).

        Folding both into the single long-lived GC loop — rather than per-detach
        timer tasks — keeps the timing robust (one task that always runs under
        the app lifespan) and the logic deterministically testable.
        """
        now = now if now is not None else time.monotonic()
        # Every clock read below is monotonic (``now``): the age cap keys off
        # ``created_monotonic`` and the idle timeout off the monotonic
        # ``presenter_last_activity``. Using the wall clock here would let a
        # suspend/resume or NTP step jump the elapsed time and force-end an
        # actively-driven session (forward step) or postpone a legitimate idle
        # teardown (backward step). ``created_at`` remains wall-clock but is
        # used only for the admin snapshot display, never for GC arithmetic.
        # (room, reason) so the age-cap, idle and grace-expiry teardowns can
        # carry distinct ``end`` reasons to their clients.
        to_teardown: List[Tuple[Room, str]] = []
        to_notify: List[Room] = []
        # (room, seconds_left) rooms whose presenter just crossed the idle
        # warning threshold; warned outside the lock.
        to_warn: List[Tuple[Room, float]] = []
        # (room, seconds_left) rooms entering the pre-max-age warning window.
        to_warn_age: List[Tuple[Room, float]] = []
        async with self._global_lock:
            for sid, room in list(self._rooms.items()):
                # Absolute age cap first, and regardless of presenter state: a
                # room with a still-attached (or endlessly reconnecting) zombie
                # presenter never reaches the grace path below, so without this
                # it could outlive any realistic session. Bounds the orphan an
                # operator can no longer reach.
                if (
                    MAX_SESSION_AGE_SECONDS > 0
                    and now - room.created_monotonic >= MAX_SESSION_AGE_SECONDS
                ):
                    self._rooms.pop(sid, None)
                    to_teardown.append((room, "max_age"))
                    continue
                if room.presenter is not None:
                    age = now - room.created_monotonic
                    if (
                        MAX_SESSION_AGE_SECONDS > 0
                        and MAX_SESSION_AGE_WARN_SECONDS > 0
                        and not room.max_age_warned
                        and age >= MAX_SESSION_AGE_SECONDS - min(
                            MAX_SESSION_AGE_WARN_SECONDS, MAX_SESSION_AGE_SECONDS / 2
                        )
                    ):
                        room.max_age_warned = True
                        to_warn_age.append(
                            (room, max(0.0, MAX_SESSION_AGE_SECONDS - age))
                        )
                    # Connected-presenter inactivity timeout. Silence is
                    # monotonic-measured (``presenter_last_activity`` is a
                    # monotonic stamp); ping/pong never refreshes it, so a zombie
                    # tab counts as idle.
                    if (
                        PRESENTER_IDLE_SECONDS > 0
                        and room.presenter_last_activity is not None
                    ):
                        idle_for = now - room.presenter_last_activity
                        if idle_for >= PRESENTER_IDLE_SECONDS:
                            self._rooms.pop(sid, None)
                            to_teardown.append((room, "presenter_idle"))
                            continue
                        if (
                            PRESENTER_IDLE_WARN_SECONDS > 0
                            and not room.presenter_idle_warned
                            and idle_for >= PRESENTER_IDLE_SECONDS - min(
                                PRESENTER_IDLE_WARN_SECONDS, PRESENTER_IDLE_SECONDS / 2
                            )
                        ):
                            room.presenter_idle_warned = True
                            to_warn.append(
                                (room, max(0.0, PRESENTER_IDLE_SECONDS - idle_for))
                            )
                    # A connected presenter never enters the absence/grace path.
                    continue
                if room.presenter_grace_until is None:
                    continue
                if now >= room.presenter_grace_until:
                    self._rooms.pop(sid, None)
                    to_teardown.append((room, "presenter_gone"))
                elif (
                    not room.presenter_absent_notified
                    and room.presenter_left_at is not None
                    and now >= room.presenter_left_at + PRESENTER_ABSENT_NOTIFY_DELAY_SECONDS
                ):
                    to_notify.append(room)
        # The reaped rooms are already out of _rooms, so their teardowns start
        # first and finish whatever happens below — a step that raised, or a
        # cancelled tick, would otherwise strand the rest unannounced (see
        # _teardown_rooms) — and run alongside each other.
        teardown = self._spawn(self._teardown_rooms(to_teardown))
        for room, seconds_left in to_warn:
            await self._warn_presenter_idle(room, seconds=seconds_left)
        for room, seconds_left in to_warn_age:
            await self._warn_presenter_max_age(room, seconds=seconds_left)
        for room in to_notify:
            await self._notify_absent(room, now=now)
        await asyncio.shield(teardown)
        return [room.session_id for room, _ in to_teardown]

    async def _warn_presenter_idle(self, room: Room, *, seconds: float) -> None:
        """Send the one-shot inactivity warning to the presenter.

        Re-checks under the room lock that a presenter is still attached and the
        warning flag is still set (presenter activity between the GC tick and
        here clears it), so a presenter who interacted in that window is not
        warned spuriously. Routed straight to the presenter — followers are not
        told; only the presenter can keep the session alive by interacting.
        """
        async with room.lock:
            if self._rooms.get(room.session_id) is not room:
                return
            presenter = room.presenter
            if presenter is None or not room.presenter_idle_warned:
                return
        await _safe_send(
            presenter.send,
            {"type": "idle_warning", "seconds": int(round(seconds))},
        )

    async def _warn_presenter_max_age(self, room: Room, *, seconds: float) -> None:
        """Send the one-shot "session reaches its maximum age soon" frame to the
        presenter (followers are not told; only the presenter can act on it)."""
        async with room.lock:
            if self._rooms.get(room.session_id) is not room:
                return
            presenter = room.presenter
            if presenter is None:
                # Left between the GC tick and here: nobody was warned, so let
                # the next attach (or tick) send it.
                room.max_age_warned = False
                return
        await _safe_send(
            presenter.send,
            {"type": "max_age_warning", "seconds": int(round(seconds))},
        )

    async def start_gc(self) -> None:
        if self._gc_task is None or self._gc_task.done():
            self._gc_task = asyncio.create_task(self._gc_loop())

    async def stop_gc(self) -> None:
        if self._gc_task and not self._gc_task.done():
            self._gc_task.cancel()
            try:
                await self._gc_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _gc_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(GC_INTERVAL_SECONDS)
                try:
                    await self.gc_step()
                except Exception:
                    _log.exception("sync gc step failed")
        except asyncio.CancelledError:
            return


# Module-level singleton used by the FastAPI WS handler. Tests should
# instantiate their own RoomManager.
manager = RoomManager()
