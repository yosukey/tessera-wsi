"""Unit tests for the per-worker slide-metadata resolve cache.

Covers the TTL behaviour, the "unknown public_id is never cached" rule, the
size cap, immediate local invalidation, and the cross-worker generation
sentinel (a bump from another worker flushes this worker on its next check).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import wsi_app.slide_resolve_cache as src


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Isolated cache bound to a temp data dir with a controllable clock."""
    cfg = SimpleNamespace(
        data_dir=str(tmp_path),
        slide_meta_cache_ttl=300,
        slide_meta_gen_check_interval=1,
        slide_meta_cache_max=4096,
    )
    monkeypatch.setattr(src, "settings", cfg)
    now = {"t": 1000.0}
    monkeypatch.setattr(src, "_monotonic", lambda: now["t"])
    src._reset_for_tests()
    try:
        yield src, cfg, now
    finally:
        src._reset_for_tests()


def test_miss_then_hit(cache):
    s, _cfg, _now = cache
    assert s.get("pub1") is s.MISS
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)


def test_ttl_expiry_forces_reload(cache):
    s, _cfg, now = cache
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)
    now["t"] += 301  # past the 300s TTL
    assert s.get("pub1") is s.MISS


def test_ttl_zero_disables_cache(cache):
    s, cfg, _now = cache
    cfg.slide_meta_cache_ttl = 0
    s.put("pub1", (5, True))
    assert s.get("pub1") is s.MISS


def test_unknown_public_id_not_cached(cache):
    s, _cfg, _now = cache
    s.put("ghost", (None, None))
    assert s.get("ghost") is s.MISS


def test_size_cap_evicts_oldest(cache):
    s, cfg, _now = cache
    cfg.slide_meta_cache_max = 3
    for i in range(5):
        s.put(f"p{i}", (i, True))
    assert s.get("p0") is s.MISS  # oldest evicted
    assert s.get("p1") is s.MISS
    assert s.get("p4") == (4, True)  # newest kept


def test_local_invalidate_flushes_immediately(cache):
    s, _cfg, _now = cache
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)
    s.invalidate_slide_meta()
    assert s.get("pub1") is s.MISS


def test_cross_worker_sentinel_bump_flushes(cache):
    s, cfg, now = cache
    cfg.slide_meta_gen_check_interval = 0  # re-check every call, deterministic
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)
    # Another worker published/deleted: it bumped the shared sentinel file
    # directly (this worker's memory is untouched).
    src._write_sentinel("worker-b-generation-token")
    now["t"] += 2
    assert s.get("pub1") is s.MISS


def test_put_dropped_when_generation_moved(cache):
    """A put carrying a generation captured before an intervening invalidation
    must be DROPPED, not re-seated — otherwise a value read just before an
    unpublish would resurrect the stale publish flag until the TTL backstop.
    """
    s, _cfg, _now = cache
    # Simulate _resolve_pk_published: capture the generation, then a
    # publish/unpublish/delete lands (bumps the generation, clears the cache)
    # before the resolved value is stored.
    gen = s.current_generation()
    s.invalidate_slide_meta()
    s.put("pub1", (5, True), gen)
    assert s.get("pub1") is s.MISS


def test_put_with_current_generation_is_stored(cache):
    """When no invalidation intervenes, a put carrying the captured generation
    stores normally (the drop only triggers on a moved generation)."""
    s, _cfg, _now = cache
    gen = s.current_generation()
    s.put("pub1", (5, True), gen)
    assert s.get("pub1") == (5, True)


def test_put_without_generation_still_stores(cache):
    """Omitting the generation makes the write unconditional."""
    s, _cfg, _now = cache
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)


def test_generation_check_is_throttled(cache):
    s, cfg, now = cache
    cfg.slide_meta_gen_check_interval = 5
    s.put("pub1", (5, True))
    assert s.get("pub1") == (5, True)
    # Sentinel changes, but not enough time has passed for a re-check.
    src._write_sentinel("later-token")
    now["t"] += 1  # < 5s interval
    assert s.get("pub1") == (5, True)  # still served, throttle not elapsed
    now["t"] += 5  # now past the interval
    assert s.get("pub1") is s.MISS  # picked up the sentinel change


def test_invalidate_before_first_check_marks_generation_initialized(cache):
    """An invalidate that runs before any get() must record the generation as
    initialized; otherwise the next check treats a later cross-worker bump as
    the "first read" and skips the flush, serving a stale entry until TTL."""
    s, cfg, now = cache
    cfg.slide_meta_gen_check_interval = 0
    s.invalidate_slide_meta()
    s.put("pub1", (5, True))
    src._write_sentinel("worker-b-generation-token")
    now["t"] += 2
    assert s.get("pub1") is s.MISS


def test_sentinel_temp_file_is_unique_per_write(cache, monkeypatch):
    """Invalidations run on the threadpool; two concurrent writers sharing one
    temp name would make the loser's os.replace fail (FileNotFoundError)."""
    s, _cfg, _now = cache
    used = []
    real_replace = s.os.replace

    def spy(src_path, dst_path):
        used.append(src_path)
        return real_replace(src_path, dst_path)

    monkeypatch.setattr(s.os, "replace", spy)
    s._write_sentinel("a")
    s._write_sentinel("b")
    assert len(used) == 2 and len(set(used)) == 2


def test_concurrent_invalidations_all_succeed_and_leave_no_temp_files(cache, caplog):
    import logging
    import os
    import threading

    s, _cfg, _now = cache
    caplog.set_level(logging.WARNING, logger=s.logger.name)
    start = threading.Barrier(8)
    errors = []

    def worker():
        start.wait()
        for _ in range(60):
            try:
                s.invalidate_slide_meta()
            except Exception as exc:  # pragma: no cover - the failure being tested
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert not [r for r in caplog.records if "could not bump" in r.getMessage()]
    sentinel_dir = os.path.dirname(s._sentinel_path())
    assert [f for f in os.listdir(sentinel_dir) if f.endswith(".tmp")] == []


def test_failed_sentinel_write_removes_its_temp_file(cache, monkeypatch):
    import os

    s, _cfg, _now = cache

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(s.os, "replace", boom)
    with pytest.raises(OSError):
        s._write_sentinel("x")
    sentinel_dir = os.path.dirname(s._sentinel_path())
    assert [f for f in os.listdir(sentinel_dir) if f.endswith(".tmp")] == []
