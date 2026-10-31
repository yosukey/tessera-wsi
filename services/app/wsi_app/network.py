"""IP-filter in-memory cache and mDNS (avahi-daemon) helper."""
from __future__ import annotations
import json
import os
import ipaddress
from dataclasses import dataclass, field
from typing import List, Tuple

from .models import NetworkSettings

# Comma-separated list of upstream proxy IPs whose X-Real-IP header is trusted.
# Defaults to loopback only (nginx on the same host).
_TRUSTED_PROXIES: frozenset[str] = frozenset(
    ip.strip()
    for ip in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if ip.strip()
)


def forwarded_proto(headers, fallback: str = "") -> str:
    """The scheme the client used, as reported by ``X-Forwarded-Proto``.

    Always takes the FIRST element. The documented single-nginx deployment
    overwrites the header, but a proxy chain that *appends* instead (an extra
    load balancer in front) delivers ``"https, http"`` — and comparing that
    whole string to ``"https"`` reads a TLS request as plaintext, which drops
    the ``Secure`` flag from session cookies and splices a comma into any URL
    built from the value.

    ``fallback`` (typically ``request.url.scheme``) is used when the header is
    absent; pass nothing to distinguish "no header" from a real scheme.
    """
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
    if client_host in _TRUSTED_PROXIES:
        for name in ("cf-connecting-ip", "x-real-ip"):
            value = _ip_literal(headers.get(name))
            if value:
                return value
    return client_host or "127.0.0.1"


def _ip_literal(value) -> str:
    """``value`` stripped, if it is an IPv4 / IPv6 address; else "".

    An IPv6 zone id (``fe80::1%eth0``) is refused: ``ipaddress`` accepts any text
    after the ``%`` (spaces, colons, thousands of characters), and a real client
    address never has one, so the string would be the client's again."""
    text = (value or "").strip()
    if "%" in text:
        return ""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return ""
    return text


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


def rules_allow_ip(client_ip: str, enabled: bool, default: str, rules) -> bool:
    """Evaluate one IP against an IP-filter rule set.

    The live filter (:func:`ip_allowed`) runs it against the cached snapshot;
    the settings PATCH runs it against the rules it is about to persist, to
    refuse a change that would lock the requesting admin out. Loopback is
    always allowed (prevents lockout); the first matching rule decides; an
    unparseable or unmatched IP falls through to ``default``.
    """
    try:
        if ipaddress.ip_address(client_ip).is_loopback:
            return True
    except ValueError:
        pass

    if not enabled:
        return True

    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return default == "allow"

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        ip_str = rule.get("ip", "")
        action = rule.get("action", "allow")
        try:
            if "/" in ip_str:
                if addr in ipaddress.ip_network(ip_str, strict=False):
                    return action == "allow"
            elif addr == ipaddress.ip_address(ip_str):
                return action == "allow"
        except ValueError:
            continue

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
            with open(_AVAHI_SERVICE_FILE, "w") as f:
                f.write(_AVAHI_SERVICE_XML)
            # avahi-daemon runs as the unprivileged `avahi` user and must be
            # able to READ this file to publish the mDNS record. The unit sets
            # UMask=0027, which would otherwise create it 0640 wsi:wsi and hide
            # it from avahi. The file only carries the public service
            # advertisement (host + port), so force it world-readable.
            os.chmod(_AVAHI_SERVICE_FILE, 0o644)
        else:
            if os.path.isfile(_AVAHI_SERVICE_FILE):
                os.remove(_AVAHI_SERVICE_FILE)
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
