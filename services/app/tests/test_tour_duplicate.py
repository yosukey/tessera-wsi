"""POST /api/admin/tours/{tour_id}/duplicate: a draft copy of a tour on the
same slide — fields, own metadata, steps with their pins and shapes, and the
uploaded assets under the copy's own public_id."""
from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

import sqlalchemy.orm as orm
from sqlalchemy.exc import IntegrityError

from wsi_app.config import settings
from wsi_app.models import Step, Tour


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def _asset_dir(public_id: str) -> str:
    return os.path.join(settings.data_dir, "assets", "tours", public_id)


def _make_tour(client, headers, slide, title="Source Tour") -> int:
    r = client.post(f"/api/admin/slides/{slide.id}/tours", data={"title": title}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_step(client, headers, tour_id, order, **extra):
    data = {
        "order": str(order),
        "center_x": "10.5", "center_y": "20.25",
        "fov_um_w": "300", "fov_um_h": "200",
        **extra,
    }
    r = client.post(f"/api/admin/tours/{tour_id}/steps", data=data, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _get(client, headers, tour_id) -> dict:
    r = client.get(f"/api/admin/tours/{tour_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _without_ids(step: dict) -> dict:
    return {k: v for k, v in step.items() if k not in ("id", "revision", "tour_id")}


def test_duplicate_copies_the_tour_as_a_draft(client, admin_headers, slide, db_session):
    src_id = _make_tour(client, admin_headers, slide)
    r = client.patch(
        f"/api/admin/tours/{src_id}",
        json={"description": "intro", "attributes": {"organ": ["liver", "lung"]}},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    _add_step(
        client, admin_headers, src_id, 1,
        label="First", description_md="**one**", rotation_deg="15",
        transition_mode="smooth_zoom",
        pins=json.dumps([{"id": "pinA", "x": 1.0, "y": 2.0, "label": "A", "color": "blue", "z": 5}]),
        shapes_l0=json.dumps([
            {"id": "shA", "kind": "rect", "x": 0.0, "y": 0.0, "w": 4.0, "h": 4.0,
             "label": "box", "fill_enabled": True, "fill_opacity": 0.25, "z": 9},
            {"id": "shB", "kind": "freehand", "x": 0.0, "y": 0.0, "w": 2.0, "h": 2.0,
             "points": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 1.0}],
             "smoothing": False},
        ]),
    )
    _add_step(client, admin_headers, src_id, 2, label="Second")
    # Published and link-only: the copy keeps link-only but starts as a draft.
    src = db_session.get(Tour, src_id)
    src.is_published = True
    src.unlisted = True
    db_session.commit()

    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "Copy"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert new_id != src_id

    source, copy = _get(client, admin_headers, src_id), _get(client, admin_headers, new_id)
    assert copy["tour"]["title"] == "Copy"
    assert copy["tour"]["public_id"] == r.json()["public_id"] != source["tour"]["public_id"]
    assert copy["tour"]["slide_id"] == slide.id
    assert copy["tour"]["description"] == "intro"
    assert copy["tour"]["attributes"] == {"organ": ["liver", "lung"]}
    assert copy["tour"]["is_published"] is False
    assert copy["tour"]["unlisted"] is True
    assert copy["tour"]["revision"] == 0

    assert len(copy["steps"]) == 2
    src_step_ids = {s["id"] for s in source["steps"]}
    assert not src_step_ids & {s["id"] for s in copy["steps"]}
    assert all(s["revision"] == 0 for s in copy["steps"])
    assert [_without_ids(s) for s in copy["steps"]] == [_without_ids(s) for s in source["steps"]]
    # The source is untouched.
    assert source["tour"]["title"] == "Source Tour"
    assert len(source["steps"]) == 2


def test_duplicate_without_title_keeps_the_source_title(client, admin_headers, slide):
    src_id = _make_tour(client, admin_headers, slide, title="Keep me")
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert _get(client, admin_headers, r.json()["id"])["tour"]["title"] == "Keep me"


def test_duplicate_refuses_a_bad_title_and_a_missing_tour(client, admin_headers, slide, db_session):
    src_id = _make_tour(client, admin_headers, slide)
    before = db_session.query(Tour).count()
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "  "}, headers=admin_headers)
    assert r.status_code == 422, r.text
    r = client.post("/api/admin/tours/999999/duplicate", data={"title": "x"}, headers=admin_headers)
    assert r.status_code == 404, r.text
    assert db_session.query(Tour).count() == before


