"""Tour / step identity and per-tour asset quotas under concurrency."""
from __future__ import annotations

import io
import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from wsi_app.config import settings
from wsi_app.db import SessionLocal
from wsi_app.main import app
from wsi_app.models import PageView, Slide, Step, Tour
from wsi_app.routers import admin_tours as _admin_tours


@pytest.fixture
def parent_slide(db_session) -> Slide:
    s = Slide(title="S", catalog_id="S-ID", wsi_filename="x.svs", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    yield s
    db_session.execute(delete(PageView))
    db_session.execute(delete(Tour).where(Tour.slide_id == s.id))
    db_session.execute(delete(Slide).where(Slide.id == s.id))
    db_session.commit()


def _step_exists(step_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.get(Step, step_id) is not None
    finally:
        db.close()


def _new_tour(db_session, slide: Slide, title: str = "T") -> Tour:
    t = Tour(title=title, slide_id=slide.id)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# --------------------------------------------------------------------------
# Ids are never recycled
# --------------------------------------------------------------------------
def test_deleting_the_newest_tour_does_not_free_its_id(db_session, parent_slide):
    """A plain INTEGER PRIMARY KEY would hand the deleted max id to the next
    tour, which would then inherit the old tour's page-view history."""
    old = _new_tour(db_session, parent_slide, "old")
    old_id = old.id
    db_session.add(PageView(kind="tour", slide_id=parent_slide.id, tour_id=old_id))
    db_session.delete(old)
    db_session.commit()

    new = _new_tour(db_session, parent_slide, "new")
    assert new.id > old_id


def test_deleting_the_newest_step_does_not_free_its_id(db_session, parent_slide):
    t = _new_tour(db_session, parent_slide)
    s = Step(tour_id=t.id, order=1)
    db_session.add(s)
    db_session.commit()
    old_id = s.id
    db_session.delete(s)
    db_session.commit()
    s2 = Step(tour_id=t.id, order=1)
    db_session.add(s2)
    db_session.commit()
    assert s2.id > old_id


def test_step_delete_is_scoped_to_its_tour(client, admin_headers, db_session, parent_slide):
    """A delete keyed by a bare step id would let an editor holding a stale id
    remove a step of a different tour."""
    a = _new_tour(db_session, parent_slide, "A")
    b = _new_tour(db_session, parent_slide, "B")
    step_b = Step(tour_id=b.id, order=1)
    db_session.add(step_b)
    db_session.commit()
    step_id = step_b.id

    r = client.delete(f"/api/admin/tours/{a.id}/steps/{step_id}", headers=admin_headers)
    assert r.status_code == 404, r.text
    assert _step_exists(step_id)

    r = client.delete(f"/api/admin/tours/{b.id}/steps/{step_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert not _step_exists(step_id)


# --------------------------------------------------------------------------
# Per-tour asset quota under concurrent uploads
# --------------------------------------------------------------------------
def _png_bytes(color) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, "PNG")
    return buf.getvalue()


def test_concurrent_uploads_cannot_both_pass_the_file_quota(admin_headers, db_session, parent_slide, monkeypatch):
    """Two uploads must not both read the same headroom before either writes,
    or together they would exceed the per-tour file cap."""
    t = _new_tour(db_session, parent_slide)
    original_cap = settings.tour_asset_max_files
    object.__setattr__(settings, "tour_asset_max_files", 1)

    real_quota = _admin_tours._enforce_tour_asset_quota
    passed = threading.Barrier(2)

    def quota_then_wait(*a, **kw):
        out = real_quota(*a, **kw)
        try:
            # Without serialisation the other upload reaches this point too
            # and both proceed with the same headroom. With it, the other
            # request is still waiting for the lock, so this times out.
            passed.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        return out

    monkeypatch.setattr(_admin_tours, "_enforce_tour_asset_quota", quota_then_wait)
    codes = []

    def upload(color):
        r = TestClient(app).post(
            f"/api/admin/tours/{t.id}/assets/upload",
            files={"file": ("pic.png", _png_bytes(color), "image/png")},
            headers=admin_headers,
        )
        codes.append(r.status_code)

    threads = [threading.Thread(target=upload, args=(c,)) for c in ((1, 2, 3), (4, 5, 6))]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    finally:
        object.__setattr__(settings, "tour_asset_max_files", original_cap)
    assert sorted(codes) == [200, 409]
    base = os.path.join(settings.data_dir, "assets", "tours", t.public_id)
    assert len([n for n in os.listdir(base) if not n.startswith(".")]) == 1
