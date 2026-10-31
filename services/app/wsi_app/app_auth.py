"""App-access authentication: Basic / LDAP / Google OAuth.

Distinct from `auth.py`, which handles the staff (admin / manager /
instructor) JWT bearer tokens, the role checks and the shared JWT_SECRET.
This module gates *every* request to the app once the admin has selected a
mode in the dashboard. mode == "none" is the default and disables the gate
entirely, leaving the app a fully public viewer.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

import httpx
import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import auth as _admin_auth
from . import config as _config
from . import login_throttle as _login_throttle
from .config import settings as _settings
from .db import SessionLocal
from .models import AuthSettings, AuthUser

_logger = logging.getLogger("wsi_app.app_auth")

# ---------------------------------------------------------------------------
# In-memory snapshot of AuthSettings (refreshed at startup and after PATCH)
#
# Same atomic-swap design as wsi_app.network: build a fresh frozen Snapshot
# in `refresh()` and assign it to the module global in one store, so request
# paths that read the snapshot mid-update can never observe a partially
# rewritten configuration. A key-by-key in-place mutation would let a
# request briefly see, e.g., an updated `mode` alongside an out-of-date
# `google` block — fine for SQLite-scale traffic but a real correctness
# bug under load.
# ---------------------------------------------------------------------------
_EMPTY_MAP: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True)
class _AuthSnapshot:
    mode: str = "none"
    session_ttl_seconds: int = 86400
    # See AuthSettings.session_generation: bumped on auth-policy tightening to
    # invalidate all already-issued app-access cookies at once.
    session_generation: int = 0
    ldap: Mapping[str, object] = field(default_factory=lambda: _EMPTY_MAP)
    google: Mapping[str, object] = field(default_factory=lambda: _EMPTY_MAP)
    login_message: str = ""
    login_button_label: str = ""
    # "off" | "ip" | "lan" — see AuthSettings.lan_direct_noauth_mode and
    # lan_direct_exempt below.
    lan_direct_noauth_mode: str = "off"


_snapshot: _AuthSnapshot = _AuthSnapshot()


def refresh(db: Session) -> None:
    global _snapshot
    s = db.get(AuthSettings, 1)
    if not s:
        _snapshot = _AuthSnapshot()
        return
    ldap = MappingProxyType({
        "server_uri": s.ldap_server_uri or "",
        "bind_dn": s.ldap_bind_dn or "",
        "bind_password": s.ldap_bind_password or "",
        "user_base_dn": s.ldap_user_base_dn or "",
        "user_filter": s.ldap_user_filter or "(uid={username})",
        "use_starttls": bool(s.ldap_use_starttls),
    })
    google = MappingProxyType({
        "client_id": s.google_client_id or "",
        "client_secret": s.google_client_secret or "",
        "allowed_domains": tuple(
            d.strip().lower()
            for d in (s.google_allowed_domains or "").split(",")
            if d.strip()
        ),
        "allowed_emails": frozenset(
            e.strip().lower()
            for e in (s.google_allowed_emails or "").split(",")
            if e.strip()
        ),
    })
    # Clamp to the same [60s, 30d] band the PATCH endpoint enforces on write:
    # a row imported via restore or edited out-of-band could otherwise carry an
    # out-of-range TTL that mints near-immortal (or instantly-expiring) sessions.
    _ttl = int(s.session_ttl_seconds or 86400)
    _ttl = max(60, min(_ttl, 30 * 24 * 3600))
    _snapshot = _AuthSnapshot(
        mode=s.mode or "none",
        session_ttl_seconds=_ttl,
        session_generation=int(s.session_generation or 0),
        ldap=ldap,
        google=google,
        login_message=s.login_message or "",
        login_button_label=s.login_button_label or "",
        lan_direct_noauth_mode=normalize_lan_direct_mode(s.lan_direct_noauth_mode),
    )


def auth_snapshot() -> _AuthSnapshot:
    """The current settings snapshot, for a login flow that must check the
    credential and stamp the resulting cookie against ONE consistent policy.

    A login that reads the policy piecemeal could verify under the old LDAP
    filter / allowed-domain list and then mint a cookie carrying the
    generation that a tightening of that very policy just bumped, promoting
    a result the new policy would refuse into a cookie the revocation check
    accepts."""
    return _snapshot


def current_mode() -> str:
    return _snapshot.mode


def session_ttl() -> int:
    return int(_snapshot.session_ttl_seconds or 86400)


def login_message() -> str:
    return _snapshot.login_message or ""


def login_button_label() -> str:
    return _snapshot.login_button_label or ""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SESSION_COOKIE = "wsi_session"
OAUTH_STATE_COOKIE = "wsi_oauth_state"
# Short-lived companion to the state cookie: the in-app path the visitor was
# trying to reach, restored after the OAuth round-trip (see safe_next_path).
OAUTH_NEXT_COOKIE = "wsi_oauth_next"
SESSION_AUDIENCE = "app-access"

# Shared parent domain for the cross-host session-carry cookie (see
# ``maybe_carry_cf_session``). Resolved once at import from settings (which
# reads ``WSI_SESSION_COOKIE_DOMAIN`` at process start); an empty value
# disables the feature. Held as a module global — rather than re-read from
# the frozen Settings on each call — so tests can monkeypatch it the same
# way they patch ``cf_access._TEAM_DOMAIN``.
SESSION_COOKIE_DOMAIN = _settings.session_cookie_domain
# Opt-in break-glass exemption (see Settings.breakglass_noauth). Held as a
# module global — like SESSION_COOKIE_DOMAIN — so tests can monkeypatch it
# without rebuilding the frozen Settings.
BREAKGLASS_NOAUTH = _settings.breakglass_noauth
# The client address ranges it admits (Settings.breakglass_trusted_networks:
# WSI_BREAKGLASS_TRUSTED_CIDRS, default the LAN ranges). A module global for
# the same reason.
BREAKGLASS_TRUSTED_NETWORKS = _settings.breakglass_trusted_networks
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Cache JWKS for an hour; Google's keys rotate roughly daily, refresh on KeyError.
_GOOGLE_JWKS_TTL = 3600
_google_jwks_cache: dict = {"fetched_at": 0.0, "client": None}

# Paths that always bypass the gate.
# /api/admin/register is included so the first-launch bootstrap path stays
# reachable after an operator restores a DB whose AuthSettings.mode is
# already "ldap" / "google_oauth" but the users table has no admin yet.
# Signing in with the admin it creates still takes the app sign-in (or the
# direct-LAN / break-glass exemption), as /api/admin/login is gated: the
# bypass only spares the operator an unrecoverable 401 on the registration
# itself. The route itself is gated by
# ALLOW_ADMIN_REGISTER plus a "no admin exists yet" check (see
# admin_auth.admin_register).
# /favicon.ico is included because the LAN-direct probe (a cross-origin
# no-cors HEAD fetch of <VITE_LAN_HOST>/favicon.ico, frontend/src/lib/lanProbe.ts
# — see docs/operations.md) uses it as the LAN reachability signal before the
# user has authenticated. A 401 here would make the probe
# falsely fail under basic-auth mode and pin same-VLAN clients to the
# Cloudflare Tunnel hairpin path.
#
# Two sets:
#   BYPASS_EXACT     -- full-path equality. Used for single endpoints so
#                       that future siblings like /favicon.ico.bak or
#                       /api/health-foo cannot accidentally inherit the
#                       unauthenticated treatment.
#   BYPASS_PREFIXES  -- genuine prefix match. Reserved for path families
#                       that legitimately span multiple endpoints; each
#                       entry MUST end with "/" so the prefix can never
#                       eat an unrelated sibling.
BYPASS_EXACT: frozenset = frozenset({
    "/api/health",
    "/api/admin/register",
    "/favicon.ico",
})
BYPASS_PREFIXES: Tuple[str, ...] = (
    "/api/auth/",
)

# The dashboard sign-in (the password step and the two-factor code step).
# Deliberately NOT bypass paths: with an app-access mode on, the dashboard is
# inside its scope like every other page, so a visitor first signs in to the
# app (or is admitted by Cloudflare Access, the break-glass or direct-LAN
# exemption) and only then can try a staff password — the staff login is no
# second, ungated door for guessing. A staff token, once issued, still
# bypasses the gate (main.app_auth_middleware), which is what lets an admin
# who saved a broken auth configuration keep the dashboard open to fix it.
ADMIN_LOGIN_PATHS: frozenset = frozenset({
    "/api/admin/login",
    "/api/admin/login/mfa",
})


def is_bypass_path(path: str) -> bool:
    if path in BYPASS_EXACT:
        return True
    return any(path.startswith(p) for p in BYPASS_PREFIXES)


# Path prefixes that, when *unauthenticated* under ldap/google_oauth mode,
# return 401. Anything else (HTML/JS/CSS for the SPA) is allowed through so
# the SPA can render its login page.
GATED_PREFIXES: Tuple[str, ...] = (
    "/api/",
    "/dzi/",
    "/thumbs/",
    "/assets/",
)


def is_gated_path(path: str) -> bool:
    return any(path.startswith(p) for p in GATED_PREFIXES)


def breakglass_exempt(headers, client_host: str) -> bool:
    """Return True when this request should skip the app-access gate as the
    break-glass raw-IP exemption.

    Opt-in via ``WSI_BREAKGLASS_NOAUTH``; a no-op (always False) otherwise, so
    stock deployments are unaffected. The exemption is keyed on three signals
    that nginx sets and a client cannot forge:

    * **Not via the Cloudflare Tunnel.** ``is_via_cloudflare_tunnel`` is true
      only when a non-empty ``Cf-Connecting-IP`` arrives on the trusted
      loopback hop (cloudflared). Any tunnel path — Path A (CF Access SSO) or
      Path B (CF Bypass) — is therefore excluded, so the public hostname never
      becomes unauthenticated.
    * **Plaintext HTTP.** nginx overwrites ``X-Forwarded-Proto`` with the
      listener scheme (``$wsi_xfp`` -> ``$scheme`` on an untrusted hop), so the
      value is ``http`` only for the raw-IP ``:80`` server block. The TLS
      LAN-direct vhost yields ``https`` and stays gated, and a forged ``Host``
      header cannot flip a TLS request onto this exemption because the scheme
      is fixed by which listener accepted the connection.
    * **The real client is in a trusted range.** As for
      :func:`lan_direct_exempt`, ``network.get_client_ip`` honours
      forwarded-IP headers only from the trusted loopback hop; the resolved
      address must fall in ``BREAKGLASS_TRUSTED_NETWORKS``
      (``WSI_BREAKGLASS_TRUSTED_CIDRS``, by default the LAN ranges — set it to
      add a campus range that hands out global addresses). nginx's plain-HTTP
      listener is a ``default_server`` for every name, so without this the
      flag would also open ``http://<public-IP>/`` to the Internet wherever
      port 80 is reachable; the break-glass route is a local fallback, and
      any other client stays gated.

    Only a genuinely plaintext, non-tunnel request from a trusted range — the
    documented raw-IP break-glass fallback — satisfies all three. Unlike the
    dashboard's ``lan_direct_noauth_mode="ip"`` it does not look at ``Host``:
    it is set in wsi.env, so it still works when nobody can sign in to the
    dashboard to change that setting. Mode-agnostic on purpose: it
    short-circuits before the basic/ldap/google_oauth switch, which is what
    makes the raw-IP path usable under google_oauth (where interactive login
    cannot complete against a private-IP/http redirect URI).
    """
    if not BREAKGLASS_NOAUTH:
        return False
    # Local import keeps module load order independent of cf_access and mirrors
    # the lazy `from . import network` used elsewhere in this package.
    from . import cf_access as _cf_access
    if _cf_access.is_via_cloudflare_tunnel(headers, client_host):
        return False
    from . import network as _network
    if _network.forwarded_proto(headers) != "http":
        return False
    if not _ip_in(_network.get_client_ip(headers, client_host), BREAKGLASS_TRUSTED_NETWORKS):
        return False
    return _forwarded_chain_within(headers, BREAKGLASS_TRUSTED_NETWORKS)


# The "on the LAN" address set the direct-IP exemption trusts (config.LAN_CIDRS,
# which explains the choice of ranges).
_LAN_NETWORKS = tuple(ipaddress.ip_network(c) for c in _config.LAN_CIDRS)


def _ip_in(value: str, networks) -> bool:
    """True if ``value`` is an IP literal inside one of ``networks``."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    # ``addr in net`` returns False (no error) when versions differ.
    return any(addr in net for net in networks)


