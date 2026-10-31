"""Tour asset upload: an overwrite that fails mid-upload must NOT destroy the
existing asset (as an O_TRUNC open of the final path would).
"""
from __future__ import annotations

import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from wsi_app.config import settings
from wsi_app.models import Slide, Tour


def _png_bytes(color=(10, 20, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def tour(db_session) -> Tour:
    s = Slide(title="S", catalog_id="S-001", wsi_filename="x.svs", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    t = Tour(title="T", slide_id=s.id)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _asset_dir(tour_public_id: str) -> str:
    # Asset dirs are keyed by the tour's public_id token, not the numeric id.
    return os.path.join(settings.data_dir, "assets", "tours", str(tour_public_id))


def test_overwrite_preserves_existing_asset_when_upload_fails(
    client: TestClient, admin_headers, tour
):
    # 1. Seed a valid asset via overwrite.
    original = _png_bytes((10, 20, 30))
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("pic.png", original, "image/png")},
        data={"duplicate_policy": "overwrite"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    stored = os.path.join(_asset_dir(tour.public_id), "pic.png")
    assert os.path.exists(stored)
    assert open(stored, "rb").read() == original

    # 2. A second overwrite of the SAME name that blows the size cap mid-stream.
    original_cap = settings.tour_asset_max_bytes
    object.__setattr__(settings, "tour_asset_max_bytes", 4)
    try:
        r = client.post(
            f"/api/admin/tours/{tour.id}/assets/upload",
            files={"file": ("pic.png", _png_bytes((99, 99, 99)) + b"x" * 64, "image/png")},
            data={"duplicate_policy": "overwrite"},
            headers=admin_headers,
        )
        assert r.status_code == 413, r.text
    finally:
        object.__setattr__(settings, "tour_asset_max_bytes", original_cap)

    # 3. The original asset must be intact (not truncated, not deleted) ...
    assert os.path.exists(stored), "failed overwrite deleted the existing asset"
    assert open(stored, "rb").read() == original, "failed overwrite corrupted the asset"

    # ... and no temp upload file should be left behind.
    leftovers = [n for n in os.listdir(_asset_dir(tour.public_id)) if n.startswith(".pic.png.upload-")]
    assert leftovers == [], f"temp upload files leaked: {leftovers}"


def test_asset_list_sweeps_stale_upload_temps(client: TestClient, admin_headers, tour):
    # A crash between the temp write and os.replace strands a hidden
    # `.{name}.upload-{token}` sibling. Because the listing filters dot-prefixed
    # names it would accumulate invisibly; listing must reap the stale ones.
    from wsi_app.routers import admin_tours

    # Seed a real asset so the dir exists and the listing has something to return.
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("real.png", _png_bytes(), "image/png")},
        data={"duplicate_policy": "overwrite"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    base = _asset_dir(tour.public_id)

    # An OLD orphaned temp (mtime well past the threshold) and a FRESH one
    # (simulating an upload still in flight) must be treated differently.
    stale = os.path.join(base, ".real.png.upload-deadbeefdeadbeef")
    fresh = os.path.join(base, ".other.png.upload-cafecafecafecafe")
    for p in (stale, fresh):
        with open(p, "wb") as fh:
            fh.write(b"partial")
    old = time.time() - admin_tours._UPLOAD_TEMP_MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))

    r = client.get(f"/api/admin/tours/{tour.id}/assets", headers=admin_headers)
    assert r.status_code == 200, r.text
    # The listing itself never surfaces temp files (dot-prefixed).
    assert {i["name"] for i in r.json()["items"]} == {"real.png"}
    # The stale temp is reaped; the fresh (in-flight) one is preserved.
    assert not os.path.exists(stale), "stale upload temp was not swept"
    assert os.path.exists(fresh), "fresh in-flight upload temp was wrongly swept"


def test_overwrite_replaces_on_success(client: TestClient, admin_headers, tour):
    first = _png_bytes((1, 2, 3))
    second = _png_bytes((200, 100, 50))
    for body in (first, second):
        r = client.post(
            f"/api/admin/tours/{tour.id}/assets/upload",
            files={"file": ("img.png", body, "image/png")},
            data={"duplicate_policy": "overwrite"},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
    stored = os.path.join(_asset_dir(tour.public_id), "img.png")
    assert open(stored, "rb").read() == second
    # Only the final file remains; no temp siblings.
    names = os.listdir(_asset_dir(tour.public_id))
    assert names == ["img.png"], names


def test_delete_asset_refuses_tour_without_public_id(
    client: TestClient, admin_headers, tour, db_session
):
    """A tour whose public_id is empty (possible mid-migration) resolves
    ``tour_asset_dir("")`` to the shared ``assets/tours/`` root. Upload
    already refuses that with a 409 and listing returns an empty list; delete
    must refuse it too (409, like upload), so a DELETE can never remove a file
    sitting directly in the shared root, and the shared-root file survives.
    """
    shared_root = os.path.join(settings.data_dir, "assets", "tours")
    os.makedirs(shared_root, exist_ok=True)
    stray = os.path.join(shared_root, "stray.png")
    with open(stray, "wb") as fh:
        fh.write(_png_bytes())

    tour.public_id = ""
    db_session.commit()

    r = client.delete(
        f"/api/admin/tours/{tour.id}/assets/stray.png", headers=admin_headers
    )
    assert r.status_code == 409, r.text
    assert os.path.isfile(stray), "delete reached into the shared assets root"


def test_delete_removes_a_long_rename_mode_name(client: TestClient, admin_headers, tour):
    """Rename mode appends "-<8 hex>" after safe_filename's 200-byte cap, so
    an upload can store a 209-character name. A delete that looked the asset
    up under safe_filename(name) — a different, 200-character name — would
    answer 404 and the attachment could never be removed."""
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("a" * 250 + ".png", _png_bytes(), "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert len(name) > 200
    stored = os.path.join(_asset_dir(tour.public_id), name)
    assert os.path.isfile(stored)

    r = client.delete(f"/api/admin/tours/{tour.id}/assets/{name}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert not os.path.exists(stored)


@pytest.mark.parametrize("name,status", [
    # Refused by the handler (400): no upload stores such a name.
    *[(n, 400) for n in (
        "%2E%2E", "%2E", "...", ".hidden.png", "a b.png", "%C3%BC.png", "x" * 256,
        "..%5Cother.png", "sub%5Cother.png",
    )],
    # A decoded "/" never matches the one-segment route at all.
    *[(n, 404) for n in ("..%2F..%2Fother.png", "%2E%2E%2Fother.png", "sub%2Fother.png")],
])
def test_delete_refuses_names_no_upload_could_store(
    client: TestClient, admin_headers, tour, name, status
):
    """The exact name is looked up, so it is held to what an upload can
    store (admin_helpers.storable_asset_name): nothing outside the tour's
    asset directory, no dotfile inside it, and no other file than the one
    named can be reached."""
    base = _asset_dir(tour.public_id)
    os.makedirs(os.path.join(base, "sub"), exist_ok=True)
    victims = [
        os.path.join(base, "..", "other.png"),
        os.path.join(base, "sub", "other.png"),
        os.path.join(base, ".hidden.png"),
        os.path.join(base, "other.png"),
    ]
    for path in victims:
        with open(path, "wb") as fh:
            fh.write(_png_bytes())
    try:
        r = client.delete(f"/api/admin/tours/{tour.id}/assets/{name}", headers=admin_headers)
        assert r.status_code == status, r.text
        for path in victims:
            assert os.path.isfile(path), path
    finally:
        for path in victims:
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# The stored extension must agree with the content (stored XSS via /assets/)
# ---------------------------------------------------------------------------
def _polyglot() -> bytes:
    # A real, verify()-clean PNG followed by an HTML document: passes the magic
    # and Pillow checks, and renders as a page if served as text/html.
    return _png_bytes() + b"<html><script>alert(document.domain)</script></html>"


def test_rename_upload_rewrites_mismatched_extension(
    client: TestClient, instructor_headers, tour
):
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("notes.html", _polyglot(), "image/png")},
        headers=instructor_headers,
    )
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert name.startswith("notes-") and name.endswith(".png"), name
    assert not any(
        n.endswith(".html") for n in os.listdir(_asset_dir(tour.public_id))
    )
    g = client.get(r.json()["url"])
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("image/png")
    assert g.headers["x-content-type-options"] == "nosniff"


def test_overwrite_upload_refuses_mismatched_extension(
    client: TestClient, admin_headers, tour
):
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("notes.html", _polyglot(), "image/png")},
        data={"duplicate_policy": "overwrite"},
        headers=admin_headers,
    )
    assert r.status_code == 415, r.text
    d = _asset_dir(tour.public_id)
    assert not os.path.isdir(d) or os.listdir(d) == []


