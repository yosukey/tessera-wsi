"""Regression tests for backup creation, listing and deletion: leftovers of a killed run, fail-fast on WSI files no archive can
carry, hard-linked files, FIFOs, archive file mode, the free-space reserve,
warnings on the job result, sidecar rebuild / id, a snapshot deleted under a
restore, and the snapshot DB's journal mode."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tarfile
import threading
import time

import pytest

from wsi_app import backup as _backup
from wsi_app import disk_space
from wsi_app.config import settings

from .test_backup import (
    _archive_members,
    _await_job,
    _make_backup_via_api,
    _reset_slides,
    _seed_slide_with_files,
)


def _published_ids() -> set:
    return {r.id for r in _backup.list_backups()}


def _no_partial_archives() -> bool:
    return not [n for n in os.listdir(settings.backup_dir) if n.endswith(".tmp")]


def _run_bounded(fn, timeout: float = 20.0):
    """Run ``fn`` on a thread and return (finished, result-or-exception): a
    FIFO opened without O_NONBLOCK blocks forever, and the test must report
    that instead of hanging the suite."""
    out: dict = {}

    def target():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), out, t


def _unblock_fifo(path: str) -> None:
    """Open a FIFO for writing so a reader blocked in open(2) wakes up."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# A killed run's partial archive and snapshot dir
# ---------------------------------------------------------------------------

def test_create_backup_reclaims_what_a_killed_backup_left_behind(db_session):
    """Fresh-mtime leftovers too: the job slot means none is a live backup's,
    and leaving them would make the preflight count them against this run."""
    os.makedirs(os.path.join(settings.backup_dir, ".staging"), exist_ok=True)
    partial_zst = os.path.join(settings.backup_dir, "20260101T000000Z-dead0001.tar.zst.tmp")
    partial_gz = os.path.join(settings.backup_dir, "20260101T000000Z-dead0002.tar.gz.tmp")
    snapdir = os.path.join(settings.backup_dir, ".staging", "wsi_backup_killed123")
    # Not create_backup's: an export staged under .staging, and an operator file.
    export_tmp = os.path.join(settings.backup_dir, ".staging", "export-0123456789ab.tar.zst.tmp")
    operator_file = os.path.join(settings.backup_dir, "notes.tmp.txt")
    for p in (partial_zst, partial_gz, export_tmp, operator_file):
        with open(p, "wb") as f:
            f.write(b"x" * 100)
    os.makedirs(snapdir)
    with open(os.path.join(snapdir, "wsi.db"), "wb") as f:
        f.write(b"partial snapshot")
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
        assert not os.path.exists(partial_zst)
        assert not os.path.exists(partial_gz)
        assert not os.path.exists(snapdir)
        assert os.path.exists(export_tmp)
        assert os.path.exists(operator_file)
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        for p in (partial_zst, partial_gz, export_tmp, operator_file):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(snapdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fail before the archive is written, with the real reason
# ---------------------------------------------------------------------------

def _forbid_archive_writer(monkeypatch) -> list:
    opened: list = []
    real = _backup._open_archive_writer

    def spy(path):
        opened.append(path)
        return real(path)

    monkeypatch.setattr(_backup, "_open_archive_writer", spy)
    return opened


def test_full_backup_with_an_absent_wsi_fails_before_writing_the_archive(
    db_session, monkeypatch,
):
    _reset_slides(db_session)
    _seed_slide_with_files(db_session, catalog_id="FAST-OK")
    lost, _w, _t = _seed_slide_with_files(db_session, catalog_id="FAST-LOST")
    os.remove(os.path.join(settings.data_dir, "wsi", lost.wsi_filename))
    opened = _forbid_archive_writer(monkeypatch)
    before = _published_ids()
    with pytest.raises(RuntimeError) as ei:
        _backup.create_backup(scope=_backup.SCOPE_FULL)
    assert "not on disk" in str(ei.value)
    assert lost.wsi_filename in str(ei.value)
    assert "concurrent" not in str(ei.value)
    assert opened == [], "the archive must not be written for a backup that cannot succeed"
    assert _published_ids() == before
    assert _no_partial_archives()


def test_full_backup_with_a_symlinked_wsi_says_so_instead_of_retry(db_session, monkeypatch):
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="FAST-LINK")
    path = os.path.join(settings.data_dir, "wsi", wsi_name)
    target = os.path.join(settings.data_dir, "elsewhere.svs")
    os.replace(path, target)
    os.symlink(target, path)
    opened = _forbid_archive_writer(monkeypatch)
    try:
        with pytest.raises(RuntimeError) as ei:
            _backup.create_backup(scope=_backup.SCOPE_FULL)
        msg = str(ei.value)
        assert "symlink" in msg and wsi_name in msg
        assert "concurrent" not in msg and "Retry the backup" not in msg
        assert opened == []
    finally:
        os.remove(path)
        os.replace(target, path)