def _forwarded_chain_within(headers, networks) -> bool:
    """False when the ``X-Forwarded-For`` chain names a hop outside ``networks``.

    The exemptions below trust ``X-Real-IP``, which nginx writes from its TCP
    peer and which the app honours only from the trusted loopback hop. That is
    the client's address only when nginx faces the client itself. Behind any
    further proxy (cloudflared on another host, a container's port mapping, a
    load balancer), the peer is that proxy's private address, so every client
    on the Internet would pass for "on the LAN". Such a proxy reports the real
    client in ``X-Forwarded-For`` and nginx appends to it
    (``$proxy_add_x_forwarded_for``), so a hop outside the trusted ranges there
    means the request did not originate inside them: no exemption.

    A client can only add hops to its own request, so forging the header can
    only withhold the exemption from itself. An entry that is not an address
    counts as outside (fail closed). ``X-Forwarded-For`` is otherwise unused by
    the app.
    """
    getlist = getattr(headers, "getlist", None)
    values = getlist("x-forwarded-for") if getlist else [headers.get("x-forwarded-for", "")]
    for raw in values:
        for hop in (raw or "").split(","):
            hop = hop.strip()
            if not hop:
                continue
            addr = _host_literal_ip(hop)
            if addr is None or not _ip_in(str(addr), networks):
                return False
    return True


def _is_lan_ip(value: str) -> bool:
    """True if ``value`` is a LAN / loopback / link-local IP literal."""
    return _ip_in(value, _LAN_NETWORKS)


