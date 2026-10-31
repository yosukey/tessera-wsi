"""End-to-end tests for backup/restore: create -> list -> stage -> plan -> apply.

The full apply path uses the dev in-process swap (BACKUP_DEV_INPROCESS_APPLY)
so the tests never depend on systemd in the test environment.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import time

import pytest

from wsi_app import app_meta as _app_meta
from wsi_app import backup as _backup
from wsi_app import disk_space
from wsi_app import migrations as _migrations
from wsi_app.config import settings
from wsi_app.version import APP_VERSION


def _await_job(client, headers, job_id, *, path="/api/admin/backup/jobs"):
    """Poll a background job to a terminal state and return its record.

    The work runs in a daemon thread, so "poll 50 times as fast as HTTP allows"
    is a busy-wait, not a wait: on a loaded machine the thread can lose the CPU
    for the whole burst, and the caller then reads ``result`` off a job still
    marked running — surfacing as a KeyError or a bare state assertion several
    lines later, in a different test each time. Sleep between polls and fail
    with the job record when the budget is genuinely exhausted.
    """
    deadline = time.monotonic() + 30.0
    rj = None
    while time.monotonic() < deadline:
        rj = client.get(f"{path}/{job_id}", headers=headers).json()
        if rj["state"] in ("succeeded", "failed"):
            return rj
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state: {rj}")


def _await_job_record(job_id: str) -> dict:
    """Like :func:`_await_job`, reading the job store directly: for a job
    whose apply signs every session out, the caller's token included."""
    from wsi_app import jobs as _jobs

    deadline = time.monotonic() + 30.0
    rec = None
    while time.monotonic() < deadline:
        job = _jobs.store.get(job_id)
        rec = job.to_dict() if job is not None else None
        if rec and rec["state"] in ("succeeded", "failed"):
            return rec
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state: {rec}")


def test_create_backup_metadata_contains_db_and_manifest(client, admin_headers, slide):
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": "first"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Poll until done; the threadpool executes inline so this should be fast.
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    backup_id = rj["result"]["backup_id"]

    # The archive sits on disk and lists.
    listing = client.get("/api/admin/backup", headers=admin_headers).json()
    assert listing["current_app_version"] == APP_VERSION
    ours = [it for it in listing["items"] if it["id"] == backup_id]
    assert len(ours) == 1
    assert ours[0]["scope"] == "metadata"
    assert ours[0]["app_version"] == APP_VERSION
    assert ours[0]["note"] == "first"
    assert ours[0]["sha256"]

    # Inspect on disk: manifest.json, wsi.db both present and sane.
    rec = _backup.find_backup(backup_id)
    assert rec is not None
    with _backup._open_archive_reader(rec.path) as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "wsi.db" in names

    # download endpoint streams bytes back.
    dl = client.get(f"/api/admin/backup/{backup_id}/download", headers=admin_headers)
    assert dl.status_code == 200
    assert len(dl.content) > 0


def test_backup_requires_admin(client):
    r = client.post("/api/admin/backup", json={"include_wsi": False})
    assert r.status_code == 401


def _make_backup_via_api(client, admin_headers) -> str:
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    return rj["result"]["backup_id"]


def _stage_job(client, headers, **request) -> dict:
    """POST /api/admin/restore (a snapshot id or an upload) and wait for the
    staging job it starts; returns the terminal job record."""
    r = client.post("/api/admin/restore", headers=headers, **request)
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"job_id"}
    rj = _await_job(client, headers, r.json()["job_id"])
    assert rj["kind"] == "restore_stage"
    return rj


def _stage_via_api(client, headers, **request) -> dict:
    """Stage an archive and return the staging job's result (``stage_id``,
    ``manifest``, ``plan``, ``current_app_version``)."""
    rj = _stage_job(client, headers, **request)
    assert rj["state"] == "succeeded", rj
    return rj["result"]


def test_backup_download_url_round_trips_token(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)

    r = client.post(
        f"/api/admin/backup/{backup_id}/download_url",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(f"/api/admin/backup/{backup_id}/download?t=")

    # The signed URL works without an Authorization header.
    dl = client.get(url)
    assert dl.status_code == 200
    assert len(dl.content) > 0


def test_backup_download_url_requires_admin(client):
    r = client.post("/api/admin/backup/anything/download_url")
    assert r.status_code == 401


def test_backup_download_url_404_for_missing(client, admin_headers):
    r = client.post(
        "/api/admin/backup/does-not-exist/download_url",
        headers=admin_headers,
    )
    assert r.status_code == 404


def test_backup_download_token_rejects_other_backup_id(client, admin_headers):
    a = _make_backup_via_api(client, admin_headers)
    b = _make_backup_via_api(client, admin_headers)
    assert a != b

    r = client.post(
        f"/api/admin/backup/{a}/download_url",
        headers=admin_headers,
    )
    token = r.json()["url"].rsplit("=", 1)[1]

    # Token issued for backup A is not accepted on backup B.
    bad = client.get(f"/api/admin/backup/{b}/download?t={token}")
    assert bad.status_code == 401


def test_backup_download_token_expires(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)

    # Mint a token with an exp claim in the past and confirm the download
    # endpoint rejects it. The JWT is crafted directly so the test does not
    # have to mock pyjwt's internal clock.
    import time as _time
    import jwt
    from wsi_app.auth import JWT_SECRET, JWT_ALG

    expired_payload = {
        "purpose": "backup_download",
        "backup_id": backup_id,
        "iat": int(_time.time()) - 600,
        "exp": int(_time.time()) - 60,
    }
    expired_token = jwt.encode(expired_payload, JWT_SECRET, algorithm=JWT_ALG)

    expired = client.get(
        f"/api/admin/backup/{backup_id}/download?t={expired_token}"
    )
    assert expired.status_code == 401


def test_backup_download_token_rejects_wrong_purpose(client, admin_headers):
    # An admin Bearer JWT (purpose != "backup_download") must NOT be accepted
    # via the ?t= query path. The query path is reserved for tokens minted
    # by /download_url so an admin session token leaking into a URL stays
    # bounded to the request that placed it there.
    backup_id = _make_backup_via_api(client, admin_headers)
    bearer = admin_headers["Authorization"].split(" ", 1)[1]
    r = client.get(f"/api/admin/backup/{backup_id}/download?t={bearer}")
    assert r.status_code == 401


def test_backup_download_admin_bearer_works(client, admin_headers):
    # Bearer-authenticated download is supported in addition to the
    # short-lived `?t=` query token, so CLI clients and scripts can use
    # the standard Authorization header.
    backup_id = _make_backup_via_api(client, admin_headers)
    r = client.get(
        f"/api/admin/backup/{backup_id}/download",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert len(r.content) > 0


def test_backup_download_rejects_unauthorized(client):
    r = client.get("/api/admin/backup/whatever/download")
    assert r.status_code == 401


def test_create_backup_rejects_non_bool_include(client, admin_headers):
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": "everything", "note": ""},
        headers=admin_headers,
    )
    # pydantic can't coerce an arbitrary string to bool: 422
    assert r.status_code == 422


def test_delete_backup_removes_file_and_sidecar(client, admin_headers):
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": ""},
        headers=admin_headers,
    )
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded"
    backup_id = rj["result"]["backup_id"]

    rec = _backup.find_backup(backup_id)
    assert rec is not None and os.path.isfile(rec.path)
    assert os.path.isfile(rec.path + ".meta.json")

    d = client.delete(f"/api/admin/backup/{backup_id}", headers=admin_headers)
    assert d.status_code == 200
    assert _backup.find_backup(backup_id) is None
    assert not os.path.isfile(rec.path)
    assert not os.path.isfile(rec.path + ".meta.json")


def test_safe_extract_rejects_path_traversal(tmp_path):
    """tar with ../etc/passwd entry must be refused."""
    archive = tmp_path / "bad.tar"
    with tarfile.open(str(archive), "w") as tar:
        # Write a small payload at a malicious path.
        data = b"pwned"
        info = tarfile.TarInfo("../escapee.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    dest = tmp_path / "out"
    with tarfile.open(str(archive), "r") as tar:
        with pytest.raises(RuntimeError, match="unsafe path"):
            _backup._safe_extract(tar, str(dest))


def test_safe_extract_rejects_symlinks(tmp_path):
    archive = tmp_path / "linky.tar"
    with tarfile.open(str(archive), "w") as tar:
        info = tarfile.TarInfo("evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    dest = tmp_path / "out"
    with tarfile.open(str(archive), "r") as tar:
        with pytest.raises(RuntimeError, match="links not allowed"):
            _backup._safe_extract(tar, str(dest))


def test_plan_restore_equal_versions_has_empty_plan(tmp_path):
    db = tmp_path / "wsi.db"
    conn = sqlite3.connect(str(db))
    # sqlite_set_app_version does not create app_meta on demand.
    conn.execute(
        "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    _app_meta.sqlite_set_app_version(str(db), APP_VERSION)
    manifest = _backup.Manifest(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    plan = _backup.plan_restore(manifest)
    assert plan.blocking_reason is None


def test_plan_restore_downgrade_blocked():
    manifest = _backup.Manifest(
        scope="metadata",
        # Pretend the archive was built by a future binary.
        app_version="999.999.0",
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    plan = _backup.plan_restore(manifest)
    assert plan.blocking_code == "DOWNGRADE_NOT_SUPPORTED"


def test_plan_restore_older_version_is_unblocked(monkeypatch):
    """A backup older than the running binary restores without any
    pre-swap migration chain: schema reconciliation happens on the
    backend's next boot via ``main._apply_column_migrations``.
    """
    monkeypatch.setattr(_backup, "APP_VERSION", "0.9.59")
    manifest = _backup.Manifest(
        scope="metadata",
        app_version="0.9.57",
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    plan = _backup.plan_restore(manifest)
    assert plan.blocking_reason is None


def test_plan_restore_refuses_an_archive_below_the_baseline_schema():
    """An archive stamped below the baseline has no
    migration path to this shape, so it is refused before any staging trial."""
    manifest = _backup.Manifest(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.BASELINE_SCHEMA_VERSION - 1,
    )
    plan = _backup.plan_restore(manifest)
    assert plan.blocking_code == "SCHEMA_TOO_OLD"


def test_full_restore_roundtrip_in_process(client, admin_headers, db_session):
    """Create a backup, modify the live DB, restore via in-process apply,
    verify the live DB reverts to the backup state.

    Relies on BACKUP_DEV_INPROCESS_APPLY=1 set by conftest so the apply path
    swaps the file in-process instead of asking systemd to restart the service.
    """
    from wsi_app.models import Slide
    assert settings.backup_dev_inprocess_apply

    # Sentinel slide that exists in the backup but will be deleted before
    # restore.
    s = Slide(title="ToBackup", catalog_id="BK-001")
    db_session.add(s)
    db_session.commit()
    sentinel_id = s.id

    # 1. Create backup.
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": "roundtrip"},
        headers=admin_headers,
    )
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    backup_id = rj["result"]["backup_id"]

    # 2. Mutate live DB: delete the sentinel.
    db_session.delete(s)
    db_session.commit()
    assert db_session.get(Slide, sentinel_id) is None

    # 3. Stage from snapshot id.
    body = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})
    stage_id = body["stage_id"]
    assert body["plan"]["blocking_reason"] is None

    # 4. Apply.
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "inprocess"

    # 5. Re-open a session against the (now-swapped) DB and verify the
    # sentinel is back. SessionLocal is re-imported because in-process apply
    # disposed/rebuilt the engine indirectly via SQLAlchemy connection
    # pool reset on dispose().
    from wsi_app.db import SessionLocal
    db2 = SessionLocal()
    try:
        assert db2.get(Slide, sentinel_id) is not None
    finally:
        db2.close()


def test_restore_corrupt_archive_fails_the_job_with_a_reason(client, admin_headers):
    """Staging a truncated/garbage upload must fail the staging job with a
    message naming the problem rather than leaking a tarfile/zstd error."""
    rj = _stage_job(
        client, admin_headers,
        files={"file": ("x.tar.gz", io.BytesIO(b"not a real archive"),
                        "application/octet-stream")},
    )
    assert rj["state"] == "failed", rj
    assert "corrupt or not a supported format" in rj["error"]
    # The slot is released and the uploaded copy removed.
    staging = os.path.join(settings.backup_dir, ".staging")
    assert not [n for n in os.listdir(staging) if n.startswith("upload-")]


def test_apply_requires_correct_password(client, admin_headers):
    # Stage anything (use a created backup) and verify a wrong password is
    # rejected before any swap happens.
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": ""},
        headers=admin_headers,
    )
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    backup_id = rj["result"]["backup_id"]

    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "wrong"},
        headers=admin_headers,
    )
    assert r.status_code == 401


def test_apply_requires_confirm_text(client, admin_headers):
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": ""},
        headers=admin_headers,
    )
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    backup_id = rj["result"]["backup_id"]

    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "yes", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Wipe-and-replace semantics: metadata restore wipes wsi/ (does not leave it
# in place). Reset wipes everything except admin/config rows.
# ---------------------------------------------------------------------------

def test_metadata_restore_wipes_wsi_directory(client, admin_headers, db_session):
    """A metadata-scope restore must drop the live wsi/ directory along with
    the DB and assets. Operators re-upload WSI files individually post-
    restore; leaving stale files behind would create dangling references
    between the new DB and old wsi/ content from a previous deployment.
    """
    assert settings.backup_dev_inprocess_apply

    # Pre-seed a wsi/ file that exists on disk but is unrelated to the
    # archive's DB rows. After the metadata restore, this file must be
    # gone (parked under .pre-restore-*, not present in the live tree).
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    sentinel_path = os.path.join(wsi_dir, "pre-restore-sentinel.svs")
    with open(sentinel_path, "wb") as f:
        f.write(b"sentinel contents")
    assert os.path.isfile(sentinel_path)

    # Create a metadata backup (archive contains DB + assets, no wsi/).
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "note": "wipe-test"},
        headers=admin_headers,
    )
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded"
    backup_id = rj["result"]["backup_id"]

    # Stage + apply.
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    # Sentinel is gone from wsi/, parked under .pre-restore-*.
    assert not os.path.isfile(sentinel_path), (
        "metadata restore should have moved wsi/ aside; sentinel still present"
    )
    parked = [
        os.path.join(settings.data_dir, name)
        for name in os.listdir(settings.data_dir)
        if name.startswith(".pre-restore-")
    ]
    assert parked, "no .pre-restore-* parked directory found after metadata restore"
    # The most recent parked dir should hold the old wsi/ tree (sentinel inside).
    latest = max(parked, key=os.path.getmtime)
    assert os.path.isfile(os.path.join(latest, "wsi", "pre-restore-sentinel.svs"))


