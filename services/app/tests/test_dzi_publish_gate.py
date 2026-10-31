"""Verify the /dzi/{slide_id}.dzi and tile routes enforce the publish gate.

Mirrors the contract advertised by /api/slides/{id}: a draft slide must
look identical to a missing one for unauthenticated callers, so the tile
bytes can't be pulled by guessing slide ids. Admin previews ride through
with a Bearer JWT — the same one the frontend already passes via OSD's
ajaxHeaders.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from wsi_app import dzi as dzi_module
from wsi_app.models import Slide


class _FakeDz:
    def get_dzi(self, fmt: str) -> str:
        return f'<Image Format="{fmt}"/>'

    def get_tile(self, level, coord):  # pragma: no cover - tile path covered by SZI tests
        raise NotImplementedError


@pytest.fixture
def published_dzi_slide(db_session) -> Slide:
    s = Slide(
        title="Pub",
        catalog_id="P-001",
        wsi_filename="pretend.svs",
        is_published=True,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture
def draft_dzi_slide(db_session) -> Slide:
    s = Slide(
        title="Draft",
        catalog_id="D-001",
        wsi_filename="pretend.svs",
        is_published=False,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture
def published_szi_slide(db_session) -> Slide:
    s = Slide(
        title="Szi",
        catalog_id="S-001",
        wsi_filename="pretend.szi",
        is_published=True,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_dzi_descriptor_on_szi_slide_404s_not_500(
    client: TestClient, published_szi_slide: Slide
) -> None:
    """Requesting the OpenSlide .dzi descriptor for an SZI slide must be a clean
    404, not a 500. An SZI body is a zip OpenSlide can't open, so feeding it to
    _open_dz would raise OpenSlideUnsupportedFormatError -> 500 + an error_log row
    that an anonymous caller could loop. The real _open_dz (not stubbed here)
    must reject the .szi filename up front.
    """
    r = client.get(f"/dzi/{published_szi_slide.public_id}.dzi")
    assert r.status_code == 404


def _make_entry() -> "dzi_module._DZEntry":
    # Mimic what _get_dz hands back: a _DZEntry carrying one ref for the
    # caller. _release() then decrements it to 0; closing stays False (the
    # entry was never retired), so no osr.close() is attempted.
    entry = dzi_module._DZEntry(osr=None, dz=_FakeDz(), etag='"etag"', size=0, path="")
    entry.refs = 1
    return entry


def _patch_get_dz():
    """Stub _get_dz so the gate tests don't need a real OpenSlide handle."""
    return patch.object(
        dzi_module,
        "_get_dz",
        side_effect=lambda slide_id: _make_entry(),
    )


def test_dzi_descriptor_published_serves_anonymous(
    client: TestClient, published_dzi_slide: Slide
) -> None:
    with _patch_get_dz():
        r = client.get(f"/dzi/{published_dzi_slide.public_id}.dzi")
    assert r.status_code == 200


def test_dzi_descriptor_draft_404s_anonymous(
    client: TestClient, draft_dzi_slide: Slide
) -> None:
    with _patch_get_dz():
        r = client.get(f"/dzi/{draft_dzi_slide.public_id}.dzi")
    assert r.status_code == 404


def test_dzi_descriptor_draft_serves_admin(
    client: TestClient, draft_dzi_slide: Slide, admin_headers
) -> None:
    with _patch_get_dz():
        r = client.get(
            f"/dzi/{draft_dzi_slide.public_id}.dzi",
            headers=admin_headers,
        )
    assert r.status_code == 200


def test_dzi_tile_draft_404s_anonymous(
    client: TestClient, draft_dzi_slide: Slide
) -> None:
    with _patch_get_dz():
        r = client.get(f"/dzi/{draft_dzi_slide.public_id}_files/0/0_0.jpeg")
    # Either 404 from the publish gate or from the tile path; the gate
    # check fires before tile rendering, so 404 is the expected status
    # and the body is never produced.
    assert r.status_code == 404


