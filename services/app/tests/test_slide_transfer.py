"""End-to-end tests for per-slide export / import (cross-tessera migration).

The additive import merges bundled slides as NEW rows into the live DB, so —
like the whole-DB backup suite — these tests run the export/import background
jobs inline (daemon threads) and poll the job endpoint. The OpenSlide helpers
are monkeypatched (mirroring the upload tests) so the placeholder WSI bytes the
``attach_wsi`` fixture writes don't need to be a real pyramid.
"""
from __future__ import annotations

import contextlib
import errno
import io
import math
import json
import os
import shutil
import tarfile
import time

import pytest

from wsi_app import backup as _backup
from wsi_app import jobs as _jobs
from wsi_app import slide_transfer as _st
from wsi_app.admin_helpers import reserve_next_slide_id, tour_asset_dir, valid_public_id
from wsi_app.config import settings
from wsi_app.models import Slide, SlideAttribute, Step, StepPin, StepShape, Tour, TourAttribute
from wsi_app.version import APP_VERSION


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _fake_props(**over):
    p = {
        "width": 120, "height": 80, "mpp_x": 0.5, "mpp_y": 0.5,
        "objective_power": 20.0, "vendor": "testvendor", "acquired_at": "2024-02-03",
    }
    p.update(over)
    return p


def _fake_thumbnail(path, out_path, max_size=(512, 512)):
    from PIL import Image
    Image.new("RGB", (1, 1), (0, 0, 0)).save(out_path, "JPEG")


@pytest.fixture
def patched_openslide(monkeypatch):
    """Make the import finalize path work on placeholder WSI bytes."""
    monkeypatch.setattr(_st, "read_slide_properties", lambda p: _fake_props())
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda slide, path: None)
    yield


def _poll(client, job_id, headers, tries=200):
    rj = {}
    for _ in range(tries):
        rj = client.get(
            f"/api/admin/slides/transfer/jobs/{job_id}", headers=headers
        ).json()
        if rj.get("state") in ("succeeded", "failed"):
            return rj
        time.sleep(0.02)
    return rj


def _build_source(db, attach_wsi, *, published=True):
    """Create a fully-populated source slide: attributes + a tour with one step
    (pin, shape, and a markdown asset reference) + an on-disk tour asset.

    Returns ``(slide, tour, basename)``. The slide's ``wsi_filename_norm`` is set
    (attach_wsi alone leaves it NULL) so importing the resulting bundle into the
    same DB triggers the filename-conflict path — and a per-test random token
    keeps the norm unique across the session-shared tables.
    """
    token = os.urandom(4).hex()
    basename = f"source-{token}.svs"
    s = Slide(
        title="Source Slide", catalog_id="SRC-1",
        is_published=published, unlisted=False, mpp_x=0.4, mpp_y=0.4,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    attach_wsi(s, name=f"{s.id}_{basename}")
    s.wsi_filename_norm = basename.casefold()
    db.add_all([
        SlideAttribute(slide_id=s.id, key="stain", value="HE"),
        SlideAttribute(slide_id=s.id, key="organ", value="Liver"),
    ])
    db.commit()

    tour = Tour(slide_id=s.id, title="Guided", description="tour body",
                is_published=True, unlisted=False)
    db.add(tour)
    db.commit()
    db.refresh(tour)
    db.add(TourAttribute(tour_id=tour.id, key="level", value="basic"))
    db.commit()

    step = Step(
        tour_id=tour.id, order=1, label="step one",
        center_x=10.0, center_y=20.0, fov_um_w=600.0, fov_um_h=450.0,
        description_md=f"look at /assets/tours/{tour.public_id}/pic.png here",
        transition_mode="smooth",
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    db.add(StepPin(pin_id="pin1", step_id=step.id, x=1.0, y=2.0,
                   shape="circle", color="red", label="p", z=1234))
    db.add(StepShape(shape_id="sh1", step_id=step.id, kind="rect",
                     x=0.0, y=0.0, w=5.0, h=5.0, stroke_color="red",
                     smoothing=False,
                     label="r", label_position="above", z=7))
    db.commit()

    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "pic.png"), "wb") as f:
        f.write(_png_bytes())
    # A PNG-prefixed HTML document under an .html name: exactly what the upload
    # path refuses to store. Import must not install it either (it would be
    # served as text/html from the app's origin).
    with open(os.path.join(adir, "evil.html"), "wb") as f:
        f.write(_png_bytes() + b"<script>alert(1)</script>")
    return s, tour, basename


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def _export(client, headers, slide_ids, note=""):
    r = client.post("/api/admin/slides/export",
                    json={"slide_ids": slide_ids, "note": note}, headers=headers)
    assert r.status_code == 200, r.text
    rj = _poll(client, r.json()["job_id"], headers)
    assert rj["state"] == "succeeded", rj
    return rj["result"]


def _download(client, headers, export_id):
    r = client.post(f"/api/admin/slides/export/{export_id}/download_url", headers=headers)
    assert r.status_code == 200, r.text
    dl = client.get(r.json()["url"])
    assert dl.status_code == 200, dl.text
    return dl.content


def _stage_job(client, headers, content, archive_format="tar.zst") -> dict:
    """Upload a bundle (202 + a staging job) and poll the job to its end."""
    r = client.post(
        "/api/admin/slides/import", headers=headers,
        files={"file": (f"bundle.{archive_format}", io.BytesIO(content),
                        "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    return _poll(client, r.json()["job_id"], headers)


def _stage(client, headers, content, archive_format="tar.zst") -> dict:
    """Stage a bundle that must stage cleanly; returns ``{stage_id, plan}``."""
    job = _stage_job(client, headers, content, archive_format)
    assert job["state"] == "succeeded", job
    return job["result"]


def _apply(client, headers, stage_id, resolutions=None):
    ra = client.post("/api/admin/slides/import/apply",
                     json={"stage_id": stage_id, "resolutions": resolutions or {}},
                     headers=headers)
    assert ra.status_code == 200, ra.text
    return _poll(client, ra.json()["job_id"], headers)


@contextlib.contextmanager
def _reader(content: bytes):
    """Open an in-memory bundle for name inspection (zstd or gzip), sniffing the
    compression from the magic bytes."""
    ext = "tar.zst" if content[:4] == b"\x28\xb5\x2f\xfd" else "tar.gz"
    full = os.path.join(
        settings.backup_dir, ".staging", f"peek-{os.urandom(3).hex()}.{ext}"
    )
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(content)
    try:
        with _backup._open_archive_reader(full) as tar:
            yield tar
    finally:
        try:
            os.remove(full)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_export_import_round_trip_copy(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, src_tour, basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id], note="migrate me")
    assert result["slide_count"] == 1
    assert result["tour_count"] == 1

    content = _download(client, admin_headers, result["export_id"])
    # The bundle carries manifest + slides.json + the WSI + the tour asset.
    with _reader(content) as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "slides.json" in names
    assert any(n.startswith(f"wsi/{src.public_id}/") for n in names)
    assert any(n.startswith(f"assets/tours/{src_tour.public_id}") for n in names)

    body = _stage(client, admin_headers, content, result["archive_format"])
    assert body["plan"]["blocking_code"] is None
    # Source filename already exists (norm set), so the slide is flagged.
    assert body["plan"]["slides"][0]["filename_conflict"] is True
    assert body["plan"]["slides"][0]["wsi_present_in_bundle"] is True

    rj = _apply(client, admin_headers, body["stage_id"])  # default resolution = copy
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["imported_count"] == 1 and res["skipped_count"] == 0 and res["failed_count"] == 0
    imported = res["imported"][0]
    new_id = imported["slide_id"]
    assert new_id != src.id
    assert imported["renamed"] is True
    assert "-imported-" in imported["wsi_filename"]

    db_session.expire_all()
    ns = db_session.get(Slide, new_id)
    # Override-able fields come from the bundle; file-truth from the (fake) props.
    assert ns.title == "Source Slide"
    assert ns.catalog_id == "SRC-1"
    assert ns.is_published is True
    assert ns.mpp_x == pytest.approx(0.4)      # bundle override preserved
    assert ns.mpp_x_file == pytest.approx(0.5)  # from props
    assert ns.width == 120 and ns.height == 80
    assert ns.vendor == "testvendor"
    assert os.path.isfile(os.path.join(settings.data_dir, "wsi", ns.wsi_filename))

    # Attributes round-tripped.
    attrs = {(a.key, a.value) for a in db_session.query(SlideAttribute).filter_by(slide_id=new_id)}
    assert ("stain", "HE") in attrs and ("organ", "Liver") in attrs

    # Tour reconstructed with a reminted public_id (collided with the source tour).
    new_tour = db_session.query(Tour).filter_by(slide_id=new_id).one()
    assert new_tour.public_id != src_tour.public_id
    # ... with its own metadata.
    tour_attrs = {(a.key, a.value) for a in db_session.query(TourAttribute).filter_by(tour_id=new_tour.id)}
    assert tour_attrs == {("level", "basic")}
    step = db_session.query(Step).filter_by(tour_id=new_tour.id).one()
    # Markdown asset path rewritten old -> new tour public_id.
    assert f"/assets/tours/{new_tour.public_id}/pic.png" in step.description_md
    assert src_tour.public_id not in step.description_md
    # Pins / shapes reconstructed, including their stacking order (z).
    pin = db_session.query(StepPin).filter_by(step_id=step.id, pin_id="pin1").one()
    assert pin.z == 1234
    sh = db_session.query(StepShape).filter_by(step_id=step.id, shape_id="sh1").one()
    assert sh.kind == "rect" and sh.stroke_color == "red" and sh.z == 7
    # The per-shape smoothing flag survives export -> import (not just defaulted).
    assert sh.smoothing is False
    # Tour asset copied under the new public_id.
    assert os.path.isfile(os.path.join(tour_asset_dir(new_tour.public_id), "pic.png"))
    # ... but not the markup payload that no upload could have stored.
    assert not os.path.exists(os.path.join(tour_asset_dir(new_tour.public_id), "evil.html"))


def test_import_skip_resolution_leaves_no_new_slide(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])
    body = _stage(client, admin_headers, content, result["archive_format"])

    rj = _apply(client, admin_headers, body["stage_id"],
                resolutions={src.public_id: "skip"})
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["skipped_count"] == 1 and res["imported_count"] == 0
    # Nothing was imported: the only slide keyed to this test's unique filename
    # is still the original source (tables are session-shared, so scope by norm).
    matches = db_session.query(Slide).filter(
        Slide.wsi_filename_norm == basename.casefold()
    ).all()
    assert [m.id for m in matches] == [src.id]


# ---------------------------------------------------------------------------
# Version guard + bundle-format guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("system", ["older", "too_new"])
def test_schema_version_mismatch_blocks_plan_and_apply(client, admin_headers, db_session, attach_wsi, patched_openslide, monkeypatch, system):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])  # manifest stamped schema_version=CURRENT
    content = _download(client, admin_headers, result["export_id"])

    # Pretend this system is at a schema_version the bundle's is outside of:
    # older than the bundle (it cannot know what a newer schema added), or so
    # far ahead that the bundle predates the compatible range — even though
    # the app_version string still matches.
    if system == "older":
        monkeypatch.setattr(_st, "CURRENT_SCHEMA_VERSION", _st.CURRENT_SCHEMA_VERSION - 1)
    else:
        monkeypatch.setattr(_st, "CURRENT_SCHEMA_VERSION", 999)
        monkeypatch.setattr(_st, "MIN_BUNDLE_SCHEMA_VERSION", 999)
    body = _stage(client, admin_headers, content, result["archive_format"])
    assert body["plan"]["blocking_code"] == "VERSION_MISMATCH"

    rj = _apply(client, admin_headers, body["stage_id"])
    assert rj["state"] == "failed", rj
    # The job result carries the full i18n key so the frontend can translate it.
    assert rj["result"].get("code") == "adminDash.apiErr.importVersionMismatch"


@pytest.mark.parametrize("bad", [[1], {}, None, "abc", 1.9, True, False])
def test_import_guard_never_raises_on_a_malformed_schema_version(bad):
    """The guard reads an untrusted bundle: it returns a verdict, never raises.

    ``True`` is in the list on purpose. bool is an int subclass in Python, so
    a JSON ``true`` would satisfy ``int(raw) == CURRENT`` and let the bundle
    through — for exactly as long as the baseline happens to sit at 1. Same
    exclusion the gen / tv claim checks make.
    """
    code, reason = _st._import_version_block({"schema_version": bad})
    assert code == "VERSION_MISMATCH", (bad, code, reason)


def test_bundle_without_a_schema_version_is_refused():
    """A bundle that declares no ``schema_version`` must be refused outright.

    Were the key optional, an exact ``app_version`` string match would be the
    only fallback, letting a bundle of entirely unknown field layout through
    on a version string alone — and every bundle this app writes carries the
    key, so its absence means the file is not one this app wrote.
    """
    code, reason = _st._import_version_block({"app_version": _st.APP_VERSION})
    assert code == "VERSION_MISMATCH"
    assert "schema_version" in reason

    # A present-but-unparseable value is refused for the same reason.
    code, _ = _st._import_version_block(
        {"app_version": _st.APP_VERSION, "schema_version": "not-a-number"}
    )
    assert code == "VERSION_MISMATCH"

    # Control: the matching integer passes.
    assert _st._import_version_block(
        {"app_version": "9.9.9-other", "schema_version": _st.CURRENT_SCHEMA_VERSION}
    ) == (None, None)


def test_same_schema_different_app_version_allows_import(client, admin_headers, db_session, attach_wsi, patched_openslide, monkeypatch):
    """A bundle whose schema_version matches imports even when the app_version
    string differs — two releases sharing a data shape can exchange slides."""
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])  # schema_version=CURRENT, app_version=real
    content = _download(client, admin_headers, result["export_id"])

    # Different app_version, same schema_version (CURRENT left untouched).
    monkeypatch.setattr(_st, "APP_VERSION", "9.9.9-other")
    body = _stage(client, admin_headers, content, result["archive_format"])
    assert body["plan"]["blocking_code"] is None, body["plan"]
    assert body["plan"]["bundle_app_version"] == APP_VERSION

    rj = _apply(client, admin_headers, body["stage_id"])  # default resolution = copy
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["imported_count"] == 1 and rj["result"]["failed_count"] == 0


def test_rejects_non_bundle_archive(client, admin_headers):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"not a bundle"
        info = tarfile.TarInfo("random.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    r = _stage_job(client, admin_headers, buf.getvalue(), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importNotABundle"


def test_slide_bundle_rejected_by_whole_db_restore(client, admin_headers, db_session, attach_wsi, patched_openslide):
    """A slide-export bundle fed to the whole-DB Restore is refused with a
    clear message rather than a cryptic 'missing wsi.db'."""
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])
    r = client.post(
        "/api/admin/restore", headers=admin_headers,
        files={"file": (f"bundle.{result['archive_format']}", io.BytesIO(content),
                        "application/octet-stream")},
    )
    # Staging runs as a job; the refusal is its error.
    assert r.status_code == 202, r.text
    job = _poll(client, r.json()["job_id"], admin_headers)
    assert job["state"] == "failed"
    assert "Slide Import" in job["error"]


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------

def test_export_requires_admin(client, manager_headers, instructor_headers, slide, attach_wsi, db_session):
    attach_wsi(slide)
    for hdr in (manager_headers, instructor_headers):
        r = client.post("/api/admin/slides/export",
                        json={"slide_ids": [slide.id]}, headers=hdr)
        assert r.status_code == 403, r.text
    # Unauthenticated.
    assert client.post("/api/admin/slides/export", json={"slide_ids": [slide.id]}).status_code == 401


def test_import_requires_admin(client, manager_headers, instructor_headers):
    files = {"file": ("bundle.tar.zst", io.BytesIO(b"x"), "application/octet-stream")}
    for hdr in (manager_headers, instructor_headers):
        assert client.post("/api/admin/slides/import", headers=hdr, files=files).status_code == 403
    assert client.post("/api/admin/slides/import", files=files).status_code == 401
    assert client.post("/api/admin/slides/import/apply",
                       json={"stage_id": "import-000000000000"}).status_code == 401


# ---------------------------------------------------------------------------
# Concurrency + download token + sweep
# ---------------------------------------------------------------------------

