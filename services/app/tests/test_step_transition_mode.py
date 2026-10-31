"""Round-trip coverage for a step's transition_mode (none/smooth/smooth_zoom)."""
from wsi_app.models import Slide, Tour


def _make_tour(db_session):
    slide = Slide(title="Slide", catalog_id="C-1")
    db_session.add(slide)
    db_session.flush()
    tour = Tour(slide_id=slide.id, title="Tour")
    db_session.add(tour)
    db_session.commit()
    return tour.id


def _add_step(client, headers, tour_id, *, order, transition_mode=None):
    form = {
        "order": order,
        "center_x": 100.0,
        "center_y": 100.0,
        "fov_um_w": 600.0,
        "fov_um_h": 450.0,
    }
    if transition_mode is not None:
        form["transition_mode"] = transition_mode
    return client.post(
        f"/api/admin/tours/{tour_id}/steps", data=form, headers=headers
    )


def _mode_of(client, headers, tour_id, step_id):
    tour = client.get(f"/api/admin/tours/{tour_id}", headers=headers).json()
    return next(s for s in tour["steps"] if s["id"] == step_id)["transition_mode"]


def test_default_mode_is_smooth(client, admin_headers, db_session):
    tour_id = _make_tour(db_session)
    r = _add_step(client, admin_headers, tour_id, order=1)
    assert r.status_code == 200, r.text
    assert _mode_of(client, admin_headers, tour_id, r.json()["id"]) == "smooth"


def test_explicit_modes_round_trip(client, admin_headers, db_session):
    tour_id = _make_tour(db_session)
    for i, mode in enumerate(("none", "smooth", "smooth_zoom"), start=1):
        r = _add_step(client, admin_headers, tour_id, order=i, transition_mode=mode)
        assert r.status_code == 200, r.text
        assert _mode_of(client, admin_headers, tour_id, r.json()["id"]) == mode


def test_invalid_mode_rejected_on_create(client, admin_headers, db_session):
    tour_id = _make_tour(db_session)
    r = _add_step(client, admin_headers, tour_id, order=1, transition_mode="warp")
    assert r.status_code == 422


def test_patch_updates_mode(client, admin_headers, db_session):
    tour_id = _make_tour(db_session)
    step_id = _add_step(
        client, admin_headers, tour_id, order=1, transition_mode="smooth_zoom"
    ).json()["id"]

    ok = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={"transition_mode": "none"},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text

    tour = client.get(f"/api/admin/tours/{tour_id}", headers=admin_headers).json()
    step = next(s for s in tour["steps"] if s["id"] == step_id)
    assert step["transition_mode"] == "none"


def test_patch_invalid_mode_rejected(client, admin_headers, db_session):
    tour_id = _make_tour(db_session)
    step_id = _add_step(client, admin_headers, tour_id, order=1).json()["id"]
    bad = client.patch(
        f"/api/admin/tours/{tour_id}/steps/{step_id}",
        json={"transition_mode": "warp"},
        headers=admin_headers,
    )
    assert bad.status_code == 422
