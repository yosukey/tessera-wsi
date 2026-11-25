"""IP-filter in-memory cache and mDNS (avahi-daemon) helper."""
from __future__ import annotations
import functools
import hashlib
import ipaddress
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import List, Tuple

from .models import NetworkSettings

logger = logging.getLogger("wsi_app.network")


def canonical_ip(value):
    """``value`` parsed as an IP address, an IPv4-mapped IPv6 address unwrapped.

    A dual-stack socket (or a proxy listening on one) reports an IPv4 client
    as ``::ffff:203.0.113.5``. ``ipaddress`` treats that as an IPv6 address
    and never as a member of an IPv4 network
    (``IPv6Address("::ffff:203.0.113.5") in IPv4Network("203.0.113.0/24")`` is
    False), so without unwrapping it every IPv4 rule — an IP-filter deny, the
    LAN / break-glass ranges — would silently miss the client. Mirrors
    ``login_throttle.ip_bucket``. Accepts a string or an address object;
    raises ``ValueError`` for anything that is not an address.
    """
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        addr = value
    else:
        addr = ipaddress.ip_address(value)
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped or addr


def _canonical_host(value) -> str:
    """``value`` as the string of its :func:`canonical_ip` form, or unchanged.

    Used to compare a socket peer against the trusted-proxy list: both sides
    go through it, so ``::ffff:127.0.0.1`` (nginx reaching a dual-stack bind
    over IPv4) and ``127.0.0.1`` are the same peer, and ``::0001`` matches
    ``::1``. Anything that is not an IP address (a test client's sentinel
    host name, a Unix-socket peer) is returned stripped but otherwise as is,
    so it can still match a literal entry of the same spelling.
    """
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return ""
    try:
        return str(canonical_ip(text))
    except ValueError:
        return text


# Comma-separated list of upstream proxy IPs whose X-Real-IP header is trusted.
# Defaults to loopback only (nginx on the same host). Entries are stored in
# their canonical form (:func:`_canonical_host`), so an operator writing
# ``::ffff:10.0.0.2`` or ``10.0.0.2`` configures the same proxy, and the peer
# is canonicalised the same way before the lookup (:func:`is_trusted_proxy`).
_TRUSTED_PROXIES: frozenset[str] = frozenset(
    _canonical_host(ip)
    for ip in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if ip.strip()
)


def is_trusted_proxy(client_host) -> bool:
    """True when the immediate TCP peer is one of ``TRUSTED_PROXIES``.

    The forwarded headers (``Cf-Connecting-IP``, ``X-Real-IP``,
    ``X-Forwarded-Proto``) are written by nginx / cloudflared on the loopback
    hop; from any other peer they are whatever the client chose to send.

    The peer is compared in its canonical form (:func:`canonical_ip`): when
    the app is bound to a dual-stack socket, nginx connecting over IPv4
    loopback is reported as ``::ffff:127.0.0.1``, which as a plain string is
    not ``127.0.0.1`` — every forwarded header would then be ignored and all
    clients recorded (and throttled, and IP-filtered) as the proxy itself.
    The raw spelling is checked too, so a non-address entry (a test client's
    host name patched into the set) still matches itself.
    """
    if not isinstance(client_host, str) or not client_host:
        return False
    return (
        client_host in _TRUSTED_PROXIES
        or _canonical_host(client_host) in _TRUSTED_PROXIES
    )