def _host_literal_ip(host: str) -> Optional["ipaddress._BaseAddress"]:
    """Parse the ``Host`` header's address as an IP literal, or None.

    Strips the port and unwraps a bracketed IPv6 literal (``[::1]:8080``).
    Returns None when the host is a DNS name rather than an IP literal — that
    is what distinguishes "typed a bare IP into the address bar" from normal
    hostname / vhost access, which is the boundary the ``"ip"`` level keys on.
    """
    host = (host or "").strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        host = host[1:end]
    elif host.count(":") == 1:
        # IPv4 / hostname with a :port suffix. An unbracketed IPv6 literal has
        # multiple colons and no port, so only strip when there's exactly one.
        host = host.split(":", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


# Valid values for AuthSettings.lan_direct_noauth_mode / the snapshot mirror.
LAN_DIRECT_MODES: Tuple[str, ...] = ("off", "ip", "lan")


def normalize_lan_direct_mode(value: object) -> str:
    """Coerce a stored / submitted level to one of LAN_DIRECT_MODES.

    Anything unrecognised (``None`` included) falls back to the safe
    ``"off"`` so a malformed row can never silently open the gate.
    """
    v = (str(value) if value is not None else "").strip().lower()
    return v if v in LAN_DIRECT_MODES else "off"


def lan_direct_exempt(headers, client_host: str) -> bool:
    """Return True when this request should skip the app-access gate because
    it is a direct LAN access and the operator has opted into this level.

    Configured via the dashboard (``AuthSettings.lan_direct_noauth_mode``,
    mirrored into the snapshot). Three levels, selected by the operator
    according to how much of the LAN segment they trust; ``"off"`` (the
    default) is a no-op so stock deployments stay fully gated:

    * ``"off"`` — never exempt.
    * ``"ip"``  — exempt only a *bare private-IP literal* ``Host`` from a LAN
      client, i.e. the visitor typed the server's LAN IP straight into the
      address bar (``http://192.168.1.50/``). A hostname / vhost ``Host`` does
      not qualify and stays gated, so the regular login UX is untouched. This
      is the narrow option.
    * ``"lan"`` — exempt any direct (non-tunnel) request from a LAN client,
      whether by bare IP *or* a LAN-direct hostname / vhost
      (``https://lan.wsi.example.org/``). The broad trusted-segment level.

    Two signals gate **every** non-``off`` level; neither can be forged across
    the trusted-proxy boundary, which is what stops a public attacker from
    reaching the exemption:

    * **Not via the Cloudflare Tunnel.** ``is_via_cloudflare_tunnel`` is true
      only when a non-empty ``Cf-Connecting-IP`` arrives on the trusted
      loopback hop (cloudflared). Excluding every tunnel path is what keeps
      the public hostname gated — it can never become unauthenticated.
    * **The real client is on the LAN.** ``network.get_client_ip`` honours
      forwarded-IP headers only from the trusted loopback hop (nginx /
      cloudflared); the resolved address must itself be private, so the
      request genuinely originates inside the network. A public client that
      sets ``Host: lan.wsi.example.org`` (or ``Host: <private-IP>``) against
      the public endpoint resolves to a *public* client IP and stays gated.

    The ``"ip"`` level adds a third, ``Host``-based condition. That check is a
    UX gate, not a security boundary (a spoofed ``Host`` still cannot pass the
    two non-forgeable signals above), but it lets an operator keep the normal
    hostname login flow intact while still allowing the bare-IP break-glass /
    convenience route.

    Mode-agnostic on purpose: it short-circuits before the
    basic/ldap/google_oauth switch, which is what makes the LAN path usable
    under google_oauth (where interactive login cannot complete against a
    private-IP redirect URI).
    """
    level = _snapshot.lan_direct_noauth_mode
    if level not in ("ip", "lan"):
        return False
    from . import cf_access as _cf_access
    if _cf_access.is_via_cloudflare_tunnel(headers, client_host):
        return False
    from . import network as _network
    if not _is_lan_ip(_network.get_client_ip(headers, client_host)):
        return False
    if not _forwarded_chain_within(headers, _LAN_NETWORKS):
        return False
    if level == "lan":
        return True
    # level == "ip": additionally require a bare private-IP literal Host.
    host_ip = _host_literal_ip(headers.get("host", ""))
    return host_ip is not None and _is_lan_ip(str(host_ip))


# ---------------------------------------------------------------------------
# Session cookie (signed JWT, separate audience from admin tokens)
# ---------------------------------------------------------------------------
def session_generation() -> int:
    return int(_snapshot.session_generation or 0)


def basic_credential_fingerprint(password_hash: str) -> str:
    """Short digest of a basic user's stored password hash, stamped into that
    user's session cookie (``cred``) so the cookie dies with the credential
    that earned it — see :func:`_basic_session_live`. Derived from the salted
    pbkdf2 hash, never the password, and truncated: it only has to change
    when the hash changes."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


# username -> (fingerprint or None when the user no longer exists, expiry).
# Bounds how stale a revocation can be per worker without a DB read on every
# gated request of a cookie-holding basic client.
_BASIC_CRED_CACHE_TTL = 30.0
_BASIC_CRED_CACHE_MAX = 4096
_basic_cred_cache: dict = {}


def _basic_session_live(username: str, fingerprint, *, raise_errors: bool = False) -> bool:
    """True iff ``username`` is still a basic user whose stored password hash
    still matches ``fingerprint``. Deleting the user or resetting their
    password therefore cuts off every cookie minted before it, on every
    worker, within ``_BASIC_CRED_CACHE_TTL`` — the per-identity revocation
    that a bare TTL could not give (a cookie outlives the credential by up to
    30 days otherwise). Performs a short indexed read on a cache miss, so
    async callers must run the decode in the threadpool.

    A failed read fails closed (False) unless ``raise_errors``: a live
    socket's periodic re-check passes it so a DB blip is retried (and only a
    run of them closes the socket) rather than read as a revocation."""
    if not isinstance(fingerprint, str) or not fingerprint:
        return False
    now = time.monotonic()
    hit = _basic_cred_cache.get(username)
    if hit is None or hit[1] <= now:
        db = SessionLocal()
        try:
            stored = db.execute(
                select(AuthUser.password_hash).where(AuthUser.username == username)
            ).scalar_one_or_none()
        except Exception:
            if raise_errors:
                raise
            # Fail closed, like every other decode error.
            return False
        finally:
            db.close()
        current = basic_credential_fingerprint(stored) if stored else None
        if len(_basic_cred_cache) >= _BASIC_CRED_CACHE_MAX:
            _basic_cred_cache.clear()
        _basic_cred_cache[username] = (current, now + _BASIC_CRED_CACHE_TTL)
    else:
        current = hit[0]
    return current is not None and secrets.compare_digest(current, fingerprint)


def forget_basic_credential(username: str) -> None:
    """Drop this worker's cached fingerprint so a delete / password reset
    takes effect here at once (other workers catch up within the TTL)."""
    _basic_cred_cache.pop(username, None)


def create_session_token(
    subject: str,
    *,
    credential: Optional[str] = None,
    generation: Optional[int] = None,
) -> str:
    """Mint an app-access cookie.

    ``generation`` is the app-session generation of the policy the subject
    was authenticated under (see :func:`auth_snapshot`). A login flow passes
    it so a policy tightening that landed mid-login leaves the cookie stamped
    with the superseded generation — rejected by the decode check — instead
    of quietly carrying the new one. Defaults to the current generation."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "aud": SESSION_AUDIENCE,
        "iat": now,
        "exp": now + session_ttl(),
        # Stamp the app-session generation so a later policy tightening
        # (mode switch, allowed-domain change, explicit revoke) that bumps the
        # generation invalidates this cookie immediately — see decode below.
        "gen": session_generation() if generation is None else int(generation),
    }
    if credential is not None:
        # basic: subjects only — binds the cookie to the password hash.
        payload["cred"] = credential
    return jwt.encode(payload, _admin_auth.JWT_SECRET, algorithm=_admin_auth.JWT_ALG)