def test_instructor_may_duplicate(client, admin_headers, instructor_headers, slide):
    src_id = _make_tour(client, admin_headers, slide)
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "Mine"}, headers=instructor_headers)
    assert r.status_code == 200, r.text


def test_duplicate_requires_staff(client, slide, admin_headers):
    src_id = _make_tour(client, admin_headers, slide)
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "x"})
    assert r.status_code == 401, r.text


def test_duplicate_copies_assets_and_rewrites_their_references(client, admin_headers, slide):
    src_id = _make_tour(client, admin_headers, slide)
    r = client.post(
        f"/api/admin/tours/{src_id}/assets/upload",
        files={"file": ("pic.png", _png_bytes(), "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    url, name = r.json()["url"], r.json()["name"]
    # Another tour's asset reference must stay as it is.
    foreign = "/assets/tours/OtherTour00/x.png"
    _add_step(client, admin_headers, src_id, 1, description_md=f"![a]({url}) ![b]({foreign})")
    src_pid = _get(client, admin_headers, src_id)["tour"]["public_id"]

    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "Copy"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    new_id, new_pid = r.json()["id"], r.json()["public_id"]

    md = _get(client, admin_headers, new_id)["steps"][0]["description_md"]
    assert md == f"![a](/assets/tours/{new_pid}/{name}) ![b]({foreign})"
    with open(os.path.join(_asset_dir(new_pid), name), "rb") as fh:
        assert fh.read() == _png_bytes()
    r = client.get(f"/api/admin/tours/{new_id}/assets", headers=admin_headers)
    assert [a["name"] for a in r.json()["items"]] == [name]

    # Each tour owns its files: deleting the source leaves the copy's.
    assert client.delete(f"/api/admin/tours/{src_id}", headers=admin_headers).status_code == 200
    assert not os.path.exists(_asset_dir(src_pid))
    assert os.path.isfile(os.path.join(_asset_dir(new_pid), name))


def test_duplicate_leaves_upload_temps_and_dotfiles_behind(client, admin_headers, slide):
    src_id = _make_tour(client, admin_headers, slide)
    src_pid = _get(client, admin_headers, src_id)["tour"]["public_id"]
    os.makedirs(_asset_dir(src_pid))
    with open(os.path.join(_asset_dir(src_pid), "a.png"), "wb") as fh:
        fh.write(_png_bytes())
    with open(os.path.join(_asset_dir(src_pid), ".a.png.upload-0123456789ab"), "wb") as fh:
        fh.write(b"partial")
    os.makedirs(os.path.join(_asset_dir(src_pid), "nested"))

    r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "Copy"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert sorted(os.listdir(_asset_dir(r.json()["public_id"]))) == ["a.png"]


def test_a_failed_duplicate_leaves_no_tour_and_no_files(client, admin_headers, slide, db_session):
    src_id = _make_tour(client, admin_headers, slide)
    r = client.post(
        f"/api/admin/tours/{src_id}/assets/upload",
        files={"file": ("pic.png", _png_bytes(), "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    _add_step(client, admin_headers, src_id, 1)
    before = db_session.query(Tour).count()
    tours_root = os.path.join(settings.data_dir, "assets", "tours")
    dirs_before = sorted(os.listdir(tours_root))

    orig_flush = orm.Session.flush

    def _flush_fails_on_step(self, *args, **kwargs):
        if any(isinstance(obj, Step) for obj in self.new):
            raise IntegrityError("INSERT INTO steps", {}, Exception("boom"))
        return orig_flush(self, *args, **kwargs)

    with patch.object(orm.Session, "flush", _flush_fails_on_step):
        try:
            r = client.post(f"/api/admin/tours/{src_id}/duplicate", data={"title": "Copy"}, headers=admin_headers)
            assert r.status_code == 500
        except IntegrityError:
            pass  # TestClient re-raises server errors by default

    db_session.expire_all()
    assert db_session.query(Tour).count() == before
    assert sorted(os.listdir(tours_root)) == dirs_before


def _tour_with_asset(client, headers, slide) -> tuple[int, str]:
    src_id = _make_tour(client, headers, slide)
    r = client.post(
        f"/api/admin/tours/{src_id}/assets/upload",
        files={"file": ("pic.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return src_id, os.path.join(settings.data_dir, "assets", "tours")


def test_a_failed_db_write_removes_the_assets_it_had_copied(
    client, admin_headers, slide, db_session, monkeypatch
):
    """The copy's asset dir is written before the rows; a row write that then
    fails must take the copied files with it."""
    from wsi_app.routers import admin_tours

    src_id, tours_root = _tour_with_asset(client, admin_headers, slide)
    dirs_before = sorted(os.listdir(tours_root))
    tours_before = db_session.query(Tour).count()
    made = []
    real_copy = admin_tours._copy_tour_assets

    def spy(src_dir, dst_dir):
        ok = real_copy(src_dir, dst_dir)
        made.append(os.listdir(dst_dir))
        return ok

    monkeypatch.setattr(admin_tours, "_copy_tour_assets", spy)
    orig_flush = orm.Session.flush

    def _flush_fails_on_tour(self, *args, **kwargs):
        if any(isinstance(obj, Tour) for obj in self.new):
            raise IntegrityError("INSERT INTO tours", {}, Exception("boom"))
        return orig_flush(self, *args, **kwargs)

    with patch.object(orm.Session, "flush", _flush_fails_on_tour):
        try:
            r = client.post(f"/api/admin/tours/{src_id}/duplicate", headers=admin_headers)
            assert r.status_code == 500
        except IntegrityError:
            pass
    assert len(made) == 1 and len(made[0]) == 1  # the file was copied first
    assert sorted(os.listdir(tours_root)) == dirs_before
    db_session.expire_all()
    assert db_session.query(Tour).count() == tours_before


def test_a_full_volume_is_507_and_leaves_nothing_behind(
    client, admin_headers, slide, db_session, monkeypatch
):
    import errno

    from wsi_app.routers import admin_tours

    src_id, tours_root = _tour_with_asset(client, admin_headers, slide)
    dirs_before = sorted(os.listdir(tours_root))
    tours_before = db_session.query(Tour).count()

    def enospc(src, dst, *a, **kw):
        dst.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(admin_tours.shutil, "copyfileobj", enospc)
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", headers=admin_headers)
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.uploadNoSpace"
    assert sorted(os.listdir(tours_root)) == dirs_before
    db_session.expire_all()
    assert db_session.query(Tour).count() == tours_before


def test_the_volume_reserve_refuses_the_copy_before_writing(
    client, admin_headers, slide, db_session, monkeypatch
):
    import dataclasses
    from collections import namedtuple

    from wsi_app import disk_space
    from wsi_app.routers import admin_tours

    src_id, tours_root = _tour_with_asset(client, admin_headers, slide)
    dirs_before = sorted(os.listdir(tours_root))
    tours_before = db_session.query(Tour).count()
    reserve = 64 * 1024 * 1024
    monkeypatch.setattr(
        admin_tours, "settings", dataclasses.replace(settings, tour_asset_min_free_bytes=reserve),
    )
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: Usage(reserve * 4, reserve * 3, reserve))
    r = client.post(f"/api/admin/tours/{src_id}/duplicate", headers=admin_headers)
    assert r.status_code == 507, r.text
    assert sorted(os.listdir(tours_root)) == dirs_before
    db_session.expire_all()
    assert db_session.query(Tour).count() == tours_before


def test_a_source_delete_waits_for_the_copy_and_leaves_it_whole(
    client, admin_headers, slide, monkeypatch
):
    """The duplicate holds the source's asset lock from the copy to the
    commit, so a delete of the source (which takes that lock) can neither
    remove files mid-copy nor delete the row before the copy has read it."""
    import threading

    from wsi_app.routers import admin_tours

    src_id, _root = _tour_with_asset(client, admin_headers, slide)
    _add_step(client, admin_headers, src_id, 1)
    copying, release = threading.Event(), threading.Event()
    real_copy = admin_tours._copy_tour_assets

    def slow_copy(src_dir, dst_dir):
        ok = real_copy(src_dir, dst_dir)
        copying.set()
        assert release.wait(10)
        return ok

    monkeypatch.setattr(admin_tours, "_copy_tour_assets", slow_copy)
    result = {}

    def duplicate():
        result["dup"] = client.post(f"/api/admin/tours/{src_id}/duplicate", headers=admin_headers)

    def delete():
        result["del"] = client.delete(f"/api/admin/tours/{src_id}", headers=admin_headers)

    dup = threading.Thread(target=duplicate)
    dup.start()
    assert copying.wait(10)
    dele = threading.Thread(target=delete)
    dele.start()
    dele.join(timeout=0.5)
    assert dele.is_alive(), "the source was deleted while it was being copied"
    release.set()
    dup.join(timeout=10)
    dele.join(timeout=10)
    assert result["dup"].status_code == 200, result["dup"].text
    assert result["del"].status_code == 200, result["del"].text

    copy = _get(client, admin_headers, result["dup"].json()["id"])
    assert len(copy["steps"]) == 1
    assert len(os.listdir(_asset_dir(copy["tour"]["public_id"]))) == 1