def forwarded_proto(headers, fallback: str = "", *, client_host=None) -> str:
    """The scheme the client used, as reported by ``X-Forwarded-Proto``.

    Always takes the FIRST element. The documented single-nginx deployment
    overwrites the header, but a proxy chain that *appends* instead (an extra
    load balancer in front) delivers ``"https, http"`` — and comparing that
    whole string to ``"https"`` reads a TLS request as plaintext, which drops
    the ``Secure`` flag from session cookies and splices a comma into any URL
    built from the value. (The first element is the one the outermost,
    client-facing proxy wrote; the last is only the scheme of the innermost
    hop, which behind a TLS-terminating balancer is always ``http``. A client
    able to prepend its own value is one talking to an appending proxy that
    does not overwrite, and such a client could equally send the header
    straight through: no choice of element defends that topology, the trusted
    hop below and nginx's overwrite do.)

    ``client_host`` is the immediate TCP peer (``request.client.host``). When
    it is given and is not a trusted proxy, the header is ignored like the
    forwarded-IP headers in :func:`get_client_ip`: it came from the client
    itself, so it says nothing about the listener it reached — the result is
    ``fallback``. Call sites that make an access decision on the scheme must
    pass it; ``None`` (the default) keeps the historic header-trusting
    behaviour for callers that have no peer to hand.

    ``fallback`` (typically ``request.url.scheme``) is used when the header is
    absent; pass nothing to distinguish "no header" from a real scheme.
    """
    if client_host is not None and not is_trusted_proxy(client_host):
        return (fallback or "").strip().lower()
    raw = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return raw or (fallback or "").strip().lower()


def get_client_ip(headers, client_host: str) -> str:
    """Return the real client IP.

    Honors forwarded-IP headers only when the immediate hop is a trusted
    proxy. Order of precedence:

    1. ``Cf-Connecting-IP`` — set by Cloudflare Tunnel for both
       authenticated (CF Access) and bypass (non-identity policy) paths.
       Preferred over X-Real-IP because cloudflared is the source of
       truth on those paths.
    2. ``X-Real-IP`` — set by the app's own nginx for non-CF traffic
       (organization-internal direct access that does not go through
       Cloudflare at all).
    3. The peer's address as observed by the socket.

    Honoring CF-Connecting-IP from any host other than cloudflared (which
    listens on loopback) would let an attacker spoof the recorded client
    IP, so the trusted-proxy gate applies to both headers.

    A header value is used only if it IS an IP address. The result keys the
    login throttles (login_throttle) next to strings the app makes up itself
    ("mfa-setup:<id>", "reauth:<id>:<digest>"), and a proxy on the same host
    that passes Cf-Connecting-IP through hands any client the choice of the
    string: one named after another account's throttle would fill it. A value
    that is not an address is ignored, as if the header were absent.
    """
    # (The same canonical comparison as every other trusted-hop check, so a
    # proxy seen as ``::ffff:127.0.0.1`` on a dual-stack bind is trusted.)
    if is_trusted_proxy(client_host):
        for name in ("cf-connecting-ip", "x-real-ip"):
            value = _ip_literal(headers.get(name))
            if value:
                return value
    # The socket peer is reported the same way: a dual-stack listener hands
    # IPv4 clients over as ``::ffff:a.b.c.d`` (see :func:`canonical_ip`).
    return _ip_literal(client_host) or client_host or "127.0.0.1"


def _ip_literal(value) -> str:
    """``value`` stripped, if it is an IPv4 / IPv6 address; else "".

    An IPv6 zone id (``fe80::1%eth0``) is refused: ``ipaddress`` accepts any text
    after the ``%`` (spaces, colons, thousands of characters), and a real client
    address never has one, so the string would be the client's again.

    An IPv4-mapped IPv6 address comes back as the plain IPv4 address
    (:func:`canonical_ip`), so the client is the same string — for the IP
    filter, the LAN checks, the throttles and the analytics record — whichever
    socket family the request arrived on."""
    text = (value or "").strip()
    if "%" in text:
        return ""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return ""
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped) if mapped is not None else text


# ---- IP filter cache (refreshed at startup and after settings change) ----
#
# Concurrency model: the cache is exposed as an immutable Snapshot dataclass.
# `refresh()` builds a fresh Snapshot in-place and assigns it to the module
# global with a single atomic store; `ip_allowed()` reads the global once
# into a local and inspects only that copy. This avoids a torn-read window:
# mutating a shared `_cache["rules"]` list key-by-key while request middleware
# concurrently iterates it could otherwise expose a partially-updated ruleset.
@dataclass(frozen=True)
class _Snapshot:
    enabled: bool = False
    default: str = "allow"
    rules: tuple[dict, ...] = field(default_factory=tuple)
    # NetworkSettings.analytics_mode, lower-cased ("off" when unset). Carried
    # here because this snapshot is what every process — the separate
    # wsi-sync worker included — already refreshes from the DB on its own
    # (main._settings_refresh_loop): the live-sync audience frame honours the
    # privacy mode (see sync_room.audience_ip_pseudonymiser) without a DB
    # read per frame.
    analytics_mode: str = "off"


