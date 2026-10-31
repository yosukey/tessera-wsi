"""Slide id reservation never recycles a deleted slide's id.

SQLite ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT`` recycles row ids:
the rowid algorithm picks ``max(rowid) + 1`` of *existing* rows, so
deleting the only slide and uploading a new one lands on the same id as
the deleted slide. That id-collision survives in the nginx /dzi/ cache
(30-day TTL keyed by URL) and in each worker's _dz_cache, so the public
viewer keeps showing the deleted slide under the new id.

These tests guard the watermark-based reservation: regardless of what
SQLite would assign for an INSERT with no explicit id, the helper hands
out monotonically increasing ids that never reuse a previously assigned
value.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import delete

from wsi_app.admin_helpers import (
    reap_orphan_slide_reservations,
    reserve_next_slide_id,
    slide_mutation_lock,
    utcnow,
)
from wsi_app.config import settings
from wsi_app.models import AppMeta, Slide, SlideAttribute, Tour


@pytest.fixture(autouse=True)
def _clean_slides(db_session):
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.execute(delete(AppMeta).where(AppMeta.key == "slides_id_hwm"))
    db_session.commit()
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.execute(delete(AppMeta).where(AppMeta.key == "slides_id_hwm"))
    db_session.commit()


def test_reserve_starts_at_1_on_empty_db(db_session):
    assert reserve_next_slide_id(db_session) == 1


def test_reserve_advances_past_current_max_when_hwm_missing(db_session):
    db_session.add(Slide(id=42, title="Pre-watermark"))
    db_session.commit()
    # Existing rows but no hwm row yet (e.g. a restored or hand-edited
    # database).
    # The reservation must still move strictly past the live max.
    assert reserve_next_slide_id(db_session) == 43


def test_reserve_does_not_recycle_after_delete(db_session):
    """The headline guarantee: after deleting the only slide, the next
    reservation does NOT reuse its id, even though SQLite's bare INSERT
    would."""
    first = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=first, title="A"))
    db_session.commit()

    db_session.execute(delete(Slide).where(Slide.id == first))
    db_session.commit()

    second = reserve_next_slide_id(db_session)
    assert second > first
    assert second == first + 1


def test_reserve_keeps_advancing_after_many_deletes(db_session):
    """Even when the slides table empties out repeatedly, ids only ever
    move forward."""
    issued = []
    for _ in range(5):
        sid = reserve_next_slide_id(db_session)
        db_session.add(Slide(id=sid, title=f"Slide {sid}"))
        db_session.commit()
        issued.append(sid)
        db_session.execute(delete(Slide).where(Slide.id == sid))
        db_session.commit()
    assert issued == [1, 2, 3, 4, 5]


def test_reserve_honours_hwm_ahead_of_live_rows(db_session):
    db_session.add(AppMeta(key="slides_id_hwm", value="10"))
    db_session.commit()
    # The watermark wins even though the slides table is empty (no row with
    # id=10): that id was handed out before and must not be reused.
    assert reserve_next_slide_id(db_session) == 11


def test_reserve_uses_live_max_when_hwm_lags(db_session):
    db_session.add(AppMeta(key="slides_id_hwm", value="10"))
    db_session.add(Slide(id=50, title="Inserted with an explicit id"))
    db_session.commit()
    assert reserve_next_slide_id(db_session) == 51
    db_session.commit()
    assert db_session.get(AppMeta, "slides_id_hwm").value == "51"


def test_corrupt_hwm_row_self_heals(db_session):
    """A non-integer hwm value (hand-edited DB, partial write) must fall
    back to ``max(slides.id)`` and be replaced on the next assignment."""
    db_session.add(AppMeta(key="slides_id_hwm", value="garbage"))
    db_session.add(Slide(id=7, title="Pre-existing"))
    db_session.commit()

    new_id = reserve_next_slide_id(db_session)
    db_session.commit()
    assert new_id == 8

    row = db_session.get(AppMeta, "slides_id_hwm")
    assert row is not None and row.value == "8"


def test_upload_then_delete_then_upload_never_reuses_id(db_session):
    """The public-viewer-stale-slide hazard expressed end-to-end against the
    reservation helper.

    Sequence:
      1. Reserve + insert slide id N.
      2. Delete it (admin "delete" path).
      3. Reserve a fresh slide.

    The reused id (which SQLite's rowid recycle would produce) is exactly
    what the nginx /dzi/ disk cache and the per-worker DZ cache key off,
    so step 3 must NEVER come back with N.
    """
    first = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=first, title="A", wsi_filename=f"{first}_a.svs"))
    db_session.commit()
    db_session.execute(delete(Slide).where(Slide.id == first))
    db_session.commit()

    second = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=second, title="B", wsi_filename=f"{second}_b.svs"))
    db_session.commit()

    assert second != first
    assert second > first


def test_reaper_removes_old_placeholder_and_orphan_file(db_session):
    """A crashed upload leaves an empty-wsi_filename placeholder row + a partial
    file. The boot reaper removes both once the row is older than the threshold."""
    sid = reserve_next_slide_id(db_session)
    old = utcnow() - timedelta(minutes=60)
    db_session.add(Slide(id=sid, wsi_filename="", created_at=old))
    db_session.commit()

    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    partial = os.path.join(wsi_dir, f"{sid}_partial.svs")
    with open(partial, "wb") as fh:
        fh.write(b"x")

    removed = reap_orphan_slide_reservations(db_session)
    assert removed >= 1
    db_session.expire_all()
    assert db_session.get(Slide, sid) is None
    assert not os.path.exists(partial)


@pytest.mark.parametrize("when", ["before_lock", "after_lock"])
def test_reaper_spares_a_reservation_finalised_while_it_runs(db_session, monkeypatch, when):
    """The reaper picks its candidates before taking each one's lock. An
    upload / import that finalises in between (sets wsi_filename, then
    releases the lock) is a live slide by the time the lock is free: the row
    is re-read under the lock, so its file and row stay. The final DELETE is
    held to placeholders too, so even a row finalised after its lock was let
    go (no legitimate owner can, but the delete must not rely on that) is
    never deleted."""
    from contextlib import contextmanager

    from sqlalchemy import update

    from wsi_app import admin_helpers
    from wsi_app.db import SessionLocal

    sid = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=sid, wsi_filename="", created_at=utcnow() - timedelta(minutes=60)))
    db_session.commit()
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    body = os.path.join(wsi_dir, f"{sid}_done.svs")
    with open(body, "wb") as fh:
        fh.write(b"finished")

    def finalise():
        with SessionLocal() as other:
            other.execute(update(Slide).where(Slide.id == sid).values(wsi_filename=f"{sid}_done.svs"))
            other.commit()

    real = admin_helpers.try_slide_mutation_lock

    @contextmanager
    def racing_lock(slide_id):
        if when == "before_lock":
            finalise()
        with real(slide_id) as got:
            yield got
        if when == "after_lock":
            finalise()

    monkeypatch.setattr(admin_helpers, "try_slide_mutation_lock", racing_lock)
    reap_orphan_slide_reservations(db_session)
    db_session.expire_all()
    row = db_session.get(Slide, sid)
    assert row is not None and row.wsi_filename == f"{sid}_done.svs"
    if when == "before_lock":
        assert os.path.exists(body)


def test_reaper_spares_recent_placeholder_and_finalised_slide(db_session):
    """The reaper must not touch a placeholder still finalising (too young) nor
    any slide that already has a wsi_filename."""
    recent_id = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=recent_id, wsi_filename=""))  # created_at = now
    db_session.commit()

    done_id = reserve_next_slide_id(db_session)
    db_session.add(Slide(id=done_id, wsi_filename=f"{done_id}_done.svs"))
    db_session.commit()

    reap_orphan_slide_reservations(db_session)
    db_session.expire_all()
    assert db_session.get(Slide, recent_id) is not None
    assert db_session.get(Slide, done_id) is not None


# ---------------------------------------------------------------------------
# A reservation in progress holds its slide's mutation lock for the whole
# upload / import (minutes for a multi-GB file). Admin actions that loop over
# every slide, or that an operator can aim at the placeholder row the admin
# list shows meanwhile, must not wait that long on it.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _lock_held_elsewhere(slide_id: int):
    """Hold ``slide_id``'s mutation lock on another thread, as an upload or
    import running on this worker would, until the block exits."""
    taken, release = threading.Event(), threading.Event()

    def holder():
        with slide_mutation_lock(slide_id):
            taken.set()
            release.wait(30)

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    assert taken.wait(5)
    try:
        yield
    finally:
        release.set()
        th.join(5)


def _new_slide(db_session, **kw) -> Slide:
    s = Slide(**kw)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_rebuild_thumbnails_skips_placeholders_and_busy_slides_without_waiting(
    client, admin_headers, db_session, attach_wsi, monkeypatch,
):
    from wsi_app.routers import admin_slides as _admin_slides

    def fake_thumb(src, out, max_size=(512, 512)):
        with open(out, "wb") as fh:
            fh.write(b"jpeg")

    monkeypatch.setattr(_admin_slides, "make_thumbnail", fake_thumb)
    placeholder = _new_slide(db_session, title="Importing", catalog_id="IMP-1")
    busy = attach_wsi(_new_slide(db_session, title="Busy", catalog_id="B-1"))
    free = attach_wsi(_new_slide(db_session, title="Free", catalog_id="F-1"))
    with _lock_held_elsewhere(placeholder.id), _lock_held_elsewhere(busy.id):
        t0 = time.monotonic()
        r = client.post("/api/admin/slides/rebuild-thumbnails", data={}, headers=admin_headers)
        elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    assert elapsed < 5, elapsed
    assert body["rebuilt"] == [free.id]
    assert body["skipped_busy"] == [busy.id]
    # A reservation is not a slide yet: neither rebuilt nor reported.
    reported = (body["rebuilt"] + body["skipped_busy"] + body["skipped_missing_wsi"]
                + body["skipped_present"] + [f["slide_id"] for f in body["failed"]])
    assert placeholder.id not in reported


@pytest.mark.parametrize("action", ["delete", "publish", "bulk_delete", "bulk_publish"])
def test_acting_on_a_reservation_in_progress_is_refused_not_queued(
    client, admin_headers, db_session, action,
):
    placeholder = _new_slide(db_session, title="Importing", catalog_id="IMP-1")
    assert placeholder.wsi_filename == ""
    with _lock_held_elsewhere(placeholder.id):
        t0 = time.monotonic()
        if action == "delete":
            r = client.delete(f"/api/admin/slides/{placeholder.id}", headers=admin_headers)
        elif action == "publish":
            r = client.post(f"/api/admin/slides/{placeholder.id}/publish",
                            data={"publish": "1"}, headers=admin_headers)
        elif action == "bulk_delete":
            r = client.post("/api/admin/slides/bulk/delete",
                            json={"slide_ids": [placeholder.id]}, headers=admin_headers)
        else:
            r = client.post("/api/admin/slides/bulk/publish",
                            json={"slide_ids": [placeholder.id], "publish": True},
                            headers=admin_headers)
        assert time.monotonic() - t0 < 5
    if action in ("delete", "publish"):
        assert r.status_code == 409, r.text
        assert "being uploaded or imported" in r.json()["detail"]
    elif action == "bulk_delete":
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == []
        assert [f["slide_id"] for f in r.json()["failed"]] == [placeholder.id]
    else:
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["error"] == "busy"
    db_session.expire_all()
    assert db_session.get(Slide, placeholder.id) is not None


def test_a_dead_reservation_and_a_briefly_busy_slide_are_still_acted_on(
    client, admin_headers, db_session,
):
    """A placeholder nobody holds (its upload died) can still be deleted, and
    a delete of a real slide whose lock is held waits for it."""
    ghost = _new_slide(db_session, title="Ghost", catalog_id="G-1")
    r = client.delete(f"/api/admin/slides/{ghost.id}", headers=admin_headers)
    assert r.status_code == 200, r.text

    real = _new_slide(db_session, title="Real", catalog_id="R-1", wsi_filename="r.svs")
    result = {}

    def call():
        result["r"] = client.delete(f"/api/admin/slides/{real.id}", headers=admin_headers)

    with _lock_held_elsewhere(real.id):
        th = threading.Thread(target=call)
        th.start()
        th.join(0.5)
        assert th.is_alive(), "a real slide's delete should wait for its lock"
    th.join(10)
    assert result["r"].status_code == 200, result["r"].text