def test_dzi_descriptor_reflects_live_publish_flag(
    client: TestClient, draft_dzi_slide: Slide, db_session, monkeypatch
) -> None:
    """Publishing on a different worker must take effect here promptly.

    The publish flag is served from the per-worker resolve cache, kept
    coherent across workers by a generation sentinel that the publishing
    worker bumps. This simulates that: the row flips AND the shared sentinel
    is bumped (as the other worker's publish handler would do), while this
    worker performs no local eviction. With the generation re-checked on each
    request, the new state is visible on the next request instead of waiting
    out the cache TTL.
    """
    from types import SimpleNamespace

    from wsi_app import slide_resolve_cache
    from wsi_app.config import settings as _settings

    # Re-check the sentinel on every request (interval 0) so the simulated
    # other-worker publish is observed deterministically without waiting out
    # the throttle window.
    monkeypatch.setattr(
        slide_resolve_cache,
        "settings",
        SimpleNamespace(
            data_dir=_settings.data_dir,
            slide_meta_cache_ttl=_settings.slide_meta_cache_ttl,
            slide_meta_gen_check_interval=0,
            slide_meta_cache_max=_settings.slide_meta_cache_max,
        ),
    )
    slide_resolve_cache._reset_for_tests()

    # Prime the cache by issuing a request while the slide is still a draft.
    with _patch_get_dz():
        r = client.get(f"/dzi/{draft_dzi_slide.public_id}.dzi")
    assert r.status_code == 404

    # Publish landed on a different uvicorn worker: the row flips and that
    # worker bumped the shared generation sentinel. No local eviction here.
    draft_dzi_slide.is_published = True
    db_session.commit()
    slide_resolve_cache._write_sentinel(str(time.time_ns()))

    with _patch_get_dz():
        r = client.get(f"/dzi/{draft_dzi_slide.public_id}.dzi")
    assert r.status_code == 200


class _RecordingOsr:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_clear_defers_close_while_entry_in_use():
    """clear_dz_cache must not close a handle that an in-flight read still
    holds a ref to — that would be a use-after-free in OpenSlide's C layer.
    The handle is closed only once the reader releases its ref.
    """
    osr = _RecordingOsr()
    entry = dzi_module._DZEntry(osr=osr, dz=_FakeDz(), etag='"e"', size=1, path="/x")
    with dzi_module._dz_lock:
        dzi_module._dz_cache[999001] = entry
        entry.refs += 1  # simulate an in-flight tile read holding the handle

    removed = dzi_module.clear_dz_cache(999001)
    assert removed == 1
    assert entry.closing is True
    assert osr.closed == 0, "must not close while a read holds a ref"

    # Reader finishes -> the last release performs the single close.
    dzi_module._release(entry)
    assert osr.closed == 1


def test_clear_closes_idle_entry_immediately():
    osr = _RecordingOsr()
    entry = dzi_module._DZEntry(osr=osr, dz=_FakeDz(), etag='"e"', size=1, path="/x")
    with dzi_module._dz_lock:
        dzi_module._dz_cache[999002] = entry  # no refs held

    removed = dzi_module.clear_dz_cache(999002)
    assert removed == 1
    assert osr.closed == 1, "an idle entry (no in-flight refs) closes at once"


class _LatchingDz:
    """Mimics OpenSlide's latching errors: once one read fails, every later
    read on the same handle fails too."""

    def __init__(self) -> None:
        self.failed = False

    def get_dzi(self, fmt: str) -> str:
        return f'<Image Format="{fmt}"/>'

    def get_tile(self, level, coord):
        from openslide import OpenSlideError
        from PIL import Image

        if self.failed or coord == (9, 9):
            self.failed = True
            raise OpenSlideError("corrupt tile")
        return Image.new("RGB", (4, 4))


def test_openslide_read_error_retires_handle(
    client: TestClient, published_dzi_slide: Slide, monkeypatch
) -> None:
    """One corrupt tile must not 500 the whole slide. OpenSlide latches the
    error on its handle, so the cached entry is retired and the next request
    reopens the slide; healthy tiles keep serving.
    """
    opened: list = []

    def fake_open(slide_id):
        entry = dzi_module._DZEntry(
            osr=_RecordingOsr(), dz=_LatchingDz(), etag='"e"', size=1, path="/x",
        )
        opened.append(entry)
        return entry

    # Treat the recording stand-in as a real (latching) OpenSlide handle.
    monkeypatch.setattr(dzi_module, "OpenSlide", _RecordingOsr)
    monkeypatch.setattr(dzi_module, "_open_dz", fake_open)
    monkeypatch.setattr(dzi_module, "_entry_is_fresh", lambda sid, e: True)
    dzi_module.clear_dz_cache(published_dzi_slide.id)
    base = f"/dzi/{published_dzi_slide.public_id}_files"
    try:
        assert client.get(f"{base}/0/0_0.jpeg").status_code == 200
        assert client.get(f"{base}/0/9_9.jpeg").status_code == 500
        # The poisoned handle was dropped from the cache and closed ...
        assert opened[0].closing is True
        assert opened[0].osr.closed == 1
        # ... so a healthy tile reopens and serves instead of 500ing.
        assert client.get(f"{base}/0/0_0.jpeg").status_code == 200
        assert client.get(f"{base}/0/1_0.jpeg").status_code == 200
        assert len(opened) == 2
    finally:
        dzi_module.clear_dz_cache(published_dzi_slide.id)