_snapshot: _Snapshot = _Snapshot()


def refresh(db) -> None:
    """Load NetworkSettings from DB and atomically swap the cache snapshot."""
    global _snapshot
    ns = db.get(NetworkSettings, 1)
    if not ns:
        _snapshot = _Snapshot()
        return
    rules: List[dict] = []
    try:
        parsed = json.loads(ns.ip_rules or "[]")
        if isinstance(parsed, list):
            rules = [r for r in parsed if isinstance(r, dict)]
    except Exception:
        rules = []
    if len(rules) > MAX_IP_RULES:
        _warn_oversized_rules(
            hashlib.sha256((ns.ip_rules or "").encode("utf-8", "replace")).hexdigest(),
            len(rules),
        )
    _snapshot = _Snapshot(
        enabled=bool(ns.ip_filter_enabled),
        default=ns.ip_filter_default or "allow",
        rules=tuple(rules),
        analytics_mode=(ns.analytics_mode or "off").strip().lower(),
    )


def analytics_mode() -> str:
    """The analytics privacy mode as of the last refresh ("off" / "hashed" /
    "plain"; anything else reads as whatever was stored, so callers compare
    against the mode they act on)."""
    return _snapshot.analytics_mode


def ip_allowed(client_ip: str) -> bool:
    """Return True if the given IP is allowed to access the server."""
    # Pin a single snapshot for the duration of this evaluation. A concurrent
    # refresh() that swaps `_snapshot` mid-call won't perturb the local view.
    snap = _snapshot
    return rules_allow_ip(client_ip, snap.enabled, snap.default, snap.rules)


# Upper bound on stored IP-filter rules. The rules are evaluated in order on
# every request (first match wins), so an unbounded list is a per-request cost
# the settings PATCH could grow without limit; a deployment needing more than
# this many entries should aggregate them into CIDRs. Enforced on save
# (routers/admin_settings); :func:`rules_allow_ip` also stops reading past it,
# so a row written some other way (a restore, a hand edit) cannot either.
MAX_IP_RULES = 1000

_IPV4_MAPPED_NET = ipaddress.ip_network("::ffff:0:0/96")


@functools.lru_cache(maxsize=16)
def _warn_oversized_rules(digest: str, count: int) -> None:
    """Log, once per distinct stored rule list, that it exceeds the save cap.

    The save path refuses a list longer than :data:`MAX_IP_RULES`, but a row
    written some other way (a list saved by an older release, a backup
    restored from one, a hand edit) can still hold one. Every rule is still
    enforced, but the operator should know that the list costs more per
    request than intended and cannot be saved again until it is trimmed, so
    :func:`refresh` reports it here. The settings refresh loop reloads the same row every few seconds in
    every process; caching on the digest of the stored JSON keeps that to a
    single line per process per distinct list (a changed list has a new
    digest and is reported again).
    """
    logger.warning(
        "IP filter: the stored rule list has %d entries, more than the %d a "
        "save accepts. All of them are still enforced, but the list cannot be "
        "saved again until it is trimmed: aggregate the rules into CIDR ranges.",
        count, MAX_IP_RULES,
    )


@functools.lru_cache(maxsize=4 * MAX_IP_RULES)
def _rule_target(ip_str: str):
    """The address or network one rule's ``ip`` names, or None if malformed.

    Cached: the live filter runs every rule on every request, and the rule
    strings only change on a settings save, so parsing each one once per
    process (instead of once per request) removes the bulk of the cost. The
    cache is keyed on the string alone, so a changed rule simply misses.

    An IPv4-mapped rule (``::ffff:10.0.0.0/104``, ``::ffff:10.0.0.1``) is
    rewritten to its IPv4 form, because the client address it is compared to
    is unwrapped the same way (:func:`canonical_ip`); left as IPv6 it could
    never match.
    """
    try:
        if "/" in ip_str:
            net = ipaddress.ip_network(ip_str, strict=False)
            if (
                net.version == 6
                and net.prefixlen >= 96
                and net.subnet_of(_IPV4_MAPPED_NET)
            ):
                net = ipaddress.ip_network(
                    (int(net.network_address) & 0xFFFFFFFF, net.prefixlen - 96)
                )
            return net
        return canonical_ip(ip_str)
    except ValueError:
        return None