def test_symlink_swapped_in_mid_run_is_reported_as_a_symlink(db_session, monkeypatch):
    """The in-loop checks still catch what changes after the pre-scan, and the
    final error names that reason rather than guessing at a race."""
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="LATE-LINK")
    path = os.path.join(settings.data_dir, "wsi", wsi_name)
    target = os.path.join(settings.data_dir, "late-elsewhere.svs")
    real_reason = _backup._wsi_unarchivable_reason

    def reason_then_swap(*a, **kw):
        r = real_reason(*a, **kw)
        os.replace(path, target)
        os.symlink(target, path)
        return r

    monkeypatch.setattr(_backup, "_wsi_unarchivable_reason", reason_then_swap)
    # Where no links can be made (the per-file path these checks guard); a
    # pinned file is archived from its link, whatever the name became.
    monkeypatch.setattr(_backup, "_pin_snapshot_wsi_files", lambda *a, **kw: ({}, 0))
    try:
        with pytest.raises(RuntimeError) as ei:
            _backup.create_backup(scope=_backup.SCOPE_FULL)
        assert "symlink" in str(ei.value)
        assert "concurrent" not in str(ei.value)
        assert _no_partial_archives()
    finally:
        os.remove(path)
        os.replace(target, path)


@pytest.mark.parametrize("name", [".hidden.svs", "sub/dir.svs"])
def test_backup_refuses_a_wsi_name_restore_would_refuse(db_session, name):
    """validate_archive_integrity refuses an archive whose DB lists such a
    name, in either scope; create must not report success for it."""
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="UNSAFE-1")
    s.wsi_filename = name
    db_session.commit()
    before = _published_ids()
    try:
        for scope in (_backup.SCOPE_METADATA, _backup.SCOPE_FULL):
            with pytest.raises(RuntimeError, match="restore refuses|escapes the wsi root|not on disk"):
                _backup.create_backup(scope=scope)
        assert _published_ids() == before
    finally:
        s.wsi_filename = wsi_name
        db_session.commit()


def test_create_and_restore_share_the_wsi_name_rule():
    for ok in ("12_scan.svs", "a..b.ndpi", "x"):
        assert _backup._is_safe_wsi_filename(ok)
    for bad in (".x", "..", "../x", "a/b", "/abs"):
        assert not _backup._is_safe_wsi_filename(bad)


# ---------------------------------------------------------------------------
# Hard-linked files are archived as regular files with their bytes
# ---------------------------------------------------------------------------