def decode_session_token(token: str, *, raise_errors: bool = False) -> Optional[dict]:
    """Validate an app-access session cookie. Returns the claims or None.

    Verifies signature, audience (``app-access``) and expiry. Only a
    ``basic:`` session is re-checked against its backing identity:

    * basic users are local ``AuthUser`` rows, so the cookie carries a
      fingerprint of the password hash it was minted against (``cred``) and
      is rejected once that user is deleted or their password reset — within
      ``_BASIC_CRED_CACHE_TTL`` on every worker (see ``_basic_session_live``).
    * For ldap / google_oauth (``"{mode}:{username}"``) there is no local row
      to consult, and re-confirming liveness would mean an external IdP
      round-trip on every gated request, which is not viable. Off-boarding
      such a user therefore does NOT invalidate an already-issued cookie: the
      holder keeps app access until it expires. The window is
      ``AuthSettings.session_ttl_seconds`` (default 86400 = 24h, configurable
      up to 30 days); TTL is the per-identity revocation mechanism there. A
      cookie is held to the TTL in force at decode time as well as its own
      ``exp``, so lowering the TTL shortens sessions already issued.

    For *bulk* revocation there is a second lever: every cookie carries a
    ``gen`` claim (the app-session generation at mint time), and a policy
    tightening — auth-mode switch, allowed-domain change, or an explicit
    operator revoke — bumps ``AuthSettings.session_generation``. A cookie
    whose ``gen`` is below the current generation is rejected below, so those
    actions invalidate every outstanding session at once rather than waiting
    out each TTL. The claim is mandatory: ``create_session_token`` always
    stamps it, so a cookie without a well-formed integer ``gen`` is malformed
    or forged and is rejected rather than read as generation 0 — which is a
    *valid* generation (the default), so defaulting would hand a
    claim-stripped cookie the same standing as a freshly minted one.

    Scope of that bulk lever: it only revokes *cookie* sessions, i.e. the
    ldap / google_oauth / basic app-access sessions decoded here. It does NOT
    cut off a Cloudflare-Access user (CF is a separate, always-live gate ahead
    of this decode — see the middleware), and under ``basic`` mode a browser
    that authenticated via the HTTP ``Authorization: Basic`` header will simply
    re-authenticate with its cached credentials on the next request after the
    cookie is rejected. Bulk revocation is therefore durable for form-login
    cookie sessions; CF-Access and cached-Basic clients are governed by their
    own mechanisms, not this counter.

    This intentionally differs from ``staff_token_valid`` below, which DOES
    re-query the ``users`` table on every staff-bypass check: staff tokens
    map to a single ``User.id`` that is cheap to look up, so immediate
    disable/demote enforcement is both feasible and worth the cost. The
    app-access cookie has no equivalent stable handle across all three
    modes, so it relies on a short-ish TTL instead. Operators who need
    tighter app-access revocation should lower ``session_ttl_seconds``.

    ``except Exception`` (rather than ``jwt.PyJWTError``) is intentional:
    this gate is fail-closed, so any unexpected decode/config error must
    map to "not authenticated" rather than bubbling a 500 out of the
    middleware on every request.
    """
    try:
        claims = jwt.decode(
            token,
            _admin_auth.JWT_SECRET,
            algorithms=[_admin_auth.JWT_ALG],
            audience=SESSION_AUDIENCE,
            # Require exp, iat and gen: create_session_token always sets all
            # three, so a cookie missing one is malformed / forged and must be
            # rejected rather than treated as non-expiring, of unknown age or
            # as generation 0. Mirrors the staff path (_decode_staff_payload)
            # — defence in depth.
            options={"require": ["exp", "iat", "gen"]},
        )
    except Exception:
        return None
    # The TTL in force now, not only the one at mint time. ``exp`` was fixed
    # when the cookie was issued, so lowering the session TTL — the lever the
    # docs give operators for a tighter revocation window — would otherwise
    # bind only cookies minted afterwards: one issued under a 30-day TTL would
    # keep working for 30 days after the TTL was cut to an hour. Measuring the
    # cookie's age against the current TTL applies a cut to every outstanding
    # session on the next request (each worker within its settings refresh);
    # raising the TTL lengthens none, since ``exp`` still holds. PyJWT has
    # already checked that ``iat`` is a number and not in the future; a bool is
    # refused here for the reason given for ``gen`` below.
    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        return None
    if time.time() >= issued_at + session_ttl():
        return None
    # Bulk-revocation check: reject a cookie stamped with a generation older
    # than the current one. A cookie somehow carrying a *newer* generation
    # than this worker's snapshot is tolerated (>=) to avoid a transient
    # logout while snapshots converge right after a bump.
    #
    # ``require`` above only proves the key is present — a JSON ``null``,
    # string or list still satisfies it — so the value is type-checked here.
    # ``bool`` is excluded explicitly because it is an ``int`` subclass in
    # Python and ``True`` would otherwise read as generation 1. Anything that
    # is not a plain integer fails closed, like the decode errors above,
    # rather than raising out of the middleware that runs this on every
    # gated request.
    generation = claims.get("gen")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return None
    if generation < session_generation():
        return None
    # A basic user's cookie is only as good as the account behind it: it must
    # carry the fingerprint of the password hash it was minted against, and
    # that hash must still be the stored one (see _basic_session_live).
    sub = claims.get("sub")
    if isinstance(sub, str) and sub.startswith("basic:"):
        # A basic session means nothing outside basic mode. A mode switch
        # bumps the generation (rejected above) — this is the backstop should
        # a cookie ever be minted with the new generation under the old mode.
        if current_mode() != "basic":
            return None
        if not _basic_session_live(
            sub[len("basic:"):], claims.get("cred"), raise_errors=raise_errors
        ):
            return None
    return claims


def maybe_carry_cf_session(request, response, email: str) -> None:
    """Mint a parent-domain app-access session cookie carrying a verified
    Cloudflare-Access identity, so a public -> LAN redirect lands on the LAN
    host already authenticated (no second interactive login).

    No-op unless ``WSI_SESSION_COOKIE_DOMAIN`` is configured: the stock
    deployment keeps cookies host-only and is unaffected. The caller must
    only invoke this for a request whose CF JWT verified on a trusted hop
    (``cf_status == "valid"``); the ``email`` is the verified claim, so the
    cookie asserts only what CF already proved. The session ``sub`` is the
    bare email — identical to what the CF JWT path records on the public
    host — so the same person buckets into one row across both hostnames.

    Re-mints when the incoming cookie is absent or carries a different
    ``sub`` (e.g. the user switched Google accounts at the CF gate); skips
    the Set-Cookie otherwise so a steady-state request adds no header.
    Decoding the incoming cookie can read the DB (a basic cookie's credential
    re-check, see :func:`decode_session_token`), so async callers run this in
    the threadpool.
    """
    domain = SESSION_COOKIE_DOMAIN
    if not domain or not email:
        return
    existing = request.cookies.get(SESSION_COOKIE)
    if existing:
        payload = decode_session_token(existing)
        if payload and payload.get("sub") == email:
            return
    from . import network as _network
    proto = _network.forwarded_proto(request.headers, request.url.scheme)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(email),
        max_age=session_ttl(),
        httponly=True,
        secure=proto == "https",
        samesite="lax",
        path="/",
        domain=domain,
    )