def test_validate_archive_integrity_full_scope_missing_wsi(tmp_path):
    """A full-scope archive whose DB references a wsi file the archive
    doesn't carry must fail integrity validation."""
    # Build a staged tree by hand under .staging/ to skip the upload + extract path.
    stage_id = "stage-" + "f" * 12
    stage_root = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(stage_root, exist_ok=True)
    stage_dir = os.path.join(stage_root, stage_id)
    os.makedirs(stage_dir, exist_ok=True)

    # Build a DB with one slide that references a file deliberately NOT placed in wsi/.
    db_path = os.path.join(stage_dir, "wsi.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE slides (id INTEGER PRIMARY KEY, wsi_filename TEXT)"
        )
        conn.execute(
            "CREATE TABLE tours (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO slides VALUES (1, 'absent.svs')")
        conn.commit()
    finally:
        conn.close()

    os.makedirs(os.path.join(stage_dir, "wsi"), exist_ok=True)
    # Note: absent.svs intentionally NOT written.

    manifest = _backup.Manifest(
        scope="full",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=1,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    report = _backup.validate_archive_integrity(stage_id, manifest)
    assert report.ok is False
    assert "absent.svs" in report.missing_wsi_files
    _backup.cleanup_stage(stage_id)


def test_validate_archive_integrity_metadata_scope_rejects_wsi_dir(tmp_path):
    """A metadata-scope archive that nonetheless ships a wsi/ directory is
    internally inconsistent — the integrity check must reject it."""
    stage_id = "stage-" + "e" * 12
    stage_root = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(stage_root, exist_ok=True)
    stage_dir = os.path.join(stage_root, stage_id)
    os.makedirs(stage_dir, exist_ok=True)

    db_path = os.path.join(stage_dir, "wsi.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE slides (id INTEGER PRIMARY KEY, wsi_filename TEXT)")
        conn.execute(
            "CREATE TABLE tours (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    os.makedirs(os.path.join(stage_dir, "wsi"), exist_ok=True)
    with open(os.path.join(stage_dir, "wsi", "should-not-be-here.svs"), "wb") as f:
        f.write(b"x")

    manifest = _backup.Manifest(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    report = _backup.validate_archive_integrity(stage_id, manifest)
    assert report.ok is False
    _backup.cleanup_stage(stage_id)


def test_validate_archive_integrity_rejects_traversal_wsi_filename(tmp_path):
    """A crafted whole-DB archive whose slide rows carry a path-traversal
    wsi_filename (contains "/" or a leading ".") must fail integrity validation
    so it can't smuggle an unsafe basename past the swap. Defense in depth."""
    stage_id = "stage-" + "d" * 12
    stage_root = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(stage_root, exist_ok=True)
    stage_dir = os.path.join(stage_root, stage_id)
    os.makedirs(stage_dir, exist_ok=True)

    db_path = os.path.join(stage_dir, "wsi.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE slides (id INTEGER PRIMARY KEY, wsi_filename TEXT)"
        )
        conn.execute(
            "CREATE TABLE tours (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO slides VALUES (1, '../../etc/passwd')")
        conn.execute("INSERT INTO slides VALUES (2, '.hidden.svs')")
        conn.commit()
    finally:
        conn.close()

    # metadata scope carries no wsi/ dir of its own; the traversal check runs
    # before the per-scope branches so it fires for either scope.
    manifest = _backup.Manifest(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=2,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    report = _backup.validate_archive_integrity(stage_id, manifest)
    assert report.ok is False
    assert any("unsafe wsi_filename" in e for e in report.errors), report.errors
    _backup.cleanup_stage(stage_id)


def test_restore_preflight_endpoint(client, admin_headers):
    # Make a backup to have something stageable.
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    r = client.get(f"/api/admin/restore/preflight/{stage_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage_id"] == stage_id
    assert body["scope"] in ("metadata", "full")
    assert body["integrity"]["ok"] is True
    assert "live_state" in body
    assert "archive_state" in body
    # metadata scope has no wsi/ to lay down; uploads_pending mirrors any
    # slides the archive's DB references.
    if body["scope"] == "metadata":
        assert body["archive_state"]["wsi_file_count"] == 0


def test_restore_preflight_rejects_a_manifest_missing_schema_version(client, admin_headers):
    """The preflight reads the staged manifest with the same strict parser.

    It is what the operator reads before confirming a destructive restore, so
    it must not report a shape the manifest does not actually state. Pinned
    alongside the apply-side check so the two readers cannot drift.
    """
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    mpath = _staged_manifest_path(stage_id)
    with open(mpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    del data["includes_analytics"]
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(data, f)

    r = client.get(f"/api/admin/restore/preflight/{stage_id}", headers=admin_headers)
    assert r.status_code == 400, r.text
    assert "includes_analytics" in str(r.json()["detail"])


def test_restore_preflight_rejects_malformed_stage_id(client, admin_headers):
    r = client.get("/api/admin/restore/preflight/../etc/passwd", headers=admin_headers)
    # FastAPI will 404 on the route mismatch ".." but the handler also blocks
    # well-formed-looking but wrong-prefix ids inside.
    assert r.status_code in (400, 404)
    r = client.get("/api/admin/restore/preflight/stage-not-hex-xx", headers=admin_headers)
    assert r.status_code == 400


def test_restore_preflight_404_for_missing_stage(client, admin_headers):
    r = client.get("/api/admin/restore/preflight/stage-aaaaaaaaaaaa", headers=admin_headers)
    assert r.status_code == 404


def test_reset_preflight_returns_live_state(client, admin_headers, slide):
    r = client.get("/api/admin/reset/preflight", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "live_state" in body
    assert body["live_state"]["slide_count"] >= 1  # the ``slide`` fixture added one
    assert "preserved" in body
    assert "users" in body["preserved"]["tables"]


def test_reset_requires_correct_password(client, admin_headers):
    r = client.post(
        "/api/admin/reset",
        json={"confirm_text": "RESET", "password": "wrong"},
        headers=admin_headers,
    )
    assert r.status_code == 401


def test_reauth_password_cap_matches_the_password_policy(client, admin_headers):
    # Restore and reset take the same passwords as login and the password
    # change (up to auth.MAX_PASSWORD_LEN). A tighter cap here would 422 an
    # admin with a long, valid password before the re-auth check ran.
    from wsi_app.auth import MAX_PASSWORD_LEN

    longest = "a" * MAX_PASSWORD_LEN
    r = client.post(
        "/api/admin/reset",
        json={"confirm_text": "RESET", "password": longest},
        headers=admin_headers,
    )
    assert r.status_code == 401
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": "stage-000000000000", "confirm_text": "RESTORE", "password": longest},
        headers=admin_headers,
    )
    assert r.status_code != 422, r.text
    r = client.post(
        "/api/admin/reset",
        json={"confirm_text": "RESET", "password": longest + "a"},
        headers=admin_headers,
    )
    assert r.status_code == 422


def test_reset_requires_confirm_text(client, admin_headers):
    r = client.post(
        "/api/admin/reset",
        json={"confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_reset_wipes_content_keeps_admin(client, admin_headers, db_session, admin_user):
    """A reset must:
      - delete all content rows (slides, tours, page views, etc.)
      - delete all on-disk wsi/ files
      - leave the admin user row intact so login still works
    """
    from wsi_app.models import Slide, Tour, User
    assert settings.backup_dev_inprocess_apply

    # Seed content.
    s = Slide(title="ResetMe", catalog_id="RM-1")
    db_session.add(s)
    db_session.commit()
    slide_id = s.id
    t = Tour(slide_id=slide_id, title="Doomed tour")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    tour_id = t.id

    # Seed a wsi/ file.
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    f_path = os.path.join(wsi_dir, "reset-doomed.svs")
    with open(f_path, "wb") as f:
        f.write(b"contents")
    assert os.path.isfile(f_path)

    admin_id = admin_user.id

    r = client.post(
        "/api/admin/reset",
        json={"confirm_text": "RESET", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["scope"] == "reset"
    assert started["stage_id"].startswith("stage-")
    rj = _await_job_record(started["job_id"])
    assert rj["state"] == "succeeded", rj
    assert rj["result"] == {"mode": "inprocess", "stage_id": started["stage_id"], "scope": "reset"}
    # The reset signed every session out, this admin's included.
    assert client.get("/api/admin/backup", headers=admin_headers).status_code == 401

    # Re-open a session against the swapped DB.
    from wsi_app.db import SessionLocal
    db2 = SessionLocal()
    try:
        # Content is gone.
        assert db2.get(Slide, slide_id) is None
        assert db2.get(Tour, tour_id) is None
        # Admin survives.
        assert db2.get(User, admin_id) is not None
    finally:
        db2.close()

    # wsi/ is wiped.
    assert not os.path.isfile(f_path)


def test_apply_refuses_archive_with_missing_wsi(client, admin_headers, db_session):
    """Surgery on a metadata-scope archive to flip its scope to ``full`` without
    adding any wsi/ files makes it internally inconsistent. The apply endpoint
    must refuse it with ARCHIVE_INCONSISTENT before any swap."""
    from wsi_app.models import Slide
    s = Slide(title="HasWsiFilename", catalog_id="X", wsi_filename="phantom.svs")
    db_session.add(s)
    db_session.commit()

    backup_id = _make_backup_via_api(client, admin_headers)

    # Stage from the snapshot, then mutate the stage's manifest to claim full
    # scope. The DB references phantom.svs but the archive carries no wsi/,
    # which is exactly the kind of internal mismatch the check exists to catch.
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    mf = os.path.join(stage_dir, "manifest.json")
    with open(mf, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["scope"] = "full"
    with open(mf, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail.get("code") == "ARCHIVE_INCONSISTENT"
    assert "phantom.svs" in detail["integrity"]["missing_wsi_files"]


def test_kick_restart_aborts_on_nonzero_exit(tmp_path):
    """A non-zero exit from the restart command must clear restore.request
    and demote the job so future POSTs aren't locked out by
    ``_restore_apply_pending()`` forever.

    Regression guard for the failure mode where subprocess.Popen succeeds
    (exec works) but the child (sudoers misconfig, masked unit, …) returns
    a non-zero code that the API would otherwise silently ignore.
    """
    from wsi_app.routers import admin_backup as _admin_backup
    from wsi_app import jobs as _jobs

    req_path = tmp_path / "restore.request"
    # settings is a frozen dataclass; object.__setattr__ bypasses the lock,
    # restored in finally so other tests see the original config.
    original_req = settings.restore_request_path
    original_cmd = settings.restore_restart_cmd
    object.__setattr__(settings, "restore_request_path", str(req_path))
    object.__setattr__(settings, "restore_restart_cmd", "/bin/false")
    try:
        job = _jobs.store.try_claim_running("restore_apply")
        assert job is not None
        # Written after the claim, as restore_apply does: a pending request
        # file makes try_claim_running refuse the slot.
        req_path.write_text("{}")
        _jobs.store.update(job.id, state="succeeded")

        _admin_backup._kick_restart(job.id)

        assert not req_path.exists(), "request file should be cleared on nonzero exit"
        j = _jobs.store.get(job.id)
        assert j is not None and j.state == "failed"
        assert "exited with code" in (j.error or "")
    finally:
        object.__setattr__(settings, "restore_request_path", original_req)
        object.__setattr__(settings, "restore_restart_cmd", original_cmd)


def test_kick_restart_writes_trigger_when_cmd_empty(tmp_path):
    """With the default (empty) restore_restart_cmd, _kick_restart drops the
    restart trigger file that the wsi-restart.path systemd unit watches, and
    leaves restore.request + the succeeded job slot intact (the swap is applied
    on the next boot, not here).
    """
    from wsi_app.routers import admin_backup as _admin_backup
    from wsi_app import jobs as _jobs

    req_path = tmp_path / "restore.request"
    trigger_path = tmp_path / "restart.trigger"
    original_req = settings.restore_request_path
    original_cmd = settings.restore_restart_cmd
    original_trigger = settings.restart_trigger_path
    object.__setattr__(settings, "restore_request_path", str(req_path))
    object.__setattr__(settings, "restore_restart_cmd", "")
    object.__setattr__(settings, "restart_trigger_path", str(trigger_path))
    try:
        job = _jobs.store.try_claim_running("restore_apply")
        assert job is not None
        # Written after the claim, as restore_apply does: a pending request
        # file makes try_claim_running refuse the slot.
        req_path.write_text("{}")
        _jobs.store.update(job.id, state="succeeded")

        _admin_backup._kick_restart(job.id)

        assert trigger_path.exists(), "trigger file should be written"
        assert req_path.exists(), "restore.request must remain until boot consumes it"
        j = _jobs.store.get(job.id)
        assert j is not None and j.state == "succeeded"
    finally:
        object.__setattr__(settings, "restore_request_path", original_req)
        object.__setattr__(settings, "restore_restart_cmd", original_cmd)
        object.__setattr__(settings, "restart_trigger_path", original_trigger)


def test_kick_restart_aborts_when_trigger_unwritable(tmp_path):
    """If the trigger file can't be written, _kick_restart must clear
    restore.request and demote the job so future POSTs aren't locked out by
    ``_restore_apply_pending()`` forever.
    """
    from wsi_app.routers import admin_backup as _admin_backup
    from wsi_app import jobs as _jobs

    req_path = tmp_path / "restore.request"
    # Parent of the trigger path is a regular file, so makedirs/open both fail.
    bad_parent = tmp_path / "not-a-dir"
    bad_parent.write_text("x")
    trigger_path = bad_parent / "restart.trigger"
    original_req = settings.restore_request_path
    original_cmd = settings.restore_restart_cmd
    original_trigger = settings.restart_trigger_path
    object.__setattr__(settings, "restore_request_path", str(req_path))
    object.__setattr__(settings, "restore_restart_cmd", "")
    object.__setattr__(settings, "restart_trigger_path", str(trigger_path))
    try:
        job = _jobs.store.try_claim_running("restore_apply")
        assert job is not None
        # Written after the claim, as restore_apply does: a pending request
        # file makes try_claim_running refuse the slot.
        req_path.write_text("{}")
        _jobs.store.update(job.id, state="succeeded")

        _admin_backup._kick_restart(job.id)

        assert not req_path.exists(), "request file should be cleared on trigger failure"
        j = _jobs.store.get(job.id)
        assert j is not None and j.state == "failed"
        assert "restart trigger" in (j.error or "")
    finally:
        object.__setattr__(settings, "restore_request_path", original_req)
        object.__setattr__(settings, "restore_restart_cmd", original_cmd)
        object.__setattr__(settings, "restart_trigger_path", original_trigger)


@contextlib.contextmanager
def _ipc_paths(tmp_path):
    """Point the restore request / result files at tmp_path for one test."""
    names = ("restore_request_path", "restore_result_path")
    saved = {n: getattr(settings, n) for n in names}
    object.__setattr__(settings, "restore_request_path", str(tmp_path / "restore.request"))
    object.__setattr__(settings, "restore_result_path", str(tmp_path / "restore.result"))
    try:
        yield tmp_path / "restore.request", tmp_path / "restore.result"
    finally:
        for n, v in saved.items():
            object.__setattr__(settings, n, v)


def _write_aged_request(path, age_seconds: float, stage_id: str) -> None:
    import datetime as _dt

    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=age_seconds)
    path.write_text(json.dumps({
        "stage_id": stage_id, "scope": "full",
        "requested_at": when.replace(tzinfo=None).isoformat() + "Z",
        "requested_by": "someone",
    }))


def test_a_request_no_restart_picked_up_stops_blocking_and_is_reported(
    client, admin_headers, tmp_path,
):
    """A request still waiting past REQUEST_MAX_AGE_SECONDS was never acted
    on, and the boot refuses it: it must stop holding backups / restores /
    imports and the stage cancel at 409, and the Backup & Restore tab must
    say what became of it. Within the limit it keeps blocking."""
    from wsi_app import restore_apply

    limit = restore_apply.REQUEST_MAX_AGE_SECONDS
    stage_id = "stage-" + "c" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    try:
        with _ipc_paths(tmp_path) as (req, res):
            _write_aged_request(req, limit - 60, stage_id)
            r = client.post("/api/admin/backup", json={"include_wsi": False}, headers=admin_headers)
            assert r.status_code == 409 and "pending service restart" in r.json()["detail"], r.text
            r = client.delete(f"/api/admin/restore/stage/{stage_id}", headers=admin_headers)
            assert r.status_code == 409, r.text
            assert client.get("/api/admin/restore/result", headers=admin_headers).json()["result"] is None
            assert req.exists()

            _write_aged_request(req, limit + 60, stage_id)
            # Reading the result (the Backup & Restore tab does on opening) reports it
            # and drops it.
            got = client.get("/api/admin/restore/result", headers=admin_headers).json()["result"]
            assert got["ok"] is False and got["stage"] == "expired", got
            assert got["stage_id"] == stage_id and "was not applied" in got["error"]
            assert not req.exists()

            # A job claim drops it too, and goes ahead.
            res.unlink()
            _write_aged_request(req, limit + 60, stage_id)
            r = client.post("/api/admin/backup", json={"include_wsi": False}, headers=admin_headers)
            assert r.status_code == 200, r.text
            rj = _await_job(client, admin_headers, r.json()["job_id"])
            _backup.delete_backup(rj["result"]["backup_id"])
            assert not req.exists()
            assert json.loads(res.read_text())["stage"] == "expired"

            # An expired request no longer pins its stage either.
            _write_aged_request(req, limit + 60, stage_id)
            r = client.delete(f"/api/admin/restore/stage/{stage_id}", headers=admin_headers)
            assert r.status_code == 200 and r.json()["removed"] is True, r.text
    finally:
        _backup.cleanup_stage(stage_id)


def test_a_request_carries_the_boot_clock_and_the_api_ages_it_by_that(tmp_path):
    """request_apply stamps the request with this boot's id and clock, so
    the API and the boot age it alike, by a clock a wall-clock step does not
    move: one whose requested_at the clock has gone back past still counts
    as pending, not expired."""
    from wsi_app import restore_apply

    stamp = restore_apply.request_clock_stamp()
    if not stamp:
        pytest.skip("no boot id / CLOCK_BOOTTIME on this platform")
    stage_id = "stage-" + "f" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    _backup._mark_stage_complete(stage_dir, stage_id)
    try:
        with _ipc_paths(tmp_path) as (req, _res):
            _backup.request_apply(stage_id, "metadata", "someone")
            body = json.loads(req.read_text())
            assert body["requested_boot_id"] == stamp["requested_boot_id"]
            assert (stamp["requested_boot_seconds"] <= body["requested_boot_seconds"]
                    <= restore_apply.request_clock_stamp()["requested_boot_seconds"])
            # The clock goes back 20 minutes past the stamp.
            _write_aged_request(req, -(restore_apply.REQUEST_MAX_AGE_SECONDS + 600), stage_id)
            stepped = json.loads(req.read_text())
            stepped.update(stamp)
            req.write_text(json.dumps(stepped))
            assert _backup.read_apply_request()[1] is False
    finally:
        _backup.cleanup_stage(stage_id)


def test_an_expired_request_is_not_dropped_under_a_running_apply(tmp_path):
    """Requests are written by restore_apply jobs; with one in flight the
    file may be about to become its fresh request, so it is left alone."""
    from wsi_app import jobs as _jobs
    from wsi_app import restore_apply
    from wsi_app.routers import admin_backup as _admin_backup

    with _ipc_paths(tmp_path) as (req, res):
        job = _jobs.store.try_claim_running("restore_apply")
        assert job is not None
        _jobs.store.update(job.id, state="running")
        try:
            _write_aged_request(req, restore_apply.REQUEST_MAX_AGE_SECONDS + 60, "stage-" + "d" * 12)
            _admin_backup._expire_stale_apply_request()
            assert req.exists() and not res.exists()
        finally:
            _jobs.store.update(job.id, state="succeeded")
            _jobs.store.prune_terminal(max_age_seconds=0)
        _admin_backup._expire_stale_apply_request()
        assert not req.exists() and res.exists()


def test_restore_stage_cancel_refused_during_apply(client, admin_headers):
    """A cancel landing during restore_apply's preflight window (the job
    slot is claimed but the request file hasn't been written yet) must be
    refused with 409 so it can't rmtree the stage out from under the running
    apply. Mirrors cancel_slide_import's has_running_kind guard."""
    from wsi_app import jobs as _jobs

    stage_id = "stage-" + "a" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)

    job = _jobs.store.try_claim_running("restore_apply")
    assert job is not None
    _jobs.store.update(job.id, state="running")
    try:
        r = client.delete(
            f"/api/admin/restore/stage/{stage_id}", headers=admin_headers,
        )
        assert r.status_code == 409, r.text
        assert os.path.isdir(stage_dir), "stage must survive a refused cancel"
    finally:
        # Free the single job slot so later tests can claim it.
        _jobs.store.update(job.id, state="succeeded")
        _jobs.store.prune_terminal(max_age_seconds=0)
        _backup.cleanup_stage(stage_id)


def test_restore_stage_cancel_allowed_when_idle(client, admin_headers):
    """With no restore-apply in flight the cancel removes the stage (200);
    this guard must not break the normal cancel path."""
    stage_id = "stage-" + "b" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    r = client.delete(
        f"/api/admin/restore/stage/{stage_id}", headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert not os.path.isdir(stage_dir)


def test_create_backup_preflight_refuses_full_disk(monkeypatch, tmp_path, db_session):
    """create_backup should fail fast when the backup volume cannot
    plausibly fit the snapshot, instead of writing partial archives until
    ENOSPC fires deep inside the tar/zstd stack.
    """
    import shutil as _shutil

    class _Usage:
        total = 100
        used = 99
        free = 1

    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: _Usage)

    with pytest.raises(RuntimeError, match="insufficient free space on backup volume"):
        _backup.create_backup(scope="metadata", note="")


# ---------------------------------------------------------------------------
# Thumbnail handling: full archives bundle thumbs/, metadata never does,
# restore swaps/wipes thumbs/ in lockstep with wsi/, and the rebuild
# endpoint regenerates thumbnails from the on-disk WSI files.
# ---------------------------------------------------------------------------

def _reset_slides(db_session):
    """Wipe all slide rows so a full-scope backup's integrity self-check
    (every wsi_filename must resolve to a file on disk) only sees the rows
    this test deliberately seeded. db_session commits to a shared SQLite DB,
    so rows leak between tests otherwise."""
    from sqlalchemy import delete
    from wsi_app.models import Slide, SlideAttribute, Tour
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()


def _seed_slide_with_files(db_session, *, catalog_id):
    """Create a slide whose WSI file and thumbnail both exist on disk."""
    from wsi_app.models import Slide
    s = Slide(title="Full scope", catalog_id=catalog_id)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(wsi_dir, exist_ok=True)
    os.makedirs(thumbs_dir, exist_ok=True)
    wsi_name = f"{s.id}_full.svs"
    thumb_name = f"{s.id}.jpg"
    with open(os.path.join(wsi_dir, wsi_name), "wb") as f:
        f.write(b"wsi-bytes")
    with open(os.path.join(thumbs_dir, thumb_name), "wb") as f:
        f.write(b"thumb-bytes")
    s.wsi_filename = wsi_name
    s.thumb_filename = thumb_name
    db_session.commit()
    db_session.refresh(s)
    return s, wsi_name, thumb_name


def _run_backup(client, admin_headers, scope):
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": scope == "full", "note": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    return rj["result"]["backup_id"]


def test_full_backup_includes_thumbs(client, admin_headers, db_session):
    _reset_slides(db_session)
    _s, wsi_name, thumb_name = _seed_slide_with_files(db_session, catalog_id="FULLTHUMB-1")
    backup_id = _run_backup(client, admin_headers, "full")
    rec = _backup.find_backup(backup_id)
    with _backup._open_archive_reader(rec.path) as tar:
        names = set(tar.getnames())
    assert f"wsi/{wsi_name}" in names
    assert f"thumbs/{thumb_name}" in names


def test_metadata_backup_excludes_thumbs(client, admin_headers, db_session):
    _reset_slides(db_session)
    _seed_slide_with_files(db_session, catalog_id="METATHUMB-1")
    backup_id = _run_backup(client, admin_headers, "metadata")
    rec = _backup.find_backup(backup_id)
    with _backup._open_archive_reader(rec.path) as tar:
        names = set(tar.getnames())
    assert not any(n == "thumbs" or n.startswith("thumbs/") for n in names)
    assert not any(n.startswith("wsi/") for n in names)


def test_full_restore_promotes_thumbs(client, admin_headers, db_session):
    """A full restore lays the archive's thumbs/ back down alongside wsi/."""
    assert settings.backup_dev_inprocess_apply
    _reset_slides(db_session)
    _s, _wsi_name, thumb_name = _seed_slide_with_files(db_session, catalog_id="FULLREST-1")
    backup_id = _run_backup(client, admin_headers, "full")

    # Lose the live thumbnail, then restore and confirm it comes back.
    thumb_path = os.path.join(settings.data_dir, "thumbs", thumb_name)
    os.remove(thumb_path)
    assert not os.path.isfile(thumb_path)

    body = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})
    assert body["manifest"]["scope"] == "full"
    stage_id = body["stage_id"]
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert os.path.isfile(thumb_path), "full restore should have promoted thumbs/"


def test_metadata_restore_wipes_thumbs_directory(client, admin_headers, db_session):
    """A metadata restore wipes thumbs/ along with wsi/ so no thumbnail is
    left without its backing WSI file."""
    assert settings.backup_dev_inprocess_apply
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    sentinel = os.path.join(thumbs_dir, "pre-restore-thumb-sentinel.jpg")
    with open(sentinel, "wb") as f:
        f.write(b"stale thumbnail")
    assert os.path.isfile(sentinel)

    backup_id = _make_backup_via_api(client, admin_headers)  # metadata scope
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert not os.path.isfile(sentinel), (
        "metadata restore should have moved thumbs/ aside; sentinel still present"
    )


def test_rebuild_thumbnails_regenerates_missing_and_skips_absent_wsi(
    client, admin_headers, db_session, attach_wsi, monkeypatch
):
    """The rebuild endpoint regenerates thumbnails for slides whose WSI file
    is present, and skips slides whose WSI body is missing — never creating a
    thumbnail without its backing image."""
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides as _admin_slides

    _reset_slides(db_session)

    # Slide A: WSI present, thumbnail missing -> rebuilt.
    a = Slide(title="A", catalog_id="REB-A")
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    attach_wsi(a)
    # A has no thumbnail yet; rebuild mints a fresh capability filename
    # (slide id + random token), so the regenerated name isn't predictable.

    # Slide B: wsi_filename set but no file on disk -> skipped.
    b = Slide(title="B", catalog_id="REB-B", wsi_filename="ghost.svs")
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)

    # Stub make_thumbnail so the test doesn't need an OpenSlide-readable file.
    def _fake_make_thumbnail(src, out, max_size=(512, 512)):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(b"jpeg-bytes")

    monkeypatch.setattr(_admin_slides, "make_thumbnail", _fake_make_thumbnail)

    r = client.post(
        "/api/admin/slides/rebuild-thumbnails", data={}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert a.id in body["rebuilt"]
    assert b.id in body["skipped_missing_wsi"]
    db_session.expire_all()
    a_name = db_session.get(Slide, a.id).thumb_filename
    assert a_name.startswith(f"{a.id}-") and a_name.endswith(".jpg")
    assert os.path.isfile(os.path.join(settings.data_dir, "thumbs", a_name))


def test_rebuild_thumbnails_failure_reasons_carry_no_server_path(
    client, admin_headers, db_session, attach_wsi, monkeypatch
):
    """The endpoint is open to managers, who are told byte counts and never
    paths: a failed slide reports the exception type and the system's reason,
    not the OSError text that names the file (full detail stays in the log)."""
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides as _admin_slides

    _reset_slides(db_session)
    a = Slide(title="A", catalog_id="REB-F")
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    attach_wsi(a)

    secret_path = os.path.join(settings.data_dir, "thumbs", ".x.jpg.tmp-abc123")

    def _boom(src, out, max_size=(512, 512)):
        raise PermissionError(13, "Permission denied", secret_path)

    monkeypatch.setattr(_admin_slides, "make_thumbnail", _boom)
    r = client.post("/api/admin/slides/rebuild-thumbnails", data={}, headers=admin_headers)
    assert r.status_code == 200, r.text
    failed = r.json()["failed"]
    assert [f["slide_id"] for f in failed] == [a.id]
    assert failed[0]["error"] == "PermissionError: Permission denied"
    assert settings.data_dir not in r.text and ".x.jpg" not in r.text


def test_sweep_reclaims_stale_tmp_archive_but_keeps_fresh():
    """A leaked partial archive (<id>.tar.*.tmp) older than the TTL is swept;
    an in-progress backup's .tmp (fresh mtime) is left alone.
    """
    import time

    os.makedirs(settings.backup_dir, exist_ok=True)
    stale = os.path.join(settings.backup_dir, "20200101T000000Z-deadbeef.tar.zst.tmp")
    fresh = os.path.join(settings.backup_dir, "20990101T000000Z-cafef00d.tar.zst.tmp")
    with open(stale, "wb") as fh:
        fh.write(b"partial")
    with open(fresh, "wb") as fh:
        fh.write(b"in-progress")
    old = time.time() - (25 * 3600)  # past the 24h TTL
    os.utime(stale, (old, old))
    try:
        _backup.sweep_stale_staging()
        assert not os.path.exists(stale), "stale .tmp should be reclaimed"
        assert os.path.exists(fresh), "fresh (in-progress) .tmp must survive"
    finally:
        for p in (stale, fresh):
            try:
                os.remove(p)
            except OSError:
                pass


def test_create_backup_cleans_tmp_on_finalize_failure(monkeypatch, db_session):
    """A failure during finalization (here: os.replace) must not leave the
    partial .tmp archive behind."""
    def _boom(src, dst):
        raise OSError("simulated disk failure during publish")

    monkeypatch.setattr(_backup.os, "replace", _boom)
    with pytest.raises(OSError):
        _backup.create_backup(scope="metadata")

    leftovers = [n for n in os.listdir(settings.backup_dir) if n.endswith(".tmp")]
    assert leftovers == [], f"partial .tmp leaked after finalize failure: {leftovers}"


def test_list_backups_tolerates_non_dict_sidecar(client, admin_headers):
    """A sidecar that is valid JSON but the wrong shape (a list) must not break
    the whole listing — it should degrade to a stub row."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    assert rec is not None
    # Corrupt the sidecar into a JSON array.
    with open(rec.path + ".meta.json", "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")

    # Neither call raises; both surface a usable (stub) record.
    items = _backup.list_backups()
    assert any(it.id == backup_id for it in items)
    again = _backup.find_backup(backup_id)
    assert again is not None and again.id == backup_id


def test_restore_empty_snapshot_id_returns_400(client, admin_headers):
    # An empty snapshot_id with no uploaded file is a bad request, not a 500.
    r = client.post(
        "/api/admin/restore",
        data={"snapshot_id": ""},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    assert "exactly one" in r.json()["detail"]


def test_reset_preserves_role_permissions():
    # A reset must keep operator-configured RBAC grants.
    assert "role_permissions" in _backup._RESET_PRESERVE_TABLES


def test_reset_preserves_dashboard_config_tables():
    # The reset UI promises "dashboard settings are preserved". These tables
    # are operator-configured dashboard settings (not content), so they must
    # survive a reset alongside viewer_settings. seasonal_backgrounds in
    # particular must not be wiped while the always-on header background in
    # viewer_settings survives — an asymmetry this guards against.
    for table in ("seasonal_backgrounds", "metadata_options"):
        assert table in _backup._RESET_PRESERVE_TABLES


@contextlib.contextmanager
def _temp_data_dir(tmp_path):
    # settings is a frozen dataclass; object.__setattr__ bypasses the lock and
    # the original is restored on exit so other tests see the real config.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original = settings.data_dir
    object.__setattr__(settings, "data_dir", str(data_dir))
    try:
        yield data_dir
    finally:
        object.__setattr__(settings, "data_dir", original)


def test_list_and_delete_rollback_dirs(tmp_path):
    # Two well-formed rollback dirs (production + dev-suffixed shapes) plus a
    # decoy that must be ignored by the lister.
    with _temp_data_dir(tmp_path) as data_dir:
        prod = data_dir / ".pre-restore-20260613T070102Z"
        dev = data_dir / ".pre-restore-20260612T010203Z-deadbeef"
        decoy = data_dir / "wsi"  # not a rollback dir
        # What a rolled-back apply could leave behind: listing it would offer
        # an empty "rollback copy" as the newest one.
        empty = data_dir / ".pre-restore-20260614T000000Z"
        for d in (prod, dev, decoy, empty):
            d.mkdir()
        (prod / "wsi.db").write_bytes(b"x" * 100)
        (dev / "assets").mkdir()

        items = _backup.list_rollback_dirs()
        names = [r.name for r in items]
        assert names == [prod.name, dev.name]  # newest first, decoy + empty excluded
        assert items[0].created_at == "2026-06-13T07:01:02Z"
        assert items[0].size_bytes == 100

        assert _backup.delete_rollback_dir(prod.name) is True
        assert not prod.exists()
        # Idempotent second delete.
        assert _backup.delete_rollback_dir(prod.name) is False
        # Decoy survived.
        assert decoy.exists()


def test_rollback_delete_leaves_the_listing_at_once_and_a_killed_one_is_finished(tmp_path):
    with _temp_data_dir(tmp_path) as data_dir:
        tree = data_dir / ".pre-restore-20260613T070102Z"
        (tree / "wsi").mkdir(parents=True)
        (tree / "wsi" / "1_a.svs").write_bytes(b"x" * 100)
        assert _backup.delete_rollback_dir(tree.name, background=True) is True
        # Out of the listing straight away, whatever the thread has done yet.
        assert not tree.exists()
        assert _backup.list_rollback_dirs() == []
        for _ in range(100):
            if not [p for p in data_dir.iterdir() if p.name.startswith(".rollback-trash-")]:
                break
            time.sleep(0.02)
        assert not [p for p in data_dir.iterdir() if p.name.startswith(".rollback-trash-")]

        # A delete killed mid-removal: the boot sweep finishes it, and only it.
        left = data_dir / ".rollback-trash-0123456789ab"
        (left / "wsi").mkdir(parents=True)
        other = data_dir / ".rollback-trash-notours"
        other.mkdir()
        assert _backup.sweep_rollback_trash() == 1
        assert not left.exists() and other.exists()


def test_delete_rollback_dir_rejects_traversal(tmp_path):
    with _temp_data_dir(tmp_path) as data_dir:
        outside = data_dir.parent / "outside"
        outside.mkdir()
        for bad in ("../outside", ".pre-restore-../../etc", "wsi", ".pre-restore-bogus"):
            with pytest.raises(ValueError):
                _backup.delete_rollback_dir(bad)
        assert outside.exists()


def test_rollback_endpoints(client, admin_headers, tmp_path):
    with _temp_data_dir(tmp_path) as data_dir:
        rb = data_dir / ".pre-restore-20260101T000000Z"
        rb.mkdir()
        (rb / "wsi.db").write_bytes(b"abc")

        r = client.get("/api/admin/restore/rollbacks", headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [i["name"] for i in body["items"]] == [rb.name]
        assert body["items"][0]["size_bytes"] == 3

        r = client.delete(
            f"/api/admin/restore/rollbacks/{rb.name}", headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "removed": True}
        assert not rb.exists()

        # Malformed name -> 400.
        r = client.delete(
            "/api/admin/restore/rollbacks/not-a-rollback", headers=admin_headers,
        )
        assert r.status_code == 400


def test_wipe_db_in_place_keeps_dashboard_config(tmp_path):
    # End-to-end check on the actual wipe routine: seeded rows in the
    # dashboard-config tables survive, content rows do not.
    db = str(tmp_path / "wsi.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE seasonal_backgrounds (id INTEGER PRIMARY KEY, label TEXT)")
    conn.execute("CREATE TABLE metadata_options (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
    conn.execute("CREATE TABLE viewer_settings (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE slides (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO seasonal_backgrounds (label) VALUES ('winter')")
    conn.execute("INSERT INTO metadata_options (key, value) VALUES ('stain', 'HE')")
    conn.execute("INSERT INTO viewer_settings (v) VALUES ('x')")
    conn.execute("INSERT INTO slides DEFAULT VALUES")
    conn.commit()
    conn.close()

    _backup._wipe_db_in_place(db, preserve=_backup._RESET_PRESERVE_TABLES)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM seasonal_backgrounds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM metadata_options").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM viewer_settings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM slides").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Selectable ancillary components: access statistics (page_views, ...) and
# activity history (audit_logs) can be excluded from a backup. Excluding a
# group empties its tables in the snapshot but keeps the schema, so a restore
# repopulates them as empty.
# ---------------------------------------------------------------------------

def _archived_table_count(rec_path: str, table: str) -> int:
    """Pull wsi.db out of an archive and return COUNT(*) for ``table``.

    The reader streams (zstd/gzip), so members are read sequentially rather
    than seeked to — wsi.db is the second member (after manifest.json).
    """
    import tempfile as _tempfile
    db_bytes = None
    with _backup._open_archive_reader(rec_path) as tar:
        for member in tar:
            if member.name == "wsi.db":
                db_bytes = tar.extractfile(member).read()
                break
    assert db_bytes is not None, "archive has no wsi.db"
    with _tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "wsi.db")
        with open(db_path, "wb") as f:
            f.write(db_bytes)
        conn = sqlite3.connect(db_path)
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        finally:
            conn.close()


def _seed_ancillary_rows(db_session):
    """Insert access-statistics rows (page view + error log) and an audit row."""
    from datetime import datetime, timezone
    from wsi_app.models import PageView, AuditLog, ErrorLog
    db_session.add(ErrorLog(
        occurred_at=datetime.now(timezone.utc),
        level="WARNING",
        logger="wsi_app.sync_room",
        message="sync join from 203.0.113.7",
    ))
    db_session.add(PageView(
        occurred_at=datetime.now(timezone.utc),
        kind="top",
        client_ip="203.0.113.7",
        user_agent="pytest",
        ua_class="other",
        referrer_host="",
        user_email="",
    ))
    db_session.add(AuditLog(
        occurred_at=datetime.now(timezone.utc),
        actor_username="tester",
        action="test.event",
        target="x",
        details="{}",
    ))
    db_session.commit()


def test_backup_excludes_analytics_keeps_history(client, admin_headers, db_session):
    _seed_ancillary_rows(db_session)
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "include_analytics": False, "note": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    backup_id = rj["result"]["backup_id"]

    rec = _backup.find_backup(backup_id)
    assert rec is not None
    # Analytics stripped, history retained. error_logs goes with the access
    # statistics: its records carry visitor IPs (sync joins, analytics beacon).
    assert _archived_table_count(rec.path, "page_views") == 0
    assert _archived_table_count(rec.path, "error_logs") == 0
    assert _archived_table_count(rec.path, "audit_logs") >= 1
    # Listing reflects the exclusion.
    listing = client.get("/api/admin/backup", headers=admin_headers).json()
    ours = [it for it in listing["items"] if it["id"] == backup_id][0]
    assert ours["includes_analytics"] is False
    assert ours["includes_history"] is True


def test_backup_excludes_history_keeps_analytics(client, admin_headers, db_session):
    _seed_ancillary_rows(db_session)
    r = client.post(
        "/api/admin/backup",
        json={"include_wsi": False, "include_history": False, "note": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    backup_id = rj["result"]["backup_id"]

    rec = _backup.find_backup(backup_id)
    assert rec is not None
    assert _archived_table_count(rec.path, "audit_logs") == 0
    assert _archived_table_count(rec.path, "page_views") >= 1
    assert _archived_table_count(rec.path, "error_logs") >= 1


def test_backup_default_includes_all_ancillary(client, admin_headers, db_session):
    # A default request (no include_* flags) is the full-scope backup, whose
    # integrity self-check requires every slide's wsi_filename to resolve on
    # disk — clear leftover rows other tests committed to the shared DB first.
    _reset_slides(db_session)
    _seed_ancillary_rows(db_session)
    # The default request keeps every ancillary group.
    r = client.post("/api/admin/backup", json={"note": ""}, headers=admin_headers)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    backup_id = rj["result"]["backup_id"]
    rec = _backup.find_backup(backup_id)
    assert _archived_table_count(rec.path, "page_views") >= 1
    assert _archived_table_count(rec.path, "error_logs") >= 1
    assert _archived_table_count(rec.path, "audit_logs") >= 1
    listing = client.get("/api/admin/backup", headers=admin_headers).json()
    ours = [it for it in listing["items"] if it["id"] == backup_id][0]
    assert ours["scope"] == "full"  # include_wsi defaults True
    assert ours["includes_analytics"] is True
    assert ours["includes_history"] is True


def test_backup_estimate_endpoint(client, admin_headers, db_session):
    _seed_ancillary_rows(db_session)
    r = client.get("/api/admin/backup/estimate", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("db_core", "analytics", "history", "wsi", "thumbs"):
        assert key in body
        assert isinstance(body[key], int)
        assert body[key] >= 0
    # Rows were seeded into both ancillary groups, so their estimates are > 0.
    assert body["analytics"] > 0
    assert body["history"] > 0


def test_backup_estimate_requires_admin(client):
    r = client.get("/api/admin/backup/estimate")
    assert r.status_code == 401

@pytest.mark.parametrize(
    "field, value",
    [
        ("slide_count", "abc"), ("slide_count", None), ("slide_count", [1]),
        ("tour_count", "many"), ("tour_count", {}),
        ("schema_version", [1]), ("schema_version", None),
    ],
)
def test_manifest_parse_failures_all_raise_runtime_error(field, value):
    """Every parse failure must be a RuntimeError, not just the anticipated ones.

    The routers catch RuntimeError and map it to a 400; anything else escapes
    as a 500 plus an error_logs row. Converting only schema_version would
    leave a manifest whose slide_count says "abc" to hit a bare int() and
    leak a ValueError — an opaque server error for a corrupt file the parser
    could have named.
    """
    data = {
        "scope": "metadata",
        "app_version": APP_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "slide_count": 0,
        "tour_count": 0,
        "includes_analytics": True,
        "includes_history": True,
        "schema_version": _migrations.CURRENT_SCHEMA_VERSION,
        field: value,
    }
    with pytest.raises(RuntimeError, match=field):
        _backup.Manifest.from_dict(data)


def test_manifest_coerces_descriptive_fields_to_str():
    """A JSON list where a string belongs is corrupt, but must not surprise
    downstream code typed for ``str`` — keep it in the parse."""
    m = _backup.Manifest.from_dict({
        "scope": ["metadata"],
        "app_version": APP_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "slide_count": 0,
        "tour_count": 0,
        "note": {"a": 1},
        "includes_analytics": True,
        "includes_history": True,
        "schema_version": _migrations.CURRENT_SCHEMA_VERSION,
    })
    assert isinstance(m.scope, str) and isinstance(m.note, str)


def test_manifest_round_trips_through_its_own_writer():
    """Whatever create_backup writes, stage_restore must be able to read.

    from_dict rejects a manifest missing schema_version or either includes_*
    flag. Those fields are emitted by to_dict, so writer and reader are only
    in step for as long as the two lists agree — and an archive an operator
    already holds cannot be re-written to match a reader that drifted. A
    tightened reader must therefore always be checked against the writer,
    which is what this does.
    """
    original = _backup.Manifest(
        scope="full",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=3,
        tour_count=2,
        note="round trip",
        archive_format="tar.zst",
        includes_analytics=False,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    assert _backup.Manifest.from_dict(original.to_dict()) == original


def test_manifest_requires_the_shape_declaring_fields():
    """includes_* and schema_version carry no defaults.

    Nothing writes a manifest without them, and a defaulted schema_version
    would sail straight past plan_restore's downgrade guard, so an omission
    is a construction error.
    """
    base = dict(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=0,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    assert _backup.Manifest(**base).schema_version == _migrations.CURRENT_SCHEMA_VERSION
    for missing in ("includes_analytics", "includes_history", "schema_version"):
        partial = {k: v for k, v in base.items() if k != missing}
        with pytest.raises(TypeError):
            _backup.Manifest(**partial)


class _ShortWriter:
    """Writer that accepts at most ``chunk`` bytes per call (a short write)."""

    def __init__(self, chunk: int):
        self.chunk = chunk
        self.buf = bytearray()

    def write(self, b) -> int:
        mv = memoryview(b)
        n = min(self.chunk, len(mv))
        self.buf += mv[:n]
        return n


def test_counting_writer_handles_short_writes():
    payload = bytes(range(256)) * 40  # 10 KiB, non-trivial
    inner = _ShortWriter(chunk=7)  # force many short writes
    cw = _backup._CountingWriter(inner)
    returned = cw.write(payload)
    # The whole buffer must reach the inner writer despite short writes...
    assert bytes(inner.buf) == payload
    assert returned == len(payload)
    # ...and the recorded hash must match exactly those on-disk bytes.
    assert cw._hash.hexdigest() == hashlib.sha256(payload).hexdigest()
    assert cw._written == len(payload)


# ---------------------------------------------------------------------------
# schema_version threading through the restore endpoints (regression tests)
# ---------------------------------------------------------------------------

def _staged_manifest_path(stage_id: str) -> str:
    return os.path.join(settings.backup_dir, ".staging", stage_id, "manifest.json")


def test_restore_preflight_reports_schema_version(client, admin_headers):
    """The preflight response's manifest must carry the archive's real
    schema_version, not the dataclass default 0 (the router rebuilds Manifest
    from manifest.json and must thread schema_version through)."""
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    r = client.get(f"/api/admin/restore/preflight/{stage_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["schema_version"] == _migrations.CURRENT_SCHEMA_VERSION


def test_apply_blocks_schema_downgrade(client, admin_headers):
    """If a staged manifest reports a schema_version newer than this binary can
    produce, the apply endpoint must refuse it (defense-in-depth re-check),
    not silently proceed because the field was dropped to 0 at reconstruction."""
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    # Tamper the staged manifest to claim a newer schema than this build supports.
    mpath = _staged_manifest_path(stage_id)
    with open(mpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["schema_version"] = _migrations.CURRENT_SCHEMA_VERSION + 1
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(data, f)

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "DOWNGRADE_NOT_SUPPORTED"


def test_apply_rejects_a_staged_manifest_missing_schema_version(client, admin_headers):
    """Dropping the field must fail the apply, not read as schema_version 0.

    The apply endpoint re-parses the staged manifest as a defence-in-depth
    re-guard. Were that re-parse to use tolerant ``.get(..., 0)`` reads, a
    manifest that lost ``schema_version`` between staging and apply would come
    back as 0 — below anything this binary produces — so plan_restore's
    downgrade check would see an "older archive" and wave it through. All
    three restore-side readers share ``Manifest.from_dict``.
    """
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    mpath = _staged_manifest_path(stage_id)
    with open(mpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    del data["schema_version"]
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(data, f)

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    assert "schema_version" in str(r.json()["detail"])


def test_manifest_from_dict_is_the_one_strict_reader():
    """Required fields are required; descriptive ones may be absent.

    ``scope``/``note``/``created_at`` missing cannot cause a wrong decision
    (scope is validated separately against VALID_SCOPES), but the three fields
    plan_restore actually weighs must be stated.
    """
    good = {
        "scope": "metadata",
        "app_version": APP_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "slide_count": 0,
        "tour_count": 0,
        "includes_analytics": True,
        "includes_history": False,
        "schema_version": _migrations.CURRENT_SCHEMA_VERSION,
    }
    m = _backup.Manifest.from_dict(good)
    assert m.schema_version == _migrations.CURRENT_SCHEMA_VERSION
    assert m.includes_history is False

    for required in ("schema_version", "includes_analytics", "includes_history"):
        with pytest.raises(RuntimeError, match=required):
            _backup.Manifest.from_dict({k: v for k, v in good.items() if k != required})

    with pytest.raises(RuntimeError, match="non-numeric schema_version"):
        _backup.Manifest.from_dict({**good, "schema_version": "abc"})

    # Descriptive fields may be absent without failing the parse.
    sparse = {k: v for k, v in good.items() if k not in ("note", "scope", "created_at")}
    assert _backup.Manifest.from_dict(sparse).scope == "?"


def test_schema_version_of_is_tolerant():
    assert _backup._schema_version_of({"schema_version": "5"}) == 5
    assert _backup._schema_version_of({"schema_version": 7}) == 7
    assert _backup._schema_version_of({"schema_version": "abc"}) == 0  # garbage -> 0
    assert _backup._schema_version_of({"schema_version": ""}) == 0
    assert _backup._schema_version_of({"schema_version": None}) == 0
    assert _backup._schema_version_of({}) == 0


def test_list_backups_tolerates_malformed_sidecar_schema_version(client, admin_headers):
    """A valid-JSON sidecar whose schema_version is non-numeric must not 500 the
    whole listing/lookup — it reads as 0."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    assert rec is not None
    sidecar = rec.path + ".meta.json"
    with open(sidecar, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["schema_version"] = "abc"  # valid JSON object, garbage value
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Neither call raises; the malformed row reads as schema_version 0.
    match = [b for b in _backup.list_backups() if b.id == backup_id]
    assert match and match[0].schema_version == 0
    assert _backup.find_backup(backup_id).schema_version == 0


# ---------------------------------------------------------------------------
# Archive membership is bounded by the data root
# ---------------------------------------------------------------------------

def test_under_root_rejects_traversal_and_accepts_children(tmp_path):
    """``wsi_filename`` / ``thumb_filename`` come from the (mutable) DB, and are
    joined and handed to ``tar.add``. A row carrying a ``..`` traversal would
    otherwise pull an arbitrary host-readable file into a downloadable archive.
    """
    root = tmp_path / "wsi"
    root.mkdir()
    (root / "real.svs").write_bytes(b"x")
    outside = tmp_path / "secret.db"
    outside.write_bytes(b"hashes")
    root_real = os.path.realpath(str(root))

    assert _backup._under_root(root_real, os.path.join(str(root), "real.svs"))
    # Escapes via ..
    assert not _backup._under_root(root_real, os.path.join(str(root), "../secret.db"))
    # Absolute path outside the root
    assert not _backup._under_root(root_real, str(outside))
    # The root itself is not a valid member (strict descent).
    assert not _backup._under_root(root_real, str(root))


def test_create_backup_skips_wsi_row_escaping_the_data_root(
    client, admin_headers, db_session, slide, monkeypatch, caplog,
):
    """A traversing wsi_filename must not be archived. The WSI cross-check then
    fails the backup loudly rather than shipping an archive whose DB references
    a file it does not contain.
    """
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    secret = os.path.join(settings.data_dir, "secret-outside.txt")
    with open(secret, "wb") as fh:
        fh.write(b"do not archive me")

    original = slide.wsi_filename
    slide.wsi_filename = "../secret-outside.txt"
    db_session.commit()

    before = {r.id for r in _backup.list_backups()}
    try:
        with caplog.at_level("WARNING", logger="wsi_app.backup"):
            # Refused before the archive is opened, naming the reason.
            with pytest.raises(RuntimeError, match="escapes the wsi root"):
                _backup.create_backup(scope=_backup.SCOPE_FULL, note="traversal")
            # A metadata archive of this DB could never be restored either
            # (validate_archive_integrity refuses the name), so it is refused too.
            with pytest.raises(RuntimeError, match="name restore refuses"):
                _backup.create_backup(scope=_backup.SCOPE_METADATA, note="traversal")
        assert any("escapes the wsi root" in m for m in caplog.messages), caplog.messages
        # The file outside the root is untouched and was never read into an archive.
        assert os.path.isfile(secret)
        assert {r.id for r in _backup.list_backups()} == before
    finally:
        # The slide row outlives the test (shared DB); a traversing name left
        # behind would make every later backup refuse to run.
        slide.wsi_filename = original
        db_session.commit()


# ---------------------------------------------------------------------------
# A tour public_id is a path segment on both the backup and restore side
# ---------------------------------------------------------------------------

def test_safe_extract_rejects_dotdot_that_resolves_inside_dest(tmp_path):
    """"assets/tours/../../wsi.db" lands inside dest, so a containment check
    alone would let it through and it would silently replace the real wsi.db
    member."""
    archive = tmp_path / "dotdot.tar"
    with tarfile.open(str(archive), "w") as tar:
        info = tarfile.TarInfo("assets/tours/../../wsi.db")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"raw"))
    with tarfile.open(str(archive), "r") as tar:
        with pytest.raises(RuntimeError, match="unsafe path"):
            _backup._safe_extract(tar, str(tmp_path / "out"))


def test_safe_extract_rejects_duplicate_member(tmp_path):
    archive = tmp_path / "dup.tar"
    with tarfile.open(str(archive), "w") as tar:
        for payload in (b"snapshot", b"live db"):
            info = tarfile.TarInfo("wsi.db")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    with tarfile.open(str(archive), "r") as tar:
        with pytest.raises(RuntimeError, match="duplicate entry"):
            _backup._safe_extract(tar, str(tmp_path / "out"))


def test_create_backup_skips_invalid_tour_public_id(db_session, slide, caplog):
    """A restored DB carrying public_id="../.." must not make the next backup
    walk DATA_DIR (old archives, the raw live wsi.db) into the archive."""
    from wsi_app.models import Tour

    os.makedirs(os.path.join(settings.data_dir, "assets", "tours"), exist_ok=True)
    tour = Tour(slide_id=slide.id, title="evil")
    db_session.add(tour)
    db_session.commit()
    tour.public_id = "../.."
    db_session.commit()
    try:
        with caplog.at_level("WARNING", logger="wsi_app.backup"):
            rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="m12")
        assert any("not a valid token" in m for m in caplog.messages), caplog.messages
        with _backup._open_archive_reader(rec.path) as tar:
            names = tar.getnames()
        assert not any(".." in n.split("/") for n in names), names
        assert names.count("wsi.db") == 1
        _backup.delete_backup(rec.id)
    finally:
        db_session.delete(tour)
        db_session.commit()


def test_backup_keeps_a_tours_other_attachments_when_one_vanishes_mid_run(
    db_session, slide, monkeypatch,
):
    """Each attachment is listed, opened and archived on its own: one deleted
    while the backup runs costs only that file (a recursive tar.add of the
    tour dir would abort on it and drop the rest of the tour), an in-flight
    upload temp is not archived, and one overwritten in the gap is stored
    whole from the inode that was opened rather than cut at the old size."""
    from wsi_app.admin_helpers import tour_asset_dir
    from wsi_app.models import Tour

    tour = Tour(slide_id=slide.id, title="attachments")
    db_session.add(tour)
    db_session.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    body = {n: n.encode() + b"-" * 1000 for n in ("a.png", "b.png", "c.png", "d.png", "e.png")}
    for n, data in body.items():
        with open(os.path.join(adir, n), "wb") as f:
            f.write(data)
    with open(os.path.join(adir, ".a.png.upload-0123456789abcdef"), "wb") as f:
        f.write(b"partial")

    real_filter = _backup._archive_filter

    def racing_filter(info):
        if info.name.endswith("/a.png"):
            # An author deletes b.png and re-uploads c.png (larger) while
            # the backup is archiving a.png.
            os.remove(os.path.join(adir, "b.png"))
            with open(os.path.join(adir, "c.png.new"), "wb") as f:
                f.write(b"C" * 5000)
            os.replace(os.path.join(adir, "c.png.new"), os.path.join(adir, "c.png"))
        return real_filter(info)

    monkeypatch.setattr(_backup, "_archive_filter", racing_filter)
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="asset-race")
        prefix = f"assets/tours/{tour.public_id}/"
        with _backup._open_archive_reader(rec.path) as tar:
            names = tar.getnames()
        members = _archive_members(rec.path)
        assert sorted(n for n in names if n.startswith(prefix)) == [
            prefix + n for n in ("a.png", "c.png", "d.png", "e.png")
        ], names
        # The tour's own directory entry still precedes its files, as the
        # recursive add wrote it (an empty tour dir round-trips).
        assert prefix.rstrip("/") in names
        assert members[prefix + "c.png"] in (body["c.png"], b"C" * 5000)
        assert members[prefix + "d.png"] == body["d.png"]
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        import shutil as _shutil
        _shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


def test_backup_warns_about_each_tour_asset_entry_it_leaves_out(
    db_session, slide, caplog,
):
    """Only a tour dir's regular, non-dot files are archived (the upload path
    writes nothing else). A subdirectory, a non-temp dotfile or a symlink
    someone copied in by hand is left out with a WARNING naming it —
    dropping them silently would lose them on the next restore without a
    trace. An in-flight upload temp is
    left out without one: it is not content yet."""
    from wsi_app.admin_helpers import tour_asset_dir
    from wsi_app.models import Tour

    tour = Tour(slide_id=slide.id, title="hand-copied")
    db_session.add(tour)
    db_session.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(os.path.join(adir, "figs"), exist_ok=True)
    for rel, data in (
        ("pic.png", b"pic"),
        ("figs/inner.png", b"inner"),
        (".notes.txt", b"notes"),
        (".pic.png.upload-0123456789abcdef", b"partial"),
    ):
        with open(os.path.join(adir, rel), "wb") as f:
            f.write(data)
    os.symlink(os.path.join(adir, "pic.png"), os.path.join(adir, "link.png"))
    rec = None
    try:
        with caplog.at_level("WARNING", logger="wsi_app.backup"):
            rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="hand-copied")
        prefix = f"assets/tours/{tour.public_id}/"
        with _backup._open_archive_reader(rec.path) as tar:
            names = [n for n in tar.getnames() if n.startswith(prefix)]
        assert names == [prefix + "pic.png"], names
        skipped = [m for m in caplog.messages if "not archived" in m]
        for name in ("figs", ".notes.txt", "link.png"):
            assert sum(f"asset {name} " in m for m in skipped) == 1, (name, skipped)
        assert not any(".upload-" in m for m in caplog.messages), caplog.messages
        assert all(tour.public_id in m and m.startswith("backup:") for m in skipped), skipped
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        import shutil as _shutil
        _shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


def test_validate_archive_integrity_warns_on_invalid_tour_public_id(caplog):
    """An invalid tour token is a warning, not a refusal: create_backup keeps
    such a row (skipping only its assets), so refusing it here would make a
    backup that reported success impossible to restore."""
    stage_id = "stage-" + "e" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(stage_dir, "wsi.db"))
    try:
        conn.execute("CREATE TABLE slides (id INTEGER PRIMARY KEY, wsi_filename TEXT)")
        conn.execute("CREATE TABLE tours (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL)")
        conn.execute("INSERT INTO tours VALUES (1, '../..')")
        conn.execute("INSERT INTO tours VALUES (2, 'Ok_Token-123')")
        conn.commit()
    finally:
        conn.close()
    manifest = _backup.Manifest(
        scope="metadata",
        app_version=APP_VERSION,
        created_at="2026-01-01T00:00:00Z",
        slide_count=0,
        tour_count=2,
        includes_analytics=True,
        includes_history=True,
        schema_version=_migrations.CURRENT_SCHEMA_VERSION,
    )
    try:
        with caplog.at_level("WARNING", logger="wsi_app.backup"):
            report = _backup.validate_archive_integrity(stage_id, manifest)
        assert report.ok is True, report.errors
        assert report.errors == []
        warns = [w for w in report.warnings if "invalid public_id" in w]
        assert len(warns) == 1 and "'../..'" in warns[0], report.warnings
        assert "Ok_Token-123" not in warns[0]
        assert report.to_dict()["warnings"] == report.warnings
        assert any("invalid public_id" in m for m in caplog.messages)
    finally:
        _backup.cleanup_stage(stage_id)


def test_backup_with_invalid_tour_public_id_stays_restorable(db_session, slide):
    """Backup and restore agree: the archive a live DB with a "../.." tour
    produces (backup succeeds with a warning) passes the restore integrity
    check with a warning instead of being refused."""
    from wsi_app.models import Tour

    os.makedirs(os.path.join(settings.data_dir, "assets", "tours"), exist_ok=True)
    tour = Tour(slide_id=slide.id, title="evil")
    db_session.add(tour)
    db_session.commit()
    tour.public_id = "../.."
    db_session.commit()
    rec = None
    stage_id = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="m12-roundtrip")
        stage_id, manifest = _backup.stage_restore(rec.path)
        report = _backup.validate_archive_integrity(stage_id, manifest)
        # Scoped to the tour finding: the shared test DB may hold rows other
        # tests planted (e.g. an unsafe wsi_filename) that are errors in
        # their own right.
        assert not any("public_id" in e for e in report.errors), report.errors
        assert any("invalid public_id" in w for w in report.warnings)
    finally:
        if stage_id is not None:
            _backup.cleanup_stage(stage_id)
        if rec is not None:
            _backup.delete_backup(rec.id)
        db_session.delete(tour)
        db_session.commit()


# ---------------------------------------------------------------------------
# Codec checksums, sidecar sha256 and the staged DB are all verified
# ---------------------------------------------------------------------------

_PAYLOAD_MARKER = b"PAYLOAD-MARKER"


def _write_corrupted_archive(path: str) -> None:
    """Write a tar.gz / tar.zst whose one member is stored uncompressed, then
    flip a byte inside that member's data. The codec still decodes it; only
    the trailing CRC32 / frame checksum can tell."""
    payload = _PAYLOAD_MARKER + os.urandom(64 * 1024)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("payload.bin")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    if path.endswith(".tar.zst"):
        blob = _backup._zstd.ZstdCompressor(level=3, write_checksum=True).compress(
            buf.getvalue()
        )
    else:
        import gzip
        blob = gzip.compress(buf.getvalue(), compresslevel=0)
    data = bytearray(blob)
    at = data.index(_PAYLOAD_MARKER) + len(_PAYLOAD_MARKER) + 100
    data[at] ^= 0xFF
    with open(path, "wb") as f:
        f.write(bytes(data))


@pytest.mark.parametrize("suffix", [".tar.gz", ".tar.zst"])
def test_safe_extract_detects_corruption_via_codec_checksum(tmp_path, suffix):
    if suffix == ".tar.zst" and not _backup._HAS_ZSTD:
        pytest.skip("zstandard not installed")
    archive = str(tmp_path / ("bad" + suffix))
    _write_corrupted_archive(archive)
    with pytest.raises(_backup._CORRUPT_ARCHIVE_ERRORS):
        with _backup._open_archive_reader(archive) as tar:
            _backup._safe_extract(tar, str(tmp_path / "out"))


def test_restore_refuses_archive_not_matching_sidecar_sha256(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    sidecar = rec.path + ".meta.json"
    with open(sidecar, "r", encoding="utf-8") as f:
        meta = json.load(f)
    good = meta["sha256"]
    try:
        # Matching sidecar: stages normally.
        staged = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})
        client.delete(f"/api/admin/restore/stage/{staged['stage_id']}",
                      headers=admin_headers)
        # Recorded hash differs from the bytes on disk: refused as damaged.
        meta["sha256"] = ("0" if good[0] != "0" else "1") + good[1:]
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        rj = _stage_job(client, admin_headers, data={"snapshot_id": backup_id})
        assert rj["state"] == "failed", rj
        assert "sha256" in rj["error"]
    finally:
        _backup.delete_backup(backup_id)


