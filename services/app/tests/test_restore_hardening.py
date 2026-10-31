"""Restore-path hardening.

* The restore / reset password re-auth is throttled per token and audited,
  sharing the account page's re-auth bucket (admin_auth.verify_reauth_password).
* An archive whose schema this binary cannot bring up to date is refused
  before the swap (backup.trial_schema_reconcile, run by the staging job and
  enforced through the plan), not left to crash-loop the
  service after it.
* Only a completely staged stage (backup.STAGE_COMPLETE_MARKER) can be
  previewed or applied.
* A staged tree is normalized to the modes the service creates (0640 / 0750).

The swap-level halves (a database on another mount, an intruder beside the
parked database, a failed sign-out) are in test_restore_apply_journal.py.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile

import pytest
from sqlalchemy import select

from wsi_app import backup as _backup
from wsi_app.config import settings
from wsi_app.models import AuditLog

from .test_backup import _make_backup_via_api, _stage_job, _stage_via_api


@contextlib.contextmanager
def _override_settings(**values):
    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


def _stage_dir(stage_id: str) -> str:
    return os.path.join(settings.backup_dir, ".staging", stage_id)


def _staged(client, admin_headers) -> tuple[str, str]:
    """A metadata snapshot staged for restore: ``(backup_id, stage_id)``."""
    backup_id = _make_backup_via_api(client, admin_headers)
    stage_id = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
    return backup_id, stage_id


# ---------------------------------------------------------------------------
# Password re-auth: throttled and audited
# ---------------------------------------------------------------------------

_NO_STAGE = "stage-000000000000"


def _apply(client, headers, password):
    return client.post(
        "/api/admin/restore/apply",
        json={"stage_id": _NO_STAGE, "confirm_text": "RESTORE", "password": password},
        headers=headers,
    )


def _reset(client, headers, password):
    return client.post(
        "/api/admin/reset", json={"confirm_text": "RESET", "password": password}, headers=headers,
    )


def _reauth_failures(db_session, user_id: int) -> list[AuditLog]:
    db_session.expire_all()
    return db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "admin.account.reauth_failure",
            AuditLog.target == str(user_id),
        )
    ).scalars().all()


def test_restore_apply_password_is_throttled_and_audited(client, admin_headers, admin_user, db_session):
    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        for _ in range(2):
            assert _apply(client, admin_headers, "guess").status_code == 401
        # Below the limit the right password still gets through (to the stage
        # lookup), and forgives the misses before it.
        r = _apply(client, admin_headers, "x")
        assert r.status_code == 404, r.text
        for _ in range(3):
            assert _apply(client, admin_headers, "guess").status_code == 401
        # Exhausted: refused before the password is even checked.
        r = _apply(client, admin_headers, "guess")
        assert r.status_code == 429 and "Retry-After" in r.headers
        assert _apply(client, admin_headers, "x").status_code == 429

    rows = _reauth_failures(db_session, admin_user.id)
    assert len(rows) == 5
    assert all(json.loads(row.details) == {"purpose": "restore.apply"} for row in rows)


def test_reset_password_is_throttled_and_shares_the_tokens_bucket(client, admin_headers, admin_user, db_session):
    """One budget per token across every re-auth: spread over restore, reset
    and the account page, the guesses must not get one budget each."""
    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        assert _apply(client, admin_headers, "guess").status_code == 401
        assert _reset(client, admin_headers, "guess").status_code == 401
        r = client.patch("/api/admin/me", json={"current_password": "guess", "new_password": "whatever-123"},
                         headers=admin_headers)
        assert r.status_code == 400
        assert _reset(client, admin_headers, "x").status_code == 429
        r = client.patch("/api/admin/me", json={"current_password": "x", "username": "renamed"},
                         headers=admin_headers)
        assert r.status_code == 429
    purposes = sorted(json.loads(row.details or "{}").get("purpose", "me")
                      for row in _reauth_failures(db_session, admin_user.id))
    assert purposes == ["me", "reset.apply", "restore.apply"]


# ---------------------------------------------------------------------------
# A schema this binary cannot upgrade is refused before the swap
# ---------------------------------------------------------------------------

def _rollback_dirs() -> set[str]:
    return {n for n in os.listdir(settings.data_dir) if n.startswith(".pre-restore-")}


def _repacked_with_db(backup_id: str, out: str, edit_db) -> str:
    """The snapshot ``backup_id`` repacked (as an upload, no sidecar) with
    its wsi.db changed by ``edit_db(sqlite3.Connection)``."""
    rec = _backup.find_backup(backup_id)
    work = os.path.dirname(out)
    db_copy = os.path.join(work, "edited.db")
    with _backup._open_archive_reader(rec.path) as src, tarfile.open(out, "w:gz") as dst:
        for member in src:
            data = src.extractfile(member) if member.isreg() else None
            if member.name == "wsi.db":
                with open(db_copy, "wb") as f:
                    shutil.copyfileobj(data, f)
                con = sqlite3.connect(db_copy)
                try:
                    edit_db(con)
                    con.commit()
                finally:
                    con.close()
                member.size = os.path.getsize(db_copy)
                data = open(db_copy, "rb")
            dst.addfile(member, data)
    return out


def _add_undroppable_column(con) -> None:
    # A stale column (the reconciler DROPs it) that a view still references:
    # SQLite refuses that DROP COLUMN, so the boot would fail on this DB.
    con.execute("ALTER TABLE slides ADD COLUMN legacy_note TEXT")
    con.execute("CREATE VIEW legacy_notes AS SELECT legacy_note FROM slides")


def test_an_archive_whose_schema_upgrade_fails_is_refused_from_staging_on(
    client, admin_headers, db_session, tmp_path,
):
    """Were the startup schema upgrade to meet the restored database only after
    the swap had committed, a failure there would crash-loop the service on
    the restored data, with the result file saying ok; run on a copy inside
    the apply request, it would need a full DB copy within the proxy's 60 s,
    with no free-space check. The steps run in the staging job, and a failure
    is the plan's SCHEMA_UPGRADE_FAILED refusal: in the staging preview, the
    preflight and the apply (the same 409), the live data untouched."""
    backup_id = _make_backup_via_api(client, admin_headers)
    archive = _repacked_with_db(backup_id, str(tmp_path / "bad.tar.gz"), _add_undroppable_column)
    with open(archive, "rb") as f:
        staged = _stage_via_api(client, admin_headers, files={"file": ("bad.tar.gz", f)})
    stage_id = staged["stage_id"]
    before = _rollback_dirs()
    live_db = _backup._live_db_path()
    live_stamp = os.stat(live_db).st_ino
    try:
        # The staging preview (the job's plan) says so.
        assert staged["plan"]["blocking_code"] == "SCHEMA_UPGRADE_FAILED"
        message = staged["plan"]["blocking_reason"]
        assert "legacy_note" in message and "live data was not touched" in message
        # So does the preflight.
        r = client.get(f"/api/admin/restore/preflight/{stage_id}", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["plan"]["blocking_code"] == "SCHEMA_UPGRADE_FAILED"
        # And the apply refuses it, with its usual detail.
        r = client.post(
            "/api/admin/restore/apply",
            json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"code": "SCHEMA_UPGRADE_FAILED", "message": message}
        # Nothing swapped, nothing requested, nothing left of the trial.
        assert _rollback_dirs() == before
        assert os.stat(live_db).st_ino == live_stamp
        assert not os.path.exists(settings.restore_request_path)
        assert not [n for n in os.listdir(_stage_dir(stage_id)) if n.startswith(".schema-trial-")]
        # The trial ran on a copy: the stage itself is as it was.
        con = sqlite3.connect(os.path.join(_stage_dir(stage_id), "wsi.db"))
        try:
            assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'legacy_notes'").fetchone()[0] == 1
        finally:
            con.close()
        # And the job slot was released.
        _backup.delete_backup(_make_backup_via_api(client, admin_headers))
    finally:
        _backup.cleanup_stage(stage_id)
        _backup.delete_backup(backup_id)


def test_a_staged_archive_records_a_passing_trial_and_the_apply_does_not_rerun_it(
    client, admin_headers, monkeypatch,
):
    backup_id, stage_id = _staged(client, admin_headers)
    try:
        marker = json.load(open(os.path.join(_stage_dir(stage_id), _backup.STAGE_COMPLETE_MARKER)))
        assert marker["schema_trial"] == {"ok": True}
        assert marker["app_version"] == _backup.APP_VERSION
        assert marker["schema_version"] == _backup.CURRENT_SCHEMA_VERSION

        def boom(_stage_id):
            raise AssertionError("the apply must not copy the staged DB again")

        def swap(*_a, **_k):
            raise RuntimeError("reached the swap")

        monkeypatch.setattr(_backup, "trial_schema_reconcile", boom)
        # Both apply paths: stop where the swap would start.
        monkeypatch.setattr(_backup, "inprocess_apply", swap)
        monkeypatch.setattr(_backup, "request_apply", swap)
        r = client.post(
            "/api/admin/restore/apply",
            json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
            headers=admin_headers,
        )
        assert "reached the swap" in r.text, r.text
    finally:
        _backup.cleanup_stage(stage_id)
        _backup.delete_backup(backup_id)


def test_a_stage_another_version_staged_is_refused_as_stale(client, admin_headers):
    """A trial decides for the binary that ran it: after an upgrade between
    staging and apply, the stage must be staged again, not applied on an
    older binary's verdict."""
    backup_id, stage_id = _staged(client, admin_headers)
    path = os.path.join(_stage_dir(stage_id), _backup.STAGE_COMPLETE_MARKER)
    try:
        marker = json.load(open(path))
        marker["app_version"] = "0.0.1"
        _backup._atomic_write_json(path, marker)
        r = client.post(
            "/api/admin/restore/apply",
            json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "STAGE_STALE"
        assert "stage the archive again" in r.json()["detail"]["message"]
        assert not os.path.exists(settings.restore_request_path)
    finally:
        _backup.cleanup_stage(stage_id)
        _backup.delete_backup(backup_id)


def test_no_room_for_the_trial_copy_fails_the_staging(client, admin_headers, monkeypatch):
    from wsi_app import disk_space

    backup_id = _make_backup_via_api(client, admin_headers)
    seen = []

    real = disk_space.check

    def check(path, nbytes, reserve=None):
        if not os.path.basename(path).startswith("stage-"):
            return real(path, nbytes, reserve)  # the upload pre-checks
        seen.append((path, nbytes))
        return disk_space.SpaceShortfall(path=path, needed=nbytes, reserve=1, free=0)

    monkeypatch.setattr(_backup._disk_space, "check", check)
    staging = os.path.join(settings.backup_dir, ".staging")
    try:
        rj = _stage_job(client, admin_headers, data={"snapshot_id": backup_id})
        assert rj["state"] == "failed", rj
        assert "insufficient free space on staging volume" in rj["error"]
        assert "schema upgrade" in rj["error"]
        (path, nbytes), = seen
        assert nbytes > 0 and path.startswith(staging)
        assert not os.path.isdir(path)  # the stage was removed
    finally:
        _backup.delete_backup(backup_id)


def test_trial_schema_reconcile_upgrades_an_older_copy_and_leaves_the_stage_alone(client, admin_headers):
    """An archive whose database lacks a column the schema has passes the trial (the boot will
    ADD it), and the trial changes nothing in the stage."""
    backup_id, stage_id = _staged(client, admin_headers)
    staged_db = os.path.join(_stage_dir(stage_id), "wsi.db")
    con = sqlite3.connect(staged_db)
    try:
        con.execute("ALTER TABLE slides DROP COLUMN vendor")
        con.commit()
    finally:
        con.close()
    try:
        before = open(staged_db, "rb").read()
        _backup.trial_schema_reconcile(stage_id)
        assert open(staged_db, "rb").read() == before
        assert sorted(os.listdir(_stage_dir(stage_id))) == sorted(
            n for n in os.listdir(_stage_dir(stage_id)) if not n.startswith(".schema-trial-")
        )
    finally:
        _backup.cleanup_stage(stage_id)
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# Only a completely staged stage can be previewed or applied
# ---------------------------------------------------------------------------

def test_a_stage_without_its_completion_marker_is_refused(client, admin_headers):
    backup_id, stage_id = _staged(client, admin_headers)
    marker = os.path.join(_stage_dir(stage_id), _backup.STAGE_COMPLETE_MARKER)
    try:
        assert os.path.isfile(marker)  # stage_restore writes it last
        assert client.get(f"/api/admin/restore/preflight/{stage_id}",
                          headers=admin_headers).status_code == 200
        os.remove(marker)  # what a staging job killed mid-extract leaves
        r = client.get(f"/api/admin/restore/preflight/{stage_id}", headers=admin_headers)
        assert r.status_code == 409 and "not completely staged" in r.json()["detail"]
        r = client.post(
            "/api/admin/restore/apply",
            json={"stage_id": stage_id, "confirm_text": "RESTORE", "password": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 409 and "not completely staged" in r.json()["detail"]
        assert not os.path.exists(settings.restore_request_path)
    finally:
        _backup.cleanup_stage(stage_id)
        _backup.delete_backup(backup_id)


def test_a_reset_stage_is_marked_complete():
    stage_id = _backup.create_reset_stage()
    try:
        assert _backup.stage_is_complete(stage_id)
    finally:
        _backup.cleanup_stage(stage_id)


def test_an_archive_cannot_bring_its_own_completion_marker(tmp_path):
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(_backup.STAGE_COMPLETE_MARKER)
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    with tarfile.open(archive) as tar, pytest.raises(RuntimeError, match="reserved name"):
        _backup._safe_extract(tar, str(tmp_path / "out"))
    assert not (tmp_path / "out" / _backup.STAGE_COMPLETE_MARKER).exists()


# ---------------------------------------------------------------------------
# A staged tree gets the modes the service creates
# ---------------------------------------------------------------------------

def test_stage_restore_normalizes_every_staged_mode(client, admin_headers, tmp_path):
    """Archives from installs without UMask=0027 record 0644 files and 0755
    directories — the DB, and every asset / slide / thumbnail too. The swap
    moves the staged tree into DATA_DIR as-is, so all of it is normalized."""
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    repacked = str(tmp_path / "repacked.tar.gz")

    def entry(name, mode, data=None):
        info = tarfile.TarInfo(name)
        info.mode = mode
        if data is None:
            info.type = tarfile.DIRTYPE
            return info, None
        info.size = len(data)
        return info, io.BytesIO(data)

    try:
        with _backup._open_archive_reader(rec.path) as src, tarfile.open(repacked, "w:gz") as dst:
            names = set()
            for member in src:
                names.add(member.name)
                data = src.extractfile(member) if member.isreg() else None
                member.mode = 0o755 if member.isdir() else 0o644
                dst.addfile(member, data)
            for name, mode, data in (
                ("assets/tours/zzmode", 0o755, None),
                ("assets/tours/zzmode/a.png", 0o644, b"png"),
            ):
                assert name not in names
                dst.addfile(*entry(name, mode, data))
        stage_id, _manifest = _backup.stage_restore(repacked)
        try:
            root = _stage_dir(stage_id)
            seen = []
            for path, dirs, files in os.walk(root):
                assert stat.S_IMODE(os.stat(path).st_mode) == 0o750, path
                for name in files:
                    p = os.path.join(path, name)
                    rel = os.path.relpath(p, root)
                    if path == root and name != "wsi.db":
                        continue  # manifest / marker: never promoted
                    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640, p
                    seen.append(rel)
            assert {"wsi.db", "assets/tours/zzmode/a.png"} <= set(seen)
        finally:
            _backup.cleanup_stage(stage_id)
    finally:
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# A snapshot deleted during its staging does not hide the staging's own
# refusal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure, deleted_message", [
    # The archive was gone when stage_restore opened it.
    ("missing", True),
    # A refusal of its own, the snapshot deleted while it ran on the open fd.
    ("refused", False),
    ("no_space", False),
])
def test_a_deleted_snapshot_is_named_only_for_the_failure_it_causes(
    client, admin_headers, monkeypatch, failure, deleted_message,
):
    backup_id = _make_backup_via_api(client, admin_headers)
    path = _backup.find_backup(backup_id).path

    def stage_restore(archive_path, progress_cb=None):
        os.remove(archive_path)
        if failure == "missing":
            try:
                open(archive_path, "rb")
            except FileNotFoundError as exc:
                raise RuntimeError("archive is corrupt or not a supported format") from exc
        if failure == "refused":
            raise RuntimeError("archive's wsi.db carries no app_meta version stamps")
        raise RuntimeError("insufficient free space on staging volume while extracting the archive")

    monkeypatch.setattr(_backup, "stage_restore", stage_restore)
    try:
        rj = _stage_job(client, admin_headers, data={"snapshot_id": backup_id})
        assert rj["state"] == "failed"
        if deleted_message:
            assert rj["error"] == f"backup {backup_id} was deleted before it could be staged"
        else:
            assert "was deleted" not in rj["error"]
            assert ("version stamps" if failure == "refused" else "insufficient free space") in rj["error"]
        assert not os.path.exists(path)
    finally:
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# A killed staging job's leftovers are reclaimed by the next one
# ---------------------------------------------------------------------------

def test_next_staging_reclaims_what_a_killed_one_left(client, admin_headers):
    staging = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(staging, exist_ok=True)
    partial = os.path.join(staging, "stage-deadbeef0001")
    complete = os.path.join(staging, "stage-deadbeef0002")
    upload = os.path.join(staging, "upload-deadbeef0003.tar.zst")
    current = os.path.join(staging, "upload-deadbeef0004.tar.zst")
    for d in (partial, complete):
        os.makedirs(os.path.join(d, "wsi"), exist_ok=True)
    with open(os.path.join(complete, _backup.STAGE_COMPLETE_MARKER), "w") as f:
        f.write("{}")
    for p in (upload, current):
        with open(p, "wb") as f:
            f.write(b"x" * 10)
    try:
        assert _backup._reclaim_abandoned_restore_files(keep=current) == 2
        assert not os.path.exists(partial)
        assert not os.path.exists(upload)
        assert os.path.isdir(complete)
        assert os.path.exists(current)
    finally:
        shutil.rmtree(complete, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.remove(current)


def test_staging_from_a_snapshot_runs_the_reclaim(client, admin_headers):
    staging = os.path.join(settings.backup_dir, ".staging")
    os.makedirs(staging, exist_ok=True)
    partial = os.path.join(staging, "stage-deadbeef0005")
    os.makedirs(partial, exist_ok=True)
    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        job = _stage_job(client, admin_headers, data={"snapshot_id": backup_id})
        assert job["state"] == "succeeded", job
        assert not os.path.exists(partial)
        _backup.cleanup_stage(job["result"]["stage_id"])
    finally:
        _backup.delete_backup(backup_id)


def test_reset_stage_db_is_in_rollback_journal_mode(db_session):
    stage_id = _backup.create_reset_stage()
    try:
        db = os.path.join(_stage_dir(stage_id), "wsi.db")
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        finally:
            conn.close()
        assert not [s for s in ("-wal", "-shm", "-journal") if os.path.exists(db + s)]
    finally:
        _backup.cleanup_stage(stage_id)


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_request_apply_refuses_a_stage_db_with_side_files(tmp_path, suffix):
    stage_id = "stage-" + "d" * 12
    stage_dir = _stage_dir(stage_id)
    os.makedirs(stage_dir, exist_ok=True)
    try:
        _backup._mark_stage_complete(stage_dir, stage_id)
        with open(os.path.join(stage_dir, "wsi.db"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(stage_dir, "wsi.db" + suffix), "wb") as f:
            f.write(b"x")
        with _override_settings(restore_request_path=str(tmp_path / "req"),
                                restore_result_path=str(tmp_path / "res")):
            with pytest.raises(RuntimeError, match="beside it"):
                _backup.request_apply(stage_id, "metadata", "someone")
            assert not (tmp_path / "req").exists()
    finally:
        _backup.cleanup_stage(stage_id)


def _held_from_file(tmp_path, data: bytes):
    from wsi_app.routers import admin_backup as _ab

    src = tmp_path / "spool"
    src.write_bytes(data)
    fd = os.open(str(src), os.O_RDONLY)
    return _ab.HeldUpload(fd=fd, data=None, size=len(data), suffix=".tar.zst")


def test_held_upload_copy_is_refused_up_front_without_room(tmp_path, monkeypatch):
    from wsi_app.routers import admin_backup as _ab

    monkeypatch.setattr(_ab, "_reflink", lambda src, dst: False)
    monkeypatch.setattr(_ab._disk_space, "free_bytes", lambda path: 10)
    os.makedirs(os.path.join(settings.backup_dir, ".staging"), exist_ok=True)
    before = set(os.listdir(os.path.join(settings.backup_dir, ".staging")))
    with pytest.raises(_ab.UploadNoSpace, match="staging volume"):
        _ab.receive_held_upload(_held_from_file(tmp_path, b"x" * 4096))
    assert set(os.listdir(os.path.join(settings.backup_dir, ".staging"))) == before


def test_held_upload_reflink_needs_no_room(tmp_path, monkeypatch):
    from wsi_app.routers import admin_backup as _ab

    def fake_reflink(src, dst):
        os.write(dst, os.pread(src, 1 << 20, 0))
        return True

    monkeypatch.setattr(_ab, "_reflink", fake_reflink)
    monkeypatch.setattr(_ab._disk_space, "free_bytes", lambda path: 10)
    path = _ab.receive_held_upload(_held_from_file(tmp_path, b"y" * 4096))
    try:
        with open(path, "rb") as f:
            assert f.read() == b"y" * 4096
    finally:
        os.remove(path)


def test_held_upload_falls_back_to_a_copy_where_reflink_is_refused(tmp_path):
    """On ext4 / tmpfs FICLONE fails and the bytes are copied."""
    from wsi_app.routers import admin_backup as _ab

    path = _ab.receive_held_upload(_held_from_file(tmp_path, b"z" * 70_000))
    try:
        with open(path, "rb") as f:
            assert f.read() == b"z" * 70_000
    finally:
        os.remove(path)


def test_a_new_staging_removes_the_stage_it_replaces_inside_its_job(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        first = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
        assert os.path.isdir(_stage_dir(first))
        second = _stage_via_api(
            client, admin_headers,
            data={"snapshot_id": backup_id, "replaces_stage_id": first},
        )["stage_id"]
        assert not os.path.exists(_stage_dir(first))
        assert os.path.isdir(_stage_dir(second))
        _backup.cleanup_stage(second)
    finally:
        _backup.delete_backup(backup_id)


def test_a_refused_staging_keeps_the_stage_it_would_replace(client, admin_headers):
    from wsi_app import jobs as _jobs

    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        first = _stage_via_api(client, admin_headers, data={"snapshot_id": backup_id})["stage_id"]
        busy = _jobs.store.try_claim_running("backup")
        try:
            r = client.post("/api/admin/restore", headers=admin_headers,
                            data={"snapshot_id": backup_id, "replaces_stage_id": first})
            assert r.status_code == 409, r.text
        finally:
            _jobs.store.update(busy.id, state="failed", error="test")
        assert os.path.isdir(_stage_dir(first))
        r = client.post("/api/admin/restore", headers=admin_headers,
                        data={"snapshot_id": backup_id, "replaces_stage_id": "../x"})
        assert r.status_code == 400, r.text
        assert os.path.isdir(_stage_dir(first))
        _backup.cleanup_stage(first)
    finally:
        _backup.delete_backup(backup_id)


def test_stage_is_flushed_with_syncfs_before_it_is_marked(tmp_path, monkeypatch):
    calls = []
    real = _backup._sync_filesystem
    monkeypatch.setattr(_backup, "_sync_filesystem", lambda p: (calls.append(p), real(p)))
    d = tmp_path / "stage-0123456789ab"
    d.mkdir()
    _backup._mark_stage_complete(str(d), d.name)
    assert calls == [str(d)]
    assert (d / _backup.STAGE_COMPLETE_MARKER).exists()


def test_a_writeback_error_fails_the_marking(tmp_path, monkeypatch):
    def fail(p):
        raise OSError(5, "syncfs failed: Input/output error", p)

    monkeypatch.setattr(_backup, "_sync_filesystem", fail)
    d = tmp_path / "stage-0123456789ac"
    d.mkdir()
    with pytest.raises(OSError):
        _backup._mark_stage_complete(str(d), d.name)
    assert not (d / _backup.STAGE_COMPLETE_MARKER).exists()
