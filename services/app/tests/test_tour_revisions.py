"""Optimistic concurrency for tour editing, and the atomic annotation append.

The step PATCH replaces whole fields (the full pins / shapes_l0 lists
included), so an editor left open in another tab could overwrite every
change saved since it loaded. These tests pin the contract that prevents it:
every tour/step mutation bumps a revision in SQL; a request carrying
``expected_revision`` only applies while that revision is current and
otherwise answers 409 with the server's copy; a request without one stays
unconditional; and "copy to next step" appends server-side so racing copies
commute instead of dropping each other.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from wsi_app.main import app
from wsi_app.models import Slide, Step, StepPin, Tour
from wsi_app.routers import admin_tours

STEP_CONFLICT = "adminTour.apiErr.stepConflict"
TOUR_CONFLICT = "adminTour.apiErr.tourConflict"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_tour(client, headers, slide) -> int:
    r = client.post(f"/api/admin/slides/{slide.id}/tours", data={"title": "T"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_step(client, headers, tour_id, order, **extra):
    data = {
        "order": str(order), "center_x": "10", "center_y": "10",
        "fov_um_w": "100", "fov_um_h": "100", **extra,
    }
    return client.post(f"/api/admin/tours/{tour_id}/steps", data=data, headers=headers)


def _detail(client, headers, tour_id) -> dict:
    r = client.get(f"/api/admin/tours/{tour_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _step(client, headers, tour_id, step_id) -> dict:
    return next(s for s in _detail(client, headers, tour_id)["steps"] if s["id"] == step_id)


def _tour_rev(client, headers, tour_id) -> int:
    return _detail(client, headers, tour_id)["tour"]["revision"]


def _pin(pin_id="", x=1.0, y=2.0, **extra):
    return {"id": pin_id, "x": x, "y": y, **extra}


def _rect(shape_id="", **extra):
    return {"id": shape_id, "kind": "rect", "x": 0.0, "y": 0.0, "w": 5.0, "h": 5.0, **extra}


@pytest.fixture
def tour_with_steps(client, admin_headers, slide):
    """A tour with two steps; returns (tour_id, [step ids in order])."""
    tour_id = _make_tour(client, admin_headers, slide)
    ids = []
    for order in (1, 2):
        r = _add_step(client, admin_headers, tour_id, order)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return tour_id, ids


# ---------------------------------------------------------------------------
# schema: additive columns the boot-time reconciler adds NOT NULL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", [Tour, Step])
def test_revision_column_is_added_not_null_with_default_zero(model):
    """No numbered migration: the reconciler's ADD COLUMN must carry the
    constant default, so existing rows read 0 and the column is NOT NULL on
    an upgraded DB exactly as on a fresh one."""
    from wsi_app.main import _column_add_clause

    clause = _column_add_clause(model.__table__.c.revision)
    assert clause == '"revision" INTEGER DEFAULT 0 NOT NULL'
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO legacy (id) VALUES (1)")
    db.execute(f"ALTER TABLE legacy ADD COLUMN {clause}")
    db.execute("INSERT INTO legacy (id) VALUES (2)")
    assert dict(db.execute("SELECT id, revision FROM legacy")) == {1: 0, 2: 0}
    notnull = {r[1]: r[3] for r in db.execute("PRAGMA table_info(legacy)")}
    assert notnull["revision"] == 1


def test_new_rows_start_at_revision_zero_and_are_reported(client, admin_headers, tour_with_steps):
    tour_id, (s1, _s2) = tour_with_steps
    d = _detail(client, admin_headers, tour_id)
    # Two adds were two structure changes.
    assert d["tour"]["revision"] == 2
    assert [s["revision"] for s in d["steps"]] == [0, 0]
    listed = client.get("/api/admin/tours", headers=admin_headers).json()
    assert next(t for t in listed if t["id"] == tour_id)["revision"] == 2


# ---------------------------------------------------------------------------
# step PATCH
# ---------------------------------------------------------------------------
def test_step_patch_with_stale_revision_is_409_with_current_state_and_no_change(
    client, admin_headers, tour_with_steps
):
    tour_id, (s1, _s2) = tour_with_steps
    url = f"/api/admin/tours/{tour_id}/steps/{s1}"
    # Another window saves first.
    r = client.patch(url, json={"label": "theirs", "pins": [_pin("p1")], "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1

    # This window still holds revision 0 and would replace their pin list.
    r = client.patch(url, json={"label": "mine", "pins": [], "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == STEP_CONFLICT
    assert detail["params"] == {"revision": 1}
    current = detail["current"]
    assert current["id"] == s1 and current["revision"] == 1
    assert current["label"] == "theirs"
    assert [p["id"] for p in current["pins"]] == ["p1"]

    # Nothing was applied and no revision was spent.
    step = _step(client, admin_headers, tour_id, s1)
    assert step["label"] == "theirs" and step["revision"] == 1
    assert [p["id"] for p in step["pins"]] == ["p1"]


def test_step_patch_with_matching_revision_applies_and_bumps(client, admin_headers, tour_with_steps):
    tour_id, (s1, _s2) = tour_with_steps
    tour_rev = _tour_rev(client, admin_headers, tour_id)
    url = f"/api/admin/tours/{tour_id}/steps/{s1}"
    r = client.patch(url, json={"label": "a", "expected_revision": 0}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["step"]["label"] == "a" and body["step"]["revision"] == 1

    # "Overwrite with mine": resend against the server's current revision.
    r = client.patch(url, json={"label": "b", "expected_revision": 1}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    assert _step(client, admin_headers, tour_id, s1)["label"] == "b"
    # Step content is not tour structure.
    assert _tour_rev(client, admin_headers, tour_id) == tour_rev


def test_step_patch_without_expected_revision_is_unconditional_but_bumps(
    client, admin_headers, tour_with_steps
):
    tour_id, (s1, _s2) = tour_with_steps
    url = f"/api/admin/tours/{tour_id}/steps/{s1}"
    for n in (1, 2):
        r = client.patch(url, json={"label": f"script {n}"}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == n
    # ...so an editor still holding revision 0 notices the scripted write.
    r = client.patch(url, json={"label": "editor", "expected_revision": 0}, headers=admin_headers)
    assert r.status_code == 409


def test_step_patch_validation_error_does_not_consume_a_revision(
    client, admin_headers, tour_with_steps
):
    tour_id, (s1, _s2) = tour_with_steps
    url = f"/api/admin/tours/{tour_id}/steps/{s1}"
    r = client.patch(url, json={"pins": [_pin("dup"), _pin("dup")], "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 422
    r = client.patch(url, json={"expected_revision": -1}, headers=admin_headers)
    assert r.status_code == 422
    assert _step(client, admin_headers, tour_id, s1)["revision"] == 0


def test_step_patch_conflict_on_deleted_step_is_404(client, admin_headers, tour_with_steps):
    tour_id, (s1, _s2) = tour_with_steps
    assert client.delete(f"/api/admin/tours/{tour_id}/steps/{s1}", headers=admin_headers).status_code == 200
    r = client.patch(f"/api/admin/tours/{tour_id}/steps/{s1}", json={"label": "x", "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# atomic append
# ---------------------------------------------------------------------------
def test_append_pin_and_shape_return_the_updated_step(client, admin_headers, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    base = f"/api/admin/tours/{tour_id}/steps/{s2}"
    r = client.post(f"{base}/pins", json=_pin("", label="copied", z=7), headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # An empty id is minted server-side, in the editor's "<prefix>-<hex>" shape.
    assert body["id"].startswith("pin-") and len(body["id"]) == len("pin-") + 12
    assert body["revision"] == 1
    assert [(p["id"], p["label"], p["z"]) for p in body["step"]["pins"]] == [(body["id"], "copied", 7)]

    r = client.post(f"{base}/shapes", json=_rect("shape-keep"), headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "shape-keep"  # an unused caller id is kept
    assert r.json()["revision"] == 2
    step = _step(client, admin_headers, tour_id, s2)
    assert [s["id"] for s in step["shapes_l0"]] == ["shape-keep"]
    assert step["revision"] == 2


def test_append_mints_a_fresh_id_when_the_callers_is_taken(client, admin_headers, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    url = f"/api/admin/tours/{tour_id}/steps/{s2}/pins"
    first = client.post(url, json=_pin("pin-same"), headers=admin_headers).json()
    second = client.post(url, json=_pin("pin-same"), headers=admin_headers).json()
    assert first["id"] == "pin-same"
    assert second["id"] != "pin-same" and second["id"].startswith("pin-")
    assert len(second["step"]["pins"]) == 2


def test_appends_from_stale_state_are_both_kept(client, admin_headers, tour_with_steps):
    """The race a client-side read-modify-write loses: two copies both
    built from the same snapshot of the target. Appends commute."""
    tour_id, (_s1, s2) = tour_with_steps
    snapshot = _step(client, admin_headers, tour_id, s2)
    assert snapshot["pins"] == []
    url = f"/api/admin/tours/{tour_id}/steps/{s2}/pins"
    a = client.post(url, json=_pin("pin-a"), headers=admin_headers)
    b = client.post(url, json=_pin("pin-b"), headers=admin_headers)
    assert a.status_code == b.status_code == 200
    step = _step(client, admin_headers, tour_id, s2)
    assert sorted(p["id"] for p in step["pins"]) == ["pin-a", "pin-b"]


def test_concurrent_appends_from_threads_are_all_kept(admin_headers, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    n = 8
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        c = TestClient(app)
        barrier.wait()
        results[i] = c.post(
            f"/api/admin/tours/{tour_id}/steps/{s2}/{'pins' if i % 2 else 'shapes'}",
            json=_pin(f"pin-{i}") if i % 2 else _rect(f"shape-{i}"),
            headers=admin_headers,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert [r.status_code for r in results] == [200] * n, [r.text for r in results]
    # Every append produced its own revision...
    assert sorted(r.json()["revision"] for r in results) == list(range(1, n + 1))
    # ...and every item survived.
    step = _step(TestClient(app), admin_headers, tour_id, s2)
    assert sorted(p["id"] for p in step["pins"]) == sorted(f"pin-{i}" for i in range(1, n, 2))
    assert sorted(s["id"] for s in step["shapes_l0"]) == sorted(f"shape-{i}" for i in range(0, n, 2))
    assert step["revision"] == n


def test_append_makes_an_editor_open_on_the_target_conflict(client, admin_headers, tour_with_steps):
    """The append itself needs no revision, but it bumps the step's, so an
    editor holding the target's old copy can't save over the new item."""
    tour_id, (_s1, s2) = tour_with_steps
    r = client.post(f"/api/admin/tours/{tour_id}/steps/{s2}/pins", json=_pin("pin-new"),
                    headers=admin_headers)
    assert r.status_code == 200
    r = client.patch(f"/api/admin/tours/{tour_id}/steps/{s2}", json={"pins": [], "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 409
    assert [p["id"] for p in r.json()["detail"]["current"]["pins"]] == ["pin-new"]


def test_append_validates_like_the_patch(client, admin_headers, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    base = f"/api/admin/tours/{tour_id}/steps/{s2}"
    too_many_points = _rect(kind="freehand", points=[{"x": i, "y": i} for i in range(4097)])
    assert client.post(f"{base}/shapes", json=too_many_points, headers=admin_headers).status_code == 422
    assert client.post(f"{base}/pins", json=_pin(shape="hexagon"), headers=admin_headers).status_code == 422
    assert client.post(f"{base}/pins", json=_pin(x=float("1e12")), headers=admin_headers).status_code == 422
    assert client.post(f"{base}/pins", json=_pin("x" * 65), headers=admin_headers).status_code == 422
    assert client.post(f"{base}/shapes", json=_rect(w=0), headers=admin_headers).status_code == 422
    # A rejected append changed nothing.
    assert _step(client, admin_headers, tour_id, s2)["revision"] == 0


def test_append_respects_the_per_step_cap(client, admin_headers, tour_with_steps, monkeypatch):
    tour_id, (_s1, s2) = tour_with_steps
    monkeypatch.setattr(admin_tours, "_MAX_STEP_PINS", 2)
    url = f"/api/admin/tours/{tour_id}/steps/{s2}/pins"
    for _ in range(2):
        assert client.post(url, json=_pin(), headers=admin_headers).status_code == 200
    r = client.post(url, json=_pin(), headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "adminTour.apiErr.annotationLimit", "params": {"max": 2}}
    step = _step(client, admin_headers, tour_id, s2)
    assert len(step["pins"]) == 2 and step["revision"] == 2


def test_step_writes_cap_annotation_counts(client, admin_headers, tour_with_steps):
    from wsi_app.schemas import _MAX_STEP_PINS, _MAX_STEP_SHAPES

    tour_id, (s1, _s2) = tour_with_steps
    too_many = [_pin(f"p{i}") for i in range(_MAX_STEP_PINS + 1)]
    r = client.patch(f"/api/admin/tours/{tour_id}/steps/{s1}", json={"pins": too_many},
                     headers=admin_headers)
    assert r.status_code == 422
    import json as _json
    r = _add_step(client, admin_headers, tour_id, 3,
                  shapes_l0=_json.dumps([_rect(f"s{i}") for i in range(_MAX_STEP_SHAPES + 1)]))
    assert r.status_code == 422


def test_append_is_scoped_to_the_tour(client, admin_headers, slide, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    other = _make_tour(client, admin_headers, slide)
    r = client.post(f"/api/admin/tours/{other}/steps/{s2}/pins", json=_pin(), headers=admin_headers)
    assert r.status_code == 404


def test_append_requires_staff(client, tour_with_steps):
    tour_id, (_s1, s2) = tour_with_steps
    r = client.post(f"/api/admin/tours/{tour_id}/steps/{s2}/pins", json=_pin())
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# structure operations: Tour.revision
# ---------------------------------------------------------------------------
def test_add_step_checks_and_bumps_the_tour_revision(client, admin_headers, tour_with_steps):
    tour_id, _ids = tour_with_steps
    rev = _tour_rev(client, admin_headers, tour_id)
    r = _add_step(client, admin_headers, tour_id, 3, expected_revision=str(rev - 1))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == TOUR_CONFLICT and detail["params"] == {"revision": rev}
    assert detail["current"]["tour"]["revision"] == rev
    assert len(detail["current"]["steps"]) == 2
    assert len(_detail(client, admin_headers, tour_id)["steps"]) == 2  # nothing added

    r = _add_step(client, admin_headers, tour_id, 3, expected_revision=str(rev))
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == rev + 1
    new_step = _step(client, admin_headers, tour_id, r.json()["id"])
    assert new_step["revision"] == 0


def test_reorder_checks_and_bumps_the_tour_revision(client, admin_headers, tour_with_steps):
    tour_id, (s1, s2) = tour_with_steps
    rev = _tour_rev(client, admin_headers, tour_id)
    url = f"/api/admin/tours/{tour_id}/steps/reorder"
    r = client.patch(url, json={"step_ids": [s2, s1], "expected_revision": rev + 5}, headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == TOUR_CONFLICT
    assert [s["id"] for s in _detail(client, admin_headers, tour_id)["steps"]] == [s1, s2]

    r = client.patch(url, json={"step_ids": [s2, s1], "expected_revision": rev}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == rev + 1
    d = _detail(client, admin_headers, tour_id)
    assert [s["id"] for s in d["steps"]] == [s2, s1]
    # order is structure, not step content
    assert [s["revision"] for s in d["steps"]] == [0, 0]

    # Unconditional (no expected_revision) still works and bumps.
    r = client.patch(url, json={"step_ids": [s1, s2]}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == rev + 2


def test_reorder_rejects_a_stale_step_set_without_consuming_a_revision(
    client, admin_headers, tour_with_steps
):
    tour_id, (s1, s2) = tour_with_steps
    rev = _tour_rev(client, admin_headers, tour_id)
    r = client.patch(f"/api/admin/tours/{tour_id}/steps/reorder", json={"step_ids": [s1]},
                     headers=admin_headers)
    assert r.status_code == 400
    assert _tour_rev(client, admin_headers, tour_id) == rev


def test_delete_step_checks_and_bumps_the_tour_revision(client, admin_headers, tour_with_steps):
    tour_id, (s1, s2) = tour_with_steps
    rev = _tour_rev(client, admin_headers, tour_id)
    r = client.delete(f"/api/admin/tours/{tour_id}/steps/{s1}",
                      params={"expected_revision": rev - 1}, headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == TOUR_CONFLICT
    assert [s["id"] for s in r.json()["detail"]["current"]["steps"]] == [s1, s2]
    assert len(_detail(client, admin_headers, tour_id)["steps"]) == 2

    r = client.delete(f"/api/admin/tours/{tour_id}/steps/{s1}",
                      params={"expected_revision": rev}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "revision": rev + 1}
    d = _detail(client, admin_headers, tour_id)
    assert [(s["id"], s["order"]) for s in d["steps"]] == [(s2, 1)]

    r = client.delete(f"/api/admin/tours/{tour_id}/steps/{s2}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == rev + 2


def test_step_content_writes_leave_the_tour_revision_alone(client, admin_headers, tour_with_steps):
    tour_id, (s1, s2) = tour_with_steps
    rev = _tour_rev(client, admin_headers, tour_id)
    client.patch(f"/api/admin/tours/{tour_id}/steps/{s1}", json={"label": "x"}, headers=admin_headers)
    client.post(f"/api/admin/tours/{tour_id}/steps/{s2}/pins", json=_pin(), headers=admin_headers)
    assert _tour_rev(client, admin_headers, tour_id) == rev
    # ...so a tour edit built from the old tour revision still applies.
    r = client.patch(f"/api/admin/tours/{tour_id}", json={"title": "New", "expected_revision": rev},
                     headers=admin_headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# tour fields
# ---------------------------------------------------------------------------
def test_tour_patch_with_stale_revision_is_409_and_unchanged(client, admin_headers, slide):
    tour_id = _make_tour(client, admin_headers, slide)
    url = f"/api/admin/tours/{tour_id}"
    r = client.patch(url, json={"title": "Theirs", "expected_revision": 0}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["tour"]["title"] == "Theirs" and body["tour"]["revision"] == 1

    r = client.patch(url, json={"title": "Mine", "expected_revision": 0}, headers=admin_headers)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == TOUR_CONFLICT
    assert detail["current"]["tour"]["title"] == "Theirs"
    assert detail["current"]["steps"] == []
    assert _detail(client, admin_headers, tour_id)["tour"]["title"] == "Theirs"

    r = client.patch(url, json={"title": "Mine", "expected_revision": 1}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == 2
    r = client.patch(url, json={"description": "unconditional"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == 3


def test_tour_patch_invalid_title_does_not_consume_a_revision(client, admin_headers, slide):
    tour_id = _make_tour(client, admin_headers, slide)
    r = client.patch(f"/api/admin/tours/{tour_id}", json={"title": "  ", "expected_revision": 0},
                     headers=admin_headers)
    assert r.status_code == 422
    assert _tour_rev(client, admin_headers, tour_id) == 0


def test_publish_and_unlisted_toggles_check_and_bump(client, admin_headers, db_session):
    s = Slide(title="Pub", catalog_id="P-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    tour_id = _make_tour(client, admin_headers, s)

    r = client.post(f"/api/admin/tours/{tour_id}/publish",
                    data={"publish": "1", "expected_revision": "3"}, headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == TOUR_CONFLICT
    assert _detail(client, admin_headers, tour_id)["tour"]["is_published"] is False

    r = client.post(f"/api/admin/tours/{tour_id}/publish",
                    data={"publish": "1", "expected_revision": "0"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "is_published": True, "revision": 1}

    r = client.post(f"/api/admin/tours/{tour_id}/unlisted",
                    data={"unlisted": "1", "expected_revision": "0"}, headers=admin_headers)
    assert r.status_code == 409
    r = client.post(f"/api/admin/tours/{tour_id}/unlisted",
                    data={"unlisted": "1", "expected_revision": "1"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unlisted": True, "revision": 2}

    # Unconditional toggles (scripts, non-SPA callers) still work and bump.
    r = client.post(f"/api/admin/tours/{tour_id}/publish", data={"publish": "0"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == 3


def test_slide_unpublish_cascade_bumps_the_cascaded_tours(client, admin_headers, db_session, attach_wsi):
    s = Slide(title="Casc", catalog_id="C-9", is_published=True)
    db_session.add(s)
    db_session.commit()
    attach_wsi(s)
    published = _make_tour(client, admin_headers, s)
    draft = _make_tour(client, admin_headers, s)
    r = client.post(f"/api/admin/tours/{published}/publish", data={"publish": "1"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["revision"] == 1

    r = client.post(f"/api/admin/slides/{s.id}/publish", data={"publish": "0"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["cascaded_tours"] == 1
    assert _tour_rev(client, admin_headers, published) == 2
    assert _tour_rev(client, admin_headers, draft) == 0  # untouched, not bumped
    # An editor still showing "published" gets a 409, not a silent no-op.
    r = client.post(f"/api/admin/tours/{published}/unlisted",
                    data={"unlisted": "1", "expected_revision": "1"}, headers=admin_headers)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# what the revision is not exposed to / carried by
# ---------------------------------------------------------------------------
def test_public_tour_omits_the_tour_revision(client, admin_headers, db_session):
    s = Slide(title="Pub", catalog_id="P-2", is_published=True)
    db_session.add(s)
    db_session.commit()
    tour_id = _make_tour(client, admin_headers, s)
    client.post(f"/api/admin/tours/{tour_id}/publish", data={"publish": "1"}, headers=admin_headers)
    public_id = _detail(client, admin_headers, tour_id)["tour"]["public_id"]
    r = client.get(f"/api/tours/{public_id}")
    assert r.status_code == 200, r.text
    assert r.json()["tour"]["revision"] is None


def test_export_bundle_drops_the_step_revision(client, admin_headers, db_session, tour_with_steps):
    from wsi_app.slide_transfer import _serialize_tour

    tour_id, (s1, _s2) = tour_with_steps
    client.patch(f"/api/admin/tours/{tour_id}/steps/{s1}", json={"label": "x"}, headers=admin_headers)
    db_session.expire_all()
    out = _serialize_tour(db_session.get(Tour, tour_id))
    assert out["steps"] and all("revision" not in st for st in out["steps"])
    assert "revision" not in out


def test_imported_style_rows_start_at_zero(db_session, slide):
    """Rows built by the ORM without an explicit revision (slide import,
    fixtures) get the default."""
    t = Tour(slide_id=slide.id, title="Imported")
    db_session.add(t)
    db_session.flush()
    st = Step(tour_id=t.id, order=1)
    db_session.add(st)
    db_session.flush()
    db_session.add(StepPin(step_id=st.id, pin_id="p"))
    db_session.commit()
    db_session.refresh(t)
    db_session.refresh(st)
    assert (t.revision, st.revision) == (0, 0)


# ---------------------------------------------------------------------------
# Races around the bump: a stale pre-bump load, a delete right after commit
# ---------------------------------------------------------------------------
def _commit_before_bump(monkeypatch, fn):
    """Run ``fn(session)`` in another session and commit it just before the
    handler's revision bump — i.e. after the handler loaded its row."""
    from wsi_app.db import SessionLocal

    real = admin_tours._bump_revision
    fired = []

    def wrapped(db, model, row_id, expected):
        if not fired:
            fired.append(1)
            with SessionLocal() as other:
                fn(other)
                other.commit()
        return real(db, model, row_id, expected)

    monkeypatch.setattr(admin_tours, "_bump_revision", wrapped)


