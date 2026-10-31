"""In-process, per-worker IP throttle for the admin login endpoint.

This complements the per-USER lockout in ``routers/admin_auth.py``. That
lockout keys on an existing, active username, so it does nothing against an
attacker who spreads guesses across many usernames, or who hammers
unknown / disabled accounts — and every such attempt still pays a full
pbkdf2 verify server-side. This module bounds the number of *failed*
admin-login attempts from a single client IP within a rolling window,
throttling credential-stuffing and the unauthenticated CPU cost of pbkdf2.

Scope (deliberately mirroring ``admin_helpers``' process-local slide-lock
rationale): the state is PROCESS-LOCAL. Production runs uvicorn with
multiple workers and ordinary requests are not pinned to a worker, so the
effective ceiling is ``N_workers × max_failures``. That is still a hard
bound far below "unlimited", needs no shared store (Redis / DB) on the
login hot path, and degrades safely if a worker restarts (its counters
reset, which can only ever *forgive* attempts, never over-block). A
cross-worker counter would be the fix only if a single instance ever needs
an exact global limit.

Only failures are recorded, each tagged with the username that was tried. A
successful login forgives only THAT username's failures from the IP
(:func:`clear_user`), so a user who fat-fingers their password a few times and
then succeeds is not penalised, while an attacker holding one valid account
cannot reset the IP's budget by interleaving a success between guesses at
other accounts.

A caller that passes the presented ``secret`` (the HTTP Basic branch) has a
failure that repeats an already-counted (username, secret) pair within the
window left uncounted. A browser tab still replaying a credential that has
since been rotated sends the same pair on every request (each tile's
edge-authz subrequest included), which would otherwise exhaust the whole IP's
budget and lock out everyone behind the same NAT; a brute-forcer has to vary
the secret, so every guess still counts.

Memory is bounded: empty windows are dropped on access, each IP keeps only as
many entries as its limit needs, and the tracked-IP map is capped
(:data:`_MAX_TRACKED_IPS`).

An IPv6 client is counted per /64, not per address (:func:`ip_bucket`):
callers key their per-IP buckets through it.
"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import secrets
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Tuple

# Hard cap on distinct IPs tracked at once, so an attacker rotating source
# IPs cannot grow the map without bound. When exceeded, the least-recently
# touched entries are evicted (they are the ones closest to ageing out
# anyway). Each entry is a small deque of floats; 50k entries is a few MB.
_MAX_TRACKED_IPS = 50_000

_LOCK = threading.Lock()
# Maps client IP -> deque of (monotonic timestamp, username tag, attempt tag or
# None) of recent failures. OrderedDict so eviction can run in
# least-recently-used order under the cap.
_Entry = Tuple[float, bytes, Optional[bytes]]
_failures: "OrderedDict[str, Deque[_Entry]]" = OrderedDict()

# Per-process key for attempt_tag. Attempt tags of presented secrets stay in
# memory for a whole window, and an unkeyed digest of a (typically real, merely
# outdated) password would be an offline-crackable copy of it.
_ATTEMPT_KEY = secrets.token_bytes(32)


def ip_bucket(ip: str) -> str:
    """The per-IP throttle key for client address ``ip``.

    An IPv6 address is reduced to its /64 prefix (``"2001:db8:1:2::/64"``).
    A /64 is the smallest block a site or a home line is normally given, and
    SLAAC / privacy addressing lets any one host in it pick fresh addresses at
    will, so a per-address key would hand an attacker on IPv6 a new budget for
    every address it rotates to — 2**64 of them — while an IPv4 client gets
    one. Everyone sharing that /64 shares one budget, the same trade-off
    a NAT'd IPv4 classroom already makes.

    An IPv4-mapped IPv6 address (``::ffff:192.0.2.7``, what a dual-stack
    socket reports for an IPv4 peer) is keyed as the IPv4 address it carries,
    so one client cannot hold two budgets by arriving both ways. An IPv4
    address is its own key, and anything unparseable (a test client's name,
    an empty peer) comes back unchanged.
    """
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ip
    if addr.version == 4:
        return str(addr)
    mapped = addr.ipv4_mapped
    if mapped is not None:
        return str(mapped)
    # Built from the integer so a scoped address ("fe80::1%eth0") cannot
    # trip ip_network's parser; the zone does not change the prefix.
    return str(ipaddress.IPv6Network(((int(addr) >> 64) << 64, 64)))


def _tag(username: str) -> bytes:
    """Fixed-size tag of a (case-folded) username. Usernames are attacker-chosen
    strings up to hundreds of characters; keeping each verbatim in every failure
    entry would multiply the tracker's worst-case memory ~8x."""
    return hashlib.blake2b((username or "").lower().encode("utf-8", "replace"), digest_size=8).digest()


def attempt_tag(username: str, secret: str) -> bytes:
    """Keyed, fixed-size digest identifying one exact (username, secret) pair.

    The username is length-prefixed so no two different pairs share an
    encoding. ``app_auth`` uses it too, to remember a rejected Basic
    credential without holding the password itself."""
    data = f"{len(username)}:{username}:{secret}".encode("utf-8", "surrogatepass")
    return hashlib.blake2b(data, key=_ATTEMPT_KEY, digest_size=16).digest()