def test_hard_linked_wsi_and_tour_assets_are_all_archived(db_session):
    from wsi_app.admin_helpers import tour_asset_dir
    from wsi_app.models import Slide, Tour

    _reset_slides(db_session)
    a, a_wsi, _t = _seed_slide_with_files(db_session, catalog_id="HL-A")
    b = Slide(title="hard link", catalog_id="HL-B")
    db_session.add(b)
    db_session.commit()
    b_wsi = f"{b.id}_same.svs"
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.link(os.path.join(wsi_dir, a_wsi), os.path.join(wsi_dir, b_wsi))
    b.wsi_filename = b_wsi
    db_session.commit()

    tour = Tour(slide_id=a.id, title="hl assets")
    db_session.add(tour)
    db_session.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "a.png"), "wb") as f:
        f.write(b"PNG-bytes")
    os.link(os.path.join(adir, "a.png"), os.path.join(adir, "b.png"))
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_FULL)
        with _backup._open_archive_reader(rec.path) as tar:
            types = {m.name: m.type for m in tar}
        members = _archive_members(rec.path)
        prefix = f"assets/tours/{tour.public_id}/"
        for name in (f"wsi/{a_wsi}", f"wsi/{b_wsi}", prefix + "a.png", prefix + "b.png"):
            assert types.get(name) == tarfile.REGTYPE, (name, types)
        assert members[f"wsi/{a_wsi}"] == members[f"wsi/{b_wsi}"] == b"wsi-bytes"
        assert members[prefix + "a.png"] == members[prefix + "b.png"] == b"PNG-bytes"
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


# ---------------------------------------------------------------------------
# A FIFO never blocks the backup (or the slide lock)
# ---------------------------------------------------------------------------

def _seed_fifo_wsi(db_session, catalog_id):
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id=catalog_id)
    path = os.path.join(settings.data_dir, "wsi", wsi_name)
    os.remove(path)
    os.mkfifo(path)
    return s, path


def test_fifo_wsi_fails_fast_at_the_prescan(db_session):
    s, path = _seed_fifo_wsi(db_session, "FIFO-1")
    try:
        done, out, _t = _run_bounded(lambda: _backup.create_backup(scope=_backup.SCOPE_FULL))
        if not done:
            _unblock_fifo(path)
        assert done, "backup blocked on a FIFO"
        assert isinstance(out.get("exc"), RuntimeError), out
        assert "not a regular file" in str(out["exc"])
    finally:
        os.remove(path)


def test_fifo_appearing_after_the_prescan_does_not_block_the_open(db_session, monkeypatch):
    """The archive loop's open is non-blocking: a FIFO there is reported as a
    non-regular file instead of hanging with the slide's mutation lock held."""
    s, path = _seed_fifo_wsi(db_session, "FIFO-2")
    monkeypatch.setattr(_backup, "_wsi_unarchivable_reason", lambda *a, **kw: None)
    try:
        done, out, _t = _run_bounded(lambda: _backup.create_backup(scope=_backup.SCOPE_FULL))
        if not done:
            _unblock_fifo(path)
        assert done, "backup blocked opening a FIFO"
        assert isinstance(out.get("exc"), RuntimeError), out
        assert "not a regular file" in str(out["exc"])
        assert "concurrent" not in str(out["exc"])
        assert _no_partial_archives()
    finally:
        os.remove(path)


def test_fifo_thumbnail_is_left_out_with_a_warning(db_session):
    _reset_slides(db_session)
    s, _w, thumb_name = _seed_slide_with_files(db_session, catalog_id="FIFO-T")
    tpath = os.path.join(settings.data_dir, "thumbs", thumb_name)
    os.remove(tpath)
    os.mkfifo(tpath)
    rec = None
    try:
        done, out, _t = _run_bounded(lambda: _backup.create_backup(scope=_backup.SCOPE_FULL))
        if not done:
            _unblock_fifo(tpath)
        assert done, "backup blocked on a FIFO thumbnail"
        rec = out.get("result")
        assert rec is not None, out
        assert any(thumb_name in w and "not a regular file" in w for w in rec.warnings), rec.warnings
        assert f"thumbs/{thumb_name}" not in _archive_members(rec.path)
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        os.remove(tpath)
        with open(tpath, "wb") as f:
            f.write(b"thumb-bytes")


# ---------------------------------------------------------------------------
# The archive is never world-readable whatever the umask (0640: owner
# read-write and the service group read, as UMask=0027 gives it)
# ---------------------------------------------------------------------------

def test_backup_archive_is_0640_under_a_permissive_umask(db_session):
    old = os.umask(0o022)
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(os.stat(rec.path).st_mode) == 0o640
    finally:
        _backup.delete_backup(rec.id)


