"""Helpers for the in-app access-analytics beacon.

Lives next to `network.py` and `app_auth.py` so the router stays small.
Three concerns:

* **Identity probing.** The beacon endpoint is publicly reachable; an
  admin User, a Cloudflare Access email, or an app-access session
  subject is attached *opportunistically* when one is available, but a
  missing/invalid token must never fail the request — analytics has to
  keep working for unauthenticated viewers.
* **User-Agent classification.** Coarse buckets so the admin dashboard
  can show a "device types" breakdown without ever displaying the raw
  UA string in aggregate views.
* **Referer host extraction.** The referrer URL the frontend forwards in
  the beacon payload (taken from `document.referrer`) is reduced to its
  host portion only, to avoid retaining query strings or path fragments
  that could carry tokens.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

import jwt
from sqlalchemy import and_, bindparam, delete, func, or_, select, update
from sqlalchemy.orm import Session

from . import analytics_hash as _hash
from . import app_auth as _app_auth
from . import auth as _admin_auth
from . import cf_access as _cf_access
from . import retention
from .config import settings
from .models import (
    NetworkSettings,
    PageView,
    SlideDwellEvent,
    TourSession,
    User,
)

_logger = logging.getLogger("wsi_app.analytics")


# Recording modes. Mirrors NetworkSettings.analytics_mode.
ANALYTICS_OFF = "off"
ANALYTICS_HASHED = "hashed"
ANALYTICS_PLAIN = "plain"
VALID_ANALYTICS_MODES = (ANALYTICS_OFF, ANALYTICS_HASHED, ANALYTICS_PLAIN)


def normalize_analytics_mode(raw: Optional[str]) -> str:
    """A stored ``analytics_mode`` value as one of ``VALID_ANALYTICS_MODES``;
    anything unset or unrecognised reads as "off" (record nothing)."""
    mode = (raw or ANALYTICS_OFF).strip().lower()
    return mode if mode in VALID_ANALYTICS_MODES else ANALYTICS_OFF


def current_analytics_mode(db: Session) -> str:
    """Return the persisted analytics mode, defaulting to "off" if unset.

    The settings row is created lazily by the admin GET endpoint, so a
    fresh install before anyone touched the network settings page will
    have no row here. Treating "no row" as "off" matches the column
    default and keeps the beacon side-effect-free until an admin opts in.
    """
    ns = db.get(NetworkSettings, 1)
    return normalize_analytics_mode(ns.analytics_mode if ns is not None else None)


class _WarningLimiter:
    """Lets a recurring WARNING through at most once per ``interval`` seconds
    per process (``interval=None``: once per process).

    The beacons run on every page view, and WARNING records are persisted to
    ``error_logs``; a condition that holds for every beacon (a misconfigured
    salt, a CF Access application without ``CF_ACCESS_AUD``) would otherwise
    write one row per view and push the real errors out of the capped table.
    """

    def __init__(self, interval: Optional[float]) -> None:
        self.interval = interval
        self._last: Optional[float] = None
        self._suppressed = 0
        self._lock = threading.Lock()

    def allow(self) -> Optional[int]:
        """None to stay quiet, else how many occurrences were held back since
        the last one let through."""
        with self._lock:
            now = time.monotonic()
            if self._last is not None and (
                self.interval is None or now - self._last < self.interval
            ):
                self._suppressed += 1
                return None
            self._last = now
            held_back, self._suppressed = self._suppressed, 0
            return held_back

    def reset(self) -> None:
        """Forget earlier occurrences (tests)."""
        with self._lock:
            self._last = None
            self._suppressed = 0


# A failing CF assertion on the beacon is either a misconfiguration (every
# beacon fails) or someone probing; one line per ten minutes shows either.
_cf_invalid_warning = _WarningLimiter(600)
# The hashing salt is fixed for the life of the process, so once is enough.
_hash_failed_warning = _WarningLimiter(None)


def maybe_hash_identifier(raw: str, mode: str) -> str:
    """Apply the current mode's transformation to a raw user identifier.

    - mode="off"    -> not expected here; resolve_identity short-circuits
                       before calling. If passed anyway it falls through to the
                       "plain" branch, so callers must gate on mode themselves.
    - mode="hashed" -> HMAC hex digest, or "" for empty input
    - mode="plain"  -> raw input, truncated to the column width
    """
    if not raw:
        return ""
    if mode == ANALYTICS_HASHED:
        return _hash.compute_user_hash(raw)
    return raw[:255]


def pseudonymise_client_ip(ip: str) -> str:
    """Keyed hash of a client IP for ``mode="hashed"``; "" if it can't be hashed.

    A raw IP next to a hashed identity would re-identify the row, so hashed
    mode stores this instead (same salt as the identity, domain-separated by
    the "ip:" prefix). Distinct IPs still map to distinct values, so the
    dashboard's unique-IP counts keep working. The 64-char digest fits
    ``PageView.client_ip`` exactly.
    """
    if not ip:
        return ""
    try:
        return _hash.compute_user_hash(f"ip:{ip}")
    except RuntimeError:
        # Salt removed out-of-band while mode is still "hashed" — store
        # nothing rather than the raw address (see resolve_identity).
        return ""


# ---------------------------------------------------------------------------
# Identity probing
# ---------------------------------------------------------------------------
def probe_identity(
    session_cookie: Optional[str],
    headers,
    client_host: str,
    db: Session,
    *,
    staff_user: Optional["User"] = None,
    verified_basic_user: Optional[str] = None,
) -> Tuple[Optional[int], str]:
    """Best-effort identity attached to a beacon request.

    Returns ``(user_id, user_email)``. Never raises; on any decode/lookup
    failure both default to ``(None, "")`` so the beacon write proceeds
    with the row marked anonymous.

    ``staff_user`` is path 1's identity: the caller resolves the forwarded
    staff bearer to a live ``User`` row once (``auth.resolve_staff_user``,
    which also enforces ``is_active`` and the ``token_version`` revocation
    check) and the same row stamps ``actor_role``. The bearer is not decoded
    again here, so a revoked or expired token never attributes a row — not
    even to whoever holds that user id now.

    Source precedence (first hit wins):

    1. **Staff JWT bearer.** The request carries a staff Bearer (admin /
       manager / instructor) in ``Authorization`` that the caller resolved
       to ``staff_user``. The page-view fetch in
       ``frontend/src/lib/api/analytics.ts`` attaches it whenever a token is
       in sessionStorage, so a logged-in staff member browsing the SPA lands
       here — which is also what lets the caller stamp ``actor_role`` on the
       row (see ``_staff_actor_role``). The dwell / tour-unload beacons do
       the same: they normally use ``navigator.sendBeacon``, which cannot
       carry an ``Authorization`` header, but ``postBeacon`` deliberately
       switches to a ``keepalive`` fetch when a token is present so those
       rows get stamped too. Learner / anonymous traffic carries no token
       (and keeps the sturdier sendBeacon delivery), falling through to
       paths 2-4.
    2. **Cloudflare Access JWT** (only when the request arrived through
       a trusted hop and the JWT verified). Routes the verified email
       into ``user_email`` for Path A (external user via CF Tunnel).
       An *invalid* CF JWT is logged at WARNING and treated as no
       identity — the row records anonymous and the access-control
       middleware separately rejects the request with 401 for non-
       beacon paths.
    3. **App-access session cookie.** Used by Path B (internal user
       via CF bypass) where the user authenticated against the app's
       own basic/ldap/google_oauth flow. The session payload's `sub`
       is the username (basic/ldap) or the verified Google email —
       either way it's the right thing to surface in the admin's
       "who viewed this" column. ``user_id`` stays NULL because these
       subjects don't sit in the `users` table.
    4. **HTTP Basic credentials the middleware verified on this request**
       (``verified_basic_user``, from ``request.state``). A client that
       does not keep the ``wsi_session`` cookie the middleware mints on a
       successful Basic check re-sends ``Authorization: Basic …`` instead;
       without this branch those users would record as anonymous. Only
       the verified username is used, never the raw header: requests
       admitted by CF Access or an exemption carry an unchecked header.
       The subject is prefixed with ``basic:`` to match the session
       cookie payload, so the user appears under one identifier either way.

    This function resolves *identity* only. Staff traffic is separately
    tagged by the caller, which stamps ``PageView.actor_role`` (admin /
    manager / instructor) whenever path 1 hit; the default dashboard
    aggregations filter on ``actor_role IS NULL`` so staff QA traversals
    stay out of the learner metrics. In ``mode="none"`` deployments with no
    staff token on the beacon there is no authentication signal at all, so
    such traversals cannot be distinguished from anonymous viewers —
    operators who need that distinction must run with CF Access, basic,
    ldap, or google_oauth.
    """
    # 1) Staff JWT bearer, as resolved by the caller (single decode + fetch,
    # shared with actor_role stamping). resolve_staff_user already guaranteed
    # the row exists, is active and the token is not revoked.
    if staff_user is not None:
        return staff_user.id, staff_user.username or ""

    # 2) Cloudflare Access JWT (verified).
    cf_status, cf_claims = _cf_access.cf_jwt_status(headers, client_host)
    if cf_status == "valid" and cf_claims is not None:
        email = _cf_access.email_from_claims(cf_claims)
        if email:
            return None, email
    if cf_status == "invalid":
        # Beacon is best-effort: don't fail the write, but flag the
        # anomaly. The middleware would normally 401 a non-beacon path
        # before it gets here; reaching this branch for the beacon
        # specifically means either (a) the operator deployed CF Access
        # without configuring CF_ACCESS_AUD, or (b) someone is probing
        # with a forged assertion. Stop here — do NOT fall through to
        # the session-cookie identity below: the request has already
        # been classified untrustworthy at the CF gate, so attributing
        # the row to a session subject would contradict the middleware's
        # 401 decision on every non-beacon path of the same request.
        held_back = _cf_invalid_warning.allow()
        if held_back is not None:
            _logger.warning(
                "CF Access JWT verification failed on beacon request from %s; "
                "recording anonymous (%d more since the last report)",
                client_host, held_back,
            )
        return None, ""

    # 3) App-access session cookie.
    if session_cookie:
        payload = _app_auth.decode_session_token(session_cookie)
        if payload:
            sub = payload.get("sub") or ""
            if isinstance(sub, str) and sub:
                return None, sub[:255]

    # 4) HTTP Basic credentials — only the username the middleware actually
    #    verified on THIS request (``request.state.verified_basic_user``,
    #    threaded in by the caller). The Authorization header itself is not
    #    evidence: requests admitted by a valid CF JWT, the break-glass or the
    #    LAN-direct exemption skip the password check entirely, so a header
    #    there is attacker-controlled and would let anyone forge dwell /
    #    completion rows in any learner's name.
    if verified_basic_user:
        return None, f"basic:{verified_basic_user}"[:255]

    return None, ""


def signed_staff_role(authorization_header: str) -> Optional[str]:
    """Staff role from a forwarded bearer whose SIGNATURE verifies, even if the
    token has expired or been revoked.

    Used only to tag analytics rows (``actor_role``), never to authorise
    anything. The SPA forwards whatever staff token sits in sessionStorage; once
    it expires (24 h) or is revoked, ``resolve_staff_user`` rejects it and the
    beacon would fall through to a NULL actor_role — counting the staff
    member's QA traversal as a learner. The ``role`` claim was signed at mint
    time, so it still proves this is a staff browser.
    """
    token = _admin_auth.bearer_token(authorization_header or "")
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            _admin_auth.JWT_SECRET,
            algorithms=[_admin_auth.JWT_ALG],
            options={"verify_aud": False, "verify_exp": False, "require": ["exp"]},
        )
    except jwt.PyJWTError:
        return None
    # aud-bearing tokens are app-access session cookies, never staff tokens.
    if "aud" in payload:
        return None
    role = payload.get("role")
    return role if role in _admin_auth.STAFF_ROLES else None


def resolve_identity(
    session_cookie: Optional[str],
    headers,
    client_host: str,
    db: Session,
    *,
    staff_user: Optional["User"] = None,
    verified_basic_user: Optional[str] = None,
    mode: Optional[str] = None,
) -> Tuple[Optional[int], str, str]:
    """Run ``probe_identity`` and apply the active analytics mode.

    ``staff_user`` is forwarded to ``probe_identity`` as the
    pre-resolved-bearer hint (see there) so the beacon paths resolve the
    forwarded staff JWT to a ``User`` row exactly once.
    ``verified_basic_user`` is forwarded as its path-4 identity.
    ``mode`` is the analytics mode when the caller already read it (the
    beacons do, first, so that mode "off" skips all identity work); it is
    read here otherwise.

    Returns ``(user_id, user_identifier, mode)`` where ``user_identifier``
    is already hashed (for ``mode="hashed"``) or truncated plaintext (for
    ``mode="plain"``). When mode is ``"off"`` nothing is probed and the
    identifier is "".
    """
    if mode is None:
        mode = current_analytics_mode(db)
    if mode == ANALYTICS_OFF:
        return None, "", mode
    user_id, raw_identifier = probe_identity(
        session_cookie, headers, client_host, db,
        staff_user=staff_user,
        verified_basic_user=verified_basic_user,
    )
    try:
        identifier = maybe_hash_identifier(raw_identifier, mode)
    except RuntimeError as exc:
        # The admin PATCH endpoint refuses to switch to "hashed" without a
        # salt configured, so reaching here means an operator removed the
        # salt out-of-band (env edited then process restarted) while the
        # persisted mode is still "hashed". Fail safe: record the row
        # anonymously, and warn once per process so the misconfiguration
        # surfaces in logs without a per-beacon 500 or error-log row.
        if _hash_failed_warning.allow() is not None:
            _logger.warning(
                "analytics mode=%s but identifier hashing failed: %s; "
                "recording anonymous", mode, exc,
            )
        identifier = ""
    return user_id, identifier, mode


# ---------------------------------------------------------------------------
# User-Agent classification
# ---------------------------------------------------------------------------
# "bot" is matched as a substring (Googlebot, bingbot, Applebot, ...), so the
# Android handset maker Cubot ("CUBOT_X19", "Cubot P40") is excluded explicitly
# rather than having its real visitors' dwell and tour events dropped.
_BOT_RE = re.compile(
    r"((?<!cu)bot|crawler|spider|crawling|preview|monitor|fetch|wget|curl|python-requests|httpclient)",
    re.IGNORECASE,
)
_TABLET_RE = re.compile(r"(ipad|tablet|playbook|silk|kindle)", re.IGNORECASE)
_MOBILE_RE = re.compile(
    r"(iphone|ipod|android.*mobile|windows phone|blackberry|opera mini|mobi)",
    re.IGNORECASE,
)
_DESKTOP_RE = re.compile(r"(windows nt|macintosh|x11|linux|cros)", re.IGNORECASE)


def classify_user_agent(ua: str) -> str:
    """Return one of: 'bot' | 'mobile' | 'tablet' | 'desktop' | 'other'.

    Order matters: bot detection beats device detection (a bot UA that
    contains "Android Mobile" should still classify as bot), and tablet
    beats mobile because most tablet UAs also include the "Mobile" token.
    """
    if not ua:
        return "other"
    if _BOT_RE.search(ua):
        return "bot"
    if _TABLET_RE.search(ua):
        return "tablet"
    if _MOBILE_RE.search(ua):
        return "mobile"
    if _DESKTOP_RE.search(ua):
        return "desktop"
    return "other"


# ---------------------------------------------------------------------------
# Referer extraction
# ---------------------------------------------------------------------------
def extract_referrer_host(referer_header: str, current_host: str) -> str:
    """Return the referrer host, or "" for direct / self-referrals.

    Self-referrals are dropped because every same-site click otherwise
    floods the "top referrers" table with the app's own host name.
    ``current_host`` is whatever the request's Host header carried
    (port stripped).
    """
    if not referer_header:
        return ""
    try:
        parsed = urlparse(referer_header)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if current_host and host == current_host.lower():
        return ""
    return host[:255]


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------
# The analytics event tables are the highest-frequency writes in the app, so
# they need the same row/age caps that ``audit_logs`` / ``error_logs`` already
# enforce (see ``audit_log.prune_audit_logs`` / ``error_log.prune_error_logs``);
# without them the tables grow unbounded and both disk usage and every
# dashboard aggregate scan degrade forever. This block mirrors that pattern,
# fanning the same two-dimensional cap across all three tables.
_prune_throttle = retention.PruneThrottle()

# (model, event-time column) for each pruned table. page_views ages on
# ``occurred_at``; the dwell / tour tables have no ``occurred_at`` and age on
# their indexed ``started_at`` event-time column instead.
_PRUNE_TABLES = (
    (PageView, PageView.occurred_at),
    (SlideDwellEvent, SlideDwellEvent.started_at),
    (TourSession, TourSession.started_at),
)


# Rows deleted per statement. SQLite has one writer at a time, so a single
# DELETE over a multi-million-row table holds the write lock for its whole
# duration and every beacon / admin write queues behind it (or times out).
# Deleting in bounded chunks and committing between them lets other writers
# interleave. A module constant so tests can shrink it.
_DELETE_CHUNK_ROWS = 10_000


def delete_until(db: Session, model, *where, deadline: Optional[float] = None) -> Tuple[int, bool]:
    """``DELETE FROM model WHERE <where>`` in ``_DELETE_CHUNK_ROWS`` slices,
    committing after each so the SQLite write lock is released in between.

    With a ``deadline`` (a ``time.monotonic()`` value) no new slice starts
    once it has passed. Returns ``(rows deleted, finished)``; ``finished`` is
    False only when the deadline stopped it with matching rows possibly left.
    NOT atomic: a failure part-way leaves the earlier slices deleted (every
    caller is idempotent and can be re-run).
    """
    total = 0
    # Keyset walk: each slice starts after the last id the previous one
    # covered. Selecting "the first N matching ids" afresh every time made
    # SQLite re-walk the primary key from id 1 on each slice, past every
    # non-matching row (all the older history for a from_date-only clear, the
    # whole table for a user purge) — quadratic in the table size. With the
    # `id > last` bound both statements are rowid range searches.
    after: Tuple = ()
    while True:
        # The id of the slice's last row: the N-th match after the previous
        # slice. None means fewer than N matches remain, so this is the last
        # slice and it needs no upper bound.
        hi = db.execute(
            select(model.id).where(*where, *after).order_by(model.id)
            .offset(_DELETE_CHUNK_ROWS - 1).limit(1)
        ).scalar_one_or_none()
        upper = () if hi is None else (model.id <= hi,)
        n = db.execute(delete(model).where(*where, *after, *upper)).rowcount or 0
        db.commit()
        total += n
        if hi is None:
            return total, True
        after = (model.id > hi,)
        if deadline is not None and time.monotonic() >= deadline:
            return total, False


def delete_all(db: Session, model) -> int:
    """Empty ``model``'s table with one unqualified ``DELETE`` and commit.

    Unlike a chunked or filtered delete this takes SQLite's truncate
    optimisation: the table and its indexes are dropped page-wise instead of
    one index entry per row — seconds for millions of rows rather than
    minutes. It holds the write lock for that time, which is acceptable for
    an explicit admin wipe. (The optimisation is lost if a DELETE trigger is
    ever added to the table.)
    """
    n = db.execute(delete(model)).rowcount or 0
    db.commit()
    return n


def prune_analytics(
    db: Session,
    max_rows: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> int:
    """Apply the row/age caps to the analytics event tables. Returns rows deleted.

    Prunes ``page_views``, ``slide_dwell_events`` and ``tour_sessions`` by
    both dimensions, exactly mirroring ``audit_log.prune_audit_logs``:

    * **age-cap** — drop rows whose event timestamp is older than
      ``max_age_days`` days. The cutoff is ``datetime.now(timezone.utc) -
      timedelta(days=…)``; the columns store naive-UTC strings (SQLite's
      ``CURRENT_TIMESTAMP`` semantics), and formatting the UTC-aware cutoff
      drops the tzinfo to the same naive-UTC representation, so the two sides
      compare apples-to-apples.
    * **row-cap** — keep only the newest ``max_rows`` rows *per table* (id
      order matches insertion order).

    Each table is capped independently. Deletes run in chunks and commit as
    they go (see :func:`delete_until`). Parameters default to
    the configured caps; tests pass explicit values. A cap of 0 disables that
    dimension.
    """
    return prune_analytics_until(db, max_rows, max_age_days)[0]


def prune_analytics_until(
    db: Session,
    max_rows: Optional[int] = None,
    max_age_days: Optional[int] = None,
    *,
    deadline: Optional[float] = None,
) -> Tuple[int, bool]:
    """:func:`prune_analytics` that starts no new slice once ``deadline`` (a
    ``time.monotonic()`` value) has passed. Returns ``(rows deleted,
    finished)``; ``finished`` is False when the deadline cut it short."""
    if max_rows is None:
        max_rows = settings.analytics_log_max_rows
    if max_age_days is None:
        max_age_days = settings.analytics_log_max_age_days
    deleted = 0
    if max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        for model, ts_col in _PRUNE_TABLES:
            n, finished = delete_until(db, model, ts_col < cutoff, deadline=deadline)
            deleted += n
            if not finished:
                return deleted, False
    if max_rows > 0:
        for model, _ts_col in _PRUNE_TABLES:
            # Newest max_rows survive; id order matches insertion order.
            threshold = db.execute(
                select(model.id).order_by(model.id.desc()).offset(max_rows).limit(1)
            ).scalar_one_or_none()
            if threshold is not None:
                n, finished = delete_until(db, model, model.id <= threshold, deadline=deadline)
                deleted += n
                if not finished:
                    return deleted, False
    return deleted, True


def prune_at_boot() -> None:
    """Boot-time prune; best-effort (called from the lifespan janitor block)."""
    retention.prune_at_boot(prune_analytics, _logger, "analytics tables")


# How long one opportunistic prune may keep deleting. It runs as a background
# task after a beacon's response has gone out, on a threadpool thread, but it
# still holds SQLite's write lock slice by slice; a backlog (a lowered cap, a
# year of rows ageing out at once) is worked off over several rounds instead
# of in one sitting. Module constants so tests can shrink them.
_OPPORTUNISTIC_PRUNE_BUDGET_S = 10.0
# Delay before the next round when a prune ran out of budget with rows left,
# instead of the full hourly throttle interval.
_OPPORTUNISTIC_PRUNE_RETRY_S = 60.0
# Held for the whole of an opportunistic prune in this worker. The throttle
# alone stamps its interval at claim time, so a prune outliving the interval
# (or rearmed early above) could otherwise overlap the next one; a second
# caller that finds this taken skips its round rather than queueing.
_prune_running = threading.Lock()


def maybe_prune_analytics() -> None:
    """Opportunistic prune, throttled per worker (see ``retention``).

    Run from the beacon endpoints as a background task (after the response),
    so a long-running deployment that never restarts still enforces the caps
    without the beacon ever waiting on it. Bounded by
    ``_OPPORTUNISTIC_PRUNE_BUDGET_S``; at most one runs per worker at a time.
    Uses its own short-lived session (never the request session) and never
    raises into its caller.
    """
    if not _prune_running.acquire(blocking=False):
        return
    try:
        # Claimed under the running lock so a skipped round never spends the
        # throttle's interval.
        if not _prune_throttle.claim():
            return
        deadline = time.monotonic() + _OPPORTUNISTIC_PRUNE_BUDGET_S
        finished = [True]

        def _prune(db: Session) -> int:
            n, finished[0] = prune_analytics_until(db, deadline=deadline)
            return n

        try:
            retention.run_prune(_prune)
        except Exception:
            _logger.warning("opportunistic analytics prune failed", exc_info=True)
            return
        if not finished[0]:
            _prune_throttle.rearm(_OPPORTUNISTIC_PRUNE_RETRY_S)
    finally:
        _prune_running.release()


# ---------------------------------------------------------------------------
# No server-side page-view dedupe
# ---------------------------------------------------------------------------
# The SPA skips a repeat view of the same page within 5 s itself. The server
# deliberately does not: what it can see of a viewer (address, identity, role)
# is shared by everyone behind one NAT / proxy and by a class signed in on one
# Basic account, so 30 students opening the same slide at the instructor's
# word would collapse into one view. A per-browser key would have to come from
# the client, which a script inflating the counts simply rotates. Volume per
# client is bounded by nginx's per-client request rate instead.
#
# Nor is there a per-identity limit on new dwell / tour sessions, for the same
# reason: one identity is often many people, and every count low enough to
# matter against a forger would silently drop their rows. What a forging
# client can add is bounded per session instead (the dwell beacon caps a new
# session's first dwell and lets it grow only with the server's clock; a tour
# starts at 0 and is receipt-anchored at both ends — see routers/analytics.py).


# ---------------------------------------------------------------------------
# Plain -> hashed conversion of recorded rows
# ---------------------------------------------------------------------------
def _sql_not_digest(col):
    """SQL: ``col`` holds a value that is NOT a ``compute_user_hash`` digest
    (64 lowercase hex chars) and is not empty. GLOB is case-sensitive, so an
    upper-case hex string counts as plaintext, as ``DIGEST_RE`` would."""
    return and_(
        col != "",
        or_(func.length(col) != 64, col.op("GLOB")("*[^0-9a-f]*")),
    )


# compute_user_hash output: 64 lowercase hex chars. A stored value of this
# shape is taken as already pseudonymised and never hashed again — which also
# covers a plaintext identifier that happens to look like one (a 64-hex-char
# username), the safe direction to be wrong in.
DIGEST_RE = re.compile(r"^[0-9a-f]{64}\Z")

# Rows rewritten per committed slice by :func:`pseudonymize_until` (module
# constant so tests can shrink it).
_PSEUDONYMIZE_CHUNK_ROWS = 2_000


def _hashed_form(value: str, *, ip: bool = False) -> str:
    """What hashed mode would have stored for a plain-mode ``value``."""
    if not value or DIGEST_RE.match(value):
        return value
    return pseudonymise_client_ip(value) if ip else maybe_hash_identifier(value, ANALYTICS_HASHED)


def pseudonymize_until(db: Session, *, deadline: Optional[float] = None) -> Tuple[dict, bool]:
    """Rewrite rows recorded in mode "plain" into exactly what mode "hashed"
    would have stored for them, under the current salt.

    The two modes' rows differ in these columns (see the beacon writers):

    * ``page_views``: ``user_email`` (raw identifier vs ``compute_user_hash``),
      ``client_ip`` (raw vs ``pseudonymise_client_ip``), ``user_agent`` (raw
      vs "") and ``user_id`` (the staff users.id vs NULL);
    * ``slide_dwell_events`` / ``tour_sessions``: ``user_identifier`` only
      (the dwell / tour writers store nothing else that identifies anyone).

    Plain mode stores the identifier as probed, which is never longer than the
    column (every probe path truncates to 255 first), so hashing the stored
    value gives the same digest hashed mode computes. Idempotent: a value
    that is already a digest is left alone, and a converted row no longer
    matches the selection. Walks each table by id in committed slices and
    starts none once ``deadline`` has passed. Returns ``({table: rows
    updated}, finished)``. Raises RuntimeError when no salt is available.
    """
    _hash._salt()  # fail before touching anything
    pv_plain = or_(
        _sql_not_digest(PageView.user_email),
        _sql_not_digest(PageView.client_ip),
        PageView.user_agent != "",
        PageView.user_id.is_not(None),
    )

    def _pv_values(row) -> dict:
        _id, email, ip, _ua, _uid = row
        return {
            "b_id": _id,
            "v_user_email": _hashed_form(email or ""),
            "v_client_ip": _hashed_form(ip or "", ip=True)[:64],
            "v_user_agent": "",
            "v_user_id": None,
        }

    def _ident_values(row) -> dict:
        _id, ident = row
        return {"b_id": _id, "v_user_identifier": _hashed_form(ident or "")}

    tables = (
        ("page_views", PageView,
         (PageView.id, PageView.user_email, PageView.client_ip, PageView.user_agent, PageView.user_id),
         pv_plain, _pv_values, ("user_email", "client_ip", "user_agent", "user_id")),
        ("slide_dwell_events", SlideDwellEvent,
         (SlideDwellEvent.id, SlideDwellEvent.user_identifier),
         _sql_not_digest(SlideDwellEvent.user_identifier), _ident_values, ("user_identifier",)),
        ("tour_sessions", TourSession,
         (TourSession.id, TourSession.user_identifier),
         _sql_not_digest(TourSession.user_identifier), _ident_values, ("user_identifier",)),
    )
    updated = {name: 0 for name, *_ in tables}
    for name, model, cols, plain, values, targets in tables:
        stmt = (
            update(model.__table__)
            .where(model.__table__.c.id == bindparam("b_id"))
            # Bind names must differ from the column names in an UPDATE.
            .values({c: bindparam(f"v_{c}") for c in targets})
        )
        last_id = 0
        while True:
            rows = db.execute(
                select(*cols).where(plain, model.id > last_id)
                .order_by(model.id).limit(_PSEUDONYMIZE_CHUNK_ROWS)
            ).all()
            if rows:
                db.execute(stmt, [values(r) for r in rows])
                db.commit()
                updated[name] += len(rows)
                last_id = rows[-1][0]
            if len(rows) < _PSEUDONYMIZE_CHUNK_ROWS:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return updated, False
    return updated, True
