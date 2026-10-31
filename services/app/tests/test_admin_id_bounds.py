"""Admin endpoints must bound their numeric input rather than 500.

SQLite binds integers as signed 64-bit, so any id or ordinal at/above 2**63
raises OverflowError at bind time — on an otherwise well-formed request. The
public router bounds the ids it accepts explicitly (see test_public_id_bounds),
but the admin routers take dozens of ``int`` path and form parameters; the
overflow is mapped to 422 centrally so all of them behave, and a caller can't
fill ``error_logs`` with 500s just by sending oversized numbers.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from wsi_app.models import Slide, Tour

# One past the largest value SQLite can bind.
_OVERSIZED = str(2**63)


@pytest.fixture
def tour(db_session) -> Tour:
    # catalog_id and public_id are unique columns and the test DB is shared
    # across the session, so both need a per-test suffix.
    suffix = os.urandom(4).hex()
    s = Slide(title="S", catalog_id=f"IDB-{suffix}", wsi_filename="x.svs")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(title="T", slide_id=s.id, public_id=f"idbounds{suffix}")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/slides/{v}",
        "/api/admin/slides/{v}/download",
        "/api/admin/tours/{v}",
    ],
)
def test_oversized_path_id_is_422_not_500(client: TestClient, admin_headers, path):
    r = client.get(path.format(v=_OVERSIZED), headers=admin_headers)
    assert r.status_code == 422, r.text


def test_oversized_step_order_is_422_not_500(client: TestClient, admin_headers, tour):
    r = client.post(
        f"/api/admin/tours/{tour.id}/steps",
        headers=admin_headers,
        data={
            "order": _OVERSIZED,
            "center_x": "0",
            "center_y": "0",
            "fov_um_w": "1",
            "fov_um_h": "1",
        },
    )
    assert r.status_code == 422, r.text


def test_in_range_step_order_still_accepted(client: TestClient, admin_headers, tour):
    """The bound must not disturb ordinary values."""
    r = client.post(
        f"/api/admin/tours/{tour.id}/steps",
        headers=admin_headers,
        data={
            "order": "1",
            "center_x": "0",
            "center_y": "0",
            "fov_um_w": "1",
            "fov_um_h": "1",
        },
    )
    assert r.status_code == 200, r.text