def staff_token_valid(authorization_header: str) -> bool:
    """Return True iff Authorization: Bearer carries a valid *staff* JWT
    (admin / manager / instructor) whose backing user is still active.

    The JWT itself is a 24h capability with no revocation list, so the backing
    user is re-checked in the DB on every call: a disabled / demoted /
    password-reset user's stale JWT stops passing immediately instead of at
    natural expiry. The cost is a single SQLite primary-key fetch on the
    bearer path; it does not run for requests without a Bearer header.

    Any staff role is accepted, not admin only. Used for the draft-preview
    bypass on the token-keyed public + /dzi/ read surfaces: every staff role can already view
    unpublished slides/tours through the ``require_staff`` admin endpoints and
    the dashboard, so gating the viewer's draft preview at admin-only would 404
    exactly the managers/instructors the RBAC model lets author and publish.
    """
    token = _admin_auth.bearer_token(authorization_header)
    if token is None:
        return False
    db = SessionLocal()
    try:
        user = _admin_auth.resolve_staff_user(token, db)
    finally:
        db.close()
    # resolve_staff_user proves identity, liveness and non-revocation but not
    # the role, so the role is checked here: only a staff role may bypass.
    return bool(user and user.role in _admin_auth.STAFF_ROLES)


def _decode_slide_download(token: str, slide_id: str, db: Session) -> bool:
    try:
        expect = int(slide_id)
    except ValueError:
        # Over int()'s digit limit: no slide has that id, and an exception
        # here would answer anyone's request with a 500 and a logged trace.
        return False
    return _admin_auth.decode_slide_download_token(token, expect, db)


# The three signed-link download routes (see auth._create_download_token),
# each with the decoder that checks its ``?t=`` token against the path's id.
# ``[0-9]+`` cannot match ``export``, so the export and slide routes never
# compete for a path.
_SIGNED_DOWNLOAD_ROUTES = (
    (re.compile(r"/api/admin/backup/([^/]+)/download"),
     _admin_auth.decode_backup_download_token),
    (re.compile(r"/api/admin/slides/export/([^/]+)/download"),
     _admin_auth.decode_export_download_token),
    (re.compile(r"/api/admin/slides/([0-9]+)/download"), _decode_slide_download),
)


def signed_download_valid(method: str, path: str, token: Optional[str]) -> bool:
    """Return True iff this is a GET/HEAD of a signed-link download route whose
    ``?t=`` token is valid for the path's id right now.

    The dashboard starts a backup / export / slide download by navigating the
    browser to the URL ``/download_url`` minted, so the request carries no
    Authorization header — under ldap / google_oauth a staff member who never
    signed in to the app itself has no app cookie either, and the gate would turn
    the download away before the route could look at the token. The token is
    a staff credential in its own right: minted only for an admin, scoped to
    one id, short-lived and re-checked against the minting user's live row
    (is_active + token_version) — the same checks the staff Bearer bypass
    relies on. The route re-validates it; this check only decides whether the
    request may reach the route, so a forged or stale ``t`` still meets the
    gate. Synchronous DB read: call it from the threadpool.
    """
    if not token or method not in ("GET", "HEAD"):
        return False
    for pattern, decode in _SIGNED_DOWNLOAD_ROUTES:
        match = pattern.fullmatch(path)
        if match is None:
            continue
        db = SessionLocal()
        try:
            return bool(decode(token, match.group(1), db))
        finally:
            db.close()
    return False


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------
# Hard ceiling on the raw Authorization: Basic header. base64-encoded
# "user:password" with auth.MAX_USERNAME_LEN (255) + MAX_PASSWORD_LEN
# (1024) is at most ceil((255+1+1024)/3)*4 ≈ 1708 bytes; rounding up to 4096
# gives generous slack and still slams the door on multi-MB headers that
# would otherwise feed straight into pbkdf2 / utf-8 decode.
_MAX_BASIC_HEADER_LEN = 4096


def parse_basic_header(authorization_header: str) -> Optional[Tuple[str, str]]:
    if not authorization_header.lower().startswith("basic "):
        return None
    if len(authorization_header) > _MAX_BASIC_HEADER_LEN:
        return None
    try:
        # validate=True so characters outside the base64 alphabet are an error
        # rather than being silently discarded. Without it a header padded with
        # junk decodes to *something*, which is a needlessly forgiving reading
        # of a credential.
        raw = base64.b64decode(
            authorization_header.split(" ", 1)[1].strip(), validate=True
        ).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    user, _, pw = raw.partition(":")
    # Bound each field to the same limits the form-login path enforces
    # (MAX_USERNAME_LEN / validate_password_strength's MAX_PASSWORD_LEN). The
    # 4096-byte header ceiling above is coarse and still admits a ~3 KB password
    # — roughly 3x the intended pbkdf2 CPU bound — into verify_password. A
    # credential longer than these caps can never match a stored one (creation
    # enforces the same limits), so fail closed by treating it as unparseable.
    if len(user) > _admin_auth.MAX_USERNAME_LEN or len(pw) > _admin_auth.MAX_PASSWORD_LEN:
        return None
    return user, pw


# login_throttle.attempt_tag(username, password) -> (fingerprint of the stored
# hash the password was rejected against, or None for an unknown user;
# expiry). A browser replaying a credential that has since been rotated
# re-sends it with every request — each tile's edge-authz subrequest included
# — and each would otherwise pay a full pbkdf2 verify to reach the same answer.
_BASIC_REJECT_TTL = 60.0
_BASIC_REJECT_MAX = 4096
_basic_reject_cache: dict = {}


def _verified_basic_hash(username: str, password: str, db: Session) -> Optional[str]:
    """Return the stored password hash ``password`` was verified against, or
    None when the user is unknown or the password does not match.

    A rejection is remembered briefly (``_basic_reject_cache``) against the
    stored hash it was checked against, so an identical retry is refused
    without another pbkdf2 verify for as long as that hash is unchanged — a
    password reset (or the user being created / deleted) re-verifies at once.
    Unknown users are remembered too, so a repeat is equally fast whether or
    not the username exists."""
    if not username:
        # Still run the dummy verify so the absent-username branch takes the
        # same wall-clock time as the real path and can't be used to probe
        # which usernames exist via timing.
        _admin_auth.dummy_verify_password(password)
        return None
    stored = db.execute(
        select(AuthUser.password_hash).where(AuthUser.username == username)
    ).scalar_one_or_none()
    current = basic_credential_fingerprint(stored) if stored else None
    key = _login_throttle.attempt_tag(username, password)
    now = time.monotonic()
    hit = _basic_reject_cache.get(key)
    if hit is not None and hit[1] > now and hit[0] == current:
        return None
    if not stored:
        _admin_auth.dummy_verify_password(password)
    elif _admin_auth.verify_password(password, stored):
        _basic_reject_cache.pop(key, None)
        return stored
    if len(_basic_reject_cache) >= _BASIC_REJECT_MAX:
        _basic_reject_cache.clear()
    _basic_reject_cache[key] = (current, now + _BASIC_REJECT_TTL)
    return None