def test_non_latching_reader_error_keeps_handle_cached(
    client: TestClient, published_dzi_slide: Slide, monkeypatch
) -> None:
    """The OME-TIFF reader raises OpenSlideError for a bad segment but does not
    latch; its handle must stay cached instead of being reopened per request."""
    opened: list = []

    def fake_open(slide_id):
        entry = dzi_module._DZEntry(
            osr=_RecordingOsr(), dz=_LatchingDz(), etag='"e"', size=1, path="/x",
        )
        opened.append(entry)
        return entry

    monkeypatch.setattr(dzi_module, "_open_dz", fake_open)
    monkeypatch.setattr(dzi_module, "_entry_is_fresh", lambda sid, e: True)
    dzi_module.clear_dz_cache(published_dzi_slide.id)
    base = f"/dzi/{published_dzi_slide.public_id}_files"
    try:
        assert client.get(f"{base}/0/9_9.jpeg").status_code == 500
        assert client.get(f"{base}/0/9_9.jpeg").status_code == 500
        assert len(opened) == 1
        assert opened[0].closing is False
    finally:
        dzi_module.clear_dz_cache(published_dzi_slide.id)


def test_busy_bounded_render_is_a_retryable_503_that_keeps_the_handle(
    client: TestClient, published_dzi_slide: Slide, monkeypatch
) -> None:
    """A worker that already has its cap of block-read tiles in hand answers
    503 with Retry-After (nginx caches no 503), logs no tile failure and
    keeps the slide's handle: the slide is fine, the worker is just busy."""
    opened: list = []

    class _BusyDz:
        def get_tile(self, level, address):
            raise dzi_module.BoundedRenderBusy("8 block-read tiles already queued")

    def fake_open(slide_id):
        entry = dzi_module._DZEntry(
            osr=_RecordingOsr(), dz=_BusyDz(), etag='"e"', size=1, path="/x",
        )
        opened.append(entry)
        return entry

    failures: list = []
    monkeypatch.setattr(dzi_module, "OpenSlide", _RecordingOsr)
    monkeypatch.setattr(dzi_module, "_open_dz", fake_open)
    monkeypatch.setattr(dzi_module, "_entry_is_fresh", lambda sid, e: True)
    monkeypatch.setattr(dzi_module, "_log_tile_failure", lambda *a: failures.append(a))
    dzi_module.clear_dz_cache(published_dzi_slide.id)
    base = f"/dzi/{published_dzi_slide.public_id}_files"
    try:
        for _ in range(2):
            r = client.get(f"{base}/0/0_0.jpeg")
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "2"
        assert len(opened) == 1 and opened[0].closing is False
        assert failures == []
    finally:
        dzi_module.clear_dz_cache(published_dzi_slide.id)


