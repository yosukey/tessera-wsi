"""Per-worker cache for the ``public_id -> (slide_id, is_published,
wsi_generation)`` resolution the /dzi/ tile path (and the nginx edge
authorization probe in front of it) performs on every request.

Why this exists
---------------
Serving a slide at full zoom fans one user action out into dozens–hundreds
of tile requests, and each tile request resolves ``public_id`` to the
internal PK plus the live publish flag and the WSI content generation (the
``?v=`` a tile URL must carry) with a DB query. Under a crowd that
amplification is the dominant source of connection-pool pressure (see
``config.py`` / ``db.py``), even though the resolved data is near-constant:
``public_id`` is immutable, ``is_published`` toggles only when an admin
publishes/unpublishes, and ``wsi_generation`` moves only on a WSI replace.

Caching that resolution in-process removes almost all of those reads. Reading
live on every request would buy cross-worker correctness — a
publish/unpublish/delete handled on worker A must not keep being served stale
by workers B/C/D, each of which holds its own cache. That property is kept
WITHOUT paying the per-request DB read by using a filesystem *generation
sentinel*:

* On any publish/delete/WSI replace, the handling worker calls
  :func:`invalidate_slide_meta`, which flushes its own cache immediately and
  bumps the sentinel file's token.
* Every other worker re-reads that token at most once per
  ``slide_meta_gen_check_interval`` seconds (a cheap, page-cached file read —
  never a DB round-trip) and flushes its cache when the token changed.

So the data TTL can be long (it is only a backstop for a missed bump) while a
hide/delete still propagates everywhere within the check interval (≤ ~1s by
default) — faster than the TTL, and independent of it.

Everything here is per-process (per uvicorn worker); there is no shared
memory between workers, which is exactly why the sentinel is a file.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional, Tuple

from .config import settings

logger = logging.getLogger(__name__)

# Indirection so tests can drive time deterministically without touching the
# global clock. monotonic() is immune to wall-clock jumps (NTP steps, DST).
_monotonic = time.monotonic

# Resolved value handed back to callers: (slide_id, is_published,
# wsi_generation). Either all are set (row found) or all are None (no such
# public_id). The cache itself treats the tuple as opaque apart from slot 0.
ResolvedValue = Tuple[Optional[int], Optional[bool], Optional[int]]

# Distinct from a cached value so callers can tell "not in cache" apart from a
# genuine miss they might one day cache. Exposed as the module's MISS sentinel.
MISS = object()

# Default for put()'s generation argument, distinct from any real token
# (including ``None``, which is a valid "sentinel absent" token). Callers that
# don't pass a captured generation keep the unconditional-write behaviour;
# callers that do get the stale-generation drop (see put / current_generation).
_GEN_UNSET = object()

_lock = threading.Lock()
# public_id -> (value, expires_monotonic)
_cache: "dict[str, tuple[ResolvedValue, float]]" = {}
# Last generation token this worker has seen; None means "sentinel absent".
_generation: Optional[str] = None
_generation_initialized = False
_last_gen_check = 0.0
# How many times this worker has seen the generation move: another worker's
# bump read from the sentinel, or its own invalidate_slide_meta. Only ever
# grows (_reset_for_tests leaves it alone), so a caller comparing it with a
# count it saved sees real changes only. dzi uses it to re-check its cached
# slide handles against their files (see dzi._retire_handles_of_changed_files).
_changes = 0


def _sentinel_path() -> str:
    # Lives under the already-created DATA_DIR/cache dir (see main.ensure_dirs).
    return os.path.join(settings.data_dir, "cache", "slide_meta.gen")


def _read_sentinel() -> Optional[str]:
    try:
        with open(_sentinel_path(), "r") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        # A transient read error must never break tile serving; treat it as
        # "unchanged" so the cache neither flushes spuriously nor crashes the
        # request.
        logger.warning("could not read slide-meta sentinel", exc_info=True)
        return _generation


def _write_sentinel(token: str) -> None:
    path = _sentinel_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic replace so a concurrent reader sees either the whole old token or
    # the whole new one, never a truncated write.
    # The temp name is unique per call: invalidations run on the threadpool, so
    # a pid-only name would let two concurrent publishes write the same file, and
    # the loser's os.replace would fail with FileNotFoundError.
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(token)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh_generation_locked(now: float) -> None:
    """Throttled cross-worker check. Caller must hold ``_lock``.

    Reads the sentinel at most once per check interval; flushes the cache when
    the token changed since the last read (i.e. another worker published/
    deleted). The first call only records the current token — the cache is
    empty at that point, so there is nothing to flush.
    """
    global _generation, _generation_initialized, _last_gen_check, _changes
    interval = settings.slide_meta_gen_check_interval
    if _generation_initialized and (now - _last_gen_check) < interval:
        return
    _last_gen_check = now
    token = _read_sentinel()
    if not _generation_initialized:
        _generation = token
        _generation_initialized = True
        return
    if token != _generation:
        _generation = token
        _changes += 1
        _cache.clear()


def change_count() -> int:
    """How many generation moves this worker has seen so far (see _changes).

    Reflects the sentinel as of the last throttled check, so read it right
    after :func:`get` / :func:`current_generation` to see a bump another
    worker made at most one check interval ago.
    """
    return _changes


def get(public_id: str):
    """Return the cached ``(slide_id, is_published, wsi_generation)`` for
    ``public_id`` or :data:`MISS`.

    Runs the throttled generation check first so a publish/delete on another
    worker takes effect here before a stale hit can be returned.
    """
    if settings.slide_meta_cache_ttl <= 0:
        return MISS
    now = _monotonic()
    with _lock:
        _refresh_generation_locked(now)
        hit = _cache.get(public_id)
        if hit is not None:
            value, expires = hit
            if expires > now:
                return value
            # Expired: drop it and fall through to a fresh DB read.
            _cache.pop(public_id, None)
    return MISS


def current_generation() -> Optional[str]:
    """Return the current generation token under the lock (after a throttled
    refresh).

    Callers capture this immediately BEFORE the DB read whose result they hand
    to :func:`put`. If an ``invalidate_slide_meta`` (a publish/unpublish/delete)
    lands between the capture and the ``put``, the generation moves and ``put``
    drops the now-stale value instead of re-inserting it. Without that check a
    value read just before an unpublish could resurrect the old ``is_published``
    flag and serve draft tiles/descriptors until the TTL backstop expires.
    """
    now = _monotonic()
    with _lock:
        _refresh_generation_locked(now)
        return _generation


def put(public_id: str, value: ResolvedValue, gen_token=_GEN_UNSET) -> None:
    """Store a freshly resolved value. Unknown public_ids are intentionally
    not cached (see the size-cap note in config): they never become valid and
    caching them would let a flood of guessed tokens evict live entries.

    ``gen_token`` is the generation captured (via :func:`current_generation`)
    BEFORE the DB read that produced ``value``. If the generation has moved
    since — i.e. an ``invalidate_slide_meta`` ran between the read and now — the
    value reflects an already-invalidated world and is DROPPED rather than
    re-inserted (which would otherwise re-seat a stale publish flag until the
    TTL backstop). Omit ``gen_token`` to keep the unconditional-write behaviour.
    """
    if settings.slide_meta_cache_ttl <= 0:
        return
    if value[0] is None:
        return
    now = _monotonic()
    with _lock:
        if gen_token is not _GEN_UNSET and gen_token != _generation:
            # A publish/unpublish/delete invalidated the cache under this lock
            # after the caller captured the generation; the read is stale.
            return
        _cache[public_id] = (value, now + settings.slide_meta_cache_ttl)
        overflow = len(_cache) - settings.slide_meta_cache_max
        if overflow > 0:
            # dict preserves insertion order, so the leading keys are oldest.
            for stale_key in list(_cache.keys())[:overflow]:
                _cache.pop(stale_key, None)


def invalidate_slide_meta() -> None:
    """A publish/unpublish/delete/WSI replace happened on THIS worker.

    Flush this worker's cache immediately (so the change is visible here with
    zero delay) and bump the sentinel so every other worker flushes on its
    next generation check.
    """
    global _generation, _generation_initialized, _changes
    token = str(time.time_ns())
    try:
        _write_sentinel(token)
    except OSError:
        # If the bump can't be persisted, other workers won't be signalled and
        # will fall back to the TTL backstop. Log loudly; still flush locally.
        logger.warning("could not bump slide-meta sentinel", exc_info=True)
    with _lock:
        _cache.clear()
        _changes += 1
        # Record the just-written token so the next local check doesn't
        # re-flush.
        _generation = token
        _generation_initialized = True


def _reset_for_tests() -> None:
    """Clear all module state. Test-only."""
    global _generation, _generation_initialized, _last_gen_check
    with _lock:
        _cache.clear()
        _generation = None
        _generation_initialized = False
        _last_gen_check = 0.0