def test_export_conflicts_with_running_job(client, admin_headers, slide, attach_wsi):
    attach_wsi(slide)
    job = _jobs.store.try_claim_running("backup")
    assert job is not None
    _jobs.store.update(job.id, state="running")
    try:
        r = client.post("/api/admin/slides/export",
                        json={"slide_ids": [slide.id]}, headers=admin_headers)
        assert r.status_code == 409, r.text
        # Coded, so the dashboard can show it in the admin's language.
        assert r.json()["detail"]["code"] == "adminDash.apiErr.transferBusy"
        # The upload, too, is refused while the slot is taken.
        r = client.post(
            "/api/admin/slides/import", headers=admin_headers,
            files={"file": ("b.tar.gz", io.BytesIO(b"x"), "application/octet-stream")},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "adminDash.apiErr.transferBusy"
    finally:
        _jobs.store.update(job.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)


def test_cancel_import_stage_refused_while_apply_running(client, admin_headers):
    """Dropping a stage while an import-apply runs would rmtree the stage
    from under the in-flight apply and corrupt the import. A running
    slide_import job must make cancel 409 instead of deleting the tree."""
    job = _jobs.store.try_claim_running("slide_import")
    assert job is not None
    _jobs.store.update(job.id, state="running")
    try:
        r = client.delete(
            "/api/admin/slides/import/stage/import-000000000000",
            headers=admin_headers,
        )
        assert r.status_code == 409, r.text
    finally:
        _jobs.store.update(job.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)


def test_cancel_import_stage_allowed_when_idle(client, admin_headers):
    """With no import-apply in flight the cancel is idempotent and returns 200
    (an unrelated running backup does not block it)."""
    other = _jobs.store.try_claim_running("backup")
    assert other is not None
    _jobs.store.update(other.id, state="running")
    try:
        r = client.delete(
            "/api/admin/slides/import/stage/import-000000000000",
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
    finally:
        _jobs.store.update(other.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)


def test_export_download_token_round_trip(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    export_id = result["export_id"]

    r = client.post(f"/api/admin/slides/export/{export_id}/download_url", headers=admin_headers)
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith(f"/api/admin/slides/export/{export_id}/download?t=")

    # Token path needs no Authorization header.
    dl = client.get(url)
    assert dl.status_code == 200 and len(dl.content) > 0

    # A tampered / foreign token is rejected.
    bad = url.split("?t=")[0] + "?t=deadbeef"
    assert client.get(bad).status_code == 401
    # Minting the URL itself requires admin.
    assert client.post(f"/api/admin/slides/export/{export_id}/download_url").status_code == 401


def test_export_writes_sidecar_and_lists(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id], note="my export")
    export_id = result["export_id"]

    # A sidecar is written next to the bundle so the history survives the
    # 7-day job prune.
    rec = _st.find_export(export_id)
    assert rec is not None
    assert os.path.isfile(rec.path + ".meta.json")

    # The listing surfaces the real metadata (counts + note), not a stub.
    r = client.get("/api/admin/slides/export/history", headers=admin_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    row = next(i for i in items if i["export_id"] == export_id)
    assert row["slide_count"] == 1
    assert row["note"] == "my export"
    assert row["size_bytes"] > 0
    # The per-slide summary lets the history view tell two same-count bundles
    # apart; it carries the public_id + title + catalog_id of each slide.
    assert row["slides"] == [
        {"public_id": src.public_id, "title": "Source Slide", "catalog_id": "SRC-1"}
    ]


def test_export_list_stub_row_when_sidecar_missing(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    export_id = _export(client, admin_headers, [src.id])["export_id"]
    rec = _st.find_export(export_id)
    os.remove(rec.path + ".meta.json")

    # A missing sidecar still yields a listable (deletable) stub row.
    r = client.get("/api/admin/slides/export/history", headers=admin_headers)
    row = next(i for i in r.json()["items"] if i["export_id"] == export_id)
    assert row["slide_count"] == 0 and row["size_bytes"] > 0
    # No sidecar → no per-slide summary either (empty, not missing).
    assert row["slides"] == []


def test_export_delete_removes_bundle_and_sidecar(client, admin_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    export_id = _export(client, admin_headers, [src.id])["export_id"]
    rec = _st.find_export(export_id)
    archive, sidecar = rec.path, rec.path + ".meta.json"
    assert os.path.isfile(archive) and os.path.isfile(sidecar)

    r = client.delete(f"/api/admin/slides/export/{export_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert not os.path.exists(archive) and not os.path.exists(sidecar)

    # Second delete 404s (idempotent-ish: nothing left to remove).
    assert client.delete(f"/api/admin/slides/export/{export_id}", headers=admin_headers).status_code == 404
    # And it no longer appears in the listing / can't be re-downloaded.
    assert all(i["export_id"] != export_id for i in client.get("/api/admin/slides/export/history", headers=admin_headers).json()["items"])
    assert client.post(f"/api/admin/slides/export/{export_id}/download_url", headers=admin_headers).status_code == 404


def test_export_list_delete_require_admin(client, admin_headers, manager_headers, instructor_headers, db_session, attach_wsi, patched_openslide):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    export_id = _export(client, admin_headers, [src.id])["export_id"]
    for hdr in (manager_headers, instructor_headers):
        assert client.get("/api/admin/slides/export/history", headers=hdr).status_code == 403
        assert client.delete(f"/api/admin/slides/export/{export_id}", headers=hdr).status_code == 403
    assert client.get("/api/admin/slides/export/history").status_code == 401
    assert client.delete(f"/api/admin/slides/export/{export_id}").status_code == 401
    # A malformed id is rejected by the route pattern (422), never reaching the fs.
    assert client.delete("/api/admin/slides/export/not-an-id", headers=admin_headers).status_code == 422


def test_sweep_reclaims_transfer_staging(tmp_path):
    staging = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(staging, exist_ok=True)
    imp = os.path.join(staging, "import-abcdef012345")
    exp = os.path.join(staging, "export-abcdef012345.tar.zst")
    # The sidecar shares the export- prefix, so the TTL reclaims it too.
    meta = exp + ".meta.json"
    os.makedirs(imp, exist_ok=True)
    with open(exp, "wb") as f:
        f.write(b"x")
    with open(meta, "w") as f:
        f.write("{}")
    old = time.time() - 100000
    os.utime(imp, (old, old))
    os.utime(exp, (old, old))
    os.utime(meta, (old, old))

    _backup.sweep_stale_staging(ttl_seconds=1)
    assert not os.path.exists(imp)
    assert not os.path.exists(exp)
    assert not os.path.exists(meta)


def test_export_unknown_slide_404(client, admin_headers):
    r = client.post("/api/admin/slides/export",
                    json={"slide_ids": [9_999_999]}, headers=admin_headers)
    assert r.status_code == 404


def test_import_batch_isolates_per_slide_failure(client, admin_headers, db_session, attach_wsi, monkeypatch):
    """One slide failing mid-finalize must not poison the DB session or abort
    the rest of the batch (guards the rollback-and-continue contract)."""
    s1, _t1, _b1 = _build_source(db_session, attach_wsi)
    s2, _t2, _b2 = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [s1.id, s2.id])
    content = _download(client, admin_headers, result["export_id"])

    # Fail the FIRST slide's finalize (unreadable transfer); the second must
    # still import cleanly afterwards.
    calls = {"n": 0}

    def flaky_props(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated unreadable transfer")
        return _fake_props()

    monkeypatch.setattr(_st, "read_slide_properties", flaky_props)
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda slide, path: None)

    body = _stage(client, admin_headers, content, result["archive_format"])
    rj = _apply(client, admin_headers, body["stage_id"])
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["failed_count"] == 1, res
    assert res["imported_count"] == 1, res


def test_import_stage_kept_on_total_failure_then_retryable(
    client, admin_headers, db_session, attach_wsi, monkeypatch
):
    """When every slide fails and none imports, the stage tree is kept so
    the operator can retry the apply without re-uploading a (potentially huge)
    bundle. A later successful apply of the same stage then cleans it up."""
    src, _tour, _base = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])

    # Fail reserve (runs BEFORE the staged WSI is moved into the live tree, so
    # the stage stays intact and a retry is viable). Toggle drives two applies.
    real_reserve = _st.reserve_next_slide_id
    state = {"fail": True}

    def flaky_reserve(db):
        if state["fail"]:
            raise RuntimeError("simulated transient DB lock during reserve")
        return real_reserve(db)

    monkeypatch.setattr(_st, "reserve_next_slide_id", flaky_reserve)
    monkeypatch.setattr(_st, "read_slide_properties", lambda p: _fake_props())
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda slide, path: None)

    body = _stage(client, admin_headers, content, result["archive_format"])
    stage_id = body["stage_id"]
    stage_dir = _st._stage_dir(stage_id)

    # First apply: the only slide fails -> wholesale failure. Stage is kept.
    rj = _apply(client, admin_headers, stage_id)
    res = rj["result"]
    assert res["failed_count"] == 1 and res["imported_count"] == 0, res
    assert os.path.isdir(stage_dir), "stage must be kept on a total-failure apply"

    # Clear the transient fault and re-apply the SAME stage: it imports now and
    # the spent stage is cleaned up.
    state["fail"] = False
    rj2 = _apply(client, admin_headers, stage_id)
    res2 = rj2["result"]
    assert res2["imported_count"] == 1 and res2["failed_count"] == 0, res2
    assert not os.path.isdir(stage_dir), "stage must be removed once an apply succeeds"


# ---------------------------------------------------------------------------
# Security: crafted-bundle hardening (public_id traversal, count cap)
# ---------------------------------------------------------------------------

def _make_crafted_bundle(manifest: dict, slides_doc: dict) -> bytes:
    """Build a minimal .tar.gz bundle with attacker-chosen manifest/slides.json."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, obj in (("manifest.json", manifest), ("slides.json", slides_doc)):
            data = json.dumps(obj).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _base_manifest(**over):
    m = {
        "bundle_format": "tessera-slide-export/1",
        "app_version": APP_VERSION,
        "archive_format": "tar.gz",
        "slide_count": 1,
        "tour_count": 0,
        "slide_public_ids": ["x"],
        "note": "",
    }
    m.update(over)
    return m


def test_import_rejects_slide_public_id_traversal(client, admin_headers):
    slides_doc = {"slides": [{
        "public_id": "../../../../../../etc",
        "wsi_original_basename": "x.svs",
        "tours": [],
    }]}
    r = _stage_job(client, admin_headers, _make_crafted_bundle(_base_manifest(), slides_doc), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_import_rejects_tour_public_id_traversal(client, admin_headers):
    slides_doc = {"slides": [{
        "public_id": "abc123",
        "wsi_original_basename": "x.svs",
        "tours": [{"public_id": "../../../../etc", "title": "t", "steps": []}],
    }]}
    r = _stage_job(client, admin_headers, _make_crafted_bundle(_base_manifest(), slides_doc), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_import_rejects_too_many_slides(client, admin_headers):
    slides = [{"public_id": f"s{i}", "wsi_original_basename": "x.svs", "tours": []}
              for i in range(_st.IMPORT_MAX_SLIDES + 1)]
    r = _stage_job(client, admin_headers,
               _make_crafted_bundle(_base_manifest(slide_count=len(slides)), {"slides": slides}),
               "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_import_corrupt_archive_is_refused_not_a_server_error(client, admin_headers):
    """A truncated/garbage upload (not a readable tar/zst) must surface as
    a 400 with the i18n code, not leak a 500 from tarfile/zstd."""
    r = _stage_job(client, admin_headers, b"this is definitely not a tar archive", "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importCorrupt"


def test_import_scalar_tours_is_refused_not_a_server_error(client, admin_headers):
    """A crafted bundle whose ``tours`` is a scalar must not TypeError
    when _validate_slides_doc iterates it — refuse the bundle instead."""
    slides_doc = {"slides": [{
        "public_id": "abc123",
        "wsi_original_basename": "x.svs",
        "tours": 5,  # not a list
    }]}
    r = _stage_job(client, admin_headers,
               _make_crafted_bundle(_base_manifest(), slides_doc), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_import_step_refuses_an_unknown_pin_shape():
    # A pin shape outside the PinShape set is a content error in the bundle,
    # as it is for every other writer (PinItem).
    with pytest.raises(ValueError):
        _st._bundle_step_fields({
            "order": 1, "center_x": 1.0, "center_y": 1.0,
            "pins": [{"id": "a", "x": 1.0, "y": 2.0, "shape": "star"}],
        })


def test_import_step_keeps_an_empty_or_missing_label():
    # Only the step editor requires a label (admin_update_step); a bundled
    # step without one still imports.
    assert _st._bundle_step_fields({"order": 1, "label": ""})["label"] == ""
    assert _st._bundle_step_fields({"order": 1, "label": "   "})["label"] == "   "
    assert _st._bundle_step_fields({"order": 1})["label"] == ""


def test_staged_wsi_file_rejects_traversal(tmp_path):
    # Defense-in-depth: even reached directly with a traversal id, the WSI
    # locator refuses to resolve outside the staged wsi/ subtree.
    assert _st._staged_wsi_file(str(tmp_path), "../../../../etc") is None
    assert _st._staged_wsi_file(str(tmp_path), "a/b") is None
    assert _st._staged_wsi_file(str(tmp_path), "") is None


def test_build_tour_remap_keeps_free_ids_and_remints_taken_ones(db_session, slide):
    taken = Tour(slide_id=slide.id, title="t")
    db_session.add(taken)
    db_session.commit()
    free = "free" + os.urandom(4).hex()
    remap = _st._build_tour_remap(db_session, [
        {"tours": [{"public_id": free}, {"public_id": taken.public_id}]},
    ])
    assert remap[free] == free
    assert remap[taken.public_id] != taken.public_id
    assert valid_public_id(remap[taken.public_id])


def test_import_reserve_failure_isolated(client, admin_headers, db_session, attach_wsi, monkeypatch):
    """A failure originating in reserve_next_slide_id (outside _import_one_slide's
    own try) must be isolated per-slide, not abort/poison the whole batch."""
    s1, _t1, _b1 = _build_source(db_session, attach_wsi)
    s2, _t2, _b2 = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [s1.id, s2.id])
    content = _download(client, admin_headers, result["export_id"])

    real_reserve = _st.reserve_next_slide_id
    calls = {"n": 0}

    def flaky_reserve(db):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB lock during reserve")
        return real_reserve(db)

    monkeypatch.setattr(_st, "reserve_next_slide_id", flaky_reserve)
    monkeypatch.setattr(_st, "read_slide_properties", lambda p: _fake_props())
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda slide, path: None)

    body = _stage(client, admin_headers, content, result["archive_format"])
    rj = _apply(client, admin_headers, body["stage_id"])
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["failed_count"] == 1, res
    assert res["imported_count"] == 1, res


# ---------------------------------------------------------------------------
# Filename-collision hardening (plan advisory + commit-time race mapping)
# ---------------------------------------------------------------------------

def test_plan_flags_catalog_id_conflict(client, admin_headers, db_session, attach_wsi, patched_openslide):
    """A bundle whose slide shares an existing catalog_id is flagged (advisory)
    in the import plan. catalog_id has no uniqueness constraint, so the import
    still succeeds — the flag just warns the operator of the coming duplicate."""
    src, _tour, _base = _build_source(db_session, attach_wsi)  # catalog_id "SRC-1"
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])

    body = _stage(client, admin_headers, content, result["archive_format"])
    row = body["plan"]["slides"][0]
    # The source (catalog_id "SRC-1") is still in the DB at plan time, so the
    # bundle's identical catalog_id collides.
    assert row["catalog_id"] == "SRC-1"
    assert row["catalog_id_conflict"] is True


def test_import_filename_race_maps_to_conflict_code(db_session, tmp_path, monkeypatch):
    """When the UNIQUE(wsi_filename_norm) backstop fires at commit — a name that
    slipped past the _norm_exists preflight because a concurrent upload grabbed
    it in the finalize window — _import_one_slide surfaces the actionable
    importFilenameConflict code, not the opaque importSlideFailed."""
    # A committed slide already owns this norm...
    existing = Slide(wsi_filename="9999_race.svs", wsi_filename_norm="race.svs")
    db_session.add(existing)
    db_session.commit()

    # ...but make the preflight *miss* it, reproducing the race where the row
    # appears only after the _norm_exists check but before the commit.
    monkeypatch.setattr(_st, "_norm_exists", lambda db, norm: False)
    monkeypatch.setattr(_st, "read_slide_properties", lambda p: _fake_props())
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda s, p: None)

    # Minimal staged bundle tree carrying just the WSI file for this slide.
    stage_dir = _st._stage_dir("import-" + os.urandom(6).hex())
    os.makedirs(os.path.join(stage_dir, "wsi", "racepid"))
    with open(os.path.join(stage_dir, "wsi", "racepid", "race.svs"), "wb") as f:
        f.write(b"WSIDATA")
    try:
        sd = {"public_id": "racepid", "wsi_original_basename": "race.svs", "tours": []}
        with pytest.raises(_st.SlideTransferError) as ei:
            _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert ei.value.code == "importFilenameConflict"
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def _stage_one_wsi(pid: str, basename: str) -> str:
    stage_dir = _st._stage_dir("import-" + os.urandom(6).hex())
    os.makedirs(os.path.join(stage_dir, "wsi", pid))
    with open(os.path.join(stage_dir, "wsi", pid, basename), "wb") as f:
        f.write(b"WSIDATA")
    return stage_dir


def _patch_wsi_readers(monkeypatch):
    monkeypatch.setattr(_st, "read_slide_properties", lambda p: _fake_props())
    monkeypatch.setattr(_st, "make_thumbnail", _fake_thumbnail)
    monkeypatch.setattr(_st, "attach_associated_images", lambda s, p: None)


def test_import_normalises_untrusted_bundle_values(db_session, monkeypatch):
    """Bundle values are untrusted: out-of-range/garbage image-adjust presets
    and overlay flags are normalised like the PATCH endpoints, and a NaN MPP
    override becomes "unset" instead of failing the slide (it would hit the
    NOT NULL column and be misreported as importFilenameConflict)."""
    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi("valpid", "val.svs")
    try:
        sd = {
            "public_id": "valpid", "wsi_original_basename": "val.svs",
            "title": "T", "catalog_id": "C", "tours": [],
            "mpp_x": float("nan"), "mpp_y": "abc",
            "image_adjust_brightness": 99, "image_adjust_contrast": "x",
            "image_adjust_saturation": 1.0,
            "show_zoom_indicator": "yes", "show_scale_bar": False,
        }
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        s = db_session.get(Slide, res["slide_id"])
        assert s.mpp_x == 0.0 and s.mpp_y == 0.0
        assert s.image_adjust_brightness == 1.5
        assert s.image_adjust_contrast is None
        assert s.image_adjust_saturation is None
        assert s.show_zoom_indicator is None
        assert s.show_scale_bar is False
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_import_applies_publish_gate(db_session, monkeypatch):
    """A bundle slide flagged published but missing its title imports as a
    draft (same gate as admin_publish_slide), and its published tour follows
    it to draft (same gate as the tour publish endpoint)."""
    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi("gatepid", "gate.svs")
    try:
        sd = {
            "public_id": "gatepid", "wsi_original_basename": "gate.svs",
            "title": "  ", "catalog_id": "C", "is_published": True,
            "tours": [{"public_id": "gatetour", "title": "t", "is_published": True, "steps": []}],
        }
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        s = db_session.get(Slide, res["slide_id"])
        assert s.is_published is False
        tour = db_session.query(Tour).filter_by(slide_id=s.id).one()
        assert tour.is_published is False
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_stage_import_enospc_reports_no_space(tmp_path, monkeypatch):
    """A disk that fills mid-extraction (the preflight only compares the
    compressed size) is reported as importNoSpace, not as a corrupt bundle."""
    import errno

    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(_make_crafted_bundle(_base_manifest(), {"slides": []}))

    def _full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_backup, "_safe_extract", _full)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.stage_import(str(archive))
    assert ei.value.code == "importNoSpace"


def test_import_copies_tour_assets_outside_write_transaction(db_session, monkeypatch):
    """Tour assets are copied before the first flush, so SQLite's write lock
    is not held across the (per-file validated) copy."""
    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi("lockpid", "lock.svs")
    os.makedirs(os.path.join(stage_dir, "assets", "tours", "locktour"))
    seen = []

    def _record(src, dst):
        raw = db_session.connection().connection.dbapi_connection
        seen.append(raw.in_transaction)
        os.makedirs(dst, exist_ok=True)
        return []

    monkeypatch.setattr(_st, "_import_tour_assets", _record)
    try:
        sd = {
            "public_id": "lockpid", "wsi_original_basename": "lock.svs",
            "title": "T", "catalog_id": "C",
            "tours": [{"public_id": "locktour", "title": "t", "steps": []}],
        }
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert res["tours"] == 1
        assert seen == [False]
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Step validation, tour-id remap vs skipped slides, and
# attachments the target refused
# ---------------------------------------------------------------------------

def _stage_bundle(slides: list, *, assets: dict | None = None) -> str:
    """A staged import (as stage_import leaves it) holding ``slides``; each
    slide's WSI is a placeholder, ``assets`` maps tour pid -> {name: bytes}."""
    from wsi_app.migrations import CURRENT_SCHEMA_VERSION

    stage_id = "import-" + os.urandom(6).hex()
    stage_dir = _st._stage_dir(stage_id)
    for sd in slides:
        d = os.path.join(stage_dir, "wsi", sd["public_id"])
        os.makedirs(d)
        with open(os.path.join(d, sd["wsi_original_basename"]), "wb") as f:
            f.write(b"WSIDATA")
    for tour_pid, files in (assets or {}).items():
        d = os.path.join(stage_dir, "assets", "tours", tour_pid)
        os.makedirs(d)
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
    manifest = _base_manifest(schema_version=CURRENT_SCHEMA_VERSION, slide_count=len(slides))
    with open(os.path.join(stage_dir, _st.MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(stage_dir, _st.SLIDES_DOC_NAME), "w") as f:
        json.dump({"slides": slides}, f)
    return stage_id


def _slide_doc(pid, basename, tours):
    return {
        "public_id": pid, "wsi_original_basename": basename,
        "title": f"T-{pid}", "catalog_id": f"C-{pid}", "tours": tours,
    }


@pytest.mark.parametrize("bad", [
    {"center_x": "Infinity"},
    {"fov_um_w": -5},
    {"rotation_deg": "NaN"},
    {"order": 0},
    {"label": "two\nlines"},
])
def test_import_refuses_steps_the_editor_would_refuse(db_session, monkeypatch, bad):
    """An import that float()ed the step fields straight into the row would
    store "Infinity" (a legal JSON string) and fail every later JSON response
    for the tour; a negative field of view would break the editor's own
    invariant. The slide is refused before anything is written."""
    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi("badstep", "badstep.svs")
    before = db_session.query(Slide).count()
    try:
        step = {"order": 1, "center_x": 1.0, "center_y": 1.0, **bad}
        sd = _slide_doc("badstep", "badstep.svs", [
            {"public_id": "badtour", "title": "Bad tour", "steps": [step]},
        ])
        with pytest.raises(_st.SlideTransferError) as ei:
            _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert ei.value.code == "importStepInvalid"
        assert ei.value.params["tour"] == "Bad tour"
        assert ei.value.params["step"] == 1
        assert db_session.query(Slide).count() == before
        # Nothing was consumed: the staged WSI is still there for a retry.
        assert _st._staged_wsi_file(stage_dir, "badstep") is not None
        assert _st._bundle_content_errors(sd)
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_plan_lists_invalid_steps_and_assets_over_the_limit(db_session, monkeypatch):
    object.__setattr__(settings, "tour_asset_max_files", 1)
    stage_id = _stage_bundle(
        [_slide_doc("planpid", "plan.svs", [{
            "public_id": "plantour", "title": "P",
            "steps": [{"order": 1, "fov_um_w": 0}],
        }])],
        assets={"plantour": {"a.png": _png_bytes(), "b.png": _png_bytes()}},
    )
    try:
        stage_dir = _st._stage_dir(stage_id)
        with open(os.path.join(stage_dir, _st.SLIDES_DOC_NAME)) as f:
            doc = json.load(f)
        with open(os.path.join(stage_dir, _st.MANIFEST_NAME)) as f:
            manifest = json.load(f)
        plan = _st.plan_import(stage_id, manifest, doc, db_session)
        row = plan["slides"][0]
        assert row["content_error_count"] == 1
        assert row["content_errors"][0]["step"] == 1
        assert row["assets_over_limit"] == 1
    finally:
        object.__setattr__(settings, "tour_asset_max_files", 200)
        _st.cleanup_import_stage(stage_id)


def test_skipped_slide_keeps_references_to_its_existing_assets(db_session, monkeypatch):
    """B's step shows an image of A's tour. A is skipped (its file already
    exists here, with that tour and image), so a remap built up front must
    not give A's tour a new id anyway and rewrite B's reference to a
    directory that is never created."""
    _patch_wsi_readers(monkeypatch)
    existing = Slide(title="A here", catalog_id="A0", wsi_filename="1_a.svs",
                     wsi_filename_norm="a.svs")
    db_session.add(existing)
    db_session.commit()
    db_session.add(Tour(slide_id=existing.id, public_id="sharedtour", title="A tour"))
    db_session.commit()
    adir = tour_asset_dir("sharedtour")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "shared.png"), "wb") as f:
        f.write(_png_bytes())

    ref = "![s](/assets/tours/sharedtour/shared.png)"
    stage_id = _stage_bundle([
        _slide_doc("srcA", "a.svs", [{"public_id": "sharedtour", "title": "A tour", "steps": []}]),
        _slide_doc("srcB", "b.svs", [{
            "public_id": "btour", "title": "B tour",
            "steps": [{"order": 1, "description_md": ref}],
        }]),
    ])
    try:
        result = _st.apply_import(stage_id, {"srcA": "skip"})
        assert result["imported_count"] == 1 and result["skipped_count"] == 1
        new_b = result["imported"][0]["slide_id"]
        tour_b = db_session.query(Tour).filter_by(slide_id=new_b).one()
        step = db_session.query(Step).filter_by(tour_id=tour_b.id).one()
        assert step.description_md == ref
        assert result["warning_count"] == 0
    finally:
        _st.cleanup_import_stage(stage_id)
        shutil.rmtree(adir, ignore_errors=True)


def test_reference_into_a_skipped_slide_without_assets_is_reported(db_session, monkeypatch):
    _patch_wsi_readers(monkeypatch)
    existing = Slide(title="C here", catalog_id="C0", wsi_filename="1_c.svs",
                     wsi_filename_norm="c.svs")
    db_session.add(existing)
    db_session.commit()
    stage_id = _stage_bundle([
        _slide_doc("srcC", "c.svs", [{"public_id": "ctour", "title": "C tour", "steps": []}]),
        _slide_doc("srcD", "d.svs", [{
            "public_id": "dtour", "title": "D tour",
            "steps": [{"order": 1, "description_md": "![x](/assets/tours/ctour/x.png)"}],
        }]),
    ], assets={"ctour": {"x.png": _png_bytes()}})
    try:
        result = _st.apply_import(stage_id, {"srcC": "skip"})
        assert result["imported_count"] == 1
        codes = [(w["code"], w["params"].get("url")) for w in result["warnings"]]
        assert codes == [("importAssetRefMissing", "/assets/tours/ctour/x.png")]
    finally:
        _st.cleanup_import_stage(stage_id)


def test_attachments_refused_by_target_limits_are_reported(db_session, monkeypatch):
    """Attachments over this system's limits must not be skipped with only a
    log line while the job reports plain success."""
    _patch_wsi_readers(monkeypatch)
    object.__setattr__(settings, "tour_asset_max_files", 1)
    md = "![a](/assets/tours/limtour/a.png) ![b](/assets/tours/limtour/b.png)"
    stage_id = _stage_bundle(
        [_slide_doc("limpid", "lim.svs", [{
            "public_id": "limtour", "title": "Lim",
            "steps": [{"order": 1, "description_md": md}],
        }])],
        assets={"limtour": {"a.png": _png_bytes(), "b.png": _png_bytes()}},
    )
    try:
        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 1
        imported = result["imported"][0]
        # Reported through the (capped) warnings only, not on the result.
        assert "assets_skipped" not in imported
        skip = next(w for w in result["warnings"] if w["code"] == "importAssetSkipped")
        assert skip["params"] == {"tour": "Lim", "name": "b.png", "reason": "tour_file_limit"}
        warnings = {(w["code"], w["params"].get("name") or w["params"].get("url")) for w in result["warnings"]}
        new_pid = db_session.query(Tour).filter_by(slide_id=imported["slide_id"]).one().public_id
        assert warnings == {
            ("importAssetSkipped", "b.png"),
            ("importAssetRefMissing", f"/assets/tours/{new_pid}/b.png"),
        }
        assert result["warning_count"] == 2
    finally:
        object.__setattr__(settings, "tour_asset_max_files", 200)
        _st.cleanup_import_stage(stage_id)


# ---------------------------------------------------------------------------
# Partial-success retry: the stage is kept while something is left to retry,
# and re-running apply (or the attachment retry) on it picks up from the
# per-slide outcomes recorded in apply-state.json.
# ---------------------------------------------------------------------------

class _Crash(BaseException):
    """Stands in for the process dying: a BaseException, so neither the
    per-slide ``except Exception`` in apply_import nor any cleanup written for
    ordinary failures handles it."""


@pytest.fixture
def asset_settings():
    """Restore the tour-asset settings a test overrides."""
    keys = (
        "tour_asset_max_files", "tour_asset_max_bytes",
        "tour_asset_max_total_bytes", "tour_asset_allowed_mime_prefixes",
    )
    saved = {k: getattr(settings, k) for k in keys}

    def _set(**over):
        for k, v in over.items():
            object.__setattr__(settings, k, v)

    yield _set
    _set(**saved)


def _tok() -> str:
    # Tables are session-shared: every public id / filename a test mints
    # carries a random token so tests never see each other's rows.
    return os.urandom(3).hex()


def _failing_props(bad: set):
    """read_slide_properties that fails for any WSI whose path contains one
    of ``bad`` (mutable: clear it to "fix the cause" before a retry)."""
    def _props(path):
        if any(b in path for b in bad):
            raise RuntimeError("simulated unreadable WSI")
        return _fake_props()
    return _props


def _slides_named(db, *basenames) -> list:
    db.expire_all()
    return db.query(Slide).filter(
        Slide.wsi_filename_norm.in_([b.casefold() for b in basenames])
    ).all()


def _state(stage_id) -> dict:
    with open(os.path.join(_st._stage_dir(stage_id), _st.APPLY_STATE_NAME)) as f:
        return json.load(f)


def test_partial_failure_keeps_stage_and_retry_imports_only_the_failed_slide(db_session, monkeypatch):
    """A partial success keeps the stage: cleaning it up would mean
    re-uploading the whole (multi-GB) bundle to retry the one failed slide."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    good, bad = f"good-{t}.svs", f"bad-{t}.svs"
    broken = {bad}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"g{t}", good, []),
        _slide_doc(f"b{t}", bad, []),
    ])
    stage_dir = _st._stage_dir(stage_id)
    try:
        first = _st.apply_import(stage_id, {})
        assert first["imported_count"] == 1 and first["failed_count"] == 1
        assert first["stage_kept"] is True
        assert first["retry"] == {"failed_slides": 1, "skipped_assets": 0}
        assert os.path.isdir(stage_dir)
        recorded = _state(stage_id)["slides"]
        assert recorded[f"g{t}"]["status"] == "imported"
        assert recorded[f"g{t}"]["slide_id"] == first["imported"][0]["slide_id"]
        assert recorded[f"b{t}"]["status"] == "failed"

        broken.clear()  # the operator fixed the cause
        second = _st.apply_import(stage_id, {})
        assert second["imported_count"] == 1 and second["failed_count"] == 0
        assert second["imported"][0]["source_public_id"] == f"b{t}"
        assert [a["source_public_id"] for a in second["already_imported"]] == [f"g{t}"]
        assert second["already_imported_count"] == 1
        # Nothing left to retry: the stage is gone, as after a full success.
        assert second["stage_kept"] is False
        assert not os.path.exists(stage_dir)
        # And no slide was imported twice.
        rows = _slides_named(db_session, good, bad)
        assert sorted(r.wsi_filename_norm for r in rows) == sorted([good.casefold(), bad.casefold()])
    finally:
        _st.cleanup_import_stage(stage_id)


def test_reapply_is_idempotent_once_a_slide_is_imported(db_session, monkeypatch):
    """Applying a kept stage again while the cause persists must not import
    anything a second time."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    broken = {f"bad-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"i{t}", f"idem-{t}.svs", []),
        _slide_doc(f"x{t}", f"bad-{t}.svs", []),
    ])
    try:
        _st.apply_import(stage_id, {})
        again = _st.apply_import(stage_id, {})  # still failing: kept again
        assert again["imported_count"] == 0 and again["already_imported_count"] == 1
        assert again["failed_count"] == 1 and again["stage_kept"] is True
        assert len(_slides_named(db_session, f"idem-{t}.svs")) == 1
    finally:
        _st.cleanup_import_stage(stage_id)


