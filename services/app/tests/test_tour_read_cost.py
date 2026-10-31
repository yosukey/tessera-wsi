"""What one tour read can cost.

Every read of a tour — including the unauthenticated public GET of a
published one — serialises every freehand vertex of every step. The writers
therefore bound a step's total vertices (_MAX_STEP_FREEHAND_POINTS), a tour's
(_MAX_TOUR_FREEHAND_POINTS, both on create, PATCH and append) and a tour's
step count (_MAX_TOUR_STEPS), and the read path serialises the stored rows
without re-running the input validators or decoding stored vertices.
"""
from __future__ import annotations

import json
import random

import pytest
import sqlalchemy as sa
import sqlalchemy.orm as orm

from wsi_app import _serializers, schemas
from wsi_app._serializers import decode_points, json_text, step_to_out
from wsi_app.db import engine
from wsi_app.models import Step, StepPin, StepShape
from wsi_app.routers import admin_tours
from wsi_app.schemas import StepOut


def _make_tour(client, headers, slide) -> int:
    r = client.post(f"/api/admin/slides/{slide.id}/tours", data={"title": "T"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_step(client, headers, tour_id, order, **extra):
    data = {
        "order": str(order), "center_x": "0.5", "center_y": "0.5",
        "fov_um_w": "100", "fov_um_h": "100", **extra,
    }
    return client.post(f"/api/admin/tours/{tour_id}/steps", data=data, headers=headers)


def _freehand(sid: str, n: int) -> dict:
    return {
        "id": sid, "kind": "freehand", "x": 0, "y": 0, "w": 10, "h": 10,
        "points": [{"x": float(i), "y": float(i)} for i in range(n)],
    }


def test_step_point_budget_on_create_patch_and_append(client, admin_headers, slide, monkeypatch):
    monkeypatch.setattr(admin_tours, "_MAX_STEP_FREEHAND_POINTS", 10)
    tour_id = _make_tour(client, admin_headers, slide)

    over = _add_step(
        client, admin_headers, tour_id, 1,
        shapes_l0=json.dumps([_freehand("a", 6), _freehand("b", 5)]),
    )
    assert over.status_code == 422
    assert over.json()["detail"] == {
        "code": "adminTour.apiErr.pointBudget", "params": {"max": 10},
    }
    r = _add_step(
        client, admin_headers, tour_id, 1,
        shapes_l0=json.dumps([_freehand("a", 6), _freehand("b", 4)]),
    )
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]

    url = f"/api/admin/tours/{tour_id}/steps/{step_id}"
    over = client.patch(
        url, json={"shapes_l0": [_freehand("a", 6), _freehand("b", 5)]}, headers=admin_headers,
    )
    assert over.status_code == 422
    assert over.json()["detail"]["code"] == "adminTour.apiErr.pointBudget"
    ok = client.patch(url, json={"shapes_l0": [_freehand("a", 9)]}, headers=admin_headers)
    assert ok.status_code == 200, ok.text

    # Append: the step already holds 9 vertices.
    over = client.post(f"{url}/shapes", json=_freehand("c", 2), headers=admin_headers)
    assert over.status_code == 409
    assert over.json()["detail"] == {
        "code": "adminTour.apiErr.pointBudget", "params": {"max": 10},
    }
    assert client.post(f"{url}/shapes", json=_freehand("c", 1), headers=admin_headers).status_code == 200
    # Shapes without vertices and pins don't count against it.
    rect = {"id": "r", "kind": "rect", "x": 0, "y": 0, "w": 1, "h": 1}
    assert client.post(f"{url}/shapes", json=rect, headers=admin_headers).status_code == 200
    pin = {"id": "p", "x": 1, "y": 1}
    assert client.post(f"{url}/pins", json=pin, headers=admin_headers).status_code == 200


def test_step_count_cap(client, admin_headers, slide, monkeypatch):
    monkeypatch.setattr(admin_tours, "_MAX_TOUR_STEPS", 2)
    tour_id = _make_tour(client, admin_headers, slide)
    assert _add_step(client, admin_headers, tour_id, 1).status_code == 200
    assert _add_step(client, admin_headers, tour_id, 2).status_code == 200
    r = _add_step(client, admin_headers, tour_id, 3)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "adminTour.apiErr.stepLimit", "params": {"max": 2}}
    # The refused add spent no revision.
    tour = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers).json()["tour"]
    assert tour["revision"] == 2