def test_archive_writer_tightens_a_preexisting_file(tmp_path):
    path = str(tmp_path / "x.tar.zst.tmp")
    with open(path, "wb") as f:
        f.write(b"old")
    os.chmod(path, 0o644)
    raw, tar, _counter, compressor = _backup._open_archive_writer(path)
    tar.close()
    compressor.close()
    raw.close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_archive_writer_keeps_group_read_under_an_owner_only_umask(tmp_path):
    """The mode is fixed, not umask-derived, in both directions: a 077 umask
    must not strip the group read the wsi group relies on."""
    path = str(tmp_path / "y.tar.zst.tmp")
    old = os.umask(0o077)
    try:
        raw, tar, _counter, compressor = _backup._open_archive_writer(path)
        tar.close()
        compressor.close()
        raw.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


# ---------------------------------------------------------------------------
# The preflight keeps a reserve on the (data) volume
# ---------------------------------------------------------------------------

def test_backup_preflight_keeps_a_fixed_reserve(monkeypatch, db_session):
    """A tiny install's estimate is a few hundred KB; 1.1x of it fits in
    almost nothing, which would leave no room for the live DB beside the archive."""
    class _Usage:
        total = 10 * 1024 ** 3
        free = disk_space.RESERVE_MIN_BYTES - 1
        used = total - free

    monkeypatch.setattr(_backup.shutil, "disk_usage", lambda _p: _Usage)
    with pytest.raises(RuntimeError, match="insufficient free space on backup volume"):
        _backup.create_backup(scope=_backup.SCOPE_METADATA)


# ---------------------------------------------------------------------------
# What a published archive left out reaches the job result
# ---------------------------------------------------------------------------

def test_backup_job_result_carries_warnings(client, admin_headers, db_session):
    _reset_slides(db_session)
    s, _w, thumb_name = _seed_slide_with_files(db_session, catalog_id="WARN-1")
    os.remove(os.path.join(settings.data_dir, "thumbs", thumb_name))
    r = client.post(
        "/api/admin/backup", json={"include_wsi": True, "note": ""}, headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    assert rj["state"] == "succeeded", rj
    warnings = rj["result"]["warnings"]
    assert any(thumb_name in w for w in warnings), warnings
    # The listing row describes the archive and is unchanged in shape.
    row = next(it for it in client.get("/api/admin/backup", headers=admin_headers).json()["items"]
               if it["id"] == rj["result"]["backup_id"])
    assert "warnings" not in row
    _backup.delete_backup(rj["result"]["backup_id"])


def test_clean_backup_job_has_an_empty_warning_list(client, admin_headers, db_session):
    _reset_slides(db_session)
    r = client.post(
        "/api/admin/backup", json={"include_wsi": False, "note": ""}, headers=admin_headers,
    )
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["warnings"] == []
    _backup.delete_backup(rj["result"]["backup_id"])


def test_tour_asset_left_out_is_a_warning_and_the_list_is_capped(db_session, slide):
    from wsi_app.admin_helpers import tour_asset_dir
    from wsi_app.models import Tour

    tour = Tour(slide_id=slide.id, title="left out")
    db_session.add(tour)
    db_session.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    n = _backup._MAX_BACKUP_WARNINGS + 5
    for i in range(n):
        with open(os.path.join(adir, f".dot{i:03d}"), "wb") as f:
            f.write(b"x")
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
        assert len(rec.warnings) == _backup._MAX_BACKUP_WARNINGS + 1
        assert ".dot000" in rec.warnings[0]
        assert "5 more" in rec.warnings[-1]
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


# ---------------------------------------------------------------------------
# Sidecars are rebuilt from the manifest; the id is the filename's
# ---------------------------------------------------------------------------

def test_missing_sidecar_is_rebuilt_from_the_manifest(client, admin_headers, db_session):
    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="rebuild me",
                                include_history=False)
    sidecar = rec.path + ".meta.json"
    os.remove(sidecar)
    try:
        row = next(r for r in _backup.list_backups() if r.id == rec.id)
        assert row.scope == "metadata"
        assert row.note == "rebuild me"
        assert row.app_version == rec.app_version
        assert row.created_at == rec.created_at
        assert row.includes_history is False
        assert row.schema_version == rec.schema_version
        assert row.sha256 == ""  # never re-hashed from the archive
        # Persisted, so the next listing reads it rather than the archive.
        with open(sidecar, encoding="utf-8") as f:
            assert json.load(f)["note"] == "rebuild me"
        again = _backup.find_backup(rec.id)
        assert again is not None and again.scope == "metadata"
    finally:
        _backup.delete_backup(rec.id)