@pytest.mark.parametrize("policy,status", [("rename", 200), ("overwrite", 415)])
def test_overlong_extension_is_not_a_500(client: TestClient, admin_headers, tour, policy, status):
    """Were safe_filename to cap the stem but keep any extension whole,
    "a." + "b" * 300 would reach the filesystem as a 302-byte name and the
    write fail with ENAMETOOLONG (a bare 500). The extension is cut with the
    rest of the name; what is left is no image extension, so "rename" gives
    the upload the one its content calls for and "overwrite" refuses it like
    any other mismatch."""
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("a." + "b" * 300, _png_bytes(), "image/png")},
        data={"duplicate_policy": policy},
        headers=admin_headers,
    )
    assert r.status_code == status, r.text
    if status == 200:
        name = r.json()["name"]
        assert name.startswith("a-") and name.endswith(".png") and len(name) < 64, name


def test_name_the_filesystem_refuses_as_too_long_is_a_400(
    client: TestClient, admin_headers, tour, monkeypatch
):
    """A filesystem with a shorter name limit than safe_filename allows for
    can still refuse the name: that is the client's input, not a server fault."""
    import errno

    from wsi_app.routers import admin_tours

    real_open = os.open

    def short_names(path, flags, mode=0o777, **kw):
        if "pic" in os.path.basename(os.fspath(path)):  # the asset or its temp
            raise OSError(errno.ENAMETOOLONG, "File name too long", path)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(admin_tours.os, "open", short_names)
    for policy in ("rename", "overwrite"):
        r = client.post(
            f"/api/admin/tours/{tour.id}/assets/upload",
            files={"file": ("pic.png", _png_bytes(), "image/png")},
            data={"duplicate_policy": policy},
            headers=admin_headers,
        )
        assert r.status_code == 400, (policy, r.text)