def _prune(dq: Deque[_Entry], cutoff: float) -> None:
    """Drop entries older than ``cutoff`` from the left of ``dq``."""
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def retry_after_seconds(ip: str, *, max_failures: int, window_seconds: int) -> int:
    """Return how many seconds ``ip`` must wait, or 0 if it may proceed.

    Pure inspection — does not record anything. A return value > 0 means the
    caller should reject with 429 and a ``Retry-After`` of that many seconds.
    """
    if not ip or max_failures <= 0 or window_seconds <= 0:
        return 0
    now = time.monotonic()
    cutoff = now - window_seconds
    with _LOCK:
        dq = _failures.get(ip)
        if dq is None:
            return 0
        _prune(dq, cutoff)
        if not dq:
            _failures.pop(ip, None)
            return 0
        if len(dq) < max_failures:
            return 0
        # Blocked until enough failures age out to bring the count below the
        # limit; the last of those sits max_failures entries from the newest.
        last_blocking = dq[len(dq) - max_failures][0]
        return max(1, math.ceil(last_blocking + window_seconds - now))


def record_failure(
    ip: str,
    *,
    window_seconds: int,
    max_failures: int,
    username: str = "",
    secret: Optional[str] = None,
) -> None:
    """Record one failed login attempt from ``ip`` against ``username``.

    ``max_failures`` is the limit :func:`retry_after_seconds` checks this key
    against; it sizes the retained history. With ``secret``, a repeat of an
    already-counted (username, secret) pair within the window is not counted
    again (see the module docstring)."""
    if not ip or window_seconds <= 0 or max_failures <= 0:
        return
    now = time.monotonic()
    attempt = attempt_tag(username, secret) if secret is not None else None
    with _LOCK:
        dq = _failures.get(ip)
        if dq is None:
            dq = deque()
            _failures[ip] = dq
        else:
            # Refresh LRU position.
            _failures.move_to_end(ip)
        _prune(dq, now - window_seconds)
        if attempt is not None and any(e[2] == attempt for e in dq):
            return
        dq.append((now, _tag(username), attempt))
        # Bound a single IP's deque by its limit: the throttle decision and its
        # Retry-After only look at the newest max_failures entries. Twice that
        # is kept so a burst of concurrent failures overshooting the limit
        # cannot push another username's entries out before clear_user
        # forgives the burst's own username.
        while len(dq) > 2 * max_failures:
            dq.popleft()
        # Evict LRU entries if the map outgrew its cap.
        while len(_failures) > _MAX_TRACKED_IPS:
            _failures.popitem(last=False)


def clear_user(ip: str, username: str) -> None:
    """Forget ``ip``'s failures against ``username`` (called on that user's
    successful login). Failures against other usernames stay counted."""
    if not ip:
        return
    user = _tag(username)
    with _LOCK:
        dq = _failures.get(ip)
        if dq is None:
            return
        kept = [e for e in dq if e[1] != user]
        if not kept:
            _failures.pop(ip, None)
        elif len(kept) != len(dq):
            dq.clear()
            dq.extend(kept)


def clear_ip(ip: str) -> None:
    """Forget ALL of ``ip``'s failure history. Only for buckets that are
    already scoped to one account (the re-auth throttle); login paths use
    :func:`clear_user`."""
    if not ip:
        return
    with _LOCK:
        _failures.pop(ip, None)


# Budgets of "slots" taken BEFORE an attempt is verified (reserve_slot): a table
# of its own, not the failures table above. That one is keyed by whatever
# string get_client_ip returns -- an address, or the peer's; today only an
# address is taken from a header, but this table is shared with every address
# ever seen (50k-entry LRU) and its keys are chosen by the sender of the
# request, so a budget kept there could be crowded out by a client.
_MAX_SLOT_KEYS = 20_000
_slots: "OrderedDict[str, Deque[float]]" = OrderedDict()


def reserve_slot(key: str, *, max_slots: int, window_seconds: int) -> bool:
    """Take one of ``max_slots`` slots of ``key`` for the next ``window_seconds``;
    False (taking nothing) when they are all in use.

    Check and take are one step under the lock, so parallel attempts cannot all
    see a free slot before any of them takes it -- which is why callers take the
    slot before the slow password / code check, not after it fails."""
    if max_slots <= 0 or window_seconds <= 0:
        return False
    now = time.monotonic()
    with _LOCK:
        dq = _slots.get(key)
        if dq is None:
            dq = deque()
            _slots[key] = dq
        else:
            _slots.move_to_end(key)
        cutoff = now - window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_slots:
            return False
        dq.append(now)
        while len(_slots) > _MAX_SLOT_KEYS:
            _slots.popitem(last=False)
        return True


def clear_slots(key: str) -> None:
    """Free every slot of ``key`` (the account's owner got in)."""
    with _LOCK:
        _slots.pop(key, None)


def reset() -> None:
    """Drop all tracked state. Used by tests to stay isolated."""
    with _LOCK:
        _failures.clear()
        _slots.clear()