def test_check_sqlite_integrity_rejects_damaged_db(tmp_path):
    db = str(tmp_path / "wsi.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("x" * 200,)] * 500)
    conn.commit()
    conn.close()
    _backup._check_sqlite_integrity(db)  # healthy DB passes

    page = 4096
    with open(db, "r+b") as f:
        f.seek(page * 2)  # a table b-tree page, well past the header
        f.write(b"\xa5" * page)
    with pytest.raises(RuntimeError, match="integrity check"):
        _backup._check_sqlite_integrity(db)


# ---------------------------------------------------------------------------
# Staging free space / ENOSPC reporting / restored DB mode
# ---------------------------------------------------------------------------

def _write_plain_tar(path: str, members: dict[str, bytes], mode: int = 0o644) -> None:
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))


def test_safe_extract_refuses_when_decompressed_size_exceeds_free_space(tmp_path):
    archive = str(tmp_path / "big.tar")
    _write_plain_tar(archive, {"a.bin": b"a" * 100, "b.bin": b"b" * 100})
    dest = tmp_path / "out"
    reserve = disk_space.RESERVE_MIN_BYTES
    with tarfile.open(archive, "r") as tar:
        with pytest.raises(RuntimeError, match="insufficient free space"):
            _backup._safe_extract(tar, str(dest), free_bytes=reserve + 150)
    # Refused before the overflowing member was written.
    assert (dest / "a.bin").exists()
    assert not (dest / "b.bin").exists()