def test_jpeg_named_png_is_stored_under_an_image_extension(
    client: TestClient, admin_headers, tour
):
    """Same family, different image format: any image extension is fine
    (browsers decode <img> by content), so the name is kept."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (5, 5, 5)).save(buf, "JPEG")
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("photo.png", buf.getvalue(), "image/png")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"].endswith(".png")


def test_stray_non_media_asset_is_served_as_sandboxed_download(
    client: TestClient, tour
):
    """A file already on disk under a non-media extension (placed by hand, or
    restored from a backup) must not render as a same-origin page.
    """
    d = _asset_dir(tour.public_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "stray.html"), "wb") as f:
        f.write(_polyglot())
    g = client.get(f"/assets/tours/{tour.public_id}/stray.html")
    assert g.status_code == 200
    assert g.headers["content-disposition"] == "attachment"
    assert "sandbox" in g.headers["content-security-policy"]
    assert g.headers["x-content-type-options"] == "nosniff"


def test_mpo_jpeg_gets_a_jpeg_extension(client: TestClient, admin_headers, tour):
    """Multi-picture JPEGs (camera/phone output) are reported by Pillow as
    "MPO"; a mis-named one must be given .jpg, not a fallback extension."""
    from PIL import Image

    buf = io.BytesIO()
    a = Image.new("RGB", (4, 4), (1, 2, 3))
    a.save(buf, "MPO", save_all=True, append_images=[Image.new("RGB", (4, 4))])
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("camera.bin", buf.getvalue(), "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"].endswith(".jpg"), r.json()["name"]


def test_decompression_bomb_image_is_415_not_500(client: TestClient, admin_headers, tour, monkeypatch):
    """Pillow's DecompressionBombError is not an OSError/ValueError; it must
    not escape the validator as a 500 (and abort a whole slide import)."""
    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(big, "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # 64*64 is > 2x this
    r = client.post(
        f"/api/admin/tours/{tour.id}/assets/upload",
        files={"file": ("bomb.png", big.getvalue(), "image/png")},
        data={"duplicate_policy": "overwrite"},
        headers=admin_headers,
    )
    assert r.status_code == 415, r.text


def test_upload_losing_a_race_with_tour_delete_is_404_and_leaves_no_files(
    client: TestClient, admin_headers, tour, monkeypatch
):
    """An upload that passed its tour-exists check, then lost the tour to a
    DELETE, must not recreate assets/tours/<public_id>/ (orphan files served
    publicly for a tour with no row)."""
    from wsi_app.routers import admin_tours

    tour_id, public_id = tour.id, tour.public_id
    real = admin_tours.safe_filename
    state = {"deleted": False}

    def delete_in_between(name):
        # Runs after the handler's existence check, before it takes the lock.
        if not state["deleted"]:
            state["deleted"] = True
            r = client.delete(f"/api/admin/tours/{tour_id}", headers=admin_headers)
            assert r.status_code == 200, r.text
        return real(name)

    monkeypatch.setattr(admin_tours, "safe_filename", delete_in_between)
    r = client.post(
        f"/api/admin/tours/{tour_id}/assets/upload",
        files={"file": ("pic.png", _png_bytes(), "image/png")},
        data={"duplicate_policy": "rename"},
        headers=admin_headers,
    )
    assert state["deleted"]
    assert r.status_code == 404, r.text
    assert not os.path.exists(_asset_dir(public_id))


def test_tour_delete_waits_for_an_in_flight_asset_upload(
    client: TestClient, admin_headers, tour
):
    """Delete takes tour_asset_lock, so it cannot interleave with the
    quota-check -> write -> publish section of an upload."""
    import threading

    from wsi_app.admin_helpers import tour_asset_lock

    result = {}

    def do_delete():
        result["r"] = client.delete(f"/api/admin/tours/{tour.id}", headers=admin_headers)

    with tour_asset_lock(tour.id):
        th = threading.Thread(target=do_delete)
        th.start()
        th.join(timeout=0.5)
        assert th.is_alive(), "delete finished while the asset lock was held"
    th.join(timeout=10)
    assert result["r"].status_code == 200


def test_slide_delete_waits_for_an_in_flight_tour_asset_upload(
    client: TestClient, admin_headers, tour
):
    """Deleting a slide cascades its tours and removes their asset directories:
    it must hold the same per-tour lock as uploads, or an upload that passed its
    tour-exists check recreates a directory for a tour that has no row."""
    import threading

    from wsi_app.admin_helpers import tour_asset_lock

    result = {}

    def do_delete():
        result["r"] = client.delete(f"/api/admin/slides/{tour.slide_id}", headers=admin_headers)

    with tour_asset_lock(tour.id):
        th = threading.Thread(target=do_delete)
        th.start()
        th.join(timeout=0.5)
        assert th.is_alive(), "slide delete finished while a tour's asset lock was held"
    th.join(timeout=10)
    assert result["r"].status_code == 200, result["r"].text


def test_slide_delete_also_locks_a_tour_created_while_it_waited(
    client: TestClient, admin_headers, db_session, tour, monkeypatch
):
    """The tours to lock are looked up before the locks are held; one created in
    between would otherwise be cascade-deleted (and its assets removed) without
    its lock. After taking the locks the delete looks again and locks the newcomer."""
    import contextlib

    from wsi_app.models import Tour as TourRow
    from wsi_app.routers import admin_slides

    tour_id, slide_id = tour.id, tour.slide_id  # the row is gone after the delete
    real = admin_slides.tour_asset_lock
    held, state = [], {"new_id": None}

    @contextlib.contextmanager
    def spy(tour_id):
        with real(tour_id):
            held.append(tour_id)
            if state["new_id"] is None:
                state["new_id"] = -1  # only the first acquisition adds the newcomer
                t = TourRow(title="late", slide_id=slide_id)
                db_session.add(t)
                db_session.commit()
                state["new_id"] = t.id
            yield

    monkeypatch.setattr(admin_slides, "tour_asset_lock", spy)
    r = client.delete(f"/api/admin/slides/{slide_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert state["new_id"] and state["new_id"] > 0
    assert state["new_id"] in held, (held, state)
    assert held.index(tour_id) < held.index(state["new_id"])  # locks taken in id order


def test_new_tours_do_not_get_past_the_volume_reserve(
    client: TestClient, instructor_headers, db_session, tour, monkeypatch
):
    """The per-tour quotas bound one tour, but the lowest staff role may also
    create tours, each with a fresh quota — so on their own they would not stop
    an instructor filling the data volume. The upload also refuses once the
    write would leave the volume under its reserve, however new the tour."""
    import dataclasses
    from collections import namedtuple

    from wsi_app import disk_space
    from wsi_app.routers import admin_tours

    Usage = namedtuple("Usage", "total used free")
    reserve = 64 * 1024 * 1024
    monkeypatch.setattr(
        admin_tours, "settings", dataclasses.replace(settings, tour_asset_min_free_bytes=reserve),
    )
    payload = _png_bytes()

    def disk_free(free: int) -> None:
        monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: Usage(free * 4, free * 3, free))

    def upload(tour_id: int):
        return client.post(
            f"/api/admin/tours/{tour_id}/assets/upload",
            files={"file": ("pic.png", payload, "image/png")},
            data={"duplicate_policy": "rename"},
            headers=instructor_headers,
        )

    disk_free(reserve + 10 * len(payload))
    assert upload(tour.id).status_code == 200

    # The volume is now within one file of its reserve: refused, on this tour
    # and on a brand-new one alike, and nothing is left behind on disk.
    disk_free(reserve + len(payload) - 1)
    r = upload(tour.id)
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.uploadNoSpace"
    created = client.post(
        f"/api/admin/slides/{tour.slide_id}/tours", data={"title": "Another"}, headers=instructor_headers,
    )
    assert created.status_code == 200, created.text
    fresh = created.json()
    r = upload(fresh["id"])
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.uploadNoSpace"
    assert len([n for n in os.listdir(_asset_dir(tour.public_id)) if not n.startswith(".")]) == 1
    fresh_dir = _asset_dir(fresh["public_id"])
    assert not os.path.isdir(fresh_dir) or os.listdir(fresh_dir) == []

    # 0 turns the reserve off; a volume that reports no capacity (some FUSE
    # mounts) is "unknown", not "full", as for slide uploads.
    monkeypatch.setattr(
        admin_tours, "settings", dataclasses.replace(settings, tour_asset_min_free_bytes=0),
    )
    assert upload(fresh["id"]).status_code == 200
    monkeypatch.setattr(
        admin_tours, "settings", dataclasses.replace(settings, tour_asset_min_free_bytes=reserve),
    )
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: Usage(0, 0, 0))
    assert upload(fresh["id"]).status_code == 200


def test_listing_reports_the_per_file_cap(client: TestClient, admin_headers, tour):
    """The tour editor checks and names TOUR_ASSET_MAX_BYTES as the listing
    reports it — also for a tour with no asset directory yet."""
    original_cap = settings.tour_asset_max_bytes
    object.__setattr__(settings, "tour_asset_max_bytes", 10 * 1024 * 1024)
    try:
        r = client.get(f"/api/admin/tours/{tour.id}/assets", headers=admin_headers)
    finally:
        object.__setattr__(settings, "tour_asset_max_bytes", original_cap)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "max_bytes": 10 * 1024 * 1024}