def test_unconditional_unpublish_is_not_lost_to_a_stale_load(
    client, admin_headers, db_session, monkeypatch
):
    """Without expected_revision the write is unconditional — and must still
    land when another commit changed the row between the handler's load and
    its bump (an assignment equal to the stale value is no ORM change)."""
    from sqlalchemy import update

    s = Slide(title="pub", is_published=True)
    db_session.add(s)
    db_session.commit()
    tid = _make_tour(client, admin_headers, s)
    _commit_before_bump(monkeypatch, lambda o: o.execute(
        update(Tour).where(Tour.id == tid).values(is_published=True, revision=Tour.revision + 1)
    ))
    r = client.post(f"/api/admin/tours/{tid}/publish", data={"publish": "0"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert _detail(client, admin_headers, tid)["tour"]["is_published"] is False


def test_unconditional_step_patch_is_not_lost_to_a_stale_load(
    client, admin_headers, slide, monkeypatch
):
    from sqlalchemy import update

    tid = _make_tour(client, admin_headers, slide)
    sid = _add_step(client, admin_headers, tid, 1, label="old").json()["id"]
    _commit_before_bump(monkeypatch, lambda o: o.execute(
        update(Step).where(Step.id == sid).values(label="new", revision=Step.revision + 1)
    ))
    r = client.patch(f"/api/admin/tours/{tid}/steps/{sid}", json={"label": "old"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["step"]["label"] == "old"
    assert _step(client, admin_headers, tid, sid)["label"] == "old"


def test_publish_rechecks_the_parent_under_the_lock(client, admin_headers, db_session, monkeypatch):
    """A slide unpublish committed after the handler's load but before its
    bump must still refuse the tour publish (no published tour on a draft)."""
    from sqlalchemy import update

    s = Slide(title="pub2", is_published=True)
    db_session.add(s)
    db_session.commit()
    tid = _make_tour(client, admin_headers, s)
    _commit_before_bump(monkeypatch, lambda o: o.execute(
        update(Slide).where(Slide.id == s.id).values(is_published=False)
    ))
    r = client.post(f"/api/admin/tours/{tid}/publish", data={"publish": "1"}, headers=admin_headers)
    assert r.status_code == 409, r.text
    assert _detail(client, admin_headers, tid)["tour"]["is_published"] is False


@pytest.mark.parametrize("what", ["step_patch", "append", "tour_patch"])
def test_a_delete_right_after_commit_does_not_turn_a_saved_write_into_500(
    admin_headers, slide, monkeypatch, what
):
    """The response is built before the commit, under the write lock, so a
    concurrent delete landing between commit and response can't 500 it."""
    from sqlalchemy import delete

    from wsi_app.db import SessionLocal

    c = TestClient(app, raise_server_exceptions=False)
    tid = _make_tour(c, admin_headers, slide)
    sid = _add_step(c, admin_headers, tid, 1).json()["id"]
    model, row_id = (Tour, tid) if what == "tour_patch" else (Step, sid)
    real_audit = admin_tours.audit

    def audit_then_delete(*a, **kw):
        with SessionLocal() as other:
            other.execute(delete(model).where(model.id == row_id))
            other.commit()
        return real_audit(*a, **kw)

    monkeypatch.setattr(admin_tours, "audit", audit_then_delete)
    if what == "step_patch":
        r = c.patch(f"/api/admin/tours/{tid}/steps/{sid}", json={"label": "x", "expected_revision": 0}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["step"]["label"] == "x"
    elif what == "append":
        r = c.post(f"/api/admin/tours/{tid}/steps/{sid}/pins", json=_pin(), headers=admin_headers)
        assert r.status_code == 200, r.text
        assert len(r.json()["step"]["pins"]) == 1
    else:
        rev = _tour_rev(c, admin_headers, tid)
        r = c.patch(f"/api/admin/tours/{tid}", json={"title": "New", "expected_revision": rev}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["tour"]["title"] == "New"


def test_tour_publish_landing_inside_a_slide_unpublish_is_cascaded(
    client, admin_headers, admin_user, db_session, attach_wsi
):
    """A tour publish committing while a slide unpublish is under way must
    still be cascaded to a draft — not left published on a draft slide, to
    go public again on its own with the slide's next publish. A cascade that
    read the slide's tours (outside the write lock) and then wrote would miss
    a publish landing right after that read. Fired here from the
    moment any tour row is loaded during the unpublish."""
    from sqlalchemy import event

    from wsi_app.db import SessionLocal
    from wsi_app.models import User

    s = Slide(title="Race", catalog_id="R-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    attach_wsi(s)
    tour_id = _make_tour(client, admin_headers, s)
    fired = []

    def publish_concurrently(target, context):
        if fired:
            return
        fired.append(1)
        other = SessionLocal()
        try:
            admin_tours.admin_publish_tour(
                tour_id, publish=1, expected_revision=None,
                admin=other.get(User, admin_user.id), db=other,
            )
        finally:
            other.close()

    event.listen(Tour, "load", publish_concurrently)
    try:
        r = client.post(f"/api/admin/slides/{s.id}/publish", data={"publish": "0"}, headers=admin_headers)
    finally:
        event.remove(Tour, "load", publish_concurrently)
    assert r.status_code == 200, r.text
    assert _detail(client, admin_headers, tour_id)["tour"]["is_published"] is False
