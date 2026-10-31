"""Tests for the bulk tour operations (visibility / attributes / delete).

Covers:
* Bulk visibility moves each tour to the level in one write, bumps its
  revision (so an open editor sees the change), skips a tour whose parent
  slide is a draft (never publishing the parent) and one changed since it
  was listed, and leaves a tour already at the level untouched.
* Bulk attributes add / remove one key's values in the tours' own metadata
  without clobbering other keys, under the same revision rule, and hold
  instructors to the registered vocabulary as a single save does.
* Bulk delete removes rows and asset directories, classifying gone ids.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from wsi_app.admin_helpers import tour_asset_dir
from wsi_app.models import MetadataOption, Slide, Tour, TourAttribute
from wsi_app.routers.admin_tours import BULK_MAX_TOURS

NOT_REGISTERED = "adminTour.apiErr.attrValueNotRegistered"


def _make_tour(client, headers, slide_id: int, title: str = "T") -> int:
    r = client.post(f"/api/admin/slides/{slide_id}/tours", data={"title": title}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tour(db_session, tour_id) -> Tour | None:
    db_session.expire_all()
    return db_session.execute(select(Tour).where(Tour.id == tour_id)).scalar_one_or_none()


def _tour_attrs(db_session, tour_id) -> set:
    db_session.expire_all()
    return {
        (a.key, a.value)
        for a in db_session.query(TourAttribute).filter(TourAttribute.tour_id == tour_id)
    }


def _publish_slide(db_session, slide: Slide) -> None:
    slide.is_published = True
    db_session.commit()


def _refs(db_session, *tour_ids):
    return [{"id": tid, "expected_revision": _tour(db_session, tid).revision} for tid in tour_ids]


def _by_id(results) -> dict:
    return {r["tour_id"]: r for r in results}


# ---- visibility ------------------------------------------------------------

@pytest.mark.parametrize(
    ("target", "published", "unlisted"),
    [("public", True, False), ("unlisted", True, True)],
)
def test_bulk_visibility_publishes_and_bumps_revisions(
    client, admin_headers, slide, db_session, target, published, unlisted,
):
    _publish_slide(db_session, slide)
    a = _make_tour(client, admin_headers, slide.id)
    b = _make_tour(client, admin_headers, slide.id)
    refs = _refs(db_session, a, b)

    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": refs, "visibility": target}, headers=admin_headers)
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    for ref in refs:
        row = res[ref["id"]]
        assert row["ok"] and row["changed"] and row["error"] is None
        assert (row["is_published"], row["unlisted"]) == (published, unlisted)
        assert row["revision"] == ref["expected_revision"] + 1
        t = _tour(db_session, ref["id"])
        assert (t.is_published, t.unlisted, t.revision) == (published, unlisted, row["revision"])


def test_bulk_visibility_draft_keeps_the_link_only_flag(client, admin_headers, slide, db_session):
    _publish_slide(db_session, slide)
    tid = _make_tour(client, admin_headers, slide.id)
    t = _tour(db_session, tid)
    t.is_published, t.unlisted = True, True
    db_session.commit()

    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": _refs(db_session, tid), "visibility": "draft"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    t = _tour(db_session, tid)
    assert (t.is_published, t.unlisted) == (False, True)


def test_bulk_visibility_skips_tours_of_a_draft_slide(client, admin_headers, slide, db_session):
    draft_slide = Slide(title="Draft", catalog_id="D-1")
    db_session.add(draft_slide)
    db_session.commit()
    _publish_slide(db_session, slide)
    ok_tid = _make_tour(client, admin_headers, slide.id)
    blocked = _make_tour(client, admin_headers, draft_slide.id)
    rev = _tour(db_session, blocked).revision

    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": _refs(db_session, ok_tid, blocked), "visibility": "public"},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    assert res[ok_tid]["ok"] is True
    assert res[blocked] == {"tour_id": blocked, "ok": False, "changed": False, "error": "parent_draft"}
    # Neither the tour nor its parent moved, and no revision was consumed.
    t = _tour(db_session, blocked)
    assert (t.is_published, t.revision) == (False, rev)
    db_session.expire_all()
    assert db_session.get(Slide, draft_slide.id).is_published is False


def test_bulk_visibility_skips_a_tour_changed_since_listed(client, admin_headers, slide, db_session):
    _publish_slide(db_session, slide)
    tid = _make_tour(client, admin_headers, slide.id)
    stale = _tour(db_session, tid).revision
    # Edited elsewhere after the dashboard listed it.
    assert client.patch(f"/api/admin/tours/{tid}", json={"title": "Edited"}, headers=admin_headers).status_code == 200
    missing = tid + 9999

    r = client.post(
        "/api/admin/tours/bulk/visibility",
        json={"tours": [{"id": tid, "expected_revision": stale}, {"id": missing, "expected_revision": 0}],
              "visibility": "public"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    assert res[tid]["error"] == "conflict"
    assert res[missing]["error"] == "not_found"
    assert _tour(db_session, tid).is_published is False


def test_bulk_visibility_leaves_a_tour_already_at_the_level(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    rev = _tour(db_session, tid).revision
    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": _refs(db_session, tid), "visibility": "draft"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert row["ok"] is True and row["changed"] is False and row["revision"] == rev
    assert _tour(db_session, tid).revision == rev


def test_bulk_visibility_rejects_an_unknown_level(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": _refs(db_session, tid), "visibility": "secret"}, headers=admin_headers)
    assert r.status_code == 422


def test_bulk_tour_ops_cap_the_batch(client, admin_headers):
    refs = [{"id": i + 1} for i in range(BULK_MAX_TOURS + 1)]
    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": refs, "visibility": "draft"}, headers=admin_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "adminDash.apiErr.bulkTooManyTours"
    r = client.post("/api/admin/tours/bulk/delete",
                    json={"tour_ids": [i + 1 for i in range(BULK_MAX_TOURS + 1)]}, headers=admin_headers)
    assert r.status_code == 422


def test_instructors_may_use_the_bulk_tour_ops(client, instructor_headers, slide, db_session):
    _publish_slide(db_session, slide)
    tid = _make_tour(client, instructor_headers, slide.id)
    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": _refs(db_session, tid), "visibility": "public"}, headers=instructor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is True


def test_bulk_tour_ops_need_a_login(client):
    r = client.post("/api/admin/tours/bulk/visibility", json={"tours": [], "visibility": "draft"})
    assert r.status_code == 401


# ---- attributes ------------------------------------------------------------

def test_bulk_attributes_add_and_remove_touch_only_the_key(client, admin_headers, slide, db_session):
    a = _make_tour(client, admin_headers, slide.id)
    b = _make_tour(client, admin_headers, slide.id)
    assert client.patch(f"/api/admin/tours/{a}", json={"attributes": {"tb_level": ["basic"], "tb_year": ["1"]}},
                        headers=admin_headers).status_code == 200
    rev_a = _tour(db_session, a).revision

    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, a, b), "key": "tb_level", "values": [" basic ", "advanced"], "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    assert res[a]["changed"] and res[a]["revision"] == rev_a + 1
    assert res[b]["changed"]
    assert _tour_attrs(db_session, a) == {("tb_level", "basic"), ("tb_level", "advanced"), ("tb_year", "1")}
    assert _tour_attrs(db_session, b) == {("tb_level", "basic"), ("tb_level", "advanced")}

    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, a, b), "key": "tb_level", "values": ["basic"], "mode": "remove"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _tour_attrs(db_session, a) == {("tb_level", "advanced"), ("tb_year", "1")}
    assert _tour_attrs(db_session, b) == {("tb_level", "advanced")}


def test_bulk_attributes_noop_keeps_the_revision(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    rev = _tour(db_session, tid).revision
    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, tid), "key": "tb_level", "values": ["basic"], "mode": "remove"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert row["ok"] is True and row["changed"] is False
    assert _tour(db_session, tid).revision == rev


def test_bulk_attributes_skip_a_tour_changed_since_listed(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    stale = _tour(db_session, tid).revision
    assert client.patch(f"/api/admin/tours/{tid}", json={"title": "Edited"}, headers=admin_headers).status_code == 200
    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": [{"id": tid, "expected_revision": stale}], "key": "tb_level", "values": ["x"], "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["error"] == "conflict"
    assert _tour_attrs(db_session, tid) == set()


def test_bulk_attributes_hold_instructors_to_the_vocabulary(client, instructor_headers, slide, db_session):
    db_session.add(MetadataOption(key="tb_vocab", value="basic"))
    db_session.commit()
    tid = _make_tour(client, instructor_headers, slide.id)

    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, tid), "key": "tb_vocab", "values": ["made-up"], "mode": "add"},
        headers=instructor_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": NOT_REGISTERED, "params": {"key": "tb_vocab", "value": "made-up"}}
    assert _tour_attrs(db_session, tid) == set()

    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, tid), "key": "tb_vocab", "values": ["basic"], "mode": "add"},
        headers=instructor_headers,
    )
    assert r.status_code == 200, r.text
    assert _tour_attrs(db_session, tid) == {("tb_vocab", "basic")}


def test_bulk_attributes_validate_the_request(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    refs = _refs(db_session, tid)
    bad_mode = client.post("/api/admin/tours/bulk/attributes",
                           json={"tours": refs, "key": "k", "values": ["v"], "mode": "toggle"}, headers=admin_headers)
    assert bad_mode.status_code == 422
    no_key = client.post("/api/admin/tours/bulk/attributes",
                         json={"tours": refs, "key": " ", "values": ["v"], "mode": "add"}, headers=admin_headers)
    assert no_key.status_code == 422


# ---- delete ----------------------------------------------------------------

def test_bulk_delete_removes_tours_and_their_assets(client, admin_headers, slide, db_session):
    a = _make_tour(client, admin_headers, slide.id)
    b = _make_tour(client, admin_headers, slide.id)
    keep = _make_tour(client, admin_headers, slide.id)
    asset_dir = tour_asset_dir(_tour(db_session, a).public_id)
    os.makedirs(asset_dir, exist_ok=True)
    with open(os.path.join(asset_dir, "x.png"), "wb") as fh:
        fh.write(b"x")
    missing = keep + 9999

    r = client.post("/api/admin/tours/bulk/delete", json={"tour_ids": [a, b, missing]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == sorted([a, b])
    assert body["not_found"] == [missing]
    assert body["failed"] == []
    assert _tour(db_session, a) is None and _tour(db_session, b) is None
    assert _tour(db_session, keep) is not None
    assert not os.path.exists(asset_dir)


def test_single_delete_still_404s_a_missing_tour(client, admin_headers):
    r = client.delete("/api/admin/tours/987654321", headers=admin_headers)
    assert r.status_code == 404


# ---- batch robustness ------------------------------------------------------

def test_a_repeated_id_is_handled_once(client, admin_headers, slide, db_session):
    _publish_slide(db_session, slide)
    tid = _make_tour(client, admin_headers, slide.id)
    ref = _refs(db_session, tid)[0]
    r = client.post("/api/admin/tours/bulk/visibility",
                    json={"tours": [ref, ref], "visibility": "public"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    # One result, ok — not a second one meeting its own bump as a conflict.
    assert [(x["tour_id"], x["ok"]) for x in results] == [(tid, True)]
    assert _tour(db_session, tid).revision == ref["expected_revision"] + 1

    r = client.post("/api/admin/tours/bulk/delete", json={"tour_ids": [tid, tid]}, headers=admin_headers)
    assert r.json()["deleted"] == [tid] and r.json()["not_found"] == []


def test_one_tours_failed_write_does_not_stop_the_batch(client, admin_headers, slide, db_session, monkeypatch):
    from wsi_app.routers import admin_tours

    a = _make_tour(client, admin_headers, slide.id)
    b = _make_tour(client, admin_headers, slide.id)
    c = _make_tour(client, admin_headers, slide.id)
    rev_b = _tour(db_session, b).revision
    real = admin_tours.apply_tour_attribute_delta

    def broken_for_b(t, key, values, mode):
        if t.id == b:
            # A row the database refuses (NOT NULL), so the write fails.
            t.attributes.append(TourAttribute(key=key, value=None))
            return True
        return real(t, key, values, mode)

    monkeypatch.setattr(admin_tours, "apply_tour_attribute_delta", broken_for_b)
    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, a, b, c), "key": "tb_level", "values": ["basic"], "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    assert res[a]["ok"] and res[c]["ok"]
    assert res[b] == {"tour_id": b, "ok": False, "changed": False, "error": "failed"}
    assert _tour_attrs(db_session, a) == {("tb_level", "basic")}
    assert _tour_attrs(db_session, c) == {("tb_level", "basic")}
    # The failed tour kept its attributes and its revision.
    assert _tour_attrs(db_session, b) == set()
    assert _tour(db_session, b).revision == rev_b


def test_the_bulk_vocabulary_check_runs_under_the_write_lock(client, instructor_headers, slide, db_session, monkeypatch):
    """Per tour, after its revision bump (as in a single save), not only once
    up front."""
    from wsi_app.routers import admin_tours

    db_session.add(MetadataOption(key="tb_lock", value="v"))
    db_session.commit()
    a = _make_tour(client, instructor_headers, slide.id)
    b = _make_tour(client, instructor_headers, slide.id)
    refs = _refs(db_session, a, b)
    seen = []
    real = admin_tours._check_vocabulary

    def spy(db, cleaned):
        seen.append(tuple(db.execute(select(Tour.revision).where(Tour.id.in_([a, b])).order_by(Tour.id)).scalars()))
        return real(db, cleaned)

    monkeypatch.setattr(admin_tours, "_check_vocabulary", spy)
    r = client.post("/api/admin/tours/bulk/attributes",
                    json={"tours": refs, "key": "tb_lock", "values": ["v"], "mode": "add"}, headers=instructor_headers)
    assert r.status_code == 200, r.text
    ra, rb = refs[0]["expected_revision"], refs[1]["expected_revision"]
    # Up front; then under tour a's bump; then under tour b's (a committed).
    assert seen == [(ra, rb), (ra + 1, rb), (ra + 1, rb + 1)]


def test_an_error_before_the_commit_is_that_tours_failure(client, admin_headers, slide, db_session, monkeypatch):
    """Not only a refused commit: anything raised under the tour's lock (a
    query in apply, the lock not coming free) fails that tour alone, and the
    batch still reports the tours committed before it."""
    from wsi_app.routers import admin_tours

    a = _make_tour(client, admin_headers, slide.id)
    b = _make_tour(client, admin_headers, slide.id)
    c = _make_tour(client, admin_headers, slide.id)
    rev_b = _tour(db_session, b).revision
    real = admin_tours.apply_tour_attribute_delta

    def raising_for_b(t, key, values, mode):
        if t.id == b:
            raise RuntimeError("database is locked")
        return real(t, key, values, mode)

    monkeypatch.setattr(admin_tours, "apply_tour_attribute_delta", raising_for_b)
    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, a, b, c), "key": "tb_level", "values": ["basic"], "mode": "add"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    res = _by_id(r.json()["results"])
    assert res[a]["ok"] and res[c]["ok"]
    assert res[b] == {"tour_id": b, "ok": False, "changed": False, "error": "failed"}
    # Its bump was rolled back with it.
    assert _tour(db_session, b).revision == rev_b


def test_bulk_attributes_refuse_an_empty_value_list(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    rev = _tour(db_session, tid).revision
    r = client.post("/api/admin/tours/bulk/attributes",
                    json={"tours": _refs(db_session, tid), "key": "k", "values": [" ", ""], "mode": "add"},
                    headers=admin_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "adminDash.apiErr.metaValRequired"
    assert _tour(db_session, tid).revision == rev


def test_bulk_delete_keeps_error_details_out_of_the_response(client, admin_headers, slide, db_session, monkeypatch):
    from wsi_app.routers import admin_tours

    tid = _make_tour(client, admin_headers, slide.id)

    def boom(db, tour_id):
        raise RuntimeError("UPDATE secret_table SET x = 1")

    monkeypatch.setattr(admin_tours, "_do_delete_tour", boom)
    r = client.post("/api/admin/tours/bulk/delete", json={"tour_ids": [tid]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["failed"] == [{"tour_id": tid, "error": "failed"}]
    assert "secret_table" not in r.text


def test_the_vocabulary_check_names_the_first_unregistered_value(client, instructor_headers, slide, db_session):
    db_session.add(MetadataOption(key="tb_names", value="b-ok"))
    db_session.commit()
    tid = _make_tour(client, instructor_headers, slide.id)
    r = client.post(
        "/api/admin/tours/bulk/attributes",
        json={"tours": _refs(db_session, tid), "key": "tb_names", "values": ["z-new", "b-ok", "c-new"], "mode": "add"},
        headers=instructor_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["params"] == {"key": "tb_names", "value": "c-new"}


def test_the_vocabulary_check_costs_a_query_per_table_not_per_value(db_session, instructor_user):
    """Repeated per tour under the write lock by a bulk change, so its cost
    must not grow with the number of values."""
    from sqlalchemy import event

    from wsi_app.routers.admin_tours import _check_tour_vocabulary

    db_session.add_all([MetadataOption(key="tb_cost", value=f"v{i}") for i in range(50)])
    db_session.commit()
    assert instructor_user.role == "instructor"  # loaded now, not counted below
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        # All registered: found in the first table, one query.
        _check_tour_vocabulary(db_session, {"tb_cost": [f"v{i}" for i in range(50)]}, instructor_user)
        assert len(statements) == 1
        statements.clear()
        # One unregistered: every table is asked once, then refused.
        with pytest.raises(Exception):
            _check_tour_vocabulary(db_session, {"tb_cost": [f"v{i}" for i in range(50)] + ["nope"]}, instructor_user)
        assert len(statements) == 3
    finally:
        event.remove(engine, "before_cursor_execute", count)


@pytest.mark.parametrize("role", ["instructor", "manager"])
def test_a_bulk_metadata_change_reads_no_user_row_under_the_lock(client, staff_client, slide, db_session, role):
    """Each tour's bump expires the session; the per-tour step must not reload
    the user (for its role) while it holds the write lock."""
    from sqlalchemy import event

    headers = staff_client(role)
    key = f"tb_users_{role}"
    db_session.add(MetadataOption(key=key, value="v"))
    db_session.commit()
    tours = [_make_tour(client, headers, slide.id) for _ in range(3)]
    refs = _refs(db_session, *tours)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        r = client.post("/api/admin/tours/bulk/attributes",
                        json={"tours": refs, "key": key, "values": ["v"], "mode": "add"}, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert r.status_code == 200, r.text
    first_bump = next(i for i, st in enumerate(statements) if st.startswith("UPDATE tours SET revision"))
    last_commit_side = max(i for i, st in enumerate(statements) if "tour_attributes" in st)
    under_lock = statements[first_bump:last_commit_side + 1]
    assert not [st for st in under_lock if "FROM users" in st]