def test_corrupt_sidecar_is_replaced_and_find_backup_rebuilds_too(db_session):
    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="corrupt")
    sidecar = rec.path + ".meta.json"
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write("{not json")
    try:
        found = _backup.find_backup(rec.id)
        assert found is not None and found.note == "corrupt" and found.scope == "metadata"
        with open(sidecar, encoding="utf-8") as f:
            assert json.load(f)["scope"] == "metadata"
    finally:
        _backup.delete_backup(rec.id)


def test_rebuild_never_overwrites_a_sidecar_that_appeared_meanwhile(tmp_path):
    """A listing racing create_backup's own sidecar write must not replace the
    real one (sha256 included) with its sha-less rebuild."""
    sidecar = str(tmp_path / "x.tar.zst.meta.json")
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump({"sha256": "a" * 64}, f)
    _backup._persist_rebuilt_sidecar(sidecar, {"sha256": ""}, replace=False)
    with open(sidecar, encoding="utf-8") as f:
        assert json.load(f)["sha256"] == "a" * 64
    assert os.listdir(tmp_path) == ["x.tar.zst.meta.json"]


def test_unreadable_archive_without_sidecar_is_a_stub_row(db_session):
    path = os.path.join(settings.backup_dir, "20260101T000000Z-garbage1.tar.zst")
    with open(path, "wb") as f:
        f.write(b"not an archive at all" * 10)
    try:
        row = next(r for r in _backup.list_backups() if r.id == "20260101T000000Z-garbage1")
        assert row.scope == "?"
        assert not os.path.exists(path + ".meta.json")
    finally:
        os.remove(path)


