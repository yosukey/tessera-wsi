"""Regression tests for the admin tour-step create endpoint.

Focus: admin_add_step's check-then-insert on the UniqueConstraint
(tour_id, order) must surface a concurrent collision as 409, not a 500.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import sqlalchemy.orm as orm
from sqlalchemy.exc import IntegrityError

from wsi_app.db import engine
from wsi_app.models import Step, StepPin


def _make_tour(client, admin_headers, slide) -> int:
    r = client.post(
        f"/api/admin/slides/{slide.id}/tours",
        data={"title": "Test Tour"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_step(client, admin_headers, tour_id, order):
    return client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={
            "order": str(order),
            "center_x": "0.5",
            "center_y": "0.5",
            "fov_um_w": "100",
            "fov_um_h": "100",
        },
        headers=admin_headers,
    )


def test_add_step_deterministic_order_collision_is_409(client, admin_headers, slide):
    tour_id = _make_tour(client, admin_headers, slide)
    assert _add_step(client, admin_headers, tour_id, 1).status_code == 200
    # The pre-check catches an order that's already used.
    r = _add_step(client, admin_headers, tour_id, 1)
    assert r.status_code == 409, r.text


def test_add_step_raced_integrity_error_is_409_not_500(client, admin_headers, slide):
    """Simulate the TOCTOU: a concurrent worker commits the same (tour_id,
    order) between the handler's pre-check SELECT and its write, so the flush trips the
    UniqueConstraint. The handler must map that to 409, not 500."""
    tour_id = _make_tour(client, admin_headers, slide)
    assert _add_step(client, admin_headers, tour_id, 1).status_code == 200

    orig_flush = orm.Session.flush

    def _flush_raises_on_step(self, *args, **kwargs):
        # Fire only for the step insert (a pending Step in the unit of work),
        # mimicking the UNIQUE(tour_id, order) violation a concurrent commit
        # would produce; leave every other flush untouched.
        if any(isinstance(obj, Step) for obj in self.new):
            raise IntegrityError("INSERT INTO steps", {}, Exception("UNIQUE constraint failed"))
        return orig_flush(self, *args, **kwargs)

    # order=2 passes the pre-check ({1} doesn't contain 2), so control reaches
    # the flush where the injected IntegrityError fires.
    with patch.object(orm.Session, "flush", _flush_raises_on_step):
        r = _add_step(client, admin_headers, tour_id, 2)
    assert r.status_code == 409, r.text

    # And the failed insert left nothing behind (rolled back cleanly).
    r = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    orders = [s["order"] for s in r.json()["steps"]]
    assert orders == [1]


def test_annotation_z_round_trips_through_add_update_and_serialize(client, admin_headers, slide):
    """The pin/shape stacking order (z) must persist through create, the PATCH
    full-replace update, and the serialized read envelope."""
    tour_id = _make_tour(client, admin_headers, slide)

    # Create a step carrying a pin and a shape with explicit z values.
    r = client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={
            "order": "1",
            "center_x": "0.5", "center_y": "0.5",
            "fov_um_w": "100", "fov_um_h": "100",
            "pins": json.dumps([{"id": "pinA", "x": 1.0, "y": 2.0, "z": 5}]),
            "shapes_l0": json.dumps([
                {"id": "shA", "kind": "rect", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0, "z": 9}
            ]),
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]

    def _read_step():
        resp = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        return next(s for s in resp.json()["steps"] if s["id"] == step_id)

    step = _read_step()
    assert step["pins"][0]["z"] == 5
    assert step["shapes_l0"][0]["z"] == 9

    # An omitted z falls back to the schema defaults: pins above shapes.
    r = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={
            "pins": [{"id": "pinA", "x": 1.0, "y": 2.0}],
            "shapes_l0": [{"id": "shA", "kind": "rect", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0}],
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    step = _read_step()
    assert step["pins"][0]["z"] > step["shapes_l0"][0]["z"]

    # An explicit reorder (shape above pin) survives the update round-trip.
    r = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={
            "pins": [{"id": "pinA", "x": 1.0, "y": 2.0, "z": 0}],
            "shapes_l0": [{"id": "shA", "kind": "rect", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0, "z": 1}],
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    step = _read_step()
    assert step["shapes_l0"][0]["z"] == 1 and step["pins"][0]["z"] == 0


def test_invalid_pin_shape_is_rejected_on_create_and_update(client, admin_headers, slide):
    """A pin shape outside the frontend's PinShape set 422s on both write paths
    instead of being persisted for the renderer to guess at."""
    tour_id = _make_tour(client, admin_headers, slide)
    base = {
        "order": "1",
        "center_x": "0.5", "center_y": "0.5",
        "fov_um_w": "100", "fov_um_h": "100",
    }
    r = client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={**base, "pins": json.dumps([{"id": "pinA", "x": 1.0, "y": 2.0, "shape": "star"}])},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text

    r = client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={**base, "pins": json.dumps([{"id": "pinA", "x": 1.0, "y": 2.0, "shape": "arrow"}])},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]

    r = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={"pins": [{"id": "pinA", "x": 1.0, "y": 2.0, "shape": "hexagon"}]},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_freehand_smoothing_round_trips_and_defaults_on(client, admin_headers, slide):
    """A freehand shape's per-annotation `smoothing` flag persists through
    create / update and serialization, and defaults to True when omitted."""
    tour_id = _make_tour(client, admin_headers, slide)

    r = client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={
            "order": "1",
            "center_x": "0.5", "center_y": "0.5",
            "fov_um_w": "100", "fov_um_h": "100",
            # shDefault omits `smoothing`; shOff turns it off explicitly.
            "shapes_l0": json.dumps([
                {"id": "shDefault", "kind": "freehand", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0,
                 "points": [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 3.0}, {"x": 4.0, "y": 1.0}]},
                {"id": "shOff", "kind": "freehand", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0,
                 "smoothing": False,
                 "points": [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 3.0}, {"x": 4.0, "y": 1.0}]},
            ]),
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]

    def _read_step():
        resp = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        return next(s for s in resp.json()["steps"] if s["id"] == step_id)

    def _by_id(step):
        return {sh["id"]: sh for sh in step["shapes_l0"]}

    shapes = _by_id(_read_step())
    assert shapes["shDefault"]["smoothing"] is True
    assert shapes["shOff"]["smoothing"] is False

    # The flag survives a full-replace PATCH update in both directions.
    r = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={
            "shapes_l0": [
                {"id": "shDefault", "kind": "freehand", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0,
                 "smoothing": False,
                 "points": [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 3.0}, {"x": 4.0, "y": 1.0}]},
                {"id": "shOff", "kind": "freehand", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0,
                 "smoothing": True,
                 "points": [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 3.0}, {"x": 4.0, "y": 1.0}]},
            ],
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    shapes = _by_id(_read_step())
    assert shapes["shDefault"]["smoothing"] is False
    assert shapes["shOff"]["smoothing"] is True


def test_public_get_tour_with_missing_parent_slide_is_not_500(
    client, admin_headers, db_session
):
    """A tour whose parent slide is gone (restore, hand-edited DB) must not 500.

    The publish gate already 404s an orphaned tour for anonymous callers, but
    staff bypass that gate and would then reach ``parent.public_id`` on a None
    parent. ``admin_tours.admin_get_tour`` guards the same way.
    """
    from wsi_app.models import Slide, Tour

    s = Slide(title="Doomed", catalog_id=f"ORPH-{os.urandom(4).hex()}",
              is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(title="Orphan", slide_id=s.id, is_published=True,
             public_id=f"orph{os.urandom(4).hex()}")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    public_id = t.public_id
    tour_id = t.id

    # Orphan the tour the way a restore or a hand-edited DB would: repoint
    # slide_id at a row that does not exist. Deleting the slide through the ORM
    # would cascade the tour away instead, which is not the case under test.
    # FKs are ON for normal connections, so the pragma is toggled for this one
    # write and restored before the connection returns to the pool.
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("UPDATE tours SET slide_id = ? WHERE id = ?", (2**31, tour_id))
        raw.commit()
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
    db_session.expire_all()

    r = client.get(f"/api/tours/{public_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["tour"]["slide_public_id"] == ""

    # Anonymous callers still get a clean 404 from the parent gate.
    r2 = client.get(f"/api/tours/{public_id}")
    assert r2.status_code == 404, r2.text


def test_step_update_requires_a_label_when_one_is_sent(client, admin_headers, slide):
    """The step editor requires a label: a PATCH that sends an empty or
    whitespace-only label is refused. Create still allows an empty label, and a
    step stored without one stays writable by anything that doesn't send it."""
    tour_id = _make_tour(client, admin_headers, slide)
    r = _add_step(client, admin_headers, tour_id, 1)
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]
    url = f"/api/admin/tours/{tour_id}/steps/{step_id}"

    for blank in ("", "   ", "　"):
        r = client.patch(url, json={"label": blank, "description_md": "x"}, headers=admin_headers)
        assert r.status_code == 422, r.text

    def _read_step():
        resp = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        return next(s for s in resp.json()["steps"] if s["id"] == step_id)

    # Nothing was written by the refused saves.
    assert _read_step()["label"] == ""
    assert _read_step()["description_md"] == ""

    # Writes that leave the label out still land on the unlabeled step.
    r = client.patch(url, json={"description_md": "kept"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.post(f"{url}/pins", json={"id": "", "x": 1.0, "y": 2.0}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.post(
        f"{url}/shapes",
        json={"id": "", "kind": "rect", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _read_step()["label"] == ""

    # A real label saves, and is stored as typed.
    r = client.patch(url, json={"label": " Overview "}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert _read_step()["label"] == " Overview "