def verify_basic_credential(username: str, password: str, db: Session) -> Optional[str]:
    """Verify a basic user's password, returning the credential fingerprint
    to stamp into the session cookie on success (None on failure)."""
    # The fingerprint must come from the very hash the password was checked
    # against. Re-reading the row afterwards would race a password reset:
    # a login proven with the OLD password would then carry the NEW hash's
    # fingerprint and survive the reset that was meant to cut it off.
    stored = _verified_basic_hash(username, password, db)
    if not stored:
        return None
    upgraded = _admin_auth.rehash_if_outdated(password, stored)
    if upgraded is not None:
        # A hash made with an older (cheaper) pbkdf2 work factor, now proven
        # right: store it at the current one. Compare-and-swap on the hash that
        # was verified, so a password reset committed meanwhile is never
        # undone. The fingerprint stamped below is then the NEW hash's — this
        # sign-in's own cookie stays valid, while cookies minted earlier for
        # the same account die (once) with the hash they were issued against.
        swapped: Optional[int]
        try:
            swapped = db.execute(
                update(AuthUser)
                .where(AuthUser.username == username, AuthUser.password_hash == stored)
                .values(password_hash=upgraded)
                .execution_options(synchronize_session=False)
            ).rowcount
            db.commit()
        except Exception:
            db.rollback()
            _logger.warning("viewer password hash upgrade failed", exc_info=True)
            swapped = None
        if swapped:
            stored = upgraded
        else:
            # Either the swap was lost (0 rows: the hash changed between the
            # check and the swap) or the write itself failed (a locked database
            # under a burst of sign-ins) and it is unknown whether a sibling's
            # upgrade landed meanwhile. A whole class signing in to one shared
            # account at once does this: the first sign-in upgrades the hash
            # and every sibling that read the old one loses the swap -- with
            # the right password, so refusing them (and counting each toward
            # the address's failure throttle) would lock the class out of its
            # own account. So look at what is stored NOW. If it changed, verify
            # once against it: the sibling's upgrade of the same password
            # passes, a password reset (the case the swap guards against) does
            # not. If it did not (the failed write left the row as it was), the
            # password was proven against exactly what is stored: sign in with
            # that and let the next sign-in retry the upgrade -- failing a
            # correct password over an optimisation would be the wrong trade.
            try:
                current = db.execute(
                    select(AuthUser.password_hash).where(AuthUser.username == username)
                ).scalar_one_or_none()
            except Exception:
                db.rollback()
                _logger.warning("viewer password hash re-read failed", exc_info=True)
                current = stored if swapped is None else None
                confirmed = False
            else:
                confirmed = True
            if swapped == 0 or current != stored:
                if not current or not _admin_auth.verify_password(password, current):
                    return None
                stored = current
            if not confirmed:
                # The row could not even be read back: the fingerprint below is
                # the one this sign-in proved, but not one the row is known to
                # hold, so it must not be written into the per-worker cache
                # (a wrong entry rejects every cookie for 30 s).
                return basic_credential_fingerprint(stored)
    fingerprint = basic_credential_fingerprint(stored)
    # Just proven against the stored hash: refresh this worker's cache so a
    # stale entry (e.g. a negative one left by a cookie of a since-recreated
    # user) cannot reject the cookie minted from this very login.
    _basic_cred_cache[username] = (fingerprint, time.monotonic() + _BASIC_CRED_CACHE_TTL)
    return fingerprint


# ---------------------------------------------------------------------------
# LDAP
# ---------------------------------------------------------------------------
def _ldap_server(uri: str):
    """Build the ldap3 Server with a certificate-verifying Tls.

    ldap3's default ``Tls()`` is ``validate=CERT_NONE``, which it also uses for
    ldaps:// and for StartTLS when no Tls is given, so an on-path attacker
    could present any certificate and read the service-account and user
    passwords. CERT_REQUIRED (with ldap3's hostname check against the URI
    host) makes both fail closed. Trust anchors are the system store unless
    LDAP_CA_CERTS_FILE names a private CA bundle.
    """
    import ssl

    from ldap3 import NONE, Server, Tls
    from ldap3.core.exceptions import LDAPCertificateError

    class _VerifyingTls(Tls):
        """ldap3's own hostname check, except for an IP-literal host.

        On Python >= 3.12 (no ssl.match_hostname) ldap3 falls back to a
        backport that only reads DNS names, so ``ldaps://192.0.2.10`` would be
        refused even with a certificate carrying that IP in its
        subjectAltName. For an IP host, skip ldap3's check ('*') and match
        the IP SAN entries here instead. Both ldap3 call sites (ldaps:// open
        and StartTLS) wrap with do_handshake=True, so the peer certificate is
        available right after the super() call.
        """

        def wrap_socket(self, connection, do_handshake=False):
            try:
                ip = ipaddress.ip_address(str(connection.server.host).strip("[]"))
            except ValueError:
                return super().wrap_socket(connection, do_handshake)
            saved, self.valid_names = self.valid_names, ["*"]
            try:
                super().wrap_socket(connection, do_handshake)
            finally:
                self.valid_names = saved
            cert = connection.socket.getpeercert() or {}
            for key, value in cert.get("subjectAltName", ()):
                if key == "IP Address":
                    try:
                        if ipaddress.ip_address(value.strip()) == ip:
                            return
                    except ValueError:
                        continue
            try:
                connection.socket.close()
            except OSError:
                pass
            raise LDAPCertificateError(f"certificate has no IP subjectAltName for {ip}")

    tls = _VerifyingTls(
        validate=ssl.CERT_REQUIRED,
        ca_certs_file=_settings.ldap_ca_certs_file or None,
    )
    # connect_timeout caps the socket-level wait so a blackholed server can't
    # pin a threadpool worker for the OS default TCP timeout.
    #
    # get_info=NONE: only the entry DN is needed, so there is no reason to
    # download the server's schema on every login — and with a schema loaded
    # ldap3 validates requested attribute names against it (see the search
    # in verify_ldap).
    return Server(uri, get_info=NONE, connect_timeout=5, tls=tls)