def test_safe_extract_keeps_a_reserve_free_for_the_running_install(tmp_path):
    """Extraction must not be allowed right down to 0 bytes free on a volume
    the live DB shares: the archive must fit with the reserve left over."""
    archive = str(tmp_path / "a.tar")
    _write_plain_tar(archive, {"a.bin": b"a" * 100})
    reserve = disk_space.RESERVE_MIN_BYTES
    with tarfile.open(archive, "r") as tar:
        with pytest.raises(RuntimeError, match="insufficient free space"):
            _backup._safe_extract(tar, str(tmp_path / "out1"), free_bytes=100 + reserve - 1)
    with tarfile.open(archive, "r") as tar:
        _backup._safe_extract(tar, str(tmp_path / "out2"), free_bytes=100 + reserve)
    assert (tmp_path / "out2" / "a.bin").read_bytes() == b"a" * 100
    # Large archives keep a tenth of their size free instead.
    big = 100 * reserve
    assert disk_space.reserve_for(big) == big // 10


def test_safe_extract_refuses_more_members_than_the_cap(tmp_path):
    """A few hundred KB of compressed empty entries must not create
    unbounded numbers of files on the data volume."""
    archive = str(tmp_path / "many.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for i in range(30):
            info = tarfile.TarInfo(f"assets/e{i}")
            tar.addfile(info, io.BytesIO(b""))
    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(RuntimeError, match="more than 20 entries"):
            _backup._safe_extract(tar, str(tmp_path / "out"), max_members=20)
    # The default is far above any real archive layout.
    assert _backup._MAX_ARCHIVE_MEMBERS >= 1_000_000
    with tarfile.open(archive, "r:gz") as tar:
        _backup._safe_extract(tar, str(tmp_path / "ok"))
    assert len(os.listdir(tmp_path / "ok" / "assets")) == 30


def _header_block(name: str, type_: bytes, size: int) -> bytes:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.size = size
    return info.tobuf(format=tarfile.USTAR_FORMAT)


def _pax_record(key: str, value: str) -> bytes:
    """One pax header record, ``"<len> <key>=<value>\\n"``, its length field
    counting itself."""
    body = f" {key}={value}\n".encode()
    n = len(body) + 1
    while len(str(n)) + len(body) != n:
        n += 1
    return str(n).encode() + body


def _pad_block(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


def _write_compressed(path: str, fmt: str, chunks) -> None:
    if fmt == "zst":
        if not _backup._HAS_ZSTD:
            pytest.skip("zstandard not installed")
        raw = open(path, "wb")
        out = _backup._zstd.ZstdCompressor(level=1).stream_writer(raw)
    else:
        import gzip
        out = gzip.open(path, "wb", compresslevel=1)
    with out:
        for chunk in chunks:
            out.write(chunk)


@pytest.mark.parametrize("fmt", ["gz", "zst"])
@pytest.mark.parametrize("type_", [
    tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
    tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
])
def test_archive_reader_refuses_an_oversized_extended_header_unread(tmp_path, fmt, type_):
    """tarfile reads a pax / GNU long-name header into memory whole, at the
    size the header declares, before it yields a member and so before any of
    _safe_extract's limits run: ~24 KB of zstd declaring a 768 MiB pax header
    would peak at 1.6 GB RSS. It is refused from its declared size, unread."""
    import tracemalloc

    declared = 64 * 1024 * 1024
    path = str(tmp_path / f"bomb.tar.{fmt}")
    zeros = b"\0" * (1 << 20)
    _write_compressed(path, fmt, [_header_block("x", type_, declared)]
                      + [zeros] * (declared // len(zeros) + 1))
    tracemalloc.start()
    try:
        with pytest.raises(tarfile.TarError, match="extended header"):
            with _backup._open_archive_reader(path) as tar:
                _backup._safe_extract(tar, str(tmp_path / "out"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024, peak


def test_archive_reader_caps_the_extended_headers_of_one_member_together(tmp_path):
    """Each header of a chain stays in memory until the member at its end is
    read, so the cap is on their sum — and global pax headers, whose keys
    tarfile keeps for the rest of the archive, count against every member
    after them. A path long enough to need a pax / GNU long-name header (all
    the app's writers ever emit) still reads."""
    half = _backup._MAX_EXTENDED_HEADER_BYTES // 2 + 512

    def chain(*types):
        out = []
        for t in types:
            out += [_header_block("x", t, half), b"\0" * half]
        return out + [_header_block("a.bin", tarfile.REGTYPE, 0), b"\0" * 1024]

    for name, types in (
        ("chain", (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME)),
        ("global", (tarfile.XGLTYPE,)),
    ):
        path = str(tmp_path / f"{name}.tar.gz")
        blocks = chain(*types)
        if name == "global":
            # One global header within the cap — holding a key, since what
            # stays in memory is the keys tarfile keeps — then a member, then
            # a pax header that is within the cap alone but not with the
            # global one.
            record = _pax_record("comment", "g" * (half - 32))
            blocks = ([_header_block("x", tarfile.XGLTYPE, len(record)), _pad_block(record)]
                      + blocks[2:3]
                      + [_header_block("x", tarfile.XHDTYPE, half), b"\0" * half]
                      + [_header_block("b.bin", tarfile.REGTYPE, 0), b"\0" * 1024])
        _write_compressed(path, "gz", blocks)
        with pytest.raises(tarfile.TarError, match="extended header"):
            with _backup._open_archive_reader(path) as tar:
                _backup._safe_extract(tar, str(tmp_path / f"out-{name}"))

    long_name = "assets/tours/T/" + "n" * 250 + ".png"
    for fmt in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
        path = str(tmp_path / f"long-{fmt}.tar.gz")
        with tarfile.open(path, "w:gz", format=fmt) as tar:
            info = tarfile.TarInfo(long_name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"png"))
        dest = tmp_path / f"long-out-{fmt}"
        with _backup._open_archive_reader(path) as tar:
            _backup._safe_extract(tar, str(dest))
        assert (dest / long_name).read_bytes() == b"png"


def test_archive_reader_counts_global_headers_by_the_keys_they_leave(tmp_path):
    """tarfile merges a global pax header's keys into one dict, a repeated
    key overwriting the last, so what globals hold for the rest of the
    archive is that dict, not the sum of every global header read. An archive
    putting a 713-byte global header in front of each of 1500 members (1 MiB
    in all, the same keys each time) reads; globals whose distinct keys
    together pass the cap are still refused."""
    members = 1500
    record = _pax_record("comment", "c" * 700)
    assert len(record) == 713
    assert members * len(record) > _backup._MAX_EXTENDED_HEADER_BYTES
    blocks = []
    for i in range(members):
        blocks += [_header_block("g", tarfile.XGLTYPE, len(record)), _pad_block(record),
                   _header_block(f"assets/e{i}", tarfile.REGTYPE, 1), _pad_block(b"x")]
    path = str(tmp_path / "globals.tar.gz")
    _write_compressed(path, "gz", blocks + [b"\0" * 1024])
    dest = tmp_path / "out"
    with _backup._open_archive_reader(path) as tar:
        _backup._safe_extract(tar, str(dest))
    assert len(os.listdir(dest / "assets")) == members

    # Distinct keys do accumulate: each global header alone is small, but
    # the dict they build passes the cap.
    value = "v" * 4000
    per = len(_pax_record("k0000", value))
    blocks = []
    for i in range(_backup._MAX_EXTENDED_HEADER_BYTES // per + 2):
        rec = _pax_record(f"k{i:04d}", value)
        blocks += [_header_block("g", tarfile.XGLTYPE, len(rec)), _pad_block(rec),
                   _header_block(f"assets/d{i}", tarfile.REGTYPE, 0)]
    path = str(tmp_path / "distinct.tar.gz")
    _write_compressed(path, "gz", blocks + [b"\0" * 1024])
    with pytest.raises(tarfile.TarError, match="extended header"):
        with _backup._open_archive_reader(path) as tar:
            _backup._safe_extract(tar, str(tmp_path / "out-distinct"))


def test_archive_reader_refuses_gnu_sparse_members(tmp_path):
    """GNU sparse maps are the other header data tarfile reads before handing
    a member over (old-style extension blocks, and the version 1.0 map at
    the start of the member's data), with no size declared up front to check.
    Nothing this app writes is sparse, so they are refused outright."""
    old = str(tmp_path / "old-sparse.tar.gz")
    _write_compressed(old, "gz", [_header_block("s.bin", tarfile.GNUTYPE_SPARSE, 0), b"\0" * 1024])
    pax10 = str(tmp_path / "pax-sparse.tar.gz")
    with tarfile.open(pax10, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("s.bin")
        info.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
        body = b"100000000\n" + b"1\n" * 4096
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    for path in (old, pax10):
        with pytest.raises(tarfile.TarError, match="sparse"):
            with _backup._open_archive_reader(path) as tar:
                _backup._safe_extract(tar, str(tmp_path / "out"))
    # Staging reports it as a bad archive (a 400), not a server fault.
    with pytest.raises(RuntimeError, match="corrupt or not a supported format"):
        _backup.stage_restore(old)


def test_safe_extract_reports_progress(tmp_path):
    archive = str(tmp_path / "p.tar")
    _write_plain_tar(archive, {"a.bin": b"a" * 10, "b.bin": b"b" * 20})
    seen: list[int] = []
    with tarfile.open(archive, "r") as tar:
        _backup._safe_extract(tar, str(tmp_path / "out"), on_progress=seen.append)
    assert seen == [10, 30]


def test_stage_restore_reports_enospc_as_no_space_not_corrupt(tmp_path, monkeypatch):
    import errno

    archive = str(tmp_path / "x.tar")
    _write_plain_tar(archive, {"manifest.json": b"{}"})

    def _full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_backup, "_safe_extract", _full)
    with pytest.raises(RuntimeError) as ei:
        _backup.stage_restore(archive)
    assert "free space" in str(ei.value)
    assert "corrupt" not in str(ei.value)


def test_stage_restore_tightens_world_readable_db_mode(client, admin_headers, tmp_path):
    """An archive recording wsi.db as 0644 (installs without UMask=0027) must not
    stage — and so restore — a world-readable DB."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    repacked = str(tmp_path / "repacked.tar.gz")
    try:
        with _backup._open_archive_reader(rec.path) as src, \
                tarfile.open(repacked, "w:gz") as dst:
            for member in src:
                data = src.extractfile(member) if member.isreg() else None
                if member.name == "wsi.db":
                    member.mode = 0o644
                dst.addfile(member, data)
        stage_id, _manifest = _backup.stage_restore(repacked)
        try:
            db = os.path.join(settings.backup_dir, ".staging", stage_id, "wsi.db")
            assert os.stat(db).st_mode & 0o777 == 0o640
        finally:
            _backup.cleanup_stage(stage_id)
    finally:
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# A full backup's DB snapshot and the WSI bytes it
# archives must be the same generation. A same-filename replace keeps the
# ``{slide_id}_{name}`` path, so a replace landing between the snapshot and the
# slide's tar.add could ship the snapshot's width/height next to the new
# image, and validate_archive_integrity (presence only) would pass it.
# ---------------------------------------------------------------------------

_NEW_WSI = b"NEW-wsi-bytes-of-another-scan"


def _seed_generation_slide(db_session, catalog_id):
    s, wsi_name, thumb_name = _seed_slide_with_files(db_session, catalog_id=catalog_id)
    s.width, s.height, s.file_size_bytes = 100, 100, len(b"wsi-bytes")
    db_session.commit()
    return s.id, wsi_name, thumb_name


def _same_name_replace(slide_id, wsi_name, thumb_name):
    """What admin_replace_slide_wsi's locked phase does for a same-name upload:
    os.replace new bytes (and thumbnail) onto the same paths and commit the
    file-derived columns, all under the slide's mutation lock."""
    from wsi_app.admin_helpers import slide_mutation_lock

    with slide_mutation_lock(slide_id):
        for sub, name, data in (("wsi", wsi_name, _NEW_WSI), ("thumbs", thumb_name, b"NEW-thumb")):
            path = os.path.join(settings.data_dir, sub, name)
            with open(path + ".upload-test", "wb") as f:
                f.write(data)
            os.replace(path + ".upload-test", path)
        conn = sqlite3.connect(_backup._live_db_path())
        try:
            conn.execute(
                "UPDATE slides SET width = 200, height = 200, file_size_bytes = ? WHERE id = ?",
                (len(_NEW_WSI), slide_id),
            )
            conn.commit()
        finally:
            conn.close()


def _archive_members(rec_path: str) -> dict:
    out = {}
    with _backup._open_archive_reader(rec_path) as tar:
        for member in tar:
            if member.isreg():
                out[member.name] = tar.extractfile(member).read()
    return out


def _archived_slide_width(members: dict, slide_id: int) -> int:
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "wsi.db")
        with open(db_path, "wb") as f:
            f.write(members["wsi.db"])
        conn = sqlite3.connect(db_path)
        try:
            return int(conn.execute("SELECT width FROM slides WHERE id = ?", (slide_id,)).fetchone()[0])
        finally:
            conn.close()


def test_full_backup_fails_when_wsi_replaced_after_snapshot(db_session, monkeypatch):
    _reset_slides(db_session)
    slide_id, wsi_name, thumb_name = _seed_generation_slide(db_session, "GEN-1")
    before = {r.id for r in _backup.list_backups()}

    real_snapshot = _backup._snapshot_sqlite

    def snapshot_then_replace(src, dst):
        real_snapshot(src, dst)
        _same_name_replace(slide_id, wsi_name, thumb_name)

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_replace)
    with pytest.raises(RuntimeError, match="replaced or deleted after the DB snapshot"):
        _backup.create_backup(scope=_backup.SCOPE_FULL, note="gen-race")
    # Nothing published, no partial archive left behind.
    assert {r.id for r in _backup.list_backups()} == before
    assert not [n for n in os.listdir(settings.backup_dir) if n.endswith(".tmp")]


@pytest.mark.parametrize("linked", [True, False])
def test_full_backup_keeps_snapshot_bytes_when_replace_lands_mid_copy(db_session, monkeypatch, linked):
    """A replace after the file was opened must not leak into the archive: the
    link taken at snapshot time — or, where links cannot be made, the fd
    opened after the generation check — pins the snapshot generation's inode,
    so the archive is consistent and the backup succeeds."""
    if not linked:
        monkeypatch.setattr(_backup, "_pin_snapshot_wsi_files", lambda *a, **kw: ({}, 0))
    _reset_slides(db_session)
    slide_id, wsi_name, thumb_name = _seed_generation_slide(db_session, "GEN-2")

    real_add = _backup._add_pinned_file
    fired = []

    def replace_then_add(tar, f, arcname):
        if arcname == f"wsi/{wsi_name}" and not fired:
            fired.append(1)
            _same_name_replace(slide_id, wsi_name, thumb_name)
        return real_add(tar, f, arcname)

    monkeypatch.setattr(_backup, "_add_pinned_file", replace_then_add)
    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="mid-copy")
    try:
        assert fired
        members = _archive_members(rec.path)
        assert members[f"wsi/{wsi_name}"] == b"wsi-bytes"
        assert _archived_slide_width(members, slide_id) == 100
        # The thumbnail is checked after the replace committed: it is the new
        # generation, so it is left out rather than paired with the old DB.
        assert f"thumbs/{thumb_name}" not in members
    finally:
        _backup.delete_backup(rec.id)


def test_full_backup_tolerates_unrelated_edit_after_snapshot(db_session, monkeypatch):
    """Only file-generation columns count: a title edit (or publish toggle)
    during a long backup must not fail it."""
    _reset_slides(db_session)
    slide_id, wsi_name, thumb_name = _seed_generation_slide(db_session, "GEN-3")

    real_snapshot = _backup._snapshot_sqlite

    def snapshot_then_edit(src, dst):
        real_snapshot(src, dst)
        conn = sqlite3.connect(_backup._live_db_path())
        try:
            conn.execute(
                "UPDATE slides SET title = 'renamed', is_published = 1 WHERE id = ?",
                (slide_id,),
            )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_edit)
    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="unrelated")
    try:
        members = _archive_members(rec.path)
        assert members[f"wsi/{wsi_name}"] == b"wsi-bytes"
        assert members[f"thumbs/{thumb_name}"] == b"thumb-bytes"
    finally:
        _backup.delete_backup(rec.id)


def test_backup_manifest_records_the_python_environment(client, admin_headers, slide):
    """Diagnostics for reproducing an incident on the tree an archive was
    taken with; restore never consults them, and an absent or malformed value
    is dropped rather than failing the parse."""
    import json as _json
    import platform as _platform

    r = client.post("/api/admin/backup", json={"include_wsi": False}, headers=admin_headers)
    assert r.status_code == 200, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    rec = _backup.find_backup(rj["result"]["backup_id"])
    with _backup._open_archive_reader(rec.path) as tar:
        # A streaming reader: take the manifest (the first member) in order.
        manifest = next(
            _json.loads(tar.extractfile(m).read()) for m in tar if m.name == "manifest.json"
        )
    assert manifest["python_version"] == _platform.python_version()
    pkgs = {k.lower(): v for k, v in manifest["python_packages"].items()}
    import fastapi
    assert pkgs["fastapi"] == fastapi.__version__

    parsed = _backup.Manifest.from_dict(manifest)
    assert parsed.python_packages == manifest["python_packages"]
    absent = {k: v for k, v in manifest.items() if k not in ("python_version", "python_packages")}
    assert _backup.Manifest.from_dict(absent).python_packages == {}
    garbled = {**manifest, "python_packages": ["not", "a", "dict"]}
    assert _backup.Manifest.from_dict(garbled).python_packages == {}


def test_full_backup_fails_when_only_wsi_generation_changed(db_session, monkeypatch):
    """A replace whose file-derived columns all come out identical — the same
    scan re-exported, a byte-level fix of the same size and metadata — still
    bumps Slide.wsi_generation, and that alone must fail the backup: the
    snapshot's row no longer describes the bytes at the path."""
    _reset_slides(db_session)
    slide_id, wsi_name, _thumb_name = _seed_generation_slide(db_session, "GEN-4")
    same_size = b"WSI-BYTES"  # same length as the seeded b"wsi-bytes"
    assert len(same_size) == len(b"wsi-bytes")

    real_snapshot = _backup._snapshot_sqlite

    def snapshot_then_identical_replace(src, dst):
        real_snapshot(src, dst)
        from wsi_app.admin_helpers import slide_mutation_lock

        with slide_mutation_lock(slide_id):
            path = os.path.join(settings.data_dir, "wsi", wsi_name)
            with open(path + ".upload-test", "wb") as f:
                f.write(same_size)
            os.replace(path + ".upload-test", path)
            conn = sqlite3.connect(_backup._live_db_path())
            try:
                # Exactly what admin_replace_slide_wsi commits for such a file:
                # every derived column unchanged, the generation bumped.
                conn.execute(
                    "UPDATE slides SET wsi_generation = wsi_generation + 1 WHERE id = ?",
                    (slide_id,),
                )
                conn.commit()
            finally:
                conn.close()

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_identical_replace)
    before = {r.id for r in _backup.list_backups()}
    with pytest.raises(RuntimeError, match="replaced or deleted after the DB snapshot"):
        _backup.create_backup(scope=_backup.SCOPE_FULL, note="gen-only")
    assert {r.id for r in _backup.list_backups()} == before


