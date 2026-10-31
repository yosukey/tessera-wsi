"""Tests for the bulk slide operations (delete / publish / attributes).

Covers:
* Bulk delete removes rows, classifies not-found ids, and survives a
  per-slide failure without aborting the batch (partial success).
* Bulk publish returns a per-slide result, skipping slides that lack a
  catalog_id / title with error="missing_fields", and cascades tours to
  draft on unpublish.
* Bulk attributes add/remove one key surgically across a heterogeneous
  selection: add only touches slides missing the value (idempotent, no
  UniqueConstraint violation), remove only touches slides that have it,
  and other keys are never clobbered.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from wsi_app.models import Slide, SlideAttribute, Tour


def _existing_ids(db_session) -> set[int]:
    # Query fresh rather than reloading stale identity-map instances (a
    # db_session.get on a row deleted via the app's own session would try to
    # refresh an expired-yet-deleted instance and raise).
    db_session.expire_all()
    return set(db_session.execute(select(Slide.id)).scalars().all())


@pytest.fixture(autouse=True)
def _clean_slides(db_session):
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()


def _mk(db_session, **kw) -> Slide:
    s = Slide(**kw)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# ---- bulk delete -----------------------------------------------------------

def test_bulk_delete_removes_rows_and_classifies(client, admin_headers, db_session):
    a_id = _mk(db_session, title="A", catalog_id="A-1").id
    b_id = _mk(db_session, title="B", catalog_id="B-1").id
    missing_id = a_id + b_id + 999

    r = client.post(
        "/api/admin/slides/bulk/delete",
        json={"slide_ids": [a_id, b_id, missing_id]},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["deleted"]) == {a_id, b_id}
    assert body["not_found"] == [missing_id]
    assert body["failed"] == []

    remaining = _existing_ids(db_session)
    assert a_id not in remaining
    assert b_id not in remaining


def test_bulk_delete_partial_failure_does_not_abort_batch(
    client, admin_headers, db_session, monkeypatch
):
    a_id = _mk(db_session, title="A", catalog_id="A-1").id
    b_id = _mk(db_session, title="B", catalog_id="B-1").id
    c_id = _mk(db_session, title="C", catalog_id="C-1").id

    import wsi_app.routers.admin_slides as mod
    real = mod._do_delete_slide

    def flaky(slide_id, admin, db):
        if slide_id == b_id:
            raise RuntimeError("boom")
        return real(slide_id, admin, db)

    monkeypatch.setattr(mod, "_do_delete_slide", flaky)

    r = client.post(
        "/api/admin/slides/bulk/delete",
        json={"slide_ids": [a_id, b_id, c_id]},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    # a and c succeed even though b fails in the middle of the batch.
    assert set(body["deleted"]) == {a_id, c_id}
    assert [f["slide_id"] for f in body["failed"]] == [b_id]
    assert body["not_found"] == []

    remaining = _existing_ids(db_session)
    assert a_id not in remaining
    assert b_id in remaining  # retryable: still present
    assert c_id not in remaining


def test_bulk_delete_requires_manager(client, instructor_headers, db_session):
    a = _mk(db_session, title="A", catalog_id="A-1")
    r = client.post(
        "/api/admin/slides/bulk/delete",
        json={"slide_ids": [a.id]},
        headers=instructor_headers,
    )
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(Slide, a.id) is not None


# ---- bulk publish ----------------------------------------------------------

def test_bulk_publish_skips_missing_fields(client, admin_headers, db_session, attach_wsi):
    ok = _mk(db_session, title="Has Both", catalog_id="OK-1")
    attach_wsi(ok)  # publishing requires the WSI body on disk
    no_cat = _mk(db_session, title="No Catalog", catalog_id="")
    no_title = _mk(db_session, title="", catalog_id="NT-1")

    r = client.post(
        "/api/admin/slides/bulk/publish",
        json={"slide_ids": [ok.id, no_cat.id, no_title.id], "publish": True},
        headers=admin_headers,
    )
    assert r.status_code == 200
    results = {x["slide_id"]: x for x in r.json()["results"]}
    assert results[ok.id]["ok"] is True and results[ok.id]["is_published"] is True
    assert results[no_cat.id]["error"] == "missing_fields"
    assert results[no_title.id]["error"] == "missing_fields"

    db_session.expire_all()
    assert db_session.get(Slide, ok.id).is_published is True
    assert db_session.get(Slide, no_cat.id).is_published is False
    assert db_session.get(Slide, no_title.id).is_published is False


def test_bulk_unpublish_cascades_tours(client, admin_headers, db_session):
    s = _mk(db_session, title="S", catalog_id="S-1", is_published=True)
    t1 = Tour(slide_id=s.id, title="T1", is_published=True)
    t2 = Tour(slide_id=s.id, title="T2", is_published=True)
    db_session.add_all([t1, t2])
    db_session.commit()

    r = client.post(
        "/api/admin/slides/bulk/publish",
        json={"slide_ids": [s.id], "publish": False},
        headers=admin_headers,
    )
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["ok"] is True
    assert res["is_published"] is False
    assert res["cascaded_tours"] == 2

    db_session.expire_all()
    assert db_session.get(Tour, t1.id).is_published is False
    assert db_session.get(Tour, t2.id).is_published is False


def test_bulk_publish_allowed_for_staff(client, instructor_headers, db_session, attach_wsi):
    s = _mk(db_session, title="S", catalog_id="S-1")
    attach_wsi(s)
    r = client.post(
        "/api/admin/slides/bulk/publish",
        json={"slide_ids": [s.id], "publish": True},
        headers=instructor_headers,
    )
    assert r.status_code == 200


def test_bulk_publish_reports_missing_wsi(client, admin_headers, db_session):
    """A slide with complete metadata but no WSI file on disk (the
    post-metadata-restore state) is reported per-slide with error
    'missing_wsi' and left as a draft, without aborting the batch."""
    s = _mk(db_session, title="No File", catalog_id="NF-1", wsi_filename="ghost.svs")
    r = client.post(
        "/api/admin/slides/bulk/publish",
        json={"slide_ids": [s.id], "publish": True},
        headers=admin_headers,
    )
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["ok"] is False
    assert res["error"] == "missing_wsi"
    db_session.expire_all()
    assert db_session.get(Slide, s.id).is_published is False


# ---- bulk attributes -------------------------------------------------------

def test_bulk_attr_add_is_surgical_and_idempotent(client, admin_headers, db_session):
    # Heterogeneous selection: one slide already has the value, one doesn't.
    has = _mk(db_session, title="Has", catalog_id="H-1")
    has.attributes.append(SlideAttribute(key="stain", value="H&E"))
    # Pre-existing unrelated attribute that must NOT be clobbered.
    has.attributes.append(SlideAttribute(key="organ", value="liver"))
    lacks = _mk(db_session, title="Lacks", catalog_id="L-1")
    db_session.commit()

    r = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [has.id, lacks.id], "key": "stain", "values": ["H&E"], "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    # Only the slide that was missing the value is reported changed.
    assert body["changed"] == [lacks.id]
    assert body["not_found"] == []

    db_session.expire_all()
    has_attrs = {(a.key, a.value) for a in db_session.get(Slide, has.id).attributes}
    lacks_attrs = {(a.key, a.value) for a in db_session.get(Slide, lacks.id).attributes}
    assert ("stain", "H&E") in has_attrs
    assert ("organ", "liver") in has_attrs  # untouched
    assert ("stain", "H&E") in lacks_attrs

    # Re-running add is a no-op (idempotent, no UniqueConstraint violation).
    r2 = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [has.id, lacks.id], "key": "stain", "values": ["H&E"], "mode": "add"},
        headers=admin_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["changed"] == []


def test_bulk_attr_remove_only_touches_holders(client, admin_headers, db_session):
    has = _mk(db_session, title="Has", catalog_id="H-1")
    has.attributes.append(SlideAttribute(key="tags", value="demo"))
    has.attributes.append(SlideAttribute(key="organ", value="kidney"))
    lacks = _mk(db_session, title="Lacks", catalog_id="L-1")
    db_session.commit()

    r = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [has.id, lacks.id], "key": "tags", "values": ["demo"], "mode": "remove"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["changed"] == [has.id]

    db_session.expire_all()
    has_attrs = {(a.key, a.value) for a in db_session.get(Slide, has.id).attributes}
    assert ("tags", "demo") not in has_attrs
    assert ("organ", "kidney") in has_attrs  # other key untouched


def test_bulk_attr_requires_manager(client, instructor_headers, db_session):
    s = _mk(db_session, title="S", catalog_id="S-1")
    r = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [s.id], "key": "stain", "values": ["H&E"], "mode": "add"},
        headers=instructor_headers,
    )
    assert r.status_code == 403


def test_bulk_attr_rejects_bad_mode(client, admin_headers, db_session):
    s = _mk(db_session, title="S", catalog_id="S-1")
    r = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [s.id], "key": "stain", "values": ["H&E"], "mode": "frob"},
        headers=admin_headers,
    )
    assert r.status_code == 422


def test_bulk_attr_caps_values(client, admin_headers, db_session):
    """values is bounded like slide_ids: rows written scale with both."""
    from wsi_app.models import SlideAttribute
    from wsi_app.routers.admin_slides import BULK_MAX_ATTR_VALUES

    s = _mk(db_session, title="S", catalog_id="S-1")
    values = [f"v{i}" for i in range(BULK_MAX_ATTR_VALUES + 1)]
    r = client.post(
        "/api/admin/slides/bulk/attributes",
        json={"slide_ids": [s.id], "key": "stain", "values": values, "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 422
    db_session.expire_all()
    assert db_session.query(SlideAttribute).filter_by(slide_id=s.id).count() == 0