def verify_ldap(
    username: str, password: str, cfg: Optional[Mapping[str, object]] = None
) -> bool:
    """Check ``username`` / ``password`` against the directory. ``cfg`` is the
    LDAP block of the snapshot the caller stamps the session with (see
    :func:`auth_snapshot`); it defaults to the current one."""
    if cfg is None:
        cfg = _snapshot.ldap or _EMPTY_MAP
    uri = cfg.get("server_uri") or ""
    if not uri or not username or not password:
        return False
    try:
        # ldap3 import is local so the dependency only loads when LDAP is in use.
        from ldap3 import (
            Connection,
            NO_ATTRIBUTES,
            SUBTREE,
            AUTO_BIND_NO_TLS,
            AUTO_BIND_TLS_BEFORE_BIND,
        )
        from ldap3.core.exceptions import LDAPException
    except Exception:
        return False

    # _ldap_server sets connect_timeout and a verifying Tls; receive_timeout
    # on each Connection below caps subsequent operations, so a hung LDAP
    # server can't pin a threadpool worker on an unauthenticated login.
    # A missing LDAP_CA_CERTS_FILE raises here: fail closed.
    try:
        server = _ldap_server(uri)
    except Exception:
        return False
    bind_dn = cfg.get("bind_dn") or ""
    bind_pw = cfg.get("bind_password") or ""
    # An ldaps:// URI is already TLS from the first byte; ldap3 refuses a
    # STARTTLS on top of it, which would fail every login. "Use StartTLS" is a
    # no-op for such a URI.
    use_tls = bool(cfg.get("use_starttls")) and not getattr(server, "ssl", False)
    user_filter = (cfg.get("user_filter") or "(uid={username})").replace(
        "{username}", _ldap_escape(username)
    )
    base_dn = cfg.get("user_base_dn") or ""
    if not base_dn:
        return False

    # STARTTLS must run *before* any BIND so the credentials never travel in
    # cleartext (RFC 4513 §3.1.5). AUTO_BIND_TLS_BEFORE_BIND is used so the
    # ordering is enforced by ldap3 itself for both the service-account search
    # bind (Step 1) and the user-rebind that actually checks the password
    # (Step 2). When use_tls is false the code explicitly falls back to a
    # plain bind; the admin opted into plaintext by leaving the toggle off.
    auto_bind_mode = AUTO_BIND_TLS_BEFORE_BIND if use_tls else AUTO_BIND_NO_TLS

    try:
        # Step 1: bind as the service account (or anonymous) and search for the user DN.
        try:
            conn = Connection(
                server,
                user=bind_dn or None,
                password=bind_pw or None,
                auto_bind=auto_bind_mode,
                receive_timeout=5,
            )
        except LDAPException:
            return False
        if not conn.bound:
            try:
                conn.unbind()
            except Exception:
                pass
            return False
        found = False
        user_dn = ""
        try:
            if conn.search(
                search_base=base_dn,
                search_filter=user_filter,
                search_scope=SUBTREE,
                # "1.1" (RFC 4511 §4.5.1.8): return no attributes, DN only.
                # "dn" is not an attribute type; against a real server's
                # schema ldap3 rejects it with LDAPAttributeError, which
                # would fail every login.
                attributes=[NO_ATTRIBUTES],
            ) and conn.entries:
                user_dn = conn.entries[0].entry_dn
                found = True
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

        # Step 2: rebind as the resolved user DN with the supplied password.
        # AUTO_BIND_TLS_BEFORE_BIND ensures the user's password is also sent
        # only after STARTTLS — rebinding on a fresh, plain connection would
        # leak it in the clear.
        #
        # When the search above returned no match, fall through to a dummy
        # bind against a DN that cannot exist so the wall-clock cost of
        # "user not found" matches "user found, wrong password". Without
        # this branch the bind round-trip is observably absent for unknown
        # usernames, leaking enumeration via response time even when the
        # caller can't distinguish error codes.
        attempt_dn = user_dn if found else (
            "cn=__wsi_dummy_" + secrets.token_hex(8) + "__," + base_dn
        )
        try:
            user_conn = Connection(
                server,
                user=attempt_dn,
                password=password,
                auto_bind=auto_bind_mode,
                receive_timeout=5,
            )
        except LDAPException:
            return False
        ok = bool(user_conn.bound) and found
        try:
            user_conn.unbind()
        except Exception:
            pass
        return ok
    except LDAPException:
        return False
    except Exception:
        return False


def test_ldap_settings(username: str = "", password: str = "") -> dict:
    """Probe the currently-saved LDAP config and return staged diagnostics.

    Used by the admin dashboard's "Test connection" button. Reads the live
    `_snapshot.ldap` populated by refresh(), so the operator must Save
    before testing — that matches the runtime semantics (verify_ldap also
    reads the snapshot) and avoids round-tripping the bind password
    through the form.

    Stages, in order of progress:
      config     - required fields missing on the saved snapshot
      import     - ldap3 not importable
      connect    - socket open / Server() construction failed
      starttls   - STARTTLS handshake failed
      bind       - service-account bind failed (wrong bind_dn / bind_pw)
      search     - search at base_dn failed (base DN typo / ACL issue)
      not_found  - user search returned no entry (username only)
      user_bind  - user rebind failed (wrong password)
      ok         - everything passed

    `detail` carries the ldap3 last_error or exception message when
    available. No credentials are echoed back.
    """
    cfg = _snapshot.ldap or _EMPTY_MAP
    uri = cfg.get("server_uri") or ""
    base_dn = cfg.get("user_base_dn") or ""
    if not uri:
        return {"ok": False, "stage": "config", "detail": "LDAP server URI is not configured"}
    if not base_dn:
        return {"ok": False, "stage": "config", "detail": "Base DN is not configured"}
    if (username and not password) or (password and not username):
        return {"ok": False, "stage": "config", "detail": "Provide both username and password, or leave both blank"}

    try:
        from ldap3 import Connection, NO_ATTRIBUTES, SUBTREE
        from ldap3.core.exceptions import LDAPException
    except Exception as exc:  # pragma: no cover - ldap3 ships as a hard dep
        return {"ok": False, "stage": "import", "detail": f"ldap3 module unavailable: {exc}"}

    bind_dn = cfg.get("bind_dn") or ""
    bind_pw = cfg.get("bind_password") or ""
    use_tls = bool(cfg.get("use_starttls"))
    user_filter_tpl = cfg.get("user_filter") or "(uid={username})"

    try:
        # Same verifying Server as verify_ldap, so the test reports a
        # certificate the login would reject. receive_timeout (below) caps
        # subsequent operations.
        server = _ldap_server(uri)
    except Exception as exc:
        return {"ok": False, "stage": "connect", "detail": str(exc)}
    # ldaps:// is already TLS; STARTTLS on top of it is refused (see verify_ldap).
    use_tls = use_tls and not getattr(server, "ssl", False)

    conn = None
    try:
        try:
            conn = Connection(
                server,
                user=bind_dn or None,
                password=bind_pw or None,
                auto_bind=False,
                receive_timeout=5,
            )
        except LDAPException as exc:
            return {"ok": False, "stage": "connect", "detail": str(exc)}
        try:
            # Connection.open() returns None on success for the sync
            # strategies (failure raises), so test the state it leaves behind
            # rather than its return value — `not conn.open()` would report every
            # successful connect as a failure.
            conn.open()
            if conn.closed:
                return {"ok": False, "stage": "connect", "detail": conn.last_error or "connection open failed"}
        except LDAPException as exc:
            return {"ok": False, "stage": "connect", "detail": str(exc)}
        if use_tls:
            try:
                if not conn.start_tls():
                    return {"ok": False, "stage": "starttls", "detail": conn.last_error or "STARTTLS failed"}
            except LDAPException as exc:
                return {"ok": False, "stage": "starttls", "detail": str(exc)}
        try:
            if not conn.bind():
                return {"ok": False, "stage": "bind", "detail": conn.last_error or "service-account bind failed"}
        except LDAPException as exc:
            return {"ok": False, "stage": "bind", "detail": str(exc)}

        if username:
            search_filter = user_filter_tpl.replace("{username}", _ldap_escape(username))
        else:
            # No username: just verify the base DN is reachable and the
            # bound account can search under it. size_limit keeps the
            # response tiny on populous trees.
            search_filter = "(objectClass=*)"
        try:
            ok = conn.search(
                search_base=base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                size_limit=1,
                # DN only; see verify_ldap for why not ["dn"].
                attributes=[NO_ATTRIBUTES],
            )
        except LDAPException as exc:
            return {"ok": False, "stage": "search", "detail": str(exc)}
        if not ok:
            # ldap3 also returns False for a *successful* search that matched
            # nothing (result code 0), which is "no such user", not a
            # connectivity problem.
            if (conn.result or {}).get("result") == 0:
                if username:
                    return {"ok": False, "stage": "not_found", "detail": f"user '{username}' not found under base DN"}
                return {"ok": False, "stage": "search", "detail": "base DN returned no entries"}
            return {"ok": False, "stage": "search", "detail": conn.last_error or "search returned no result (base DN unreachable?)"}
        user_dn = conn.entries[0].entry_dn if conn.entries else ""
        if username and not user_dn:
            return {"ok": False, "stage": "not_found", "detail": f"user '{username}' not found under base DN"}
    finally:
        if conn is not None:
            try:
                conn.unbind()
            except Exception:
                pass

    if username and password and user_dn:
        user_conn = None
        try:
            try:
                user_conn = Connection(
                    server,
                    user=user_dn,
                    password=password,
                    auto_bind=False,
                    receive_timeout=5,
                )
            except LDAPException as exc:
                return {"ok": False, "stage": "user_bind", "detail": str(exc)}
            try:
                user_conn.open()  # returns None; see the service-account open above
                if user_conn.closed:
                    return {"ok": False, "stage": "user_bind", "detail": user_conn.last_error or "user connection open failed"}
            except LDAPException as exc:
                return {"ok": False, "stage": "user_bind", "detail": str(exc)}
            if use_tls:
                try:
                    if not user_conn.start_tls():
                        return {"ok": False, "stage": "user_bind", "detail": user_conn.last_error or "STARTTLS failed during user bind"}
                except LDAPException as exc:
                    return {"ok": False, "stage": "user_bind", "detail": str(exc)}
            try:
                if not user_conn.bind():
                    return {"ok": False, "stage": "user_bind", "detail": user_conn.last_error or "user bind failed (wrong password?)"}
            except LDAPException as exc:
                return {"ok": False, "stage": "user_bind", "detail": str(exc)}
        finally:
            if user_conn is not None:
                try:
                    user_conn.unbind()
                except Exception:
                    pass

    return {"ok": True, "stage": "ok", "detail": ""}