def test_manifest_infinity_is_runtime_error_not_overflow():
    """json.load accepts Infinity; int(float('inf')) is an OverflowError that
    must still surface as the documented RuntimeError."""
    import json

    data = json.loads('{"schema_version": Infinity}')
    with pytest.raises(RuntimeError):
        _backup.Manifest.from_dict(data)
    assert _backup._schema_version_of(data) == 0


def test_full_backup_with_missing_wsi_file_says_files_are_absent(db_session, attach_wsi):
    from wsi_app.models import Slide

    ok = Slide(title="ok", catalog_id="OKA")
    lost = Slide(title="lost", catalog_id="LOSTB")
    db_session.add_all([ok, lost])
    db_session.commit()
    attach_wsi(ok)
    lost.wsi_filename = f"{lost.id}_gone.svs"  # row says the file exists, disk doesn't
    db_session.commit()
    with pytest.raises(RuntimeError) as ei:
        _backup.create_backup(scope=_backup.SCOPE_FULL)
    assert "not on disk" in str(ei.value)
    assert "concurrent" not in str(ei.value)


# ---------------------------------------------------------------------------
# Archive compression is detected from the content, not the filename
# ---------------------------------------------------------------------------

def _tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"hello"))
    return buf.getvalue()


def _compressed(fmt: str) -> bytes:
    if fmt == "gz":
        import gzip
        return gzip.compress(_tar_bytes())
    if not _backup._HAS_ZSTD:
        pytest.skip("zstandard not installed")
    return _backup._zstd.ZstdCompressor().compress(_tar_bytes())