@pytest.mark.parametrize("hardlinks", [True, False], ids=["hardlink", "copy"])
def test_slide_deleted_after_import_is_importable_again(client, admin_headers, db_session, monkeypatch, hardlinks):
    """The staged WSI is linked (or, across filesystems, copied) into wsi/
    instead of moved, so a kept stage can re-import a slide whose import was
    deleted meanwhile; its record no longer matches a row and is dropped."""
    _patch_wsi_readers(monkeypatch)
    if not hardlinks:
        import errno as _errno

        def _no_link(src, dst, *a, **k):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")
        monkeypatch.setattr(_st.os, "link", _no_link)
    t = _tok()
    keep_basename = f"del-{t}.svs"
    broken = {f"bad-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"d{t}", keep_basename, []),
        _slide_doc(f"f{t}", f"bad-{t}.svs", []),
    ])
    try:
        first = _st.apply_import(stage_id, {})
        new_id = first["imported"][0]["slide_id"]
        # The staged copy survived the import.
        assert _st._staged_wsi_file(_st._stage_dir(stage_id), f"d{t}") is not None
        assert client.delete(f"/api/admin/slides/{new_id}", headers=admin_headers).status_code == 200

        plan = _st.stage_plan(stage_id, db_session)["plan"]
        row = next(s for s in plan["slides"] if s["public_id"] == f"d{t}")
        assert row["apply_status"] is None  # deleted: not imported any more

        broken.clear()
        second = _st.apply_import(stage_id, {})
        assert second["imported_count"] == 2 and second["already_imported_count"] == 0
        again = next(r for r in second["imported"] if r["source_public_id"] == f"d{t}")
        assert again["slide_id"] != new_id
        # The bundle public_id was free again, so it is preserved.
        assert again["public_id"] == f"d{t}"
        assert os.path.isfile(os.path.join(settings.data_dir, "wsi", again["wsi_filename"]))
        assert len(_slides_named(db_session, keep_basename)) == 1
    finally:
        _st.cleanup_import_stage(stage_id)


def test_crash_between_commit_and_state_write_never_imports_twice(db_session, monkeypatch):
    """The process dies after a slide's final commit but before its outcome
    is recorded. The "pending" record written ahead of the commit names the
    slide id, so the retry recognises the finished row as imported."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    first_b, second_b = f"c1-{t}.svs", f"c2-{t}.svs"
    stage_id = _stage_bundle([
        _slide_doc(f"c{t}", first_b, []),
        _slide_doc(f"n{t}", second_b, []),
    ])
    real_save = _st._save_apply_state

    def _dying_save(stage_dir, state):
        if any(r.get("status") == "imported" for r in state["slides"].values()):
            raise _Crash()
        real_save(stage_dir, state)

    try:
        monkeypatch.setattr(_st, "_save_apply_state", _dying_save)
        with pytest.raises(_Crash):
            _st.apply_import(stage_id, {})
        monkeypatch.setattr(_st, "_save_apply_state", real_save)
        # On disk: the first slide is committed, its record still "pending";
        # the second was never attempted.
        assert len(_slides_named(db_session, first_b)) == 1
        recorded = _state(stage_id)["slides"]
        assert recorded[f"c{t}"]["status"] == "pending"
        assert f"n{t}" not in recorded
        # The read-only preflight already reports it as imported.
        plan = _st.stage_plan(stage_id, db_session)["plan"]
        assert {s["public_id"]: s["apply_status"] for s in plan["slides"]} == {
            f"c{t}": "imported", f"n{t}": None,
        }

        result = _st.apply_import(stage_id, {})
        assert [a["source_public_id"] for a in result["already_imported"]] == [f"c{t}"]
        assert [r["source_public_id"] for r in result["imported"]] == [f"n{t}"]
        assert len(_slides_named(db_session, first_b)) == 1
        assert len(_slides_named(db_session, second_b)) == 1
        assert result["stage_kept"] is False
    finally:
        _st.cleanup_import_stage(stage_id)


def test_crash_between_reservation_and_finalise_is_cleaned_up(db_session, monkeypatch):
    """The process dies after the placeholder row is committed but before the
    slide is finalised. The retry removes the placeholder (it holds the
    bundle public_id) and its leftover files, then imports the slide with
    its public_id preserved."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    basename = f"res-{t}.svs"
    pid = f"r{t}"
    stage_id = _stage_bundle([_slide_doc(pid, basename, [
        {"public_id": f"rt{t}", "title": "R", "steps": []},
    ])], assets={f"rt{t}": {"a.png": _png_bytes()}})
    real_reserve = _st._reserve_slide_row

    def _reserve_then_die(db, desired, before_commit=None):
        slide = real_reserve(db, desired, before_commit=before_commit)
        # What a dead attempt may have left on disk by then.
        os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
        with open(os.path.join(settings.data_dir, "wsi", f"{slide.id}_{basename}"), "wb") as f:
            f.write(b"partial")
        os.makedirs(tour_asset_dir(f"rt{t}"), exist_ok=True)
        raise _Crash()

    try:
        monkeypatch.setattr(_st, "_reserve_slide_row", _reserve_then_die)
        with pytest.raises(_Crash):
            _st.apply_import(stage_id, {})
        monkeypatch.setattr(_st, "_reserve_slide_row", real_reserve)
        rec = _state(stage_id)["slides"][pid]
        assert rec["status"] == "pending"
        assert rec["new_asset_dirs"] == [f"rt{t}"]
        db_session.expire_all()
        placeholder = db_session.get(Slide, rec["slide_id"])
        assert placeholder is not None and not placeholder.wsi_filename
        assert placeholder.public_id == pid

        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 1
        db_session.expire_all()
        assert db_session.get(Slide, rec["slide_id"]) is None
        assert not os.path.exists(os.path.join(settings.data_dir, "wsi", rec["wsi_filename"]))
        assert result["imported"][0]["public_id"] == pid
        tour = db_session.query(Tour).filter_by(slide_id=result["imported"][0]["slide_id"]).one()
        assert tour.public_id == f"rt{t}"
        assert os.path.isfile(os.path.join(tour_asset_dir(tour.public_id), "a.png"))
        assert db_session.query(Slide).filter_by(public_id=pid).count() == 1
    finally:
        _st.cleanup_import_stage(stage_id)