def _add_step_multipart(client, headers, tour_id, order, **fields):
    """POST the add-step form as multipart text fields, as the editor's
    FormData does (``data=`` alone would be urlencoded)."""
    base = {
        "order": str(order), "center_x": "0.5", "center_y": "0.5",
        "fov_um_w": "100", "fov_um_h": "100", **fields,
    }
    return client.post(
        f"/api/admin/tours/{tour_id}/steps",
        files={k: (None, v) for k, v in base.items()},
        headers=headers,
    )


def test_step_create_takes_a_step_at_its_point_budget(client, admin_headers, slide):
    """The editor duplicates a step by posting its shapes back to create. A
    step at the per-step budget, with the full-precision coordinates a drawn
    stroke has, is a shapes_l0 field of over 2 MB: past Starlette's default
    1 MiB per form field, which would refuse it (400) though PATCH saves it."""
    tour_id = _make_tour(client, admin_headers, slide)
    rnd = random.Random(1)
    budget = schemas._MAX_STEP_FREEHAND_POINTS
    per_shape = 4000
    shapes = [
        {
            "id": f"s{i}", "kind": "freehand", "x": 0, "y": 0, "w": 10, "h": 10,
            "points": [
                {"x": rnd.uniform(0, 150_000), "y": rnd.uniform(0, 150_000)}
                for _ in range(min(per_shape, budget - i))
            ],
        }
        for i in range(0, budget, per_shape)
    ]
    body = json.dumps(shapes, separators=(",", ":"))
    assert len(body) > 2 * 1024 * 1024

    r = _add_step_multipart(client, admin_headers, tour_id, 1, shapes_l0=body)
    assert r.status_code == 200, r.text
    steps = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers).json()["steps"]
    assert sum(len(sh["points"]) for sh in steps[0]["shapes_l0"]) == budget


def test_step_create_form_stays_bounded(client, admin_headers, slide, monkeypatch):
    """The raised per-field cap still caps every field, and the form takes no
    more fields than the route has, and no file."""
    tour_id = _make_tour(client, admin_headers, slide)
    monkeypatch.setattr(admin_tours, "_STEP_FORM_MAX_PART", 1024)
    r = _add_step_multipart(client, admin_headers, tour_id, 1, description_md="x" * 1025)
    assert r.status_code == 400
    assert "maximum size" in r.json()["detail"]
    r = _add_step_multipart(client, admin_headers, tour_id, 1, description_md="x" * 1000)
    assert r.status_code == 200, r.text

    extra = {f"junk{i}": "x" for i in range(admin_tours._STEP_FORM_MAX_FIELDS)}
    r = _add_step_multipart(client, admin_headers, tour_id, 2, **extra)
    assert r.status_code == 400
    assert "Too many fields" in r.json()["detail"]

    r = client.post(
        f"/api/admin/tours/{tour_id}/steps",
        data={"order": "2", "center_x": "0.5", "center_y": "0.5", "fov_um_w": "1", "fov_um_h": "1"},
        files={"shapes_l0": ("shapes.json", b"[]", "application/json")},
        headers=admin_headers,
    )
    assert r.status_code == 400
    assert "Too many files" in r.json()["detail"]


TOUR_BUDGET = {"code": "adminTour.apiErr.tourPointBudget", "params": {"max": 10}}