@pytest.mark.parametrize("fmt", ["gz", "zst"])
@pytest.mark.parametrize("name", [
    "a.tar.gz", "a.tar.zst", "A.TAR.GZ", "A.TAR.ZST", "a.tgz", "archive.bin", "noext",
])
def test_archive_reader_ignores_a_misleading_filename(tmp_path, fmt, name):
    p = tmp_path / name
    p.write_bytes(_compressed(fmt))
    with _backup._open_archive_reader(str(p)) as tar:
        assert [m.name for m in tar] == ["hello.txt"]


def test_archive_reader_still_opens_an_uncompressed_tar(tmp_path):
    p = tmp_path / "plain.tar"  # no known magic and no known suffix: tar auto-detect
    p.write_bytes(_tar_bytes())
    with _backup._open_archive_reader(str(p)) as tar:
        assert [m.name for m in tar] == ["hello.txt"]


@pytest.mark.parametrize("name", ["BACKUP.TAR.GZ", "backup.bin", "backup.tar.zst"])
def test_restore_upload_of_a_gzip_archive_is_staged_whatever_its_name(
    client, admin_headers, name
):
    """A staged suffix taken from the (case-sensitive) filename, defaulting
    to .tar.zst, would fail a valid gzip archive named BACKUP.TAR.GZ as a
    corrupt zstd stream."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    try:
        with _backup._open_archive_reader(rec.path) as src:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as dst:
                for m in src:
                    f = src.extractfile(m) if m.isfile() else None
                    dst.addfile(m, f)
        staged = _stage_via_api(
            client, admin_headers,
            files={"file": (name, io.BytesIO(buf.getvalue()), "application/octet-stream")},
        )
        client.delete(f"/api/admin/restore/stage/{staged['stage_id']}", headers=admin_headers)
    finally:
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# Restore staging and reset run as jobs; sessions end with a restore
# ---------------------------------------------------------------------------

def test_restore_staging_runs_as_a_job_not_in_the_request(client, admin_headers, monkeypatch):
    """Extracting a large archive takes far longer than nginx (600 s) or a
    Cloudflare tunnel (100 s) waits: the request must hand it to a job and
    answer at once, holding the job slot until the job is done."""
    import threading

    backup_id = _make_backup_via_api(client, admin_headers)
    entered, release = threading.Event(), threading.Event()
    real = _backup.stage_restore

    def held(path, progress_cb=None):
        entered.set()
        assert release.wait(10)
        return real(path, progress_cb=progress_cb)

    monkeypatch.setattr(_backup, "stage_restore", held)
    try:
        r = client.post("/api/admin/restore", data={"snapshot_id": backup_id}, headers=admin_headers)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert entered.wait(10)
        rec = client.get(f"/api/admin/backup/jobs/{job_id}", headers=admin_headers).json()
        assert rec["state"] == "running" and rec["kind"] == "restore_stage"
        assert rec["progress"]["snapshot_id"] == backup_id
        busy = client.post("/api/admin/restore", data={"snapshot_id": backup_id}, headers=admin_headers)
        assert busy.status_code == 409, busy.text
    finally:
        release.set()
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    assert res["plan"]["blocking_reason"] is None
    assert res["manifest"]["scope"] == "metadata"
    assert res["current_app_version"] == APP_VERSION
    assert os.path.isdir(os.path.join(settings.backup_dir, ".staging", res["stage_id"]))
    r = client.delete(f"/api/admin/restore/stage/{res['stage_id']}", headers=admin_headers)
    assert r.json() == {"ok": True, "removed": True}


def test_stage_restore_reports_extraction_progress(client, admin_headers, monkeypatch):
    backup_id = _make_backup_via_api(client, admin_headers)
    monkeypatch.setattr(_backup, "_STAGE_PROGRESS_INTERVAL", 0.0)
    seen: list[dict] = []
    stage_id, _manifest = _backup.stage_restore(_backup.find_backup(backup_id).path, progress_cb=seen.append)
    try:
        assert seen[0]["phase"] == "extracting"
        assert seen[-1] == {"phase": "verifying"}
        assert seen[-2]["bytes_extracted"] > 0
    finally:
        _backup.cleanup_stage(stage_id)


def _padded_archive(client, admin_headers) -> bytes:
    """A real backup repacked as .tar.gz with 2 MiB of incompressible padding,
    so Starlette spools the upload to a temporary file (past its 1 MiB
    in-memory threshold) rather than keeping it in memory."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    try:
        buf = io.BytesIO()
        with _backup._open_archive_reader(rec.path) as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
            for m in src:
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
            pad = os.urandom(2 * 1024 * 1024)
            info = tarfile.TarInfo("assets/padding.bin")
            info.size = len(pad)
            dst.addfile(info, io.BytesIO(pad))
    finally:
        _backup.delete_backup(backup_id)
    return buf.getvalue()