def test_attachment_retry_after_raising_the_per_tour_limit(db_session, monkeypatch, asset_settings):
    """An attachment refused by this system's per-tour file limit keeps the
    stage; once the limit is raised, the attachment-only retry copies it into
    the tour the import created — under its reminted id — and the warning
    clears. The tour's existing files count against the limit."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    tour_pid = f"lt{t}"
    # A tour already holding the bundle's tour id forces a remint on import.
    holder = Slide(title="holder", catalog_id="H", wsi_filename=f"1_h{t}.svs",
                   wsi_filename_norm=f"h{t}.svs")
    db_session.add(holder)
    db_session.commit()
    db_session.add(Tour(slide_id=holder.id, public_id=tour_pid, title="taken"))
    db_session.commit()

    asset_settings(tour_asset_max_files=1)
    md = f"![a](/assets/tours/{tour_pid}/a.png) ![b](/assets/tours/{tour_pid}/b.png)"
    stage_id = _stage_bundle(
        [_slide_doc(f"l{t}", f"lim-{t}.svs", [{
            "public_id": tour_pid, "title": "Lim",
            "steps": [{"order": 1, "description_md": md}],
        }])],
        assets={tour_pid: {"a.png": _png_bytes(), "b.png": _png_bytes()}},
    )
    try:
        first = _st.apply_import(stage_id, {})
        assert first["imported_count"] == 1 and first["failed_count"] == 0
        assert first["stage_kept"] is True
        assert first["retry"] == {"failed_slides": 0, "skipped_assets": 1}
        new_pid = db_session.query(Tour).filter_by(slide_id=first["imported"][0]["slide_id"]).one().public_id
        assert new_pid != tour_pid
        tour_dir = tour_asset_dir(new_pid)
        assert sorted(os.listdir(tour_dir)) == ["a.png"]

        # Still at the limit: a.png (already there) counts, b.png is refused.
        still = _st.retry_import_assets(stage_id)
        assert still["copied_count"] == 0 and still["still_skipped_count"] == 1
        assert still["stage_kept"] is True

        asset_settings(tour_asset_max_files=200)
        done = _st.retry_import_assets(stage_id)
        assert done["copied"] == [{"public_id": f"l{t}", "tour": "Lim", "name": "b.png"}]
        assert sorted(os.listdir(tour_dir)) == ["a.png", "b.png"]
        # The reference check re-ran: both images resolve now.
        assert done["warning_count"] == 0
        assert done["stage_kept"] is False
        assert not os.path.exists(_st._stage_dir(stage_id))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_type_allow_list_refusal_is_retryable_but_a_bad_extension_is_not(db_session, monkeypatch, asset_settings):
    """A PDF refused by TOUR_ASSET_ALLOWED_MIME_PREFIXES (a setting) keeps the
    stage and can be retried once allowed; a PNG named .html — the file's own
    defect — is reported (extension_mismatch) but keeps nothing."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    asset_settings(tour_asset_allowed_mime_prefixes=("image/",))
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    stage_id = _stage_bundle(
        [_slide_doc(f"p{t}", f"pdf-{t}.svs", [{"public_id": f"pt{t}", "title": "P", "steps": []}])],
        assets={f"pt{t}": {"doc.pdf": pdf, "evil.html": _png_bytes() + b"<script>"}},
    )
    try:
        first = _st.apply_import(stage_id, {})
        reasons = {
            w["params"]["name"]: w["params"]["reason"]
            for w in first["warnings"] if w["code"] == "importAssetSkipped"
        }
        assert reasons == {"doc.pdf": "type_not_allowed", "evil.html": "extension_mismatch"}
        assert first["retry"]["skipped_assets"] == 1 and first["stage_kept"] is True

        asset_settings(tour_asset_allowed_mime_prefixes=("image/", "application/pdf"))
        done = _st.retry_import_assets(stage_id)
        assert [c["name"] for c in done["copied"]] == ["doc.pdf"]
        # evil.html is still reported, but it doesn't hold the stage.
        skipped_names = [w["params"]["name"] for w in done["warnings"] if w["code"] == "importAssetSkipped"]
        assert skipped_names == ["evil.html"]
        assert done["stage_kept"] is False
    finally:
        _st.cleanup_import_stage(stage_id)