def test_real_openslide_corrupt_tile_does_not_poison_slide(
    client: TestClient, db_session
) -> None:
    """End to end with a real OpenSlide handle and a genuinely corrupt tile:
    OpenSlide latches the error, so without retiring the handle every healthy
    tile of the slide went on returning 500 afterwards."""
    import os

    import numpy as np
    import tifffile

    from wsi_app.config import settings

    s = Slide(title="Corrupt", catalog_id="C-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    name = f"{s.id}_corrupt.tif"
    path = os.path.join(wsi_dir, name)
    rng = np.random.default_rng(0)
    tifffile.imwrite(
        path, rng.integers(0, 255, (1024, 1024, 3), dtype=np.uint8),
        tile=(256, 256), compression="jpeg",
    )
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        # Bottom-right TIFF tile (row 3, col 3: pixels 768-1023), so the
        # healthy DZI tiles below (256 px + 1 px overlap) never touch it.
        off, cnt = page.dataoffsets[15], page.databytecounts[15]
    with open(path, "r+b") as f:
        f.seek(off)
        f.write(b"\0" * cnt)
    s.wsi_filename = name
    db_session.commit()

    dzi_module.clear_dz_cache(s.id)
    try:
        r = client.get(f"/dzi/{s.public_id}.dzi")
        assert r.status_code == 200
        width = int(r.text.split('Width="')[1].split('"')[0])
        top = (width - 1).bit_length()  # DZI max level = ceil(log2(size))
        base = f"/dzi/{s.public_id}_files/{top}"
        assert client.get(f"{base}/0_0.jpeg").status_code == 200
        # DZI tile 3_3 (pixels 767-1023) reads the zeroed TIFF tile.
        assert client.get(f"{base}/3_3.jpeg").status_code == 500
        assert client.get(f"{base}/0_0.jpeg").status_code == 200
        assert client.get(f"{base}/1_0.jpeg").status_code == 200
    finally:
        dzi_module.clear_dz_cache(s.id)


def test_tile_coordinates_must_be_canonical_decimal(
    client: TestClient, published_dzi_slide: Slide
) -> None:
    """``level`` / ``col`` / ``row`` typed as int would accept "010", "+1",
    "1_0", ... — one tile to the generator, but a distinct nginx cache key
    ($uri) per spelling, so each spelling would be a fresh render and cache
    entry.
    Only canonical decimal reaches the generator; everything else is a 404."""
    from PIL import Image

    rendered = []

    class _TileDz(_FakeDz):
        def get_tile(self, level, coord):
            rendered.append((level, coord))
            return Image.new("RGB", (4, 4))

    def _entry(slide_id):
        entry = dzi_module._DZEntry(osr=None, dz=_TileDz(), etag='"etag"', size=0, path="")
        entry.refs = 1
        return entry

    base = f"/dzi/{published_dzi_slide.public_id}_files"
    with patch.object(dzi_module, "_get_dz", side_effect=_entry):
        assert client.get(f"{base}/10/0_12.jpeg").status_code == 200
        for path in ("010/0_12", "10/00_12", "10/0_012", "+10/0_12", "10/0_1_2", "10/0_1.0",
                     "10/-0_12", "10/0_%2012", "1_0/0_12", "10/0_1234567890"):
            assert client.get(f"{base}/{path}.jpeg").status_code == 404, path
    assert rendered == [(10, (0, 12))]


def test_dz_etag_includes_render_params(tmp_path, monkeypatch):
    """The DZI/tile ETag must move when the tile size / overlap / quality
    changes, so browsers don't 304 a descriptor cut on the old grid. The SZI
    ETag (archive served verbatim) stays file-identity only."""
    import os

    f = tmp_path / "x.svs"
    f.write_bytes(b"x")
    st = os.stat(f)
    before = dzi_module._dz_etag(1, st)
    assert before != dzi_module._file_etag(1, st)
    monkeypatch.setattr(dzi_module, "_RENDER_PARAMS", "t512o0q80")
    assert dzi_module._dz_etag(1, st) != before
    entry = dzi_module._DZEntry(osr=None, dz=None, etag=before, size=1, path=str(f))
    assert dzi_module._entry_is_fresh(1, entry) is False


def test_repeated_corrupt_tile_logs_one_traceback_per_window(
    client: TestClient, published_dzi_slide: Slide, monkeypatch, caplog
) -> None:
    """A corrupt tile is re-requested by every viewer on every pan. Only the
    first failure per (slide, tile) per minute is a full ERROR traceback; the
    repeats are one-line DEBUG records, so the error_logs table is not spent
    on one bad tile."""
    import logging

    def fake_open(slide_id):
        return dzi_module._DZEntry(
            osr=_RecordingOsr(), dz=_LatchingDz(), etag='"e"', size=1, path="/x",
        )

    clock = [1000.0]
    monkeypatch.setattr(dzi_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dzi_module, "_open_dz", fake_open)
    monkeypatch.setattr(dzi_module, "_entry_is_fresh", lambda sid, e: True)
    dzi_module._tile_err_last.clear()
    dzi_module.clear_dz_cache(published_dzi_slide.id)
    base = f"/dzi/{published_dzi_slide.public_id}_files"

    def errors() -> list:
        return [
            r for r in caplog.records
            if r.levelno >= logging.ERROR and "Failed to read tile" in r.getMessage()
        ]

    try:
        with caplog.at_level(logging.DEBUG, logger=dzi_module.logger.name):
            for _ in range(5):
                assert client.get(f"{base}/0/9_9.jpeg").status_code == 500
            assert len(errors()) == 1 and errors()[0].exc_info
            assert any(
                r.levelno == logging.DEBUG and "Failed again" in r.getMessage()
                for r in caplog.records
            )
            # Once the window expires the next failure is logged in full again.
            clock[0] += dzi_module._TILE_ERR_LOG_INTERVAL_S + 1
            assert client.get(f"{base}/0/9_9.jpeg").status_code == 500
            assert len(errors()) == 2
    finally:
        dzi_module.clear_dz_cache(published_dzi_slide.id)
        dzi_module._tile_err_last.clear()


def test_tile_failure_log_keys_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(dzi_module, "_TILE_ERR_LOG_MAX_KEYS", 8)
    dzi_module._tile_err_last.clear()
    try:
        for col in range(50):
            dzi_module._log_tile_failure(1, 0, col, 0, RuntimeError("x"))
        assert len(dzi_module._tile_err_last) <= 8
    finally:
        dzi_module._tile_err_last.clear()


# ---------------------------------------------------------------------------
# A slide deleted / replaced on another worker: this worker's handle on its
# file is dropped when the resolve cache's sentinel moves.
# ---------------------------------------------------------------------------
def _cached_handle_on(slide_id: int, path: str) -> "dzi_module._DZEntry":
    """Cache a handle on ``path`` as _open_dz would have (matching ETag)."""
    import os

    entry = dzi_module._DZEntry(
        osr=_RecordingOsr(), dz=_FakeDz(), etag=dzi_module._dz_etag(slide_id, os.stat(path)),
        size=1, path=path,
    )
    with dzi_module._dz_lock:
        dzi_module._dz_cache[slide_id] = entry
    return entry


@pytest.fixture
def check_sentinel_every_call(monkeypatch):
    from types import SimpleNamespace

    from wsi_app import slide_resolve_cache
    from wsi_app.config import settings

    monkeypatch.setattr(
        slide_resolve_cache, "settings",
        SimpleNamespace(
            data_dir=settings.data_dir,
            slide_meta_cache_ttl=300,
            slide_meta_gen_check_interval=0,
            slide_meta_cache_max=4096,
        ),
    )
    slide_resolve_cache._reset_for_tests()
    return slide_resolve_cache


def test_another_workers_delete_closes_this_workers_handle(
    client: TestClient, published_dzi_slide: Slide, tmp_path, check_sentinel_every_call,
) -> None:
    """The deleted slide's public_id never resolves again, so _get_dz's
    freshness check never ran for its entry: the handle — and the unlinked
    file under it, whose space uploads would lack (507) — would stay open until
    LRU eviction. Seeing the sentinel move must re-check every cached handle."""
    deleted, replaced, kept = tmp_path / "deleted.tif", tmp_path / "replaced.tif", tmp_path / "kept.tif"
    for p in (deleted, replaced, kept):
        p.write_bytes(b"old bytes")
    gone = _cached_handle_on(990001, str(deleted))
    swapped = _cached_handle_on(990002, str(replaced))
    fine = _cached_handle_on(990003, str(kept))
    swapped.refs += 1  # an in-flight tile read on the replaced slide
    try:
        with _patch_get_dz():
            # First sight of the sentinel on this worker: nothing to compare.
            assert client.get(f"/dzi/{published_dzi_slide.public_id}.dzi").status_code == 200
            # Another worker deletes one slide and replaces another, then bumps.
            deleted.unlink()
            replaced.write_bytes(b"the new, longer file")
            check_sentinel_every_call._write_sentinel("another-workers-bump")
            assert client.get(f"/dzi/{published_dzi_slide.public_id}.dzi").status_code == 200
        with dzi_module._dz_lock:
            cached = dict(dzi_module._dz_cache)
        assert 990001 not in cached and gone.osr.closed == 1
        # Retired at once, but closed only when the read holding it is done.
        assert 990002 not in cached and swapped.closing and swapped.osr.closed == 0
        dzi_module._release(swapped)
        assert swapped.osr.closed == 1
        # A handle whose file is untouched stays cached and open.
        assert cached.get(990003) is fine and fine.osr.closed == 0
    finally:
        for sid in (990001, 990002, 990003):
            dzi_module.clear_dz_cache(sid)


def test_handles_are_rechecked_once_per_sentinel_move(
    client: TestClient, published_dzi_slide: Slide, monkeypatch, check_sentinel_every_call,
) -> None:
    """The sweep stat()s every cached handle, so it runs when the sentinel
    moves, not on every request."""
    checked: list = []
    monkeypatch.setattr(dzi_module, "_entry_is_fresh", lambda sid, e: checked.append(sid) or True)
    with dzi_module._dz_lock:
        dzi_module._dz_cache[990004] = dzi_module._DZEntry(
            osr=_RecordingOsr(), dz=_FakeDz(), etag='"e"', size=1, path="/x",
        )
    try:
        with _patch_get_dz():
            url = f"/dzi/{published_dzi_slide.public_id}.dzi"
            assert client.get(url).status_code == 200
            checked.clear()
            check_sentinel_every_call._write_sentinel("bump-1")
            for _ in range(3):
                assert client.get(url).status_code == 200
            # (Other tests may have left entries of their own in the cache.)
            assert checked.count(990004) == 1
    finally:
        dzi_module.clear_dz_cache(990004)