def _upload_leftovers() -> list[str]:
    staging = os.path.join(settings.backup_dir, ".staging")
    return [n for n in os.listdir(staging) if n.startswith("upload-")] if os.path.isdir(staging) else []


def test_restore_upload_answers_before_it_is_copied_into_staging(client, admin_headers, monkeypatch):
    """Copying the spooled upload into staging is a full physical copy on a
    filesystem without reflinks, which outlasts a proxy's response timeout for
    a large archive: the 202 (and its job id) must come first, the copy in the
    job, from the spool file the request no longer holds."""
    import threading

    from wsi_app.routers import admin_backup

    blob = _padded_archive(client, admin_headers)
    gate, entered = threading.Event(), threading.Event()
    seen: list = []
    real = admin_backup._copy_held_upload

    def slow(held, out, copied):
        entered.set()
        assert gate.wait(10)
        # The request has ended (Starlette closed its spool file); the job's
        # own descriptor still reads the whole upload.
        assert held.fd is not None and os.fstat(held.fd).st_size == len(blob)
        seen.append(held)
        real(held, out, copied)

    monkeypatch.setattr(admin_backup, "_copy_held_upload", slow)
    r = client.post(
        "/api/admin/restore", headers=admin_headers,
        files={"file": ("big.tar.gz", io.BytesIO(blob), "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert entered.wait(10)
    rec = client.get(f"/api/admin/backup/jobs/{job_id}", headers=admin_headers).json()
    assert rec["state"] == "running", rec
    assert rec["progress"]["phase"] == "copying"
    assert rec["progress"]["total_bytes"] == len(blob)
    assert rec["progress"]["snapshot_id"] is None
    gate.set()
    rj = _await_job(client, admin_headers, job_id)
    assert rj["state"] == "succeeded", rj
    assert rj["progress"]["bytes_copied"] == len(blob)
    # The job released the spool file and removed its staged copy.
    assert seen and seen[0].fd is None
    assert _upload_leftovers() == []
    client.delete(f"/api/admin/restore/stage/{rj['result']['stage_id']}", headers=admin_headers)


@pytest.mark.parametrize("errno_name", ["ENOSPC", "EDQUOT"])
def test_restore_upload_on_a_full_disk_fails_the_job_as_no_space(
    client, admin_headers, monkeypatch, errno_name,
):
    import errno

    from wsi_app import jobs as _jobs
    from wsi_app.routers import admin_backup

    seen: list = []

    def full(held, out, copied):
        seen.append(held)
        os.write(out, b"partial")
        raise OSError(getattr(errno, errno_name), "No space left on device")

    monkeypatch.setattr(admin_backup, "_copy_held_upload", full)
    r = client.post(
        "/api/admin/restore",
        files={"file": ("b.tar.zst", io.BytesIO(b"x" * (2 * 1024 * 1024)), "application/octet-stream")},
        headers=admin_headers,
    )
    assert r.status_code == 202, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    assert rj["state"] == "failed", rj
    assert rj["result"]["code"] == "adminDash.apiErr.uploadNoSpace"
    assert _upload_leftovers() == []
    assert seen and seen[0].fd is None
    assert _jobs.store.has_running() is False


def test_restore_upload_is_released_when_the_job_thread_cannot_start(client, admin_headers, monkeypatch):
    import threading
    import types

    from wsi_app import jobs as _jobs
    from wsi_app.routers import admin_backup

    held: list = []
    real_hold = admin_backup.hold_upload

    def hold(upload, max_bytes):
        h = real_hold(upload, max_bytes)
        held.append(h)
        return h

    class _NoStart(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(admin_backup, "hold_upload", hold)
    monkeypatch.setattr(admin_backup, "threading", types.SimpleNamespace(Thread=_NoStart, Lock=threading.Lock))
    with pytest.raises(RuntimeError, match="can't start new thread"):
        client.post(
            "/api/admin/restore",
            files={"file": ("b.tar.zst", io.BytesIO(b"x" * (2 * 1024 * 1024)), "application/octet-stream")},
            headers=admin_headers,
        )
    assert held and held[0].fd is None
    assert _jobs.store.has_running() is False
    assert _upload_leftovers() == []


def test_hold_upload_refuses_an_upload_over_the_cap_and_lets_it_go(tmp_path):
    from tempfile import SpooledTemporaryFile

    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from wsi_app.routers import admin_backup

    spool = SpooledTemporaryFile(max_size=4, dir=tmp_path)
    spool.write(b"\x1f\x8b" + b"y" * 98)
    upload = UploadFile(spool, filename="x.bin")
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HTTPException) as ei:
        admin_backup.hold_upload(upload, 99)
    assert ei.value.status_code == 413
    assert len(os.listdir("/proc/self/fd")) == before
    held = admin_backup.hold_upload(upload, 100)
    try:
        assert held.size == 100 and held.suffix == ".tar.gz" and held.fd is not None
        assert len(os.listdir("/proc/self/fd")) == before + 1
    finally:
        held.close()
        held.close()
    assert len(os.listdir("/proc/self/fd")) == before
    spool.close()


def test_hold_upload_out_of_descriptors_does_not_read_the_spool_into_memory(tmp_path, monkeypatch):
    """When the spool cannot be dup'd (EMFILE), the upload — anything up to
    the 1 TiB cap — must not be read into the worker's memory instead: the
    request is refused (503) and the spool left alone."""
    import errno
    from tempfile import SpooledTemporaryFile

    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from wsi_app.routers import admin_backup

    spool = SpooledTemporaryFile(max_size=4, dir=tmp_path)
    spool.write(b"\x1f\x8b" + b"y" * 98)
    upload = UploadFile(spool, filename="x.bin")

    def no_fds(fd):
        raise OSError(errno.EMFILE, "Too many open files")

    def no_read(*a, **kw):
        raise AssertionError("the spool must not be read into memory")

    monkeypatch.setattr(admin_backup.os, "dup", no_fds)
    monkeypatch.setattr(spool, "read", no_read)
    with pytest.raises(HTTPException) as ei:
        admin_backup.hold_upload(upload, 1 << 40)
    assert ei.value.status_code == 503
    spool.close()


def _set_session_generation(db, value: int) -> None:
    from wsi_app.admin_helpers import get_or_create_singleton
    from wsi_app.models import AuthSettings

    get_or_create_singleton(db, AuthSettings).session_generation = value
    db.commit()


def test_restore_signs_out_tokens_and_cookies_revoked_after_the_backup(
    client, admin_headers, admin_user, db_session,
):
    """Restoring the archive's token_version / session generation as-is would
    revive every staff token and app cookie revoked since the backup was
    taken. Both counters must end up past either side."""
    from wsi_app import app_auth
    from wsi_app.auth import create_token
    from wsi_app.db import SessionLocal
    from wsi_app.models import AuthSettings, User

    _set_session_generation(db_session, 5)
    tv_at_backup = admin_user.token_version
    cookie_at_backup = app_auth.create_session_token("ldap:alice", generation=5)
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    # After the backup: this admin's tokens are revoked (as a password change
    # does) and every app session too.
    user = db_session.get(User, admin_user.id)
    user.token_version = tv_at_backup + 3
    db_session.commit()
    _set_session_generation(db_session, 9)
    current_headers = {"Authorization": f"Bearer {create_token(user)}"}

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=current_headers,
    )
    assert r.status_code == 200, r.text

    db2 = SessionLocal()
    try:
        assert db2.get(User, admin_user.id).token_version == tv_at_backup + 4
        assert db2.get(AuthSettings, 1).session_generation == 10
        restored_user = db2.get(User, admin_user.id)
        new_headers = {"Authorization": f"Bearer {create_token(restored_user)}"}
    finally:
        db2.close()
    # The token revoked after the backup stays dead, and so does the one in use.
    assert client.get("/api/admin/backup", headers=admin_headers).status_code == 401
    assert client.get("/api/admin/backup", headers=current_headers).status_code == 401
    assert client.get("/api/admin/backup", headers=new_headers).status_code == 200
    # The process's session snapshot followed the swapped-in generation.
    assert app_auth.session_generation() == 10
    assert app_auth.decode_session_token(cookie_at_backup) is None


def test_inprocess_restore_of_an_older_schema_archive_is_reconciled(client, admin_headers, admin_user, db_session):
    """A production restore is applied before the service boots, so the
    boot's schema reconciliation adds the columns an older archive lacks. The
    dev in-process swap must do the same, or every slide query 500s with "no
    such column" until the process restarts."""
    from wsi_app.auth import create_token
    from wsi_app.db import SessionLocal
    from wsi_app.models import Slide, User

    db_session.add(Slide(title="Old", catalog_id="OLD-1"))
    db_session.commit()
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    staged = os.path.join(settings.backup_dir, ".staging", stage_id, "wsi.db")
    conn = sqlite3.connect(staged)
    try:
        for col in ("credit_text", "license_id", "source_url"):  # an archive lacking them
            conn.execute(f'ALTER TABLE slides DROP COLUMN "{col}"')
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    db2 = SessionLocal()
    try:
        headers = {"Authorization": f"Bearer {create_token(db2.get(User, admin_user.id))}"}
    finally:
        db2.close()
    r = client.get("/api/admin/slides", headers=headers)
    assert r.status_code == 200, r.text


def _fresh_admin_headers(admin_user) -> dict:
    """A token for ``admin_user`` as the swapped-in database knows it (an
    apply signs every earlier token out)."""
    from wsi_app.auth import create_token
    from wsi_app.db import SessionLocal
    from wsi_app.models import User

    db = SessionLocal()
    try:
        return {"Authorization": f"Bearer {create_token(db.get(User, admin_user.id))}"}
    finally:
        db.close()


def test_dev_reset_reports_its_outcome_in_the_result_file(client, admin_headers, admin_user):
    """The dev in-process reset signs everyone out — the dashboard polling the
    job included — before the job is marked succeeded, so no poll ever reads
    the outcome. Like a production apply, it is reported in the restore result
    under the reset's stage id, for the dashboard to read after signing in
    again."""
    r = client.post("/api/admin/reset", json={"confirm_text": "RESET", "password": "x"},
                    headers=admin_headers)
    assert r.status_code == 202, r.text
    stage_id = r.json()["stage_id"]
    rec = _await_job_record(r.json()["job_id"])
    assert rec["state"] == "succeeded" and rec["result"]["mode"] == "inprocess", rec
    assert client.get(f"/api/admin/backup/jobs/{rec['id']}", headers=admin_headers).status_code == 401
    res = client.get("/api/admin/restore/result", headers=_fresh_admin_headers(admin_user)).json()["result"]
    assert res["ok"] is True and res["stage_id"] == stage_id and res["scope"] == "reset", res
    assert res["applied_at"] and "session_revocation_error" not in res


def test_dev_restore_apply_failure_is_reported_in_the_result_file(client, admin_headers, monkeypatch):
    from wsi_app import restore_apply as _ra

    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]

    def broken(journal, journal_path):
        raise OSError("disk on fire")

    monkeypatch.setattr(_ra, "apply_journal", broken)
    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 500, r.text
    res = client.get("/api/admin/restore/result", headers=admin_headers).json()["result"]
    assert res["ok"] is False and res["stage_id"] == stage_id and res["stage"] == "swap", res
    assert "disk on fire" in res["error"]


def _revoked_and_live_cookies(db_session):
    """Cookies revoked at generations 5..9 and one issued at 9, with the
    process's session snapshot at 9."""
    from wsi_app import app_auth

    _set_session_generation(db_session, 9)
    app_auth.refresh(db_session)
    revoked = app_auth.create_session_token("ldap:mallory", generation=4)
    live = app_auth.create_session_token("ldap:alice", generation=9)
    assert app_auth.decode_session_token(revoked) is None
    assert app_auth.decode_session_token(live) is not None
    return revoked, live


def test_restore_of_an_archive_without_auth_settings_keeps_cookies_revoked(
    client, admin_headers, db_session,
):
    """An install that never saved its authentication settings has no
    auth_settings row to move the generation on: the restore leaves it beside
    the database and the apply's boot steps record it."""
    from wsi_app import app_auth
    from wsi_app import restore_apply as _ra

    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    conn = sqlite3.connect(os.path.join(settings.backup_dir, ".staging", stage_id, "wsi.db"))
    try:
        conn.execute("DELETE FROM auth_settings")
        conn.commit()
    finally:
        conn.close()
    revoked, live = _revoked_and_live_cookies(db_session)

    r = client.post(
        "/api/admin/restore/apply",
        json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert app_auth.session_generation() == 10
    assert app_auth.decode_session_token(revoked) is None
    assert app_auth.decode_session_token(live) is None
    assert not os.path.exists(_backup._live_db_path() + _ra.SESSION_FLOOR_SUFFIX)


def test_a_session_generation_floor_never_lowers_the_generation(db_session):
    from wsi_app import restore_apply as _ra
    from wsi_app.models import AuthSettings

    path = _backup._live_db_path() + _ra.SESSION_FLOOR_SUFFIX
    _set_session_generation(db_session, 12)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"session_generation": 10}, f)
    _backup.apply_session_generation_floor()
    db_session.expire_all()
    assert db_session.get(AuthSettings, 1).session_generation == 12
    assert not os.path.exists(path)