def test_only_permanent_attachment_skips_clean_the_stage_up(db_session, monkeypatch):
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_id = _stage_bundle(
        [_slide_doc(f"e{t}", f"evil-{t}.svs", [{"public_id": f"et{t}", "title": "E", "steps": []}])],
        assets={f"et{t}": {"evil.html": _png_bytes() + b"<script>", "ok.png": _png_bytes()}},
    )
    try:
        result = _st.apply_import(stage_id, {})
        assert result["warning_count"] == 1
        assert result["stage_kept"] is False
        assert result["retry"] == {"failed_slides": 0, "skipped_assets": 0}
        assert not os.path.exists(_st._stage_dir(stage_id))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_skipped_slide_stays_skipped_on_retry_unless_named(db_session, monkeypatch):
    """"Retry failed slides" must not quietly import a slide the operator
    chose to skip, even once its filename conflict has gone away."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    conflict = f"conf-{t}.svs"
    existing = Slide(title="here", catalog_id="X", wsi_filename=f"1_{conflict}",
                     wsi_filename_norm=conflict.casefold())
    db_session.add(existing)
    db_session.commit()
    broken = {f"bad-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"s{t}", conflict, []),
        _slide_doc(f"f{t}", f"bad-{t}.svs", []),
    ])
    try:
        first = _st.apply_import(stage_id, {f"s{t}": "skip"})
        assert first["skipped_count"] == 1 and first["failed_count"] == 1
        db_session.delete(existing)  # the conflict is gone
        db_session.commit()

        retry = _st.apply_import(stage_id, {})
        assert retry["skipped"] == [{"public_id": f"s{t}", "reason": "filename_conflict"}]
        assert retry["imported_count"] == 0 and retry["failed_count"] == 1
        assert _slides_named(db_session, conflict) == []

        broken.clear()
        named = _st.apply_import(stage_id, {f"s{t}": "copy"})
        assert sorted(r["source_public_id"] for r in named["imported"]) == [f"f{t}", f"s{t}"]
        assert named["stage_kept"] is False
    finally:
        _st.cleanup_import_stage(stage_id)


def test_retry_reuses_the_tour_id_planned_for_a_failed_slide(db_session, monkeypatch):
    """B (imported) was rewritten to point at the id planned for A's tour —
    reminted because the target already had A's bundle id. A failed; when the
    retry imports it, it must get that same id or B's image stays broken."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    a_tour = f"at{t}"
    holder = Slide(title="holder", catalog_id="H", wsi_filename=f"1_ho{t}.svs",
                   wsi_filename_norm=f"ho{t}.svs")
    db_session.add(holder)
    db_session.commit()
    db_session.add(Tour(slide_id=holder.id, public_id=a_tour, title="taken"))
    db_session.commit()
    ref = f"![p](/assets/tours/{a_tour}/pic.png)"
    broken = {f"a-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"B{t}", f"b-{t}.svs", [{
            "public_id": f"bt{t}", "title": "B", "steps": [{"order": 1, "description_md": ref}],
        }]),
        _slide_doc(f"A{t}", f"a-{t}.svs", [{"public_id": a_tour, "title": "A", "steps": []}]),
    ], assets={a_tour: {"pic.png": _png_bytes()}})
    try:
        first = _st.apply_import(stage_id, {})
        assert first["failed_count"] == 1
        b_slide = first["imported"][0]["slide_id"]
        b_tour = db_session.query(Tour).filter_by(slide_id=b_slide).one()
        planned = db_session.query(Step).filter_by(tour_id=b_tour.id).one().description_md
        assert a_tour not in planned

        broken.clear()
        second = _st.apply_import(stage_id, {})
        a_new = db_session.query(Tour).filter_by(slide_id=second["imported"][0]["slide_id"]).one()
        assert f"/assets/tours/{a_new.public_id}/pic.png" in planned
        assert os.path.isfile(os.path.join(tour_asset_dir(a_new.public_id), "pic.png"))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_bundle_carrying_an_apply_state_is_refused(client, admin_headers):
    """The stage directory is the extracted archive, so a bundle could
    pre-seed "already imported" records pointing at another slide's tours."""
    from wsi_app.migrations import CURRENT_SCHEMA_VERSION

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, obj in (
            ("manifest.json", _base_manifest(schema_version=CURRENT_SCHEMA_VERSION)),
            ("slides.json", {"slides": []}),
            (_st.APPLY_STATE_NAME, {"format": "tessera-import-apply-state/1", "slides": {}}),
        ):
            data = json.dumps(obj).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    r = _stage_job(client, admin_headers, buf.getvalue(), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importLayout"


@pytest.mark.parametrize("damage", [
    "not json",
    json.dumps({"format": "other", "slides": {}}),
    json.dumps({"format": "tessera-import-apply-state/1", "slides": {"ok": {
        "status": "pending", "slide_id": 5, "public_id": "ok", "tours": {},
        "wsi_filename": "../../../etc/passwd", "thumb_filename": "t.jpg",
        "new_asset_dirs": [],
    }}}),
], ids=["garbage", "wrong-format", "traversal"])
def test_unreadable_apply_state_is_refused_not_ignored(db_session, monkeypatch, damage):
    """Reading a damaged state as empty would import every slide again."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_id = _stage_bundle([_slide_doc(f"u{t}", f"u-{t}.svs", [])])
    try:
        with open(os.path.join(_st._stage_dir(stage_id), _st.APPLY_STATE_NAME), "w") as f:
            f.write(damage)
        with pytest.raises(_st.SlideTransferError) as ei:
            _st.apply_import(stage_id, {})
        assert ei.value.code == "importStateUnreadable"
        assert _slides_named(db_session, f"u-{t}.svs") == []
    finally:
        _st.cleanup_import_stage(stage_id)


def test_state_write_failure_aborts_the_apply_before_any_commit(db_session, monkeypatch):
    """Without the record written ahead of a commit, a retry after a crash
    could import that slide twice — so no slide is committed without it."""
    import errno as _errno

    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_id = _stage_bundle([_slide_doc(f"w{t}", f"w-{t}.svs", [])])

    def _full(path, payload):
        raise OSError(_errno.ENOSPC, "No space left on device")

    try:
        monkeypatch.setattr(_st._backup, "_atomic_write_json", _full)
        with pytest.raises(_st.SlideTransferError) as ei:
            _st.apply_import(stage_id, {})
        assert ei.value.code == "importStateWriteFailed"
        assert _slides_named(db_session, f"w-{t}.svs") == []
        assert os.path.isdir(_st._stage_dir(stage_id))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_retry_assets_requires_an_applied_stage(db_session):
    stage_id = _stage_bundle([_slide_doc(f"q{_tok()}", "q.svs", [])])
    try:
        with pytest.raises(_st.SlideTransferError) as ei:
            _st.retry_import_assets(stage_id)
        assert ei.value.code == "importNothingToRetry"
        # A stage that was never applied is not cleaned up by the refusal.
        assert os.path.isdir(_st._stage_dir(stage_id))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_http_partial_import_then_retry_and_preflight(client, admin_headers, db_session, attach_wsi, monkeypatch):
    """The whole flow through the endpoints: a real export of two slides, one
    failing on import; the job result says the stage was kept; the stage's
    preflight marks the imported slide; the retry imports the other."""
    s1, _t1, _b1 = _build_source(db_session, attach_wsi)
    s2, _t2, b2 = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [s1.id, s2.id])
    content = _download(client, admin_headers, result["export_id"])
    # The stem: on this same DB the import is a renamed "-imported-" copy.
    broken = {os.path.splitext(b2)[0]}
    _patch_wsi_readers(monkeypatch)
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))

    stage_id = _stage(client, admin_headers, content, result["archive_format"])["stage_id"]
    try:
        rj = _apply(client, admin_headers, stage_id)
        assert rj["state"] == "succeeded", rj
        res = rj["result"]
        assert res["imported_count"] == 1 and res["failed_count"] == 1
        assert res["stage_kept"] is True and res["stage_id"] == stage_id
        assert res["retry"]["failed_slides"] == 1

        r = client.get(f"/api/admin/slides/import/stage/{stage_id}", headers=admin_headers)
        assert r.status_code == 200, r.text
        plan = r.json()["plan"]
        assert plan["applied"] is True and plan["already_imported_count"] == 1
        by_pid = {s["public_id"]: s for s in plan["slides"]}
        assert by_pid[s1.public_id]["apply_status"] == "imported"
        assert by_pid[s2.public_id]["apply_status"] == "failed"
        assert by_pid[s2.public_id]["failure"]["error"]

        broken.clear()
        rj2 = _apply(client, admin_headers, stage_id)
        res2 = rj2["result"]
        assert res2["imported_count"] == 1 and res2["already_imported_count"] == 1
        assert res2["stage_kept"] is False
        r = client.get(f"/api/admin/slides/import/stage/{stage_id}", headers=admin_headers)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "adminDash.apiErr.importStageMissing"
    finally:
        _st.cleanup_import_stage(stage_id)


def test_http_retry_assets_job(client, admin_headers, db_session, monkeypatch, asset_settings):
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    asset_settings(tour_asset_max_bytes=10)  # every attachment is "too large"
    stage_id = _stage_bundle(
        [_slide_doc(f"h{t}", f"http-{t}.svs", [{"public_id": f"ht{t}", "title": "H", "steps": []}])],
        assets={f"ht{t}": {"a.png": _png_bytes()}},
    )
    try:
        assert _st.apply_import(stage_id, {})["stage_kept"] is True
        asset_settings(tour_asset_max_bytes=10 * 1024 * 1024)
        r = client.post("/api/admin/slides/import/retry-assets",
                        json={"stage_id": stage_id}, headers=admin_headers)
        assert r.status_code == 200, r.text
        rj = _poll(client, r.json()["job_id"], admin_headers)
        assert rj["state"] == "succeeded", rj
        assert rj["kind"] == "slide_import_assets"
        assert rj["result"]["copied_count"] == 1 and rj["result"]["stage_kept"] is False
    finally:
        _st.cleanup_import_stage(stage_id)


def test_retry_endpoints_share_the_job_slot(client, admin_headers):
    """Retry-assets and a re-apply are refused while another background job
    holds the slot, and a stage can't be discarded while an attachment retry
    reads it."""
    other = _jobs.store.try_claim_running("backup")
    assert other is not None
    _jobs.store.update(other.id, state="running")
    try:
        r = client.post("/api/admin/slides/import/retry-assets",
                        json={"stage_id": "import-000000000000"}, headers=admin_headers)
        assert r.status_code == 409, r.text
        r = client.post("/api/admin/slides/import/apply",
                        json={"stage_id": "import-000000000000"}, headers=admin_headers)
        assert r.status_code == 409, r.text
    finally:
        _jobs.store.update(other.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)

    job = _jobs.store.try_claim_running("slide_import_assets")
    assert job is not None
    _jobs.store.update(job.id, state="running")
    try:
        r = client.delete("/api/admin/slides/import/stage/import-000000000000", headers=admin_headers)
        assert r.status_code == 409, r.text
    finally:
        _jobs.store.update(job.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)


def test_retry_endpoints_require_admin(client, manager_headers):
    body = {"stage_id": "import-000000000000"}
    assert client.post("/api/admin/slides/import/retry-assets", json=body).status_code == 401
    assert client.post("/api/admin/slides/import/retry-assets", json=body,
                       headers=manager_headers).status_code == 403
    assert client.get("/api/admin/slides/import/stage/import-000000000000").status_code == 401
    assert client.get("/api/admin/slides/import/stage/import-000000000000",
                      headers=manager_headers).status_code == 403


def test_kept_stage_mtime_is_refreshed_for_the_sweep(db_session, monkeypatch):
    """A kept stage counts its 24 h from the last apply, and the sweep spares
    it until then."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    broken = {f"bad-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([_slide_doc(f"m{t}", f"bad-{t}.svs", [])])
    stage_dir = _st._stage_dir(stage_id)
    try:
        old = time.time() - 100000
        os.utime(stage_dir, (old, old))
        assert _st.apply_import(stage_id, {})["stage_kept"] is True
        assert os.stat(stage_dir).st_mtime > time.time() - 60
        _backup.sweep_stale_staging()
        assert os.path.isdir(stage_dir)
    finally:
        _st.cleanup_import_stage(stage_id)


def test_retried_slide_references_an_earlier_imports_reminted_tour(db_session, monkeypatch):
    """A failed slide's markdown points at a tour of a slide the first run
    imported under a reminted id; the retry rewrites it with the id that tour
    was actually given (recorded in the apply state), not a fresh guess."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    b_tour = f"bt{t}"
    holder = Slide(title="holder", catalog_id="H", wsi_filename=f"1_hb{t}.svs",
                   wsi_filename_norm=f"hb{t}.svs")
    db_session.add(holder)
    db_session.commit()
    db_session.add(Tour(slide_id=holder.id, public_id=b_tour, title="taken"))
    db_session.commit()
    broken = {f"a-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    stage_id = _stage_bundle([
        _slide_doc(f"A{t}", f"a-{t}.svs", [{
            "public_id": f"at{t}", "title": "A",
            "steps": [{"order": 1, "description_md": f"![x](/assets/tours/{b_tour}/x.png)"}],
        }]),
        _slide_doc(f"B{t}", f"b-{t}.svs", [{"public_id": b_tour, "title": "B", "steps": []}]),
    ], assets={b_tour: {"x.png": _png_bytes()}})
    try:
        first = _st.apply_import(stage_id, {})
        b_new = db_session.query(Tour).filter_by(slide_id=first["imported"][0]["slide_id"]).one().public_id
        assert b_new != b_tour

        broken.clear()
        second = _st.apply_import(stage_id, {})
        a_tour = db_session.query(Tour).filter_by(slide_id=second["imported"][0]["slide_id"]).one()
        md = db_session.query(Step).filter_by(tour_id=a_tour.id).one().description_md
        assert md == f"![x](/assets/tours/{b_new}/x.png)"
        assert second["warning_count"] == 0
    finally:
        _st.cleanup_import_stage(stage_id)


def test_bundle_with_duplicate_slide_public_ids_is_refused():
    """Two slides sharing a public_id would share one apply-state record (so a
    retry re-imports the one whose record was overwritten) and hard-link one
    staged WSI into two live slides. A real export never produces this."""
    good = _slide_doc("dupslide1", "a.svs", [])
    twin = _slide_doc("dupslide1", "b.svs", [])
    with pytest.raises(_st.SlideTransferError) as ei:
        _st._validate_slides_doc({"slides": [good, twin]})
    assert ei.value.code == "importInvalid"


def test_bundle_json_is_bounded_by_element_count_as_well_as_bytes(tmp_path):
    """Nested empty lists (~26x their size in Python objects) and arrays of short
    strings (~15x) within the byte cap could still exhaust memory: the element
    count is capped too, before the parser allocates anything."""
    ok = tmp_path / "ok.json"
    ok.write_text('{"slides": []}')
    assert _st._load_bundle_json(str(ok), _st._MAX_METADATA_JSON_BYTES) == {"slides": []}

    for label, filler in (("containers", "[],"), ("scalars", '"a",')):
        bomb = tmp_path / f"bomb-{label}.json"
        bomb.write_text('{"slides": [' + filler * (_st._MAX_JSON_ELEMENTS + 1) + "0]}")
        assert bomb.stat().st_size < _st._MAX_METADATA_JSON_BYTES, label
        with pytest.raises(_st.SlideTransferError) as ei:
            _st._load_bundle_json(str(bomb), _st._MAX_METADATA_JSON_BYTES)
        assert ei.value.code == "importInvalid", label

    big = tmp_path / "big.json"
    big.write_text('{"slides": "' + "x" * 200 + '"}')
    with pytest.raises(_st.SlideTransferError) as ei:
        _st._load_bundle_json(str(big), 100)
    assert ei.value.code == "importInvalid"


def test_a_bundle_the_app_itself_exports_at_the_per_tour_vertex_budget_still_imports(tmp_path):
    """The cap is not tighter than what the app writes: two tours at the
    per-tour vertex budget (300,000), as ``indent=2`` JSON exactly like the
    exporter's, come to ~78 MiB, past a 64 MiB cap."""
    def tour(vertices, shapes=25):
        return {
            "public_id": "t" * 12, "title": "t", "description": "", "is_published": True,
            "steps": [{"label": "s", "shapes_l0": [
                {"id": f"s{k}", "kind": "freehand", "points": [
                    {"x": 12345.678901 + i, "y": 23456.789012 + i} for i in range(vertices // shapes)
                ], "smoothing": True} for k in range(shapes)
            ]}],
        }

    doc = {"slides": [{"public_id": f"p{i}", "tours": [tour(300_000)]} for i in range(2)]}
    p = tmp_path / "slides.json"
    p.write_text(json.dumps(_st._finite_json(doc), indent=2, default=str, allow_nan=False))
    assert p.stat().st_size > 64 * 1024 * 1024
    assert len(_st._load_bundle_json(str(p), _st._MAX_METADATA_JSON_BYTES)["slides"]) == 2


def test_bundle_json_still_refuses_non_finite_numbers(tmp_path):
    p = tmp_path / "nan.json"
    p.write_text('{"slides": [], "x": NaN}')
    with pytest.raises(ValueError):
        _st._load_bundle_json(str(p), _st._MAX_METADATA_JSON_BYTES)


def test_pending_record_does_not_adopt_an_unrelated_slide_at_its_id(db_session, monkeypatch):
    """A crash after the "pending" record but before the reservation commits
    leaves the id unused; an ordinary upload of the same file name can then
    take that id and even the predicted ``{id}_{name}``. The retry must still
    import the bundle slide rather than count that upload as done."""
    from wsi_app.admin_helpers import reserve_next_slide_id

    _patch_wsi_readers(monkeypatch)
    t = _tok()
    pid = f"p{t}"
    basename = f"same-{t}.svs"
    stage_id = _stage_bundle([_slide_doc(pid, basename, [
        {"public_id": f"pt{t}", "title": "Tour", "steps": []},
    ])])
    real_save = _st._save_apply_state

    def save_then_die(stage_dir, state):
        real_save(stage_dir, state)
        if any(r.get("status") == "pending" for r in state["slides"].values()):
            raise _Crash()

    try:
        monkeypatch.setattr(_st, "_save_apply_state", save_then_die)
        with pytest.raises(_Crash):
            _st.apply_import(stage_id, {})
        monkeypatch.setattr(_st, "_save_apply_state", real_save)
        rec = _state(stage_id)["slides"][pid]
        assert rec["status"] == "pending"
        db_session.expire_all()
        assert db_session.get(Slide, rec["slide_id"]) is None

        new_id = reserve_next_slide_id(db_session)
        assert new_id == rec["slide_id"]
        db_session.add(Slide(
            id=new_id, public_id=f"up{t}", wsi_filename=f"{new_id}_{basename}",
            wsi_filename_norm=basename.casefold(), title="manual upload",
        ))
        db_session.commit()

        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 1
        db_session.expire_all()
        assert db_session.query(Tour).filter(Tour.public_id == f"pt{t}").count() == 1
        assert db_session.get(Slide, new_id).title == "manual upload"
    finally:
        _st.cleanup_import_stage(stage_id)


@pytest.mark.parametrize("field", ["pins", "shapes_l0"])
def test_bundled_step_past_the_editor_cap_is_refused(field):
    """The editor refuses (422) every save of a step over the per-step caps,
    since it always sends the full lists — so an import must not create one."""
    from wsi_app.schemas import _MAX_STEP_PINS, _MAX_STEP_SHAPES

    if field == "pins":
        items = [{"id": f"p{i}", "x": 1.0, "y": 1.0} for i in range(_MAX_STEP_PINS + 1)]
    else:
        items = [
            {"id": f"s{i}", "kind": "rect", "x": 1.0, "y": 1.0, "w": 2.0, "h": 2.0}
            for i in range(_MAX_STEP_SHAPES + 1)
        ]
    step = {"order": 1, field: items}
    with pytest.raises(ValueError, match="per step"):
        _st._bundle_step_fields(step)
    step[field] = items[:-1]
    _st._bundle_step_fields(step)


@pytest.mark.parametrize("field", ["pins", "shapes_l0"])
def test_bundled_step_with_duplicate_annotation_ids_is_refused(field):
    """UNIQUE(step_id, pin_id / shape_id) would otherwise fail the slide at
    apply time with a raw IntegrityError the preflight never mentioned."""
    if field == "pins":
        items = [{"id": "dup", "x": 1.0, "y": 1.0}, {"id": "dup", "x": 2.0, "y": 2.0}]
    else:
        items = [
            {"id": "dup", "kind": "rect", "x": 1.0, "y": 1.0, "w": 2.0, "h": 2.0},
            {"id": "dup", "kind": "rect", "x": 3.0, "y": 3.0, "w": 2.0, "h": 2.0},
        ]
    with pytest.raises(ValueError, match="duplicate id"):
        _st._bundle_step_fields({"order": 1, field: items})


def _freehand_shapes(total: int) -> list:
    """Freehand shapes carrying ``total`` vertices between them, each within
    the per-shape cap."""
    from wsi_app.schemas import _MAX_FREEHAND_POINTS

    shapes = []
    while total > 0:
        n = min(total, _MAX_FREEHAND_POINTS)
        shapes.append({
            "id": f"f{len(shapes)}", "kind": "freehand", "x": 1.0, "y": 1.0, "w": 2.0, "h": 2.0,
            "points": [{"x": float(k), "y": 1.0} for k in range(n)],
        })
        total -= n
    return shapes


def test_bundled_step_past_the_freehand_point_budget_is_refused():
    """Every editor write refuses a step whose freehand shapes hold more than
    the per-step vertex budget between them (each shape within its own cap);
    an imported one could never be saved from the editor again."""
    from wsi_app.schemas import _MAX_STEP_FREEHAND_POINTS

    step = {"order": 1, "shapes_l0": _freehand_shapes(_MAX_STEP_FREEHAND_POINTS + 1)}
    with pytest.raises(ValueError, match="freehand points per step"):
        _st._bundle_step_fields(step)
    step["shapes_l0"] = _freehand_shapes(_MAX_STEP_FREEHAND_POINTS)
    _st._bundle_step_fields(step)


@pytest.mark.parametrize("limit", ["freehand_points", "tour_freehand_points", "tour_steps"])
def test_tour_limits_are_reported_at_preflight_and_refused_at_apply(db_session, monkeypatch, limit):
    """The editor's per-step and per-tour freehand budgets and per-tour step
    cap hold for an imported tour too: the preflight lists the offending step
    / tour, and the apply refuses the slide with the same codes as any other
    invalid step or tour, before anything is written."""
    from wsi_app.schemas import _MAX_STEP_FREEHAND_POINTS, _MAX_TOUR_STEPS

    _patch_wsi_readers(monkeypatch)
    t = os.urandom(3).hex()
    if limit == "freehand_points":
        steps = [
            {"order": 1},
            {"order": 2, "shapes_l0": _freehand_shapes(_MAX_STEP_FREEHAND_POINTS + 1)},
        ]
        code, step_pos = "importStepInvalid", 2
    elif limit == "tour_freehand_points":
        # Every step within its own budget; the tour, between them, past its.
        monkeypatch.setattr(_st, "_MAX_TOUR_FREEHAND_POINTS", 10)
        steps = [
            {"order": 1, "shapes_l0": _freehand_shapes(6)},
            {"order": 2, "shapes_l0": _freehand_shapes(5)},
        ]
        code, step_pos = "importTourInvalid", 0
    else:
        steps = [{"order": n} for n in range(1, _MAX_TOUR_STEPS + 2)]
        code, step_pos = "importTourInvalid", 0
    sd = _slide_doc(f"tl{t}", f"tl-{t}.svs", [
        {"public_id": f"tt{t}", "title": "Long tour", "steps": steps},
    ])
    stage_id = _stage_bundle([sd])
    try:
        stage_dir = _st._stage_dir(stage_id)
        with open(os.path.join(stage_dir, _st.SLIDES_DOC_NAME)) as f:
            doc = json.load(f)
        with open(os.path.join(stage_dir, _st.MANIFEST_NAME)) as f:
            manifest = json.load(f)
        row = _st.plan_import(stage_id, manifest, doc, db_session)["slides"][0]
        assert row["content_error_count"] == 1
        error = row["content_errors"][0]
        assert (error["tour"], error["step"]) == ("Long tour", step_pos)

        with pytest.raises(_st.SlideTransferError) as ei:
            _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert ei.value.code == code
        assert ei.value.params["detail"] == error["detail"]
        assert db_session.query(Slide).filter(Slide.public_id == sd["public_id"]).count() == 0

        # At the limit itself the tour is accepted.
        if limit == "freehand_points":
            steps[1]["shapes_l0"] = _freehand_shapes(_MAX_STEP_FREEHAND_POINTS)
        elif limit == "tour_freehand_points":
            steps[1]["shapes_l0"] = _freehand_shapes(4)
        else:
            steps.pop()
        assert _st._bundle_content_errors(sd) == []
    finally:
        db_session.rollback()
        _st.cleanup_import_stage(stage_id)


def test_recovering_an_unrecorded_import_does_not_restore_deleted_attachments(
    client, admin_headers, db_session, monkeypatch
):
    """An import committed but never recorded (the "imported" state write hit
    a full volume) is recovered on retry. Its slide has been live since, and
    an attachment an author deleted meanwhile must stay deleted — recovery
    only reconstructs what the original copy pass skipped."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    pid, tpid = f"p{t}", f"pt{t}"
    stage_id = _stage_bundle([_slide_doc(pid, f"ra-{t}.svs", [
        {"public_id": tpid, "title": "T", "steps": []},
    ])], assets={tpid: {"a.png": _png_bytes(), "b.png": _png_bytes()}})
    real_save = _st._save_apply_state

    def full_disk_after_commit(stage_dir, state):
        if any(r.get("status") == "imported" for r in state["slides"].values()):
            raise _st._StateWriteError("disk full", code="importStateWriteFailed")
        real_save(stage_dir, state)

    try:
        monkeypatch.setattr(_st, "_save_apply_state", full_disk_after_commit)
        with pytest.raises(_st._StateWriteError):
            _st.apply_import(stage_id, {})
        monkeypatch.setattr(_st, "_save_apply_state", real_save)
        assert _state(stage_id)["slides"][pid]["status"] == "pending"
        db_session.expire_all()
        tour = db_session.query(Tour).filter_by(public_id=tpid).one()
        r = client.delete(f"/api/admin/tours/{tour.id}/assets/b.png", headers=admin_headers)
        assert r.status_code == 200, r.text
        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 0 and result["already_imported_count"] == 1
        assert not os.path.exists(os.path.join(tour_asset_dir(tpid), "b.png"))
        assert os.path.exists(os.path.join(tour_asset_dir(tpid), "a.png"))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_an_imported_slide_is_current_at_the_edge_before_the_batch_ends(db_session, monkeypatch):
    """A kept public_id resolved while its import placeholder exists must not
    stay cached as a draft until the whole batch finishes, or the edge would
    refuse the finished, published slide's tiles for the rest of the batch."""
    from wsi_app import dzi

    _patch_wsi_readers(monkeypatch)
    t = _tok()
    a, b = f"a{t}", f"b{t}"
    docs = [_slide_doc(a, f"ea-{t}.svs", []), _slide_doc(b, f"eb-{t}.svs", [])]
    for d in docs:
        d.update(is_published=True, title="T", catalog_id="C")
    stage_id = _stage_bundle(docs)
    real_props = _st.read_slide_properties
    seen = {}

    def props(path):
        if f"ea-{t}" in path:
            dzi._resolve_slide(a)  # cached while a is still a placeholder
        if f"eb-{t}" in path:
            seen["a_at_the_edge"] = dzi.edge_slide_access(f"/dzi/{a}.dzi", staff=False)
        return real_props(path)

    monkeypatch.setattr(_st, "read_slide_properties", props)
    try:
        assert _st.apply_import(stage_id, {})["imported_count"] == 2
        assert seen["a_at_the_edge"] is True
    finally:
        _st.cleanup_import_stage(stage_id)


def _tar_gz_with(members):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def test_import_unsafe_tar_members_are_refused_not_a_server_error(client, admin_headers):
    """_safe_extract refuses links, '..' and duplicate names with RuntimeError;
    the staging job must fail with importUnsafe, not an opaque error."""
    import tarfile

    link = tarfile.TarInfo("evil-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    dotdot = tarfile.TarInfo("a/../b")
    dotdot.size = 1
    for members in ([(link, None)], [(dotdot, b"x")]):
        r = _stage_job(client, admin_headers, _tar_gz_with(members), "tar.gz")
        assert r["state"] == "failed", r
        assert r["result"]["code"] == "adminDash.apiErr.importUnsafe"


def test_stage_import_extraction_respects_free_space(tmp_path, monkeypatch):
    """The free-space figure is handed to the extractor, so a tiny archive that
    expands past the free space is refused instead of filling the volume."""
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(_make_crafted_bundle(_base_manifest(), {"slides": []}))
    monkeypatch.setattr(_st.shutil, "disk_usage", lambda p: type("U", (), {"total": 10**12, "free": 10})())
    # compressed size > 10 bytes trips the preflight itself; bypass it so the
    # extractor's own (expanded-size) check is the one under test.
    monkeypatch.setattr(_st.os.path, "getsize", lambda p: 0)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.stage_import(str(archive))
    assert ei.value.code == "importNoSpace"


# ---------------------------------------------------------------------------
# Untrusted slides.json field types, extension preflight, attribute validation
# ---------------------------------------------------------------------------

def _import_stages() -> set:
    root = _st._staging_root()
    return {n for n in os.listdir(root) if n.startswith("import-")} if os.path.isdir(root) else set()


@pytest.mark.parametrize("field,value", [
    ("wsi_original_basename", 5),
    ("wsi_original_basename", ["a.svs"]),
    ("catalog_id", ["c1"]),
    ("catalog_id", {"a": 1}),
    ("title", 12),
])
def test_import_rejects_non_string_slide_fields(client, admin_headers, field, value):
    """A list/number where slides.json promises a string must not reach
    safe_filename / a set lookup in the preflight: a 500 that would leave the
    extracted stage behind for 24 h. It is refused as a non-bundle instead."""
    sd = {"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": []}
    sd[field] = value
    before = _import_stages()
    r = _stage_job(client, admin_headers,
               _make_crafted_bundle(_base_manifest(), {"slides": [sd]}), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"
    assert _import_stages() == before


def test_unexpected_plan_failure_discards_the_stage(client, admin_headers, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(_st, "plan_import", boom)
    sd = {"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": []}
    before = _import_stages()
    job = _stage_job(client, admin_headers,
                     _make_crafted_bundle(_base_manifest(), {"slides": [sd]}), "tar.gz")
    assert job["state"] == "failed" and "boom" in job["error"], job
    assert _import_stages() == before


def test_plan_and_apply_report_an_unsupported_wsi_extension(db_session, monkeypatch):
    """The preflight must not say OK only for the apply to fail with an
    English, code-less "400: Unsupported WSI extension" string."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_id = _stage_bundle([_slide_doc(f"x{t}", f"scan-{t}.exe", [])])
    try:
        stage_dir = _st._stage_dir(stage_id)
        with open(os.path.join(stage_dir, _st.SLIDES_DOC_NAME)) as f:
            doc = json.load(f)
        with open(os.path.join(stage_dir, _st.MANIFEST_NAME)) as f:
            manifest = json.load(f)
        row = _st.plan_import(stage_id, manifest, doc, db_session)["slides"][0]
        assert row["content_error_count"] == 1
        assert ".exe" in row["content_errors"][0]["detail"]

        res = _st.apply_import(stage_id, {})
        assert res["imported_count"] == 0
        assert res["failed"][0]["code"] == "importUnsupportedExtension"
        assert res["failed"][0]["params"] == {"ext": ".exe"}
    finally:
        _st.cleanup_import_stage(stage_id)


def test_import_validates_and_normalises_attributes(db_session, monkeypatch):
    """Attribute pairs the metadata editor refuses (':' / ',' in the key,
    control characters, >255-char values) must not be stored (and silently
    truncated), since the public filter, which splits on ':', can never
    match them. Values are stripped like the editor does."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_dir = _stage_one_wsi(f"at{t}", f"at-{t}.svs")
    try:
        sd = _slide_doc(f"at{t}", f"at-{t}.svs", [])
        sd["attributes"] = {
            "Stain": ["  HE  ", "IHC", "HE", "", "x" * 300, "bad\x07"],
            "a:b": ["v"],
            "c,d": ["v"],
            "bad\nkey": ["v"],
            "Site": "liver",
            "Num": [5],
        }
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        rows = {
            (a.key, a.value)
            for a in db_session.query(SlideAttribute).filter_by(slide_id=res["slide_id"])
        }
        assert rows == {("Stain", "HE"), ("Stain", "IHC"), ("Site", "liver")}
        assert set(res["attributes_skipped"]) == {"Stain", "a:b", "c,d", "bad\nkey", "Num"}
        warns = _st._import_warnings(db_session, [{**res, "source_public_id": "p"}])
        assert {w["params"]["key"] for w in warns if w["code"] == "importAttributeSkipped"} == {
            "Stain", "a:b", "c,d", "bad\nkey", "Num",
        }
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_import_validates_tour_attributes_like_the_slides(db_session, monkeypatch):
    """A tour's own attributes are read as leniently as the slide's: refused
    pairs are skipped and their keys reported with the slide's."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    stage_dir = _stage_one_wsi(f"ta{t}", f"ta-{t}.svs")
    try:
        sd = _slide_doc(f"ta{t}", f"ta-{t}.svs", [{
            "public_id": f"tt{t}", "title": "Tour",
            "attributes": {"level": [" basic ", "basic"], "a:b": ["v"]},
            "steps": [],
        }])
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        tour = db_session.query(Tour).filter_by(slide_id=res["slide_id"]).one()
        rows = {(a.key, a.value) for a in db_session.query(TourAttribute).filter_by(tour_id=tour.id)}
        assert rows == {("level", "basic")}
        assert res["attributes_skipped"] == ["a:b"]
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Export: rows are re-read under each slide's lock
# ---------------------------------------------------------------------------

def _hook_second_slide_lock(monkeypatch, mutate):
    """Run ``mutate(slide_id)`` (another admin's edit, on its own session) just
    before the export takes the SECOND slide's mutation lock."""
    real = _st.slide_mutation_lock
    seen: list = []

    @contextlib.contextmanager
    def hooked(slide_id):
        seen.append(slide_id)
        if len(seen) == 2:
            mutate(slide_id)
        with real(slide_id):
            yield

    monkeypatch.setattr(_st, "slide_mutation_lock", hooked)


def test_export_follows_a_slide_replaced_while_the_export_runs(db_session, attach_wsi, monkeypatch):
    """The rows are loaded once, before any lock. A slide replaced (new file
    name, new metadata) while earlier slides stream must not be looked up
    under its old name (reported as exportWsiMissing) or, for a same-name
    replace, exported with stale metadata."""
    from wsi_app.db import SessionLocal

    a, _t, _b = _build_source(db_session, attach_wsi)
    b, _t2, _b2 = _build_source(db_session, attach_wsi)
    b_id, b_pid = b.id, b.public_id
    new_name = f"{b_id}_replaced.svs"
    wsi_dir = os.path.join(settings.data_dir, "wsi")

    def mutate(slide_id):
        assert slide_id == b_id
        other = SessionLocal()
        try:
            row = other.get(Slide, slide_id)
            os.remove(os.path.join(wsi_dir, row.wsi_filename))
            with open(os.path.join(wsi_dir, new_name), "wb") as f:
                f.write(b"replaced-bytes")
            row.wsi_filename = new_name
            row.title = "Edited meanwhile"
            other.commit()
        finally:
            other.close()

    _hook_second_slide_lock(monkeypatch, mutate)
    rec = _st.export_slides([a.id, b_id])
    try:
        members = {}
        with _backup._open_archive_reader(rec.path) as tar:
            for m in tar:  # the archive is a stream: read it in order
                if m.isfile():
                    members[m.name] = tar.extractfile(m).read()
        doc = json.loads(members[_st.SLIDES_DOC_NAME])
        assert members[f"wsi/{b_pid}/replaced.svs"] == b"replaced-bytes"
        row = next(s for s in doc["slides"] if s["public_id"] == b_pid)
        assert row["title"] == "Edited meanwhile"
        assert row["wsi_original_basename"] == "replaced.svs"
    finally:
        _st.delete_export(rec.id)


def test_export_fails_cleanly_when_a_slide_is_deleted_while_it_runs(db_session, attach_wsi, monkeypatch):
    from wsi_app.db import SessionLocal

    a, _t, _b = _build_source(db_session, attach_wsi)
    b, _t2, _b2 = _build_source(db_session, attach_wsi)

    def mutate(slide_id):
        other = SessionLocal()
        try:
            other.delete(other.get(Slide, slide_id))
            other.commit()
        finally:
            other.close()

    _hook_second_slide_lock(monkeypatch, mutate)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.export_slides([a.id, b.id])
    assert ei.value.code == "exportUnknownSlide"


def _crafted_tour_slide(**tour_over):
    tour = {"public_id": "tour1", "title": "t", "steps": []}
    tour.update(tour_over)
    return {"slides": [{"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": [tour]}]}


@pytest.mark.parametrize("over", [
    {"title": ["not", "a", "string"]},
    {"description": {"a": 1}},
    {"is_published": "false"},   # a non-empty string would be read as True
    {"unlisted": 1},
])
def test_import_rejects_mistyped_tour_fields(client, admin_headers, over):
    r = _stage_job(client, admin_headers, _make_crafted_bundle(_base_manifest(), _crafted_tour_slide(**over)), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_import_preflight_flags_out_of_range_step_order(client, admin_headers):
    step = {"order": 2 ** 70, "label": "s"}
    sd = _crafted_tour_slide(steps=[step])
    r = _stage(client, admin_headers, _make_crafted_bundle(_base_manifest(), sd), "tar.gz")
    row = r["plan"]["slides"][0]
    assert row["content_error_count"] == 1, row


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN", "1e999"])
def test_import_non_finite_json_numbers_are_refused_not_a_server_error(
    client, admin_headers, literal
):
    """json.load accepts Infinity/NaN/1e999 but the app answers with strict
    JSON (the staging job's result included), so such a value is refused at
    the door rather than failing later on."""
    import json as _json

    manifest = _base_manifest()
    raw_manifest = _json.dumps(manifest)[:-1] + ', "tour_count": ' + literal + "}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (
            ("manifest.json", raw_manifest.encode()),
            ("slides.json", _json.dumps(_crafted_tour_slide()).encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    r = _stage_job(client, admin_headers, buf.getvalue(), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


def test_export_with_non_finite_values_is_importable(
    client, admin_headers, db_session, attach_wsi, patched_openslide
):
    """A hand-edited row can hold inf/NaN. Written as ``Infinity`` it would make
    the (strict) importer refuse the bundle, so the exporter writes null."""
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    src.mpp_x_file = float("inf")
    src.objective_power_file = float("-inf")  # (SQLite stores NaN as NULL, only inf survives)
    src.mpp_x = float("inf")
    db_session.commit()

    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])
    raw = ""
    with _reader(content) as tar:
        for member in tar:  # a streaming reader: no seeking back to a named member
            if member.name == "slides.json":
                raw = tar.extractfile(member).read().decode()
                break
    assert raw and "Infinity" not in raw and "NaN" not in raw

    stage = _stage(client, admin_headers, content, result["archive_format"])
    rj = _apply(client, admin_headers, stage["stage_id"])
    assert rj["state"] == "succeeded" and rj["result"]["imported_count"] == 1, rj
    new_id = rj["result"]["imported"][0]["slide_id"]
    db_session.expire_all()
    imported = db_session.get(Slide, new_id)
    assert imported.mpp_x is None or imported.mpp_x == 0 or math.isfinite(imported.mpp_x)


@pytest.mark.parametrize("field", ["is_published", "unlisted"])
def test_import_rejects_non_boolean_slide_flags(client, admin_headers, field):
    """"false" is a non-empty string: bool("false") would publish the slide."""
    doc = {"slides": [{"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": [], field: "false"}]}
    r = _stage_job(client, admin_headers, _make_crafted_bundle(_base_manifest(), doc), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


# ---------------------------------------------------------------------------
# Staging as a background job; bundle hygiene
# ---------------------------------------------------------------------------

def test_staging_answers_202_and_the_job_carries_stage_and_plan(
    client, admin_headers, db_session, attach_wsi, patched_openslide,
):
    """Extracting and checking a multi-GB bundle outlasts the proxy timeout,
    so the upload only starts a job; its result is what the dashboard needs
    (no manifest echo, no plan fields nothing reads)."""
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])
    r = client.post(
        "/api/admin/slides/import", headers=admin_headers,
        files={"file": (f"b.{result['archive_format']}", io.BytesIO(content),
                        "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"job_id"}
    job = _poll(client, r.json()["job_id"], admin_headers)
    assert job["state"] == "succeeded" and job["kind"] == "slide_import_stage", job
    body = job["result"]
    assert set(body) == {"stage_id", "plan"}
    plan = body["plan"]
    for gone in ("note", "current_app_version", "current_schema_version", "bundle_schema_version"):
        assert gone not in plan
    for gone in ("imported_slide_id", "imported_public_id"):
        assert gone not in plan["slides"][0]
    # Counted from slides.json, like slide_count.
    assert plan["tour_count"] == 1 and plan["slide_count"] == 1
    assert plan["unknown_fields"] == [] and plan["slides"][0]["credit_skipped"] == []
    # The re-read endpoint answers in the same shape.
    again = client.get(f"/api/admin/slides/import/stage/{body['stage_id']}", headers=admin_headers)
    assert set(again.json()) == {"stage_id", "plan"}
    _st.cleanup_import_stage(body["stage_id"])


def _import_upload_leftovers() -> list[str]:
    staging = os.path.join(settings.backup_dir, ".staging")
    return [n for n in os.listdir(staging) if n.startswith("upload-")] if os.path.isdir(staging) else []


def test_staging_answers_before_the_upload_is_copied_into_staging(
    client, admin_headers, db_session, attach_wsi, patched_openslide, monkeypatch,
):
    """The copy of the received bundle into staging runs in the job too: on a
    filesystem without reflinks it is a full physical copy, which for a large
    bundle outlasts the proxy's response timeout."""
    import threading

    from wsi_app.routers import admin_backup

    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    content = _download(client, admin_headers, result["export_id"])
    gate, entered = threading.Event(), threading.Event()
    real = admin_backup._copy_held_upload

    def slow(held, out, copied):
        entered.set()
        assert gate.wait(10)
        real(held, out, copied)

    monkeypatch.setattr(admin_backup, "_copy_held_upload", slow)
    r = client.post(
        "/api/admin/slides/import", headers=admin_headers,
        files={"file": (f"b.{result['archive_format']}", io.BytesIO(content),
                        "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert entered.wait(10)
    rec = client.get(f"/api/admin/slides/transfer/jobs/{job_id}", headers=admin_headers).json()
    assert rec["state"] == "running", rec
    assert rec["progress"]["phase"] == "copying"
    assert rec["progress"]["total_bytes"] == len(content)
    gate.set()
    job = _poll(client, job_id, admin_headers)
    assert job["state"] == "succeeded", job
    assert _import_upload_leftovers() == []
    _st.cleanup_import_stage(job["result"]["stage_id"])


def test_staging_on_a_full_disk_fails_the_job_as_no_space(client, admin_headers, monkeypatch):
    from wsi_app.routers import admin_backup

    def full(held, out, copied):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(admin_backup, "_copy_held_upload", full)
    r = client.post(
        "/api/admin/slides/import", headers=admin_headers,
        files={"file": ("b.tar.zst", io.BytesIO(b"x" * (2 * 1024 * 1024)), "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    job = _poll(client, r.json()["job_id"], admin_headers)
    assert job["state"] == "failed", job
    assert job["result"]["code"] == "adminDash.apiErr.uploadNoSpace"
    assert _import_upload_leftovers() == []
    assert not _jobs.store.has_running()


def test_staging_releases_the_upload_when_the_job_thread_cannot_start(
    client, admin_headers, monkeypatch,
):
    import threading
    import types

    from wsi_app.routers import admin_slide_transfer

    held: list = []
    real_hold = admin_slide_transfer.hold_upload

    def hold(upload, max_bytes):
        h = real_hold(upload, max_bytes)
        held.append(h)
        return h

    class _NoStart(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(admin_slide_transfer, "hold_upload", hold)
    monkeypatch.setattr(admin_slide_transfer, "threading", types.SimpleNamespace(Thread=_NoStart))
    with pytest.raises(RuntimeError, match="can't start new thread"):
        client.post(
            "/api/admin/slides/import", headers=admin_headers,
            files={"file": ("b.tar.zst", io.BytesIO(b"x" * (2 * 1024 * 1024)), "application/octet-stream")},
        )
    assert held and held[0].fd is None
    assert not _jobs.store.has_running()
    assert _import_upload_leftovers() == []


def test_unknown_transfer_job_is_a_coded_404(client, admin_headers):
    r = client.get("/api/admin/slides/transfer/jobs/nope", headers=admin_headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "adminDash.apiErr.transferJobMissing"


def test_every_exported_key_is_known_to_the_importer(db_session, attach_wsi):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    doc = _st._serialize_slide(src)
    # Everything an export writes is a key the importer knows.
    assert set(doc) <= _st._KNOWN_SLIDE_KEYS
    assert set(doc["tours"][0]) <= _st._KNOWN_TOUR_KEYS
    assert set(doc["tours"][0]["steps"][0]) <= _st._KNOWN_STEP_KEYS


def test_unknown_bundle_fields_are_reported_but_known_keys_are_not(db_session, monkeypatch):
    """A bundle from a newer release whose fields this build doesn't know:
    the preflight names them and the apply warns, instead of dropping them
    silently. Known keys (a step's revision, among them) are not flagged."""
    _patch_wsi_readers(monkeypatch)
    t = os.urandom(3).hex()
    sd = _slide_doc(f"uk{t}", f"uk-{t}.svs", [{
        "public_id": f"ukt{t}", "title": "U", "tour_extra": 1,
        "steps": [{"order": 1, "revision": 4, "step_extra": True}],
    }])
    sd["credit_holder_v2"] = "x"
    stage_id = _stage_bundle([sd])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        plan = _st.plan_import(stage_id, manifest, doc, db_session)
        assert plan["unknown_fields"] == ["credit_holder_v2", "tour.tour_extra", "step.step_extra"]
        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 1, result
        unknown = [w for w in result["warnings"] if w["code"] == "importUnknownField"]
        assert [w["params"]["field"] for w in unknown] == plan["unknown_fields"]
        assert all(w["public_id"] == "" for w in unknown)
    finally:
        _st.cleanup_import_stage(stage_id)


def test_oversized_manifest_and_deep_nesting_are_refused(client, admin_headers):
    big = _base_manifest(padding="x" * (_st._MAX_MANIFEST_JSON_BYTES + 1))
    doc = {"slides": [{"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": []}]}
    r = _stage_job(client, admin_headers, _make_crafted_bundle(big, doc), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"

    manifest = json.dumps(_base_manifest()).encode()
    nested = b"[" * 200_000 + b"]" * 200_000
    members = []
    for name, data in (("manifest.json", manifest), ("slides.json", nested)):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    r = _stage_job(client, admin_headers, _tar_gz_with(members), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"


@pytest.mark.parametrize("extra", [
    "junk/0", "notes.txt", "apply-state.json", "wsi/abc123/sub/x.svs", "wsi/bad..id/x.svs",
    "assets/other/x.png", "assets/tours/t1/nested/x.png",
    # Well-formed ids, but of no slide / tour in slides.json.
    "wsi/other9/x.svs", "assets/tours/t9/x.png",
])
def test_members_outside_the_bundle_layout_are_refused(client, admin_headers, extra):
    manifest = json.dumps(_base_manifest()).encode()
    doc = json.dumps({"slides": [
        {"public_id": "abc123", "wsi_original_basename": "x.svs",
         "tours": [{"public_id": "t1", "title": "T"}]},
    ]}).encode()
    members = []
    for name, data in (("manifest.json", manifest), ("slides.json", doc),
                       ("wsi/abc123/x.svs", b"W"), (extra, b"x")):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    before = _import_stages()
    r = _stage_job(client, admin_headers, _tar_gz_with(members), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importLayout"
    assert _import_stages() == before


def _bundle_with_attachments(path, n_assets, *, dir_entries=False):
    """A one-slide bundle whose one tour (t1) carries ``n_assets`` empty
    attachments; ``dir_entries`` adds the directory members a bundle re-packed
    with tar has (export writes none)."""
    manifest = json.dumps(_base_manifest()).encode()
    doc = json.dumps({"slides": [
        {"public_id": "abc123", "wsi_original_basename": "x.svs",
         "tours": [{"public_id": "t1", "title": "T"}]},
    ]}).encode()
    members = []
    if dir_entries:
        for d in ("wsi", "wsi/abc123", "assets", "assets/tours", "assets/tours/t1"):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            members.append((info, None))
    files = [("manifest.json", manifest), ("wsi/abc123/x.svs", b"W")]
    files += [(f"assets/tours/t1/f{i}.png", b"") for i in range(n_assets)]
    files.append(("slides.json", doc))
    for name, data in files:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    path.write_bytes(_tar_gz_with(members))


@contextlib.contextmanager
def _tour_file_cap(value):
    saved = settings.tour_asset_max_files
    object.__setattr__(settings, "tour_asset_max_files", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "tour_asset_max_files", saved)


def test_import_member_cap_is_derived_from_the_import_limits():
    """The cap budgets IMPORT_MAX_SLIDES slides with a WSI and a full tour
    each (never below the default 200 attachments, even under a tighter
    TOUR_ASSET_MAX_FILES), well under the generic archive cap; with the
    per-tour cap disabled there is nothing to derive it from. It is a total:
    the export counts any spread of the same attachments over more tours
    against it the way the import counts the bundle's members."""
    default_budget = 5 + _st.IMPORT_MAX_SLIDES * (3 + 200)
    assert default_budget <= _backup._MAX_ARCHIVE_MEMBERS // 5
    for cap, expected in ((200, default_budget), (20, default_budget),
                          (0, _backup._MAX_ARCHIVE_MEMBERS)):
        with _tour_file_cap(cap):
            assert _st._import_max_members() == expected
    with _tour_file_cap(200):
        assert _st._bundle_entries([[200]] * _st.IMPORT_MAX_SLIDES) == default_budget
        # Two tours of 102 attachments on each of 500 slides: past the budget,
        # so the export refuses that selection instead of building a bundle
        # the import would refuse.
        assert _st._bundle_entries([[102, 102]] * 500) > _st._import_max_members()
        assert _st._bundle_entries([[160] * 6] * 100) <= _st._import_max_members()


def test_stage_import_refuses_more_members_than_the_import_limits_allow(tmp_path, monkeypatch):
    """A bundle of many tiny members (a few hundred KB can carry hundreds of
    thousands) is refused, with a code of its own naming the limit, once it
    holds more than a bundle within the import limits could: extraction
    stops there instead of running to the end, and no stage is left. One at
    the budget, directory entries included, still stages."""
    monkeypatch.setattr(_st, "IMPORT_MAX_SLIDES", 1)
    before = _import_stages()
    with _tour_file_cap(200):
        assert _st._import_max_members() == 208

        at_budget = tmp_path / "at-budget.tar.gz"
        _bundle_with_attachments(at_budget, 200, dir_entries=True)  # 208 members
        stage_id, _manifest, _doc = _st.stage_import(str(at_budget))
        try:
            tour_dir = os.path.join(_st._stage_dir(stage_id), "assets", "tours", "t1")
            assert len(os.listdir(tour_dir)) == 200
        finally:
            _st.cleanup_import_stage(stage_id)

        over = tmp_path / "over.tar.gz"
        _bundle_with_attachments(over, 5000)
        extracted = []
        real_extract = tarfile.TarFile.extract

        def counting_extract(self, member, *args, **kwargs):
            extracted.append(member)
            return real_extract(self, member, *args, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extract", counting_extract)
        with pytest.raises(_st.SlideTransferError) as ei:
            _st.stage_import(str(over))
    assert ei.value.code == "importTooManyFiles"
    assert ei.value.params == {"limit": 208}
    assert "more than 208 files and directories" in str(ei.value)
    assert len(extracted) == 208
    assert _import_stages() == before


def test_staging_job_reports_an_over_cap_bundle_with_its_own_code(
    client, admin_headers, tmp_path, monkeypatch,
):
    """Through the upload: the staging job fails with the member-cap code
    and the limit for the message, not as "not a slide-export bundle"."""
    monkeypatch.setattr(_st, "IMPORT_MAX_SLIDES", 1)
    over = tmp_path / "over.tar.gz"
    _bundle_with_attachments(over, 300)
    before = _import_stages()
    with _tour_file_cap(200):
        r = _stage_job(client, admin_headers, over.read_bytes(), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importTooManyFiles"
    assert r["result"]["params"] == {"limit": 208}
    assert _import_stages() == before


def _multi_tour_source(db, attach_wsi, *, slides=2, tours=3, assets=4):
    """``slides`` slides, each with ``tours`` tours whose one step shows one
    of the tour's ``assets`` PNG attachments. Returns the slides and the
    number of members the bundle of all of them holds as the import counts
    them (directories of a re-packed copy included)."""
    made = []
    for i in range(slides):
        token = os.urandom(4).hex()
        s = Slide(title=f"Multi {i}", catalog_id=f"MT-{token}", is_published=True)
        db.add(s)
        db.commit()
        db.refresh(s)
        attach_wsi(s, name=f"{s.id}_multi-{token}.svs")
        s.wsi_filename_norm = f"multi-{token}.svs"
        for k in range(tours):
            tour = Tour(slide_id=s.id, title=f"Tour {i}.{k}", description="")
            db.add(tour)
            db.commit()
            db.refresh(tour)
            db.add(Step(
                tour_id=tour.id, order=1, label="s", center_x=1.0, center_y=1.0,
                fov_um_w=100.0, fov_um_h=100.0,
                description_md=f"![a](/assets/tours/{tour.public_id}/img0.png)",
            ))
            adir = tour_asset_dir(tour.public_id)
            os.makedirs(adir, exist_ok=True)
            for a in range(assets):
                with open(os.path.join(adir, f"img{a}.png"), "wb") as f:
                    f.write(_png_bytes())
        db.commit()
        made.append(s)
    entries = 5 + slides * (2 + tours * (1 + assets))
    return made, entries


def _export_leftovers() -> set:
    root = _st._staging_root()
    return {n for n in os.listdir(root) if n.startswith("export-")} if os.path.isdir(root) else set()


def _repacked_with_directories(content: bytes) -> bytes:
    """The bundle unpacked and packed again with tar: the same files plus a
    member per directory."""
    files, dirs = [], set()
    with _reader(content) as tar:
        for m in tar:
            files.append((m.name, tar.extractfile(m).read()))
            parts = m.name.split("/")[:-1]
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    members = []
    for d in sorted(dirs):
        info = tarfile.TarInfo(d)
        info.type = tarfile.DIRTYPE
        members.append((info, None))
    for name, data in files:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    return _tar_gz_with(members)


def test_multi_tour_export_round_trips_at_the_member_budget(
    client, admin_headers, db_session, attach_wsi, patched_openslide, monkeypatch,
):
    """Several tours per slide: a selection the export accepts — here one
    exactly at the member budget — imports on the same system, as built and
    re-packed with directory entries, with every tour and attachment."""
    slides, entries = _multi_tour_source(db_session, attach_wsi)
    monkeypatch.setattr(_st, "_import_max_members", lambda: entries)
    result = _export(client, admin_headers, [s.id for s in slides])
    assert result["slide_count"] == 2 and result["tour_count"] == 6
    content = _download(client, admin_headers, result["export_id"])
    with _reader(content) as tar:
        assert len(tar.getnames()) == 2 + 2 + 6 * 4

    repacked = _stage(client, admin_headers, _repacked_with_directories(content), "tar.gz")
    _st.cleanup_import_stage(repacked["stage_id"])

    body = _stage(client, admin_headers, content, result["archive_format"])
    assert body["plan"]["blocking_code"] is None
    assert [p["tour_count"] for p in body["plan"]["slides"]] == [3, 3]
    rj = _apply(client, admin_headers, body["stage_id"])
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["imported_count"] == 2 and res["failed_count"] == 0, res
    assert not [w for w in res["warnings"] if w["code"] == "importAssetSkipped"], res["warnings"]
    db_session.expire_all()
    for imported in res["imported"]:
        tours = db_session.query(Tour).filter_by(slide_id=imported["slide_id"]).all()
        assert len(tours) == 3
        for tour in tours:
            assert sorted(os.listdir(tour_asset_dir(tour.public_id))) == [
                f"img{a}.png" for a in range(4)
            ]


def test_export_refuses_a_selection_over_the_member_budget(
    client, admin_headers, db_session, attach_wsi, monkeypatch,
):
    """One member over the budget: the export job fails before writing any
    archive, with a code of its own carrying the bundle's size and the
    limit, instead of building a bundle the import would refuse."""
    slides, entries = _multi_tour_source(db_session, attach_wsi)
    monkeypatch.setattr(_st, "_import_max_members", lambda: entries - 1)
    opened = []
    real_writer = _backup._open_archive_writer
    monkeypatch.setattr(
        _backup, "_open_archive_writer", lambda path: opened.append(path) or real_writer(path),
    )
    before = _export_leftovers()
    r = client.post("/api/admin/slides/export",
                    json={"slide_ids": [s.id for s in slides]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    job = _poll(client, r.json()["job_id"], admin_headers)
    assert job["state"] == "failed", job
    assert job["result"]["code"] == "adminDash.apiErr.exportTooManyFiles"
    assert job["result"]["params"] == {"count": entries, "limit": entries - 1}
    assert opened == []
    assert _export_leftovers() == before


def test_export_recounts_attachments_uploaded_while_it_runs(db_session, attach_wsi, monkeypatch):
    """An attachment uploaded after the preflight counted the selection still
    counts: the export stops once the bundle passes the budget and removes
    the partial archive."""
    slides, entries = _multi_tour_source(db_session, attach_wsi, slides=1, tours=2, assets=2)
    monkeypatch.setattr(_st, "_import_max_members", lambda: entries)
    late_dir = tour_asset_dir(slides[0].tours[0].public_id)
    real_add = _backup._add_pinned_file

    def add_then_upload(tar, f, arcname):
        if arcname.startswith("wsi/"):
            with open(os.path.join(late_dir, "late.png"), "wb") as out:
                out.write(_png_bytes())
        return real_add(tar, f, arcname)

    monkeypatch.setattr(_backup, "_add_pinned_file", add_then_upload)
    before = _export_leftovers()
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.export_slides([slides[0].id])
    assert ei.value.code == "exportTooManyFiles"
    assert ei.value.params == {"count": entries + 1, "limit": entries}
    assert _export_leftovers() == before


def test_export_skips_upload_temps_and_non_regular_asset_entries(
    client, admin_headers, db_session, attach_wsi, patched_openslide, caplog,
):
    """Only regular, non-dot files go into a bundle (import refuses anything
    nested deeper); each hand-copied entry left out is logged by name, an
    in-flight upload temp is not."""
    src, tour, _basename = _build_source(db_session, attach_wsi)
    adir = tour_asset_dir(tour.public_id)
    with open(os.path.join(adir, ".pic.png.upload-0123456789abcdef"), "wb") as f:
        f.write(b"partial")
    with open(os.path.join(adir, ".notes"), "wb") as f:
        f.write(b"notes")
    os.makedirs(os.path.join(adir, "subdir"))
    os.symlink(os.path.join(adir, "pic.png"), os.path.join(adir, "link.png"))
    with caplog.at_level("WARNING", logger="wsi_app.backup"):
        result = _export(client, admin_headers, [src.id])
    with _reader(_download(client, admin_headers, result["export_id"])) as tar:
        names = set(tar.getnames())
    prefix = f"assets/tours/{tour.public_id}/"
    assert {n for n in names if n.startswith(prefix)} == {prefix + "pic.png", prefix + "evil.html"}
    skipped = [m for m in caplog.messages if m.startswith("export:") and "not archived" in m]
    for name in ("subdir", ".notes", "link.png"):
        assert sum(f"asset {name} " in m for m in skipped) == 1, (name, skipped)
    assert not any(".upload-" in m for m in caplog.messages), caplog.messages


def test_export_archives_the_wsi_without_holding_the_slide_lock(
    client, admin_headers, db_session, attach_wsi, monkeypatch,
):
    """The lock is released once the WSI is open: a replace / delete can run
    while the (multi-GB) copy does, and the bundle still holds the bytes the
    serialised metadata describes — the inode the open fd pinned."""
    import threading

    from wsi_app.admin_helpers import slide_mutation_lock

    src, _tour, _basename = _build_source(db_session, attach_wsi)
    wsi_path = _st._slide_wsi_path(src)
    with open(wsi_path, "rb") as f:
        original = f.read()
    real = _backup._add_pinned_file
    seen = {}

    def spy(tar, f, arcname):
        if arcname.startswith("wsi/"):
            got = threading.Event()

            def other_writer():
                with slide_mutation_lock(src.id):
                    tmp = wsi_path + ".new"
                    with open(tmp, "wb") as out:
                        out.write(b"REPLACED")
                    os.replace(tmp, wsi_path)
                got.set()

            th = threading.Thread(target=other_writer)
            th.start()
            seen["lock_free"] = got.wait(5)
            th.join(5)
        return real(tar, f, arcname)

    monkeypatch.setattr(_backup, "_add_pinned_file", spy)
    result = _export(client, admin_headers, [src.id])
    assert seen["lock_free"] is True
    archived = None
    with _reader(_download(client, admin_headers, result["export_id"])) as tar:
        for member in tar:
            if member.name.startswith("wsi/"):
                archived = tar.extractfile(member).read()
                break
    assert archived == original


def test_long_rename_mode_attachment_names_import(tmp_path):
    """Rename mode adds "-<8 hex>" after safe_filename's 200-byte cap, so an
    upload can store a 209-character name; the import accepts what upload
    can store, and still refuses what it can't."""
    from wsi_app.admin_helpers import safe_filename, storable_asset_name

    stem, ext = os.path.splitext(safe_filename("a" * 250 + ".png"))
    stored = f"{stem}-0123abcd{ext}"
    assert len(stored) > 200
    src = tmp_path / "src"
    src.mkdir()
    (src / stored).write_bytes(_png_bytes())
    assert _st._import_tour_assets(str(src), str(tmp_path / "dst")) == []
    assert (tmp_path / "dst" / stored).is_file()
    for bad in (".hidden.png", "..", "a b.png", "ü.png", "x" * 252 + ".png"):
        assert not storable_asset_name(bad), bad


def _stage_slide(sd: dict) -> str:
    stage_dir = _stage_one_wsi(sd["public_id"], sd["wsi_original_basename"])
    return stage_dir


def test_import_holds_the_slide_lock_against_a_sibling_boot_reaper(db_session, monkeypatch):
    """Every worker's (and wsi-sync's) startup sweep reaps placeholder rows
    older than 30 minutes, with their wsi/{id}_* files, skipping only those
    whose slide lock is held. Uploads hold it, and so must the import: else a
    process starting while one slide's import ran that long (a multi-GB copy
    onto another volume, a slow thumbnail) would delete its row and the WSI
    already placed under it, and the slide would fail."""
    from datetime import timedelta

    from sqlalchemy import update

    from wsi_app.admin_helpers import reap_orphan_slide_reservations, utcnow
    from wsi_app.db import SessionLocal

    _patch_wsi_readers(monkeypatch)
    t = os.urandom(3).hex()
    sd = _slide_doc(f"rp{t}", f"rp-{t}.svs", [])
    stage_dir = _stage_slide(sd)
    seen = {}

    def props_while_a_sibling_boots(path):
        # The row is reserved and the WSI placed: age the row past the
        # cutoff and run the sweep a starting process would.
        sid = int(os.path.basename(path).split("_", 1)[0])
        with SessionLocal() as other:
            other.execute(update(Slide).where(Slide.id == sid).values(
                created_at=utcnow() - timedelta(hours=2)))
            other.commit()
            reap_orphan_slide_reservations(other)
            seen["row_kept"] = other.get(Slide, sid) is not None
        seen["file_kept"] = os.path.exists(path)
        return _fake_props()

    monkeypatch.setattr(_st, "read_slide_properties", props_while_a_sibling_boots)
    try:
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert seen == {"row_kept": True, "file_kept": True}
        s = db_session.get(Slide, res["slide_id"])
        assert s.wsi_filename == res["wsi_filename"]
        assert os.path.exists(os.path.join(settings.data_dir, "wsi", s.wsi_filename))
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


@pytest.mark.parametrize("over,code", [
    ({"title": "Liver\tbiopsy"}, "importSlideFieldInvalid"),
    ({"catalog_id": "C" * 129}, "importSlideFieldInvalid"),
    ({"tours": [{"public_id": "tt1", "title": "Tour\x07", "steps": []}]}, "importTourInvalid"),
    ({"tours": [{"public_id": "tt2", "title": "T", "description": "d" * 8001, "steps": []}]},
     "importTourInvalid"),
    ({"tours": [{"public_id": "tt3", "title": "T", "description": "a\x00b", "steps": []}]},
     "importTourInvalid"),
])
def test_values_the_editors_refuse_are_refused_at_preflight_and_apply(db_session, monkeypatch, over, code):
    _patch_wsi_readers(monkeypatch)
    t = os.urandom(3).hex()
    sd = {**_slide_doc(f"ed{t}", f"ed-{t}.svs", []), **over}
    errors = _st._bundle_content_errors(sd)
    assert len(errors) == 1 and errors[0]["step"] == 0, errors
    stage_dir = _stage_slide(sd)
    try:
        with pytest.raises(_st.SlideTransferError) as ei:
            _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert ei.value.code == code
        assert ei.value.params["detail"] == errors[0]["detail"]
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_imported_titles_are_stored_as_the_editors_store_them(db_session, monkeypatch):
    _patch_wsi_readers(monkeypatch)
    t = os.urandom(3).hex()
    sd = _slide_doc(f"ws{t}", f"ws-{t}.svs", [
        {"public_id": f"wt{t}", "title": "  Spaced  ", "steps": []},
        {"public_id": f"wu{t}", "title": "   ", "steps": []},
    ])
    sd["title"], sd["catalog_id"] = "  Liver  ", " C-1 "
    stage_dir = _stage_slide(sd)
    try:
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        s = db_session.get(Slide, res["slide_id"])
        assert (s.title, s.catalog_id) == ("Liver", "C-1")
        titles = sorted(x.title for x in db_session.query(Tour).filter_by(slide_id=s.id))
        assert titles == ["Spaced", "Tour"]
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


@pytest.mark.parametrize("where,exc,code", [
    ("read", ValueError("not a slide"), "wsiUnreadable"),
    ("thumb", RuntimeError("decoder"), "thumbnailFailed"),
    ("thumb", OSError(errno.ENOSPC, "No space left on device"), "importNoSpace"),
    ("place", OSError(errno.EDQUOT, "Disk quota exceeded"), "importNoSpace"),
])
def test_per_slide_failures_carry_specific_codes(db_session, monkeypatch, where, exc, code):
    _patch_wsi_readers(monkeypatch)

    def boom(*a, **k):
        raise exc

    target = {"read": "read_slide_properties", "thumb": "make_thumbnail",
              "place": "_place_staged_wsi"}[where]
    monkeypatch.setattr(_st, target, boom)
    t = os.urandom(3).hex()
    sd = _slide_doc(f"fc{t}", f"fc-{t}.svs", [])
    stage_dir = _stage_slide(sd)
    try:
        with pytest.raises(_st.SlideTransferError) as ei:
            _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert ei.value.code == code
        assert db_session.query(Slide).filter(Slide.public_id == sd["public_id"]).count() == 0
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_an_id_race_keeps_the_free_bundle_public_id(db_session, slide, monkeypatch):
    """Losing the slides.id race to a concurrent upload retries with the next
    id; the bundle's public_id is only reminted when a row holds it."""
    _patch_wsi_readers(monkeypatch)
    real = _st.reserve_next_slide_id
    calls = {"n": 0}

    def racing(db):
        calls["n"] += 1
        got = real(db)
        return slide.id if calls["n"] == 1 else got

    monkeypatch.setattr(_st, "reserve_next_slide_id", racing)
    t = os.urandom(3).hex()
    sd = _slide_doc(f"race{t}", f"race-{t}.svs", [])
    stage_dir = _stage_slide(sd)
    try:
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert calls["n"] == 2
        assert res["public_id"] == sd["public_id"]
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_data_volume_space_is_checked_when_the_wsi_must_be_copied(db_session, monkeypatch):
    """Stage and wsi/ on different filesystems: placing a slide copies its
    bytes, so the preflight says when they won't fit and the apply refuses
    the slide before reserving a row."""
    _patch_wsi_readers(monkeypatch)
    monkeypatch.setattr(_st, "_same_filesystem", lambda a, b: False)
    monkeypatch.setattr(
        _st._disk_space, "check",
        lambda path, nbytes, reserve=None: _st._disk_space.SpaceShortfall(path, nbytes, 1, 0),
    )
    t = os.urandom(3).hex()
    stage_id = _stage_bundle([_slide_doc(f"sp{t}", f"sp-{t}.svs", [])])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        plan = _st.plan_import(stage_id, manifest, doc, db_session)
        assert plan["data_space"]["needed_bytes"] == len(b"WSIDATA"), plan["data_space"]
        result = _st.apply_import(stage_id, {})
        assert result["failed"][0]["code"] == "importNoSpace", result
        assert db_session.query(Slide).filter(Slide.public_id == f"sp{t}").count() == 0
    finally:
        _st.cleanup_import_stage(stage_id)

    # Same filesystem: hard-linked, no data-volume space needed.
    monkeypatch.setattr(_st, "_same_filesystem", lambda a, b: True)
    stage_id = _stage_bundle([_slide_doc(f"sq{t}", f"sq-{t}.svs", [])])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        assert _st.plan_import(stage_id, manifest, doc, db_session)["data_space"] is None
    finally:
        _st.cleanup_import_stage(stage_id)


# ---------------------------------------------------------------------------
# Re-imports, failed cleanups, bundle errors, version range
# ---------------------------------------------------------------------------

def _live_slide(db, public_id: str, basename: str) -> Slide:
    s = Slide(public_id=public_id, title="Existing", catalog_id="EX")
    db.add(s)
    db.commit()
    db.refresh(s)
    s.wsi_filename = f"{s.id}_{basename}"
    s.wsi_filename_norm = basename.casefold()
    db.commit()
    return s


def test_public_id_conflict_is_reported_and_skip_honours_it(db_session, monkeypatch):
    """Re-importing a bundle that was applied before must give a sign of it —
    not only a filename conflict, which the dashboard defaults to "copy" —
    or it would create a second, published copy of every slide under reminted
    ids. The plan says a slide here already carries the public_id, and "skip"
    leaves such a slide out even when its file name is free (renamed since)."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    pid = f"pc{t}"
    existing = _live_slide(db_session, pid, f"renamed-{t}.svs")
    stage_id = _stage_bundle([_slide_doc(pid, f"orig-{t}.svs", [])])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        row = _st.plan_import(stage_id, manifest, doc, db_session)["slides"][0]
        assert row["public_id_conflict"] is True
        assert row["filename_conflict"] is False

        result = _st.apply_import(stage_id, {pid: "skip"})
        assert result["imported_count"] == 0 and result["skipped_count"] == 1, result
        assert result["skipped"] == [{"public_id": pid, "reason": "public_id_conflict"}]
        db_session.expire_all()
        assert db_session.query(Slide).filter(Slide.public_id == pid).count() == 1
    finally:
        _st.cleanup_import_stage(stage_id)

    # The API default stays "copy" (see ImportApplyRequest): a copy under a
    # reminted public_id, the existing slide untouched.
    stage_id = _stage_bundle([_slide_doc(pid, f"orig2-{t}.svs", [])])
    try:
        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 1, result
        assert result["imported"][0]["public_id"] != pid
        db_session.expire_all()
        assert db_session.get(Slide, existing.id).public_id == pid
    finally:
        _st.cleanup_import_stage(stage_id)


def test_plan_public_id_conflict_ignores_free_ids_and_placeholders(db_session):
    t = _tok()
    placeholder = Slide(public_id=f"ph{t}")  # a reservation: no WSI yet
    db_session.add(placeholder)
    db_session.commit()
    stage_id = _stage_bundle([
        _slide_doc(f"ph{t}", f"ph-{t}.svs", []),
        _slide_doc(f"fr{t}", f"fr-{t}.svs", []),
    ])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        rows = _st.plan_import(stage_id, manifest, doc, db_session)["slides"]
        assert [r["public_id_conflict"] for r in rows] == [False, False]
    finally:
        _st.cleanup_import_stage(stage_id)
        db_session.delete(placeholder)
        db_session.commit()


def test_apply_refuses_an_unknown_resolution_value(client, admin_headers):
    """Anything but "skip" must not mean copy, or a typo would import the very
    duplicate it was meant to leave out."""
    r = client.post(
        "/api/admin/slides/import/apply", headers=admin_headers,
        json={"stage_id": "import-" + "0" * 12, "resolutions": {"abc123": "Skip"}},
    )
    assert r.status_code == 422, r.text


def _session_with_failing_slide_delete(monkeypatch, failing: dict):
    """Make apply_import's session refuse to delete Slide rows while
    ``failing["on"]`` — a placeholder whose cleanup does not go through."""
    real = _st.SessionLocal

    def factory():
        session = real()
        orig_delete = session.delete

        def delete(obj):
            if failing["on"] and isinstance(obj, Slide):
                raise RuntimeError("simulated: database is locked")
            return orig_delete(obj)

        session.delete = delete
        return session

    monkeypatch.setattr(_st, "SessionLocal", factory)


def test_failed_cleanup_keeps_the_reservation_so_the_retry_keeps_the_public_id(db_session, monkeypatch):
    """A slide fails after its placeholder was committed and the placeholder
    cannot be deleted. Were the failed record to replace the pending one and
    drop the slide id, the ghost row would keep the bundle's public_id and the
    retry remint it (every link to the slide would break)."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    pid = f"gc{t}"
    broken = {f"ghost-{t}"}
    monkeypatch.setattr(_st, "read_slide_properties", _failing_props(broken))
    failing = {"on": True}
    _session_with_failing_slide_delete(monkeypatch, failing)
    stage_id = _stage_bundle([_slide_doc(pid, f"ghost-{t}.svs", [])])
    try:
        first = _st.apply_import(stage_id, {})
        assert first["failed_count"] == 1 and first["stage_kept"] is True
        rec = _state(stage_id)["slides"][pid]
        assert rec["status"] == "failed"
        ghost_id = rec["reserved"]["slide_id"]
        assert rec["reserved"]["public_id"] == pid
        db_session.expire_all()
        ghost = db_session.get(Slide, ghost_id)
        assert ghost is not None and not ghost.wsi_filename and ghost.public_id == pid
        # Still reported as failed (with its reason) by the preflight.
        row = _st.stage_plan(stage_id, db_session)["plan"]["slides"][0]
        assert row["apply_status"] == "failed"

        broken.clear()
        failing["on"] = False
        second = _st.apply_import(stage_id, {})
        assert second["imported_count"] == 1, second
        assert second["imported"][0]["public_id"] == pid
        db_session.expire_all()
        assert db_session.get(Slide, ghost_id) is None
        assert db_session.query(Slide).filter(Slide.public_id == pid).count() == 1
    finally:
        _st.cleanup_import_stage(stage_id)


def test_reservation_that_landed_despite_an_error_is_cleaned_up_on_retry(db_session, monkeypatch):
    """The reservation's commit lands but the call still fails (here: the
    refresh after it). The outcome is unknown to the apply; the record must
    still name the row, or it holds the public_id for good."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    pid = f"rl{t}"
    real_reserve = _st._reserve_slide_row

    def _reserve_then_fail(db, desired, before_commit=None):
        real_reserve(db, desired, before_commit=before_commit)
        raise RuntimeError("simulated failure after the reservation committed")

    stage_id = _stage_bundle([_slide_doc(pid, f"landed-{t}.svs", [])])
    try:
        monkeypatch.setattr(_st, "_reserve_slide_row", _reserve_then_fail)
        first = _st.apply_import(stage_id, {})
        assert first["failed_count"] == 1
        rec = _state(stage_id)["slides"][pid]
        assert rec["status"] == "failed" and rec["reserved"]["public_id"] == pid
        monkeypatch.setattr(_st, "_reserve_slide_row", real_reserve)

        second = _st.apply_import(stage_id, {})
        assert second["imported_count"] == 1, second
        assert second["imported"][0]["public_id"] == pid
        db_session.expire_all()
        assert db_session.get(Slide, rec["reserved"]["slide_id"]) is None
        assert db_session.query(Slide).filter(Slide.public_id == pid).count() == 1
    finally:
        _st.cleanup_import_stage(stage_id)


def test_retry_leaves_the_files_of_a_slide_that_since_took_the_reserved_id(db_session, monkeypatch):
    """A reservation whose commit failed (rolled back) also rolled back the id
    watermark, so an ordinary upload can then reserve the same id N. Its
    finalise places wsi/N_<name> before its row's wsi_filename is set; the
    retry's discard would find no row referencing that name and delete the
    upload's bytes. A row at the recorded id with another public_id
    means that id's files are not the import's to remove."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    basename = f"rs-{t}.svs"
    pid = f"p{t}"
    stage_id = _stage_bundle([_slide_doc(pid, basename, [])])
    real_reserve = _st._reserve_slide_row
    seen: dict = {}

    def _reserve_commit_fails(db, desired, before_commit=None):
        cid = reserve_next_slide_id(db)
        before_commit(cid, desired)
        seen["id"] = cid
        db.rollback()  # the commit failed (e.g. SQLITE_BUSY): nothing landed
        raise _st.SlideTransferError("busy", code="importReserveFailed")

    upath = None
    up = None
    try:
        monkeypatch.setattr(_st, "_reserve_slide_row", _reserve_commit_fails)
        assert _st.apply_import(stage_id, {})["failed_count"] == 1
        rec = _state(stage_id)["slides"][pid]
        assert rec["status"] == "failed" and rec["reserved"]["slide_id"] == seen["id"]
        monkeypatch.setattr(_st, "_reserve_slide_row", real_reserve)

        # An ordinary upload reserves the same id and is mid-finalise: its
        # file is at wsi/{id}_{name}, its row still a placeholder.
        db_session.expire_all()
        nid = reserve_next_slide_id(db_session)
        assert nid == seen["id"]
        up = Slide(id=nid, public_id=f"u{t}")
        db_session.add(up)
        db_session.commit()
        wdir = os.path.join(settings.data_dir, "wsi")
        os.makedirs(wdir, exist_ok=True)
        upath = os.path.join(wdir, f"{nid}_{basename}")
        with open(upath, "wb") as f:
            f.write(b"UPLOAD IN FLIGHT")

        # The retry fails before reserving, so only the discard could touch it.
        monkeypatch.setattr(_st, "_staged_wsi_file", lambda *a, **k: None)
        assert _st.apply_import(stage_id, {})["failed_count"] == 1
        assert os.path.exists(upath), "the retry deleted another slide's WSI"
        db_session.expire_all()
        assert db_session.get(Slide, nid) is not None  # nor its placeholder row
    finally:
        _st.cleanup_import_stage(stage_id)
        if upath and os.path.exists(upath):
            os.remove(upath)
        if up is not None:
            db_session.expire_all()
            row = db_session.get(Slide, up.id)
            if row is not None:
                db_session.delete(row)
                db_session.commit()


def test_a_malformed_reservation_in_a_failed_record_is_refused():
    good = {"status": "failed", "code": None, "error": "x", "params": {}}
    assert _st._valid_state_record(good)
    reserved = {
        "slide_id": 5, "public_id": "abc123", "wsi_filename": "5_a.svs",
        "thumb_filename": "5_t.jpg", "title": "", "renamed": False,
        "tours": {}, "new_asset_dirs": [],
    }
    assert _st._valid_state_record({**good, "reserved": reserved})
    assert not _st._valid_state_record({**good, "reserved": {**reserved, "wsi_filename": "../x"}})
    assert not _st._valid_state_record({**good, "reserved": "nope"})


def _bundle_members(extra_files: dict, *, basename="x.svs") -> bytes:
    manifest = json.dumps(_base_manifest(schema_version=_st.CURRENT_SCHEMA_VERSION)).encode()
    doc = json.dumps({"slides": [
        {"public_id": "abc123", "wsi_original_basename": basename, "tours": []},
    ]}).encode()
    members = []
    for name, data in {"manifest.json": manifest, "slides.json": doc, **extra_files}.items():
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    return _tar_gz_with(members)


def test_staged_bundle_files_do_not_keep_the_archives_world_readable_modes(client, admin_headers):
    """An archive records the modes its files had (0755 from a hand-made one)
    and an imported WSI is a hard link to the staged file: staging normalises
    them like restore staging does, so the live file is 0640 in a 0750 tree."""
    manifest = json.dumps(_base_manifest(schema_version=_st.CURRENT_SCHEMA_VERSION)).encode()
    doc = json.dumps({"slides": [{"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": []}]}).encode()
    members = []
    for name, data, mode in (
        ("manifest.json", manifest, 0o644), ("slides.json", doc, 0o755), ("wsi/abc123/x.svs", b"WSI", 0o777),
    ):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = mode
        members.append((info, data))
    body = _stage(client, admin_headers, _tar_gz_with(members), "tar.gz")
    try:
        stage_dir = _st._stage_dir(body["stage_id"])
        for rel in ("manifest.json", "slides.json", "wsi/abc123/x.svs"):
            assert os.stat(os.path.join(stage_dir, rel)).st_mode & 0o7777 == 0o640, rel
        for rel in ("", "wsi", "wsi/abc123"):
            assert os.stat(os.path.join(stage_dir, rel)).st_mode & 0o7777 == 0o750, rel
    finally:
        _st.cleanup_import_stage(body["stage_id"])


def test_a_slide_folder_with_a_second_file_is_refused(client, admin_headers):
    """A bundle unpacked and re-packed on macOS gains ``._x.svs`` next to
    ``x.svs``; the import must not take whichever sorts first as the WSI."""
    before = _import_stages()
    r = _stage_job(client, admin_headers, _bundle_members({
        "wsi/abc123/x.svs": b"WSI", "wsi/abc123/._x.svs": b"\x00\x05\x16\x07",
    }), "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importWsiExtraFile"
    assert r["result"]["params"] == {"slide": "abc123", "entry": "wsi/abc123/._x.svs"}
    assert _import_stages() == before


def test_staged_wsi_file_needs_exactly_one_file(tmp_path):
    d = tmp_path / "wsi" / "abc123"
    d.mkdir(parents=True)
    (d / "x.svs").write_bytes(b"W")
    assert _st._staged_wsi_file(str(tmp_path), "abc123") == str(d / "x.svs")
    (d / "._x.svs").write_bytes(b"A")
    assert _st._staged_wsi_file(str(tmp_path), "abc123") is None


def test_a_non_ascii_wsi_name_stages(client, admin_headers):
    name = "スライド-ä.svs"
    body = _stage(client, admin_headers, _bundle_members({f"wsi/abc123/{name}": b"W"}, basename=name), "tar.gz")
    try:
        row = body["plan"]["slides"][0]
        assert row["wsi_present_in_bundle"] is True
        assert row["wsi_original_basename"] == name
    finally:
        _st.cleanup_import_stage(body["stage_id"])


def test_a_system_backup_and_a_foreign_archive_get_their_own_codes(client, admin_headers):
    backup_manifest = json.dumps({
        "scope": "full", "app_version": APP_VERSION, "created_at": "", "slide_count": 0,
        "tour_count": 0, "includes_analytics": True, "includes_history": True,
        "schema_version": _st.CURRENT_SCHEMA_VERSION,
    }).encode()
    members = []
    for name, data in (("manifest.json", backup_manifest), ("wsi.db", b"SQLite format 3\x00")):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        members.append((info, data))
    r = _stage_job(client, admin_headers, _tar_gz_with(members), "tar.gz")
    assert r["result"]["code"] == "adminDash.apiErr.importIsSystemBackup", r

    other = json.dumps({"name": "something else"}).encode()
    info = tarfile.TarInfo("manifest.json")
    info.size = len(other)
    r = _stage_job(client, admin_headers, _tar_gz_with([(info, other)]), "tar.gz")
    assert r["result"]["code"] == "adminDash.apiErr.importNotABundle", r


def test_invalid_slides_json_carries_its_detail(client, admin_headers):
    slides_doc = {"slides": [{"public_id": "abc123", "wsi_original_basename": "x.svs", "tours": 5}]}
    r = _stage_job(client, admin_headers, _make_crafted_bundle(_base_manifest(), slides_doc), "tar.gz")
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"
    assert r["result"]["params"] == {"detail": "malformed tours list"}

    info = tarfile.TarInfo("manifest.json")
    data = b"{not json"
    info.size = len(data)
    r = _stage_job(client, admin_headers, _tar_gz_with([(info, data)]), "tar.gz")
    assert r["result"]["code"] == "adminDash.apiErr.importInvalid"
    assert r["result"]["params"]["detail"].startswith("manifest.json is not valid JSON")


def test_a_truncated_archive_is_reported_as_corrupt(client, admin_headers):
    whole = _make_crafted_bundle(_base_manifest(), {"slides": []})
    r = _stage_job(client, admin_headers, whole[: len(whole) // 2], "tar.gz")
    assert r["state"] == "failed", r
    assert r["result"]["code"] == "adminDash.apiErr.importCorrupt"


def test_bundles_within_the_compatible_schema_range_import():
    """Migrations since MIN_BUNDLE_SCHEMA_VERSION do not change what a bundle
    carries, so a bundle from an older schema in that range imports; one
    older than it, or newer than this system, does not."""
    cur, low = _st.CURRENT_SCHEMA_VERSION, _st.MIN_BUNDLE_SCHEMA_VERSION
    for sv in range(low, cur + 1):
        assert _st._import_version_block({"schema_version": sv}) == (None, None), sv
    assert _st._import_version_block({"schema_version": str(low)}) == (None, None)
    assert _st._import_version_block({"schema_version": low - 1})[0] == "VERSION_MISMATCH"
    code, reason = _st._import_version_block({"schema_version": cur + 1})
    assert code == "VERSION_MISMATCH" and "newer" in reason


def test_wsi_copy_needs_room_for_a_reserve(db_session, monkeypatch, tmp_path):
    """The data volume must keep the shared reserve (disk_space.reserve_for)
    free after the copy (the file merely fitting is not enough), and the
    copy that replaces a refused hard link is checked too."""
    from wsi_app import disk_space
    from collections import namedtuple

    usage = namedtuple("U", "total used free")
    size = 10 * 1024 * 1024
    src = tmp_path / "src.svs"
    src.write_bytes(b"\x00" * size)
    reserve = disk_space.reserve_for(size)
    assert reserve == disk_space.RESERVE_MIN_BYTES

    free = {"n": size + reserve - 1}
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: usage(10**13, 0, free["n"]))
    assert disk_space.check(str(tmp_path), size) is not None
    free["n"] = size + reserve
    assert disk_space.check(str(tmp_path), size) is None

    def _no_link(a, b, *k, **kw):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(_st.os, "link", _no_link)
    free["n"] = size * 2  # fits, but not with the reserve
    dst = tmp_path / "out" / "dst.svs"
    dst.parent.mkdir()
    with pytest.raises(_st.SlideTransferError) as ei:
        _st._place_staged_wsi(str(src), str(dst))
    assert ei.value.code == "importNoSpace"
    assert not dst.exists()
    free["n"] = size + reserve
    _st._place_staged_wsi(str(src), str(dst))
    assert dst.stat().st_size == size


def test_export_refuses_a_slide_with_a_malformed_public_id(client, admin_headers, db_session, attach_wsi):
    s = Slide(title="Bad id", catalog_id="B")
    db_session.add(s)
    db_session.commit()
    attach_wsi(s)
    s.public_id = "bad/../id"
    db_session.commit()
    try:
        r = client.post("/api/admin/slides/export", json={"slide_ids": [s.id]}, headers=admin_headers)
        assert r.status_code == 200, r.text
        job = _poll(client, r.json()["job_id"], admin_headers)
        assert job["state"] == "failed", job
        assert job["result"]["code"] == "adminDash.apiErr.exportInvalidPublicId"
        assert job["result"]["params"] == {"slide": "Bad id"}
    finally:
        db_session.delete(s)
        db_session.commit()


def test_export_refuses_a_tour_with_a_malformed_public_id(
    client, admin_headers, db_session, attach_wsi, patched_openslide,
):
    # The tour's public_id is written into slides.json as is (while its
    # attachments would be silently skipped), and the importer refuses the whole
    # bundle over one malformed tour token — so the export must refuse it up
    # front, naming slide and tour, instead of handing over an unimportable
    # bundle.
    src, tour, _basename = _build_source(db_session, attach_wsi)
    good_pid = tour.public_id
    tour.public_id = "bad id!"
    db_session.commit()
    try:
        r = client.post("/api/admin/slides/export", json={"slide_ids": [src.id]}, headers=admin_headers)
        assert r.status_code == 200, r.text
        job = _poll(client, r.json()["job_id"], admin_headers)
        assert job["state"] == "failed", job
        assert job["result"]["code"] == "adminDash.apiErr.exportInvalidTourPublicId"
        assert job["result"]["params"] == {"slide": "Source Slide", "tour": "Guided"}
    finally:
        tour.public_id = good_pid
        db_session.commit()


def test_token_download_is_audited_against_the_minting_admin(
    client, admin_headers, admin_user, db_session, attach_wsi, patched_openslide, caplog,
):
    src, _tour, _basename = _build_source(db_session, attach_wsi)
    result = _export(client, admin_headers, [src.id])
    url = client.post(
        f"/api/admin/slides/export/{result['export_id']}/download_url", headers=admin_headers,
    ).json()["url"]
    caplog.clear()
    with caplog.at_level("INFO", logger="wsi_app.audit"):
        assert client.get(url).status_code == 200
    records = [
        json.loads(r.getMessage()) for r in caplog.records
        if r.name == "wsi_app.audit" and '"slide.export.download"' in r.getMessage()
    ]
    assert len(records) == 1, records
    assert records[0]["actor_id"] == admin_user.id
    assert records[0]["via_token"] is True