def test_record_id_comes_from_the_filename_not_the_sidecar(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    with open(rec.path + ".meta.json", encoding="utf-8") as f:
        data = json.load(f)
    data["id"] = "something-else"
    with open(rec.path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert any(r.id == backup_id for r in _backup.list_backups())
    assert not any(r.id == "something-else" for r in _backup.list_backups())
    assert _backup.find_backup(backup_id).id == backup_id
    r = client.post(f"/api/admin/backup/{backup_id}/download_url", headers=admin_headers)
    assert r.status_code == 200
    assert client.delete(f"/api/admin/backup/{backup_id}", headers=admin_headers).status_code == 200


# ---------------------------------------------------------------------------
# A snapshot deleted under a restore; orphan sidecars
# ---------------------------------------------------------------------------

def test_staging_a_snapshot_deleted_meanwhile_says_it_was_deleted(
    client, admin_headers, monkeypatch,
):
    backup_id = _make_backup_via_api(client, admin_headers)
    real_find = _backup.find_backup

    def find_then_delete(bid):
        rec = real_find(bid)
        if rec is not None:
            # A DELETE racing the stage request, landing after its lookup.
            os.remove(rec.path + ".meta.json")
            os.remove(rec.path)
        return rec

    monkeypatch.setattr(_backup, "find_backup", find_then_delete)
    r = client.post("/api/admin/restore", headers=admin_headers, data={"snapshot_id": backup_id})
    assert r.status_code == 202, r.text
    rj = _await_job(client, admin_headers, r.json()["job_id"])
    assert rj["state"] == "failed"
    assert "was deleted" in rj["error"]
    assert "corrupt" not in rj["error"]


def test_delete_backup_removes_the_sidecar_first(client, admin_headers, monkeypatch):
    backup_id = _make_backup_via_api(client, admin_headers)
    rec = _backup.find_backup(backup_id)
    order: list = []
    real_remove = os.remove

    def recording_remove(p, *a, **kw):
        order.append(os.path.basename(p))
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(_backup.os, "remove", recording_remove)
    assert _backup.delete_backup(backup_id)
    name = os.path.basename(rec.path)
    assert order[:2] == [name + ".meta.json", name]
    assert not os.path.exists(rec.path) and not os.path.exists(rec.path + ".meta.json")


def test_sweep_removes_orphan_sidecars_only(client, admin_headers):
    backup_id = _make_backup_via_api(client, admin_headers)
    live = _backup.find_backup(backup_id)
    orphan = os.path.join(settings.backup_dir, "20200101T000000Z-0rphan00.tar.zst.meta.json")
    other = os.path.join(settings.backup_dir, "operator-notes.meta.json")
    for p in (orphan, other):
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
    old = time.time() - 25 * 3600
    for p in (orphan, other, live.path + ".meta.json"):
        os.utime(p, (old, old))
    try:
        _backup.sweep_stale_staging()
        assert not os.path.exists(orphan)
        assert os.path.exists(other)
        assert os.path.exists(live.path + ".meta.json")
    finally:
        for p in (orphan, other):
            if os.path.exists(p):
                os.remove(p)
        _backup.delete_backup(backup_id)


# ---------------------------------------------------------------------------
# The archived snapshot is not a WAL database
# ---------------------------------------------------------------------------

def test_archived_snapshot_is_in_rollback_journal_mode_with_its_stamps(db_session, tmp_path):
    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
    try:
        db_bytes = _archive_members(rec.path)["wsi.db"]
    finally:
        _backup.delete_backup(rec.id)
    # Header bytes 18/19 are 2 for a WAL database, 1 for rollback-journal.
    assert db_bytes[18:20] == b"\x01\x01"
    db = tmp_path / "wsi.db"
    db.write_bytes(db_bytes)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        stamps = dict(conn.execute("SELECT key, value FROM app_meta").fetchall())
    finally:
        conn.close()
    assert stamps.get("schema_version")


def test_backup_refuses_to_archive_a_snapshot_with_a_wal(db_session, monkeypatch):
    """Belt and braces for the journal-mode switch: a WAL file beside the
    snapshot when it is archived (here planted after the last read of it)
    fails the backup instead of shipping wsi.db without the WAL's writes."""
    real_enumerate = _backup._enumerate_snapshot

    def enumerate_then_leave_a_wal(path):
        out = real_enumerate(path)
        with open(path + "-wal", "wb") as f:
            f.write(b"\0" * 32)
        return out

    monkeypatch.setattr(_backup, "_enumerate_snapshot", enumerate_then_leave_a_wal)
    before = _published_ids()
    with pytest.raises(RuntimeError, match="write-ahead log"):
        _backup.create_backup(scope=_backup.SCOPE_METADATA)
    assert _published_ids() == before
    assert _no_partial_archives()


def test_the_sidecar_with_the_sha256_exists_before_the_archive_is_published(db_session, monkeypatch):
    """Written after the publish, a crash in between would leave an archive
    whose rebuilt sidecar had no sha256, so no later restore from it would be
    verified."""
    real_replace = os.replace
    seen = {}

    def spy(src, dst, *a, **kw):
        if str(src).endswith(".tmp") and not str(dst).endswith(".json"):
            sidecar = str(dst) + ".meta.json"
            if os.path.exists(sidecar):
                with open(sidecar, encoding="utf-8") as f:
                    seen["sha256"] = json.load(f)["sha256"]
            else:
                seen["sha256"] = None
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
    monkeypatch.setattr(os, "replace", real_replace)
    try:
        assert seen["sha256"] == rec.sha256 and len(rec.sha256) == 64
    finally:
        _backup.delete_backup(rec.id)


def test_strip_compacts_into_a_sibling_and_leaves_nothing_behind(tmp_path):
    """The opted-out tables are emptied and the file shrinks through VACUUM
    INTO a sibling (no temp copy in TMPDIR, no rollback journal), swapped in
    with the header values kept."""
    db = str(tmp_path / "wsi.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=42")
    conn.execute("CREATE TABLE page_views (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.execute("CREATE TABLE keep (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO page_views (blob) VALUES (?)", [("x" * 4000,)] * 500)
    conn.execute("INSERT INTO keep DEFAULT VALUES")
    conn.commit()
    conn.close()
    before = os.path.getsize(db)

    _backup._strip_tables_in_place(db, ["page_views", "not_there"])

    assert os.path.getsize(db) < before / 4
    assert sorted(os.listdir(tmp_path)) == ["wsi.db"]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM page_views").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM keep").fetchone() == (1,)
        assert conn.execute("PRAGMA user_version").fetchone() == (42,)
    finally:
        conn.close()



def test_a_pinned_wsi_is_archived_whatever_its_name_became(db_session, monkeypatch):
    """Linked right after the snapshot, the file is archived from the link:
    a symlink swapped in at its name afterwards does not matter."""
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="PIN-LINK")
    path = os.path.join(settings.data_dir, "wsi", wsi_name)
    target = os.path.join(settings.data_dir, "pin-elsewhere.svs")
    real_pin = _backup._pin_snapshot_wsi_files

    def pin_then_swap(*a, **kw):
        out = real_pin(*a, **kw)
        os.replace(path, target)
        os.symlink(target, path)
        return out

    monkeypatch.setattr(_backup, "_pin_snapshot_wsi_files", pin_then_swap)
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_FULL)
        try:
            with _backup._open_archive_reader(rec.path) as tar:
                names = {m.name: m for m in tar if m.isreg()}
            assert f"wsi/{wsi_name}" in names
        finally:
            _backup.delete_backup(rec.id)
    finally:
        os.remove(path)
        os.replace(target, path)


def test_a_sidecar_whose_dir_fsync_failed_is_not_reported_missing(db_session, monkeypatch):
    real = _backup._atomic_write_json

    def write_then_fail(path, payload):
        real(path, payload)
        if path.endswith(".meta.json"):
            raise OSError(5, "fsync failed")

    monkeypatch.setattr(_backup, "_atomic_write_json", write_then_fail)
    rec = _backup.create_backup(scope=_backup.SCOPE_METADATA)
    try:
        assert not [w for w in rec.warning_items if w.get("code") == "sidecarWriteFailed"]
        with open(rec.path + ".meta.json", encoding="utf-8") as f:
            assert json.load(f)["sha256"] == rec.sha256
    finally:
        _backup.delete_backup(rec.id)


def test_a_rollback_tree_that_cannot_be_fully_removed_is_logged(tmp_path, monkeypatch, caplog):
    tree = tmp_path / ".rollback-trash-0123456789ab"
    (tree / "wsi").mkdir(parents=True)
    real = shutil.rmtree

    def stubborn(path, *a, onexc=None, **kw):
        onexc(os.unlink, str(tree / "wsi" / "x"), PermissionError(1, "Operation not permitted"))

    monkeypatch.setattr(_backup.shutil, "rmtree", stubborn)
    with caplog.at_level("WARNING", logger="wsi_app.backup"):
        assert _backup._rmtree_logged(str(tree), tree.name) is False
    assert "could not fully remove" in caplog.text
    monkeypatch.setattr(_backup.shutil, "rmtree", real)


def test_a_real_file_under_a_name_restore_refuses_is_still_refused(db_session):
    """Pinned files skip the pre-scan, so a file restore would refuse by its
    name (here a leading dot) must never be pinned."""
    _reset_slides(db_session)
    s, wsi_name, _t = _seed_slide_with_files(db_session, catalog_id="DOT-1")
    hidden = ".hidden.svs"
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.replace(os.path.join(wsi_dir, wsi_name), os.path.join(wsi_dir, hidden))
    s.wsi_filename = hidden
    db_session.commit()
    before = _published_ids()
    try:
        with pytest.raises(RuntimeError, match="restore refuses"):
            _backup.create_backup(scope=_backup.SCOPE_FULL)
        assert _published_ids() == before
    finally:
        os.remove(os.path.join(wsi_dir, hidden))
        _reset_slides(db_session)