def test_reset_job_reports_a_staging_failure(client, admin_headers, monkeypatch):
    from wsi_app import jobs as _jobs

    def boom(stage_id=None):
        raise OSError("disk on fire")

    monkeypatch.setattr(_backup, "create_reset_stage", boom)
    r = client.post("/api/admin/reset", json={"confirm_text": "RESET", "password": "x"},
                    headers=admin_headers)
    assert r.status_code == 202, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    assert rj["state"] == "failed", rj
    assert "reset staging failed" in rj["error"] and "disk on fire" in rj["error"]
    assert _jobs.store.has_running() is False


def test_reset_job_requests_the_restart_apply_in_production(client, admin_headers, tmp_path):
    """Production: the job builds the stage, writes the restore request, is
    marked succeeded, and only then triggers the restart that applies it."""
    req, trigger = tmp_path / "restore.request", tmp_path / "restart.trigger"
    names = ("backup_dev_inprocess_apply", "restore_request_path",
             "restart_trigger_path", "restore_restart_cmd")
    saved = {n: getattr(settings, n) for n in names}
    object.__setattr__(settings, "backup_dev_inprocess_apply", False)
    object.__setattr__(settings, "restore_request_path", str(req))
    object.__setattr__(settings, "restart_trigger_path", str(trigger))
    object.__setattr__(settings, "restore_restart_cmd", "")
    stage_id = None
    try:
        r = client.post("/api/admin/reset", json={"confirm_text": "RESET", "password": "x"},
                        headers=admin_headers)
        assert r.status_code == 202, r.text
        stage_id = r.json()["stage_id"]
        rj = _await_job(client, admin_headers, r.json()["job_id"])
        assert rj["state"] == "succeeded", rj
        assert rj["result"] == {"mode": "restart", "stage_id": stage_id, "scope": "reset"}
        payload = json.loads(req.read_text())
        assert payload["stage_id"] == stage_id and payload["scope"] == "reset"
        deadline = time.monotonic() + 10
        while not trigger.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert trigger.exists()
        # The pending restart now refuses further jobs.
        busy = client.post("/api/admin/reset", json={"confirm_text": "RESET", "password": "x"},
                           headers=admin_headers)
        assert busy.status_code == 409
    finally:
        for n, v in saved.items():
            object.__setattr__(settings, n, v)
        if stage_id:
            _backup.cleanup_stage(stage_id)


def test_kick_restart_aborts_on_an_unparseable_restart_cmd(tmp_path):
    """One restart mechanism for restore and settings changes: an explicit
    but unparseable RESTORE_RESTART_CMD is a failed kick (the pending restore
    is cleared), not a silent fall back to the trigger file."""
    from wsi_app import jobs as _jobs
    from wsi_app.routers import admin_backup as _admin_backup

    req_path, trigger = tmp_path / "restore.request", tmp_path / "restart.trigger"
    saved = {n: getattr(settings, n) for n in
             ("restore_request_path", "restore_restart_cmd", "restart_trigger_path")}
    object.__setattr__(settings, "restore_request_path", str(req_path))
    object.__setattr__(settings, "restore_restart_cmd", 'systemctl "restart')
    object.__setattr__(settings, "restart_trigger_path", str(trigger))
    try:
        job = _jobs.store.try_claim_running("restore_apply")
        req_path.write_text("{}")
        _jobs.store.update(job.id, state="succeeded")
        _admin_backup._kick_restart(job.id)
        assert not req_path.exists() and not trigger.exists()
        j = _jobs.store.get(job.id)
        assert j.state == "failed" and "unparseable" in (j.error or "")
    finally:
        for n, v in saved.items():
            object.__setattr__(settings, n, v)


def test_restore_ipc_dir_check_is_skipped_in_the_sync_process(monkeypatch, caplog, tmp_path):
    """wsi-sync runs the same app but never applies a restore, and its unit
    grants no write access to /run/wsi: the boot check would warn on every start."""
    from pathlib import Path

    from wsi_app import main as _main

    saved = settings.restore_request_path
    object.__setattr__(settings, "restore_request_path", str(tmp_path / "absent" / "restore.request"))
    try:
        monkeypatch.setenv("WSI_PROCESS_ROLE", "sync")
        with caplog.at_level("WARNING", logger="wsi_app.startup"):
            _main._warn_restore_ipc_dir()
        assert "RESTORE" not in caplog.text
        monkeypatch.delenv("WSI_PROCESS_ROLE")
        with caplog.at_level("WARNING", logger="wsi_app.startup"):
            _main._warn_restore_ipc_dir()
        assert "does not exist" in caplog.text
    finally:
        object.__setattr__(settings, "restore_request_path", saved)
    unit = Path(__file__).resolve().parents[3] / "infra" / "systemd" / "wsi-sync.service"
    assert "\nEnvironment=WSI_PROCESS_ROLE=sync\n" in unit.read_text()


def test_integrity_report_has_no_unused_asset_field():
    report = _backup.IntegrityReport(ok=True, errors=[], missing_wsi_files=[], unexpected_wsi_files=[])
    assert set(report.to_dict()) == {
        "ok", "errors", "warnings", "missing_wsi_files", "unexpected_wsi_files",
    }


def test_full_backup_survives_a_replace_after_the_snapshot_was_pinned(db_session, monkeypatch):
    """Each WSI is hard-linked at snapshot time (after its generation check), so
    a replace landing while the archive is being written — hours in, on a
    large library — does not fail the backup: the archive carries the
    snapshot's bytes with the snapshot's row."""
    _reset_slides(db_session)
    slide_id, wsi_name, thumb_name = _seed_generation_slide(db_session, "GEN-PIN")

    real_pin = _backup._pin_snapshot_wsi_files

    def pin_then_replace(*a, **kw):
        out = real_pin(*a, **kw)
        _same_name_replace(slide_id, wsi_name, thumb_name)
        return out

    monkeypatch.setattr(_backup, "_pin_snapshot_wsi_files", pin_then_replace)
    progress = []
    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="pinned", progress_cb=progress.append)
    try:
        members = _archive_members(rec.path)
        assert members[f"wsi/{wsi_name}"] == b"wsi-bytes"
        assert _archived_slide_width(members, slide_id) != 200
        wsi_ticks = [p for p in progress if p["phase"] == "writing_wsi"]
        assert wsi_ticks and wsi_ticks[0]["wsi_bytes_total"] == len(b"wsi-bytes")
        # The links went with the snapshot dir.
        assert not [
            n for n in os.listdir(os.path.join(settings.backup_dir, ".staging"))
            if n.startswith(_backup._SNAPSHOT_TMPDIR_PREFIX)
        ]
    finally:
        _backup.delete_backup(rec.id)


def test_full_backup_falls_back_when_files_cannot_be_linked(db_session, monkeypatch):
    """wsi/ on another filesystem: no links (EXDEV), and each file is checked
    when it is archived."""
    import errno as _errno

    _reset_slides(db_session)
    _slide_id, wsi_name, _thumb = _seed_generation_slide(db_session, "GEN-NOLINK")

    def no_link(*a, **kw):
        raise OSError(_errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(_backup.os, "link", no_link)
    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="no-link")
    try:
        assert _archive_members(rec.path)[f"wsi/{wsi_name}"] == b"wsi-bytes"
    finally:
        _backup.delete_backup(rec.id)


def test_backup_estimate_is_reused_for_a_while(monkeypatch):
    calls = []
    real = _backup._estimate_backup_components

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(_backup, "_estimate_backup_components", counting)
    first = _backup.estimate_backup_components()
    assert _backup.estimate_backup_components() == first
    assert len(calls) == 1
    _backup.estimate_backup_components(max_age=0)
    assert len(calls) == 2


def test_reapplying_after_an_expired_request_drops_the_stale_report(tmp_path):
    """Claiming the slot for the re-apply writes the expired request's report
    for the same stage_id; the dashboard, waiting for a result naming that
    stage, read it as this apply's failure while the restart applied it. The
    new request removes the old report."""
    from wsi_app.routers import admin_backup as _admin_backup

    stage_id = "stage-" + "e" * 12
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    _backup._mark_stage_complete(stage_dir, stage_id)
    try:
        with _ipc_paths(tmp_path) as (req, res):
            _write_aged_request(req, _restore_apply_max_age() + 60, stage_id)
            _admin_backup._expire_stale_apply_request()
            assert json.loads(res.read_text())["stage"] == "expired"
            _backup.request_apply(stage_id, "metadata", "someone")
            assert req.exists()
            assert not res.exists()
    finally:
        _backup.cleanup_stage(stage_id)


def _restore_apply_max_age() -> int:
    from wsi_app import restore_apply

    return restore_apply.REQUEST_MAX_AGE_SECONDS


def test_a_slide_deleted_after_it_was_pinned_does_not_fail_the_backup(db_session, monkeypatch):
    """Deleted between the pin and the pre-scan (minutes, on a large DB with
    the strip on): the pre-scan must not report it as changed after the
    snapshot, since its link holds the snapshot's bytes."""
    _reset_slides(db_session)
    slide_id, wsi_name, _thumb = _seed_generation_slide(db_session, "GEN-DEL")
    real_pin = _backup._pin_snapshot_wsi_files

    def pin_then_delete(*a, **kw):
        out = real_pin(*a, **kw)
        from wsi_app.admin_helpers import slide_mutation_lock

        with slide_mutation_lock(slide_id):
            conn = sqlite3.connect(_backup._live_db_path())
            try:
                conn.execute("DELETE FROM slides WHERE id = ?", (slide_id,))
                conn.commit()
            finally:
                conn.close()
            os.remove(os.path.join(settings.data_dir, "wsi", wsi_name))
        return out

    monkeypatch.setattr(_backup, "_pin_snapshot_wsi_files", pin_then_delete)
    rec = _backup.create_backup(scope=_backup.SCOPE_FULL, note="pinned-delete")
    try:
        assert _archive_members(rec.path)[f"wsi/{wsi_name}"] == b"wsi-bytes"
    finally:
        _backup.delete_backup(rec.id)