def _revisions(client, headers, tour_id):
    d = client.get(f"/api/admin/tours/{tour_id}", headers=headers).json()
    return d["tour"]["revision"], {s["id"]: s["revision"] for s in d["steps"]}


def test_tour_point_budget_on_create_patch_and_append(client, admin_headers, slide, monkeypatch):
    """The per-step budget times the step cap would still let one tour hold 5M
    vertices, every one of them rendered by each (anonymous) read. The tour's
    total is bounded too, on every writer; what counts is the tour's other
    steps plus what the write leaves in this one."""
    monkeypatch.setattr(admin_tours, "_MAX_TOUR_FREEHAND_POINTS", 10)
    tour_id = _make_tour(client, admin_headers, slide)
    r = _add_step(client, admin_headers, tour_id, 1, shapes_l0=json.dumps([_freehand("a", 6)]))
    assert r.status_code == 200, r.text
    s1 = r.json()["id"]

    # Create (and so duplicate, which the editor does through create).
    before = _revisions(client, admin_headers, tour_id)
    over = _add_step(client, admin_headers, tour_id, 2, shapes_l0=json.dumps([_freehand("a", 5)]))
    assert over.status_code == 409
    assert over.json()["detail"] == TOUR_BUDGET
    assert _revisions(client, admin_headers, tour_id) == before
    r = _add_step(client, admin_headers, tour_id, 2, shapes_l0=json.dumps([_freehand("a", 4)]))
    assert r.status_code == 200, r.text
    s2 = r.json()["id"]
    # At the budget, a step without vertices can still be added.
    rect = {"id": "r", "kind": "rect", "x": 0, "y": 0, "w": 1, "h": 1}
    r = _add_step(client, admin_headers, tour_id, 3, shapes_l0=json.dumps([rect]))
    assert r.status_code == 200, r.text
    s3 = r.json()["id"]

    # PATCH: the step's own stored vertices are replaced, not added to.
    url2 = f"/api/admin/tours/{tour_id}/steps/{s2}"
    before = _revisions(client, admin_headers, tour_id)
    over = client.patch(url2, json={"shapes_l0": [_freehand("a", 5)]}, headers=admin_headers)
    assert over.status_code == 409
    assert over.json()["detail"] == TOUR_BUDGET
    assert _revisions(client, admin_headers, tour_id) == before
    for n in (3, 4):
        r = client.patch(url2, json={"shapes_l0": [_freehand("a", n)]}, headers=admin_headers)
        assert r.status_code == 200, r.text

    # Append, onto a step with no vertices of its own.
    url3 = f"/api/admin/tours/{tour_id}/steps/{s3}"
    before = _revisions(client, admin_headers, tour_id)
    over = client.post(f"{url3}/shapes", json=_freehand("b", 1), headers=admin_headers)
    assert over.status_code == 409
    assert over.json()["detail"] == TOUR_BUDGET
    assert _revisions(client, admin_headers, tour_id) == before
    rect2 = {**rect, "id": "r2"}
    assert client.post(f"{url3}/shapes", json=rect2, headers=admin_headers).status_code == 200
    assert client.post(f"{url3}/pins", json={"id": "p", "x": 1, "y": 1}, headers=admin_headers).status_code == 200

    # A tour already past the budget (e.g. after the budget is lowered) stays
    # editable: only a write that adds vertices is refused.
    monkeypatch.setattr(admin_tours, "_MAX_TOUR_FREEHAND_POINTS", 5)
    url1 = f"/api/admin/tours/{tour_id}/steps/{s1}"
    r = client.patch(url1, json={"label": "x", "shapes_l0": [_freehand("a", 6)]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.patch(url1, json={"shapes_l0": [_freehand("a", 2)]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    over = client.patch(url1, json={"shapes_l0": [_freehand("a", 3)]}, headers=admin_headers)
    assert over.status_code == 409
    assert over.json()["detail"] == {**TOUR_BUDGET, "params": {"max": 5}}


def test_tour_point_budget_counts_other_tours_out(client, admin_headers, slide, monkeypatch):
    monkeypatch.setattr(admin_tours, "_MAX_TOUR_FREEHAND_POINTS", 10)
    for _ in range(2):
        tour_id = _make_tour(client, admin_headers, slide)
        r = _add_step(client, admin_headers, tour_id, 1, shapes_l0=json.dumps([_freehand("a", 10)]))
        assert r.status_code == 200, r.text


def test_read_serialises_stored_rows_without_revalidating(client, admin_headers, slide):
    """Stored rows were validated on write; reads emit them as stored. A row
    a validator would refuse (a control character a hand edit left in a label)
    still reads instead of failing the whole tour."""
    tour_id = _make_tour(client, admin_headers, slide)
    r = _add_step(
        client, admin_headers, tour_id, 1,
        shapes_l0=json.dumps([_freehand("a", 3)]),
        pins=json.dumps([{"id": "p", "x": 1, "y": 2, "label": "pin"}]),
    )
    step_id = r.json()["id"]
    with orm.Session(engine) as db:
        db.add(StepShape(
            shape_id="handmade", step_id=step_id, kind="rect", x=1, y=1, w=2, h=2,
            stroke_color="red", fill_enabled=False, fill_opacity=0.5, points_json="",
            smoothing=True, label="two\nlines", label_position="above", z=0,
        ))
        db.commit()

    admin = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
    assert admin.status_code == 200, admin.text
    step = admin.json()["steps"][0]
    assert [sh["label"] for sh in step["shapes_l0"]] == ["", "two\nlines"]
    assert step["shapes_l0"][0]["points"] == [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}]
    # Staff preview of the draft through the public route: same steps.
    public = client.get(
        f"/api/tours/{admin.json()['tour']['public_id']}", headers=admin_headers,
    )
    assert public.status_code == 200, public.text
    assert public.json()["steps"] == admin.json()["steps"]
    assert public.json()["tour"]["revision"] is None


def test_step_to_out_matches_the_stepout_contract(client, admin_headers, slide):
    """step_to_out builds the StepOut shape by hand; for a valid row it must
    be exactly what the model would have produced."""
    tour_id = _make_tour(client, admin_headers, slide)
    r = _add_step(
        client, admin_headers, tour_id, 1, label="Intro", description_md="**md**",
        transition_mode="smooth_zoom",
        shapes_l0=json.dumps([_freehand("a", 4), {
            "id": "c", "kind": "circle", "x": 1, "y": 2, "w": 3, "h": 4,
            "stroke_color": "#00ff00", "fill_enabled": True, "fill_opacity": 0.25,
            "label": "c", "label_position": "below", "z": 5,
        }]),
        pins=json.dumps([{"id": "p", "x": 1, "y": 2, "shape": "arrow", "color": "blue",
                          "rotation_deg": 45, "label": "pin", "z": 7}]),
    )
    assert r.status_code == 200, r.text
    with orm.Session(engine) as db:
        out = step_to_out(db.get(Step, r.json()["id"]))
    assert StepOut.model_validate(out).model_dump() == out
    assert json.loads(json.dumps(out)) == out


def _jsonresponse_body(content) -> bytes:
    """What a JSONResponse sends for ``content`` — the plain rendering the
    tour reads, which splice stored vertices in, must match."""
    return json.dumps(
        content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"),
    ).encode("utf-8")


_EDGE_FLOATS = [
    0.0, -0.0, 1.0, -1.0, 0.5, 0.1, 0.30000000000000004, 1e-05, -1.5e-07, 0.0001,
    123456789.0, 1e9, -1e9, 999999999.9999999, 12345.678901234567, 5e-324,
    2.2250738585072014e-308, 1e-300, 1e16, 1.2345678901234568e+17, 1e+100,
    1.7976931348623157e+308,
]


def _random_floats(rng: random.Random, n: int) -> list:
    out = []
    for _ in range(n):
        kind = rng.randrange(5)
        if kind == 0:
            out.append(rng.uniform(-1e5, 1e5))
        elif kind == 4:
            out.append(rng.uniform(-10, 10))
        elif kind == 1:
            out.append(rng.uniform(-1, 1) * 10 ** rng.randint(-320, 300))
        elif kind == 2:
            out.append(float(rng.randint(-10**9, 10**9)))
        else:
            out.append(round(rng.uniform(0, 1e5), rng.randint(0, 6)))
    return out


def test_stored_points_render_exactly_like_decoded_points():
    """The read splices stored vertices in as text; for everything the
    writers store (json.dumps of floats within schemas._COORD_MAX) that is
    exactly what decoding and re-encoding them produced. Larger values, which
    no writer stores, are decoded instead."""
    rng = random.Random(20261008)
    values = _EDGE_FLOATS + _random_floats(rng, 20_000)
    stored = [v for v in values if abs(v) <= schemas._COORD_MAX]
    assert len(stored) > 15_000
    for i in range(0, len(stored), 500):
        chunk = stored[i:i + 500]
        raw = json.dumps([{"x": x, "y": y} for x, y in zip(chunk, reversed(chunk))])
        assert _serializers._stored_points_json(raw) == json_text(decode_points(raw))
    for v in values:
        if abs(v) > schemas._COORD_MAX:
            raw = json.dumps([{"x": v, "y": 1.0}])
            spliced = _serializers._stored_points_json(raw)
            assert spliced is None or spliced == json_text(decode_points(raw))


@pytest.mark.parametrize("raw", [
    "", "[]", "not json", '{"x": 1.0, "y": 2.0}',
    '[{"x": 1, "y": 2}]', '[{"x": 1.50, "y": 2.0}]', '[{"x": 1e5, "y": 2.0}]',
    '[{"x": 01.0, "y": 2.0}]', '[{"x": 1.0, "y": 2.0, "z": 3.0}]', '[{"y": 1.0, "x": 2.0}]',
    '[{"x":1.0,"y":2.0}]', '[{"x": "1.0", "y": 2.0}]', '[{"x": NaN, "y": 1.0}]',
    '[{"x": 1.0, "y": 2.0}] ', '[{"x": ١.0, "y": 2.0}]', '[{"x": 1.0, "y": 2.0},]',
    # Past what a finite literal of the writers' shape can hold.
    '[{"x": 1e+400, "y": 2.0}]', '[{"x": 9.9e+308, "y": 2.0}]', '[{"x": 1e+16, "y": 2.0}]',
    '[{"x": 12345678901234567.0, "y": 2.0}]',
    b'[{"x": 1.0, "y": 2.0}]',
])
def test_points_not_in_the_writers_form_are_decoded(raw):
    """Anything a writer would not have stored (differently spaced JSON, hand
    edits, a BLOB in the column) takes the decoding path, which normalises
    it — so no literal that overflows to Infinity reaches a browser."""
    assert _serializers._stored_points_json(raw) is None


def test_decoding_skips_vertices_no_response_can_carry():
    """A hand-edited literal that overflows decodes to inf, which JSON cannot
    carry: the vertex is skipped instead of failing the whole tour read."""
    raw = '[{"x": 1e+400, "y": 1.0}, {"x": 2.0, "y": -1e999}, {"x": 3.0, "y": 4.0}]'
    assert decode_points(raw) == [{"x": 3.0, "y": 4.0}]


def test_tour_reads_render_byte_for_byte_like_step_to_out(client, admin_headers, slide):
    """The tour reads render the StepOut objects step_to_out builds, byte for
    byte as JSONResponse rendered them — writer-stored vertices spliced in as
    text, other rows decoded, alike."""
    tour_id = _make_tour(client, admin_headers, slide)
    coords = [v for v in _EDGE_FLOATS if abs(v) <= 1e9]
    freehand = {
        "id": "f", "kind": "freehand", "x": 0, "y": 0, "w": 10, "h": 10, "label": "auf ", "z": 3,
        "points": [{"x": x, "y": y} for x, y in zip(coords, reversed(coords))],
    }
    tricky = '標本 ✓ "q" a, b: c \\  '
    r = _add_step(
        client, admin_headers, tour_id, 1, label="Intro", description_md=f"# {tricky}\n",
        shapes_l0=json.dumps([
            {"id": "r", "kind": "rect", "x": 1, "y": 2, "w": 3, "h": 4, "label": tricky},
            freehand,
            {"id": "c", "kind": "circle", "x": 1.5, "y": 2, "w": 3, "h": 4},
            {**freehand, "id": "g", "points": [{"x": 1.0, "y": 2.0}]},
            {**freehand, "id": "e", "points": []},
        ]),
        pins=json.dumps([{"id": "p", "x": 1, "y": 2, "label": tricky}]),
    )
    assert r.status_code == 200, r.text
    step_id = r.json()["id"]
    assert _add_step(client, admin_headers, tour_id, 2).status_code == 200
    handmade = [
        '[{"x": 1, "y": 2}]', '[{"x":1.5,"y":2.5}]', "garbage", '{"x": 1.0}',
        '[{"x": 1.0, "y": 2.0, "extra": 1}, 7, {"x": "3", "y": null}]',
        '[{"x": 1e+400, "y": 1.0}, {"x": 2.0, "y": 3.0}]',
        '[{"x": 4.0, "y": 5.0}]',  # turned into a BLOB below
    ]
    with orm.Session(engine) as db:
        for i, points_json in enumerate(handmade):
            db.add(StepShape(
                shape_id=f"handmade{i}", step_id=step_id, kind="freehand", x=1, y=1, w=2, h=2,
                stroke_color="red", fill_enabled=False, fill_opacity=0.5,
                points_json=points_json, smoothing=False, label="", label_position="center", z=i,
            ))
        db.add(StepPin(pin_id="handmade", step_id=step_id, x=1, y=1, shape="star", color="red",
                       rotation_deg=0, label="", z=0))
        db.commit()
        db.execute(sa.text(
            "UPDATE step_shapes SET points_json = CAST(points_json AS BLOB) WHERE shape_id = :sid"
        ), {"sid": f"handmade{len(handmade) - 1}"})
        db.commit()

    admin = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
    public = client.get(f"/api/tours/{admin.json()['tour']['public_id']}", headers=admin_headers)
    with orm.Session(engine) as db:
        steps = db.query(Step).filter(Step.tour_id == tour_id).order_by(Step.order).all()
        expected_steps = [step_to_out(s) for s in steps]
    assert len(expected_steps[0]["shapes_l0"]) == 12
    for r in (admin, public):
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/json"
        assert r.content == _jsonresponse_body({"tour": r.json()["tour"], "steps": expected_steps})


def test_tour_read_does_not_decode_stored_vertices(client, admin_headers, slide, monkeypatch):
    tour_id = _make_tour(client, admin_headers, slide)
    r = _add_step(
        client, admin_headers, tour_id, 1,
        shapes_l0=json.dumps([_freehand("a", 50), _freehand("b", 3)]),
    )
    assert r.status_code == 200, r.text
    decoded = []
    real = _serializers.decode_points
    monkeypatch.setattr(_serializers, "decode_points", lambda raw: decoded.append(raw) or real(raw))
    admin = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers)
    public = client.get(f"/api/tours/{admin.json()['tour']['public_id']}", headers=admin_headers)
    assert admin.status_code == public.status_code == 200
    assert decoded == []
    assert [len(sh["points"]) for sh in public.json()["steps"][0]["shapes_l0"]] == [50, 3]