def _ldap_escape(value: str) -> str:
    """Minimal LDAP filter escaping (RFC 4515)."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\5c")
        elif ch == "*":
            out.append("\\2a")
        elif ch == "(":
            out.append("\\28")
        elif ch == ")":
            out.append("\\29")
        elif ch == "\x00":
            out.append("\\00")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Google OAuth (OIDC)
# ---------------------------------------------------------------------------
def google_authorize_url(state: str, redirect_uri: str) -> str:
    cfg = _snapshot.google or _EMPTY_MAP
    client_id = cfg.get("client_id") or ""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def google_exchange_code(code: str, redirect_uri: str) -> Optional[dict]:
    cfg = _snapshot.google or _EMPTY_MAP
    client_id = cfg.get("client_id") or ""
    client_secret = cfg.get("client_secret") or ""
    if not client_id or not client_secret:
        return None
    try:
        r = httpx.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _google_jwks_client(force_refresh: bool = False):
    """Return a cached PyJWKClient pointed at Google's signing keys."""
    now = time.time()
    cached = _google_jwks_cache.get("client")
    if (
        cached is not None
        and not force_refresh
        and (now - _google_jwks_cache.get("fetched_at", 0.0)) < _GOOGLE_JWKS_TTL
    ):
        return cached
    client = jwt.PyJWKClient(GOOGLE_JWKS_URI, cache_keys=True)
    _google_jwks_cache["client"] = client
    _google_jwks_cache["fetched_at"] = now
    return client


def verify_google_id_token(id_token: str) -> Optional[dict]:
    """Verify Google's ID token: signature, issuer, audience, and expiry.

    Returns the validated claims dict on success, or None on any failure
    (bad signature, wrong audience, expired, etc.). Inspecting the
    unverified payload would be unsafe — nothing else in the flow proves
    the token has not been tampered with after Google issued it.
    """
    cfg = _snapshot.google or _EMPTY_MAP
    client_id = cfg.get("client_id") or ""
    if not client_id:
        return None

    for force_refresh in (False, True):
        try:
            jwks_client = _google_jwks_client(force_refresh=force_refresh)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            payload = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=list(GOOGLE_ISSUERS),
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            return payload
        except jwt.PyJWKClientError:
            # Key not found in cache; one retry with a fresh fetch covers
            # Google's daily rotation. Subsequent failures fall through.
            if force_refresh:
                return None
            continue
        except jwt.InvalidTokenError:
            return None
        except Exception:
            return None
    return None


def google_email_allowed(
    email: str,
    cfg: Optional[Mapping[str, object]] = None,
    hd: object = None,
) -> bool:
    """Return True iff ``email`` is on the individual allow-list, or its
    domain is in the configured domain allow-list AND the ID token's ``hd``
    (Google Workspace hosted domain) claim is too.

    An individually listed address is matched exactly (case-insensitive) and
    needs no ``hd``: it is how an operator admits a named outside collaborator
    or personal Google account (e.g. a gmail.com address) without opening a
    whole domain. The exception is an address whose domain is itself on the
    domain list: it keeps the ``hd`` requirement. The caller has already
    required ``email_verified``.

    The email domain alone is not proof of organisation membership: a personal
    (non-Workspace) Google account can be registered under an address at a
    domain the operator allows, and Google then issues a verified-email token
    with no ``hd`` claim. Only Workspace accounts carry ``hd``, so requiring it
    to be an allowed domain ties the sign-in to the organisation's tenant.

    Fail closed when both allow-lists are empty. This is only reached
    from the google_oauth callback (which has already confirmed the active
    mode), so an operator who selected Google OAuth but never named an allowed
    domain or address would otherwise admit *every* verified Google account on the
    internet — a fail-open gate. Requiring at least one configured domain
    forces the operator to scope who can sign in; the other auth modes never
    call this, so denying here can't affect them.

    ``cfg`` is the Google block of the snapshot the caller stamps the session
    with (see :func:`auth_snapshot`); it defaults to the current one.
    """
    if cfg is None:
        cfg = _snapshot.google or _EMPTY_MAP
    if "@" not in email:
        return False
    allowed = cfg.get("allowed_domains") or ()
    domain = email.split("@", 1)[1].strip().lower()
    # A listed address at a listed (Workspace) domain gains nothing but the
    # skipped hd check, which would only admit a personal account registered
    # under that address — so it stays under the domain rule below.
    if domain not in allowed and email.strip().lower() in (
        cfg.get("allowed_emails") or ()
    ):
        return True
    if not allowed:
        return False
    if domain not in allowed:
        return False
    return isinstance(hd, str) and hd.strip().lower() in allowed


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


_NEXT_MAX_LEN = 2048


def safe_next_path(raw: Optional[str]) -> Optional[str]:
    """Return ``raw`` if it is a same-origin relative path, else ``None``.

    The post-login redirect target is attacker-influenceable (a link query
    parameter), so only a single-slash absolute path is accepted: no scheme or
    authority (``//host`` is protocol-relative), no backslash (browsers treat
    ``/\\host`` like ``//host``) and no control characters. ``/login`` itself is
    refused so a successful login can't bounce back into the login screen.
    """
    if not raw or len(raw) > _NEXT_MAX_LEN or not raw.startswith("/"):
        return None
    if raw.startswith("//") or "\\" in raw:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if raw == "/login" or raw.startswith(("/login?", "/login#", "/login/")):
        return None
    return raw