def rules_allow_ip(client_ip: str, enabled: bool, default: str, rules) -> bool:
    """Evaluate one IP against an IP-filter rule set.

    The live filter (:func:`ip_allowed`) runs it against the cached snapshot;
    the settings PATCH runs it against the rules it is about to persist, to
    refuse a change that would lock the requesting admin out. Loopback is
    always allowed (prevents lockout); the first matching rule decides; an
    unparseable or unmatched IP falls through to ``default``.

    The client address is compared in its canonical form
    (:func:`canonical_ip`): an IPv4 client seen through a dual-stack socket as
    ``::ffff:a.b.c.d`` must hit the IPv4 rules written for it — otherwise a
    ``deny`` rule is skipped under ``default="allow"`` and an ``allow`` rule
    under ``default="deny"`` refuses the very client it names.
    """
    try:
        addr = canonical_ip(client_ip)
    except ValueError:
        addr = None

    if addr is not None and addr.is_loopback:
        return True

    if not enabled:
        return True

    if addr is None:
        return default == "allow"

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        ip_str = rule.get("ip", "")
        # (Checked before the cached parse: a non-string — a list from a
        # hand-edited row — is unhashable and would raise inside lru_cache.)
        target = _rule_target(ip_str) if isinstance(ip_str, str) else None
        if target is None:
            continue
        action = rule.get("action", "allow")
        if isinstance(target, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            # ``addr in net`` is False (no error) when the versions differ.
            if addr in target:
                return action == "allow"
        elif addr == target:
            return action == "allow"

    return default == "allow"


# ---- mDNS via Avahi service file ----
_AVAHI_SERVICES_DIR = "/etc/avahi/services"
_AVAHI_SERVICE_FILE = os.path.join(_AVAHI_SERVICES_DIR, "wsi.service")
_AVAHI_SERVICE_XML = """\
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Tessera (%h)</name>
  <service>
    <type>_http._tcp</type>
    <port>80</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
"""


def apply_mdns(enabled: bool) -> Tuple[bool, str]:
    """Write or remove the Avahi service file.

    Reload is handled automatically by wsi-avahi-reload.path (systemd path
    unit that watches /etc/avahi/services).  The application process runs with
    NoNewPrivileges=true, so it cannot invoke systemctl directly.
    """
    try:
        if enabled:
            os.makedirs(_AVAHI_SERVICES_DIR, exist_ok=True)
            # Written to a temp file in the same directory and renamed over
            # the target, so avahi-daemon (woken by wsi-avahi-reload.path the
            # moment the directory changes) never reads a half-written or
            # still-unreadable file, and two processes applying it at once
            # (several workers at startup) each publish a complete file
            # rather than truncating each other's.
            fd, tmp = tempfile.mkstemp(
                dir=_AVAHI_SERVICES_DIR, prefix=".wsi.service.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(_AVAHI_SERVICE_XML)
                    f.flush()
                    # avahi-daemon runs as the unprivileged `avahi` user and
                    # must be able to READ this file to publish the mDNS
                    # record. mkstemp creates it 0600 (and the unit's
                    # UMask=0027 would make an open() 0640 wsi:wsi), hiding it
                    # from avahi. The file only carries the public service
                    # advertisement (host + port), so make it world-readable
                    # before it becomes visible under its real name.
                    os.fchmod(f.fileno(), 0o644)
                os.replace(tmp, _AVAHI_SERVICE_FILE)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        else:
            try:
                os.remove(_AVAHI_SERVICE_FILE)
            except FileNotFoundError:
                pass
        return True, ""

    except PermissionError:
        return False, (
            f"Permission denied writing to {_AVAHI_SERVICES_DIR}. "
            "Grant the application user write access to that directory."
        )
    except Exception as e:
        return False, str(e)


def mdns_active() -> bool:
    """Return True if the Avahi service file exists (service is being advertised)."""
    return os.path.isfile(_AVAHI_SERVICE_FILE)
