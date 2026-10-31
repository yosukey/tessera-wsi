"""A tour, or one of its attachments, deleted between the backup's DB
snapshot and the archiving of its assets.

The snapshot fixes which tours the archive describes; their asset dirs are
read from the live disk afterwards, so an author deleting an attachment — or
the whole tour — meanwhile is ordinary. The backup must still succeed, and
an attachment the archived DB may reference but the archive lacks must be
named in the job result rather than only in the log.
"""
from __future__ import annotations

import os
import shutil

from wsi_app import backup as _backup
from wsi_app.admin_helpers import tour_asset_dir
from wsi_app.db import SessionLocal
from wsi_app.models import Tour

from .test_backup import _await_job


def _tour_with_assets(db, slide, names) -> Tour:
    tour = Tour(slide_id=slide.id, title="vanishing")
    db.add(tour)
    db.commit()
    adir = tour_asset_dir(tour.public_id)
    os.makedirs(adir, exist_ok=True)
    for n in names:
        with open(os.path.join(adir, n), "wb") as f:
            f.write(n.encode())
    return tour


def _archived_names(rec_path: str, prefix: str) -> list[str]:
    with _backup._open_archive_reader(rec_path) as tar:
        return sorted(n for n in tar.getnames() if n.startswith(prefix))


def test_an_attachment_deleted_after_the_snapshot_is_a_job_warning(
    client, admin_headers, db_session, slide, monkeypatch,
):
    tour = _tour_with_assets(db_session, slide, ["a.png", "b.png", "c.png"])
    adir = tour_asset_dir(tour.public_id)
    real_scan = _backup._scan_tour_asset_dir

    def scan_then_delete(tour_dir):
        listed = real_scan(tour_dir)
        if os.path.realpath(tour_dir) == os.path.realpath(adir):
            os.remove(os.path.join(adir, "b.png"))  # the author deletes it now
        return listed

    monkeypatch.setattr(_backup, "_scan_tour_asset_dir", scan_then_delete)
    backup_id = None
    try:
        r = client.post("/api/admin/backup", json={"include_wsi": False, "note": ""},
                        headers=admin_headers)
        assert r.status_code == 200, r.text
        rj = _await_job(client, admin_headers, r.json()["job_id"])
        assert rj["state"] == "succeeded", rj
        backup_id = rj["result"]["backup_id"]
        warnings = rj["result"]["warnings"]
        assert any(tour.public_id in w and "b.png" in w and "removed" in w for w in warnings), warnings
        prefix = f"assets/tours/{tour.public_id}/"
        assert _archived_names(_backup.find_backup(backup_id).path, prefix) == [
            prefix + "a.png", prefix + "c.png",
        ]
    finally:
        if backup_id:
            _backup.delete_backup(backup_id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.delete(tour)
        db_session.commit()


def test_a_tour_deleted_after_the_snapshot_does_not_fail_the_backup(
    db_session, slide, monkeypatch,
):
    """The tour row is in the snapshot; its dir is gone by the time the
    assets are archived (the tour delete removes both). The backup succeeds
    and stays restorable — the archived tour simply has no attachments — and
    the job result says so: the tour is no longer in the live DB, which is
    what tells this apart from a tour that never had uploads."""
    tour = _tour_with_assets(db_session, slide, ["a.png"])
    pid, tour_id = tour.public_id, tour.id
    adir = tour_asset_dir(pid)
    real_add_dir = _backup._add_dir_entry

    def delete_tour_first(tar, src_path, arcname):
        if arcname == f"assets/tours/{pid}":
            other = SessionLocal()
            try:
                other.delete(other.get(Tour, tour_id))
                other.commit()
            finally:
                other.close()
            shutil.rmtree(adir)
        return real_add_dir(tar, src_path, arcname)

    # Gone between the isdir check and the dir entry — the narrowest gap;
    # gone before the check is the plain "no dir" path.
    monkeypatch.setattr(_backup, "_add_dir_entry", delete_tour_first)
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="tour-race")
        assert not os.path.isdir(adir)
        assert _archived_names(rec.path, f"assets/tours/{pid}") == []
        assert any(pid in w and "deleted after the DB snapshot" in w
                   for w in rec.warnings), rec.warnings
        # The archive stages, and its integrity check passes.
        stage_id, manifest = _backup.stage_restore(rec.path)
        try:
            report = _backup.validate_archive_integrity(stage_id, manifest)
            assert report.ok, report.to_dict()
        finally:
            _backup.cleanup_stage(stage_id)
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.expire_all()
        leftover = db_session.get(Tour, tour_id)
        if leftover is not None:
            db_session.delete(leftover)
            db_session.commit()


def test_a_tour_deleted_before_its_dir_check_is_a_warning(db_session, slide, monkeypatch):
    """The plain "no dir" path: the tour (row and dir) is gone before the
    asset walk reaches it, not mid-way."""
    tour = _tour_with_assets(db_session, slide, ["a.png"])
    pid, tour_id = tour.public_id, tour.id
    adir = tour_asset_dir(pid)
    real_snapshot = _backup._snapshot_sqlite

    def snapshot_then_delete(src, dst):
        real_snapshot(src, dst)
        other = SessionLocal()
        try:
            other.delete(other.get(Tour, tour_id))
            other.commit()
        finally:
            other.close()
        shutil.rmtree(adir)

    monkeypatch.setattr(_backup, "_snapshot_sqlite", snapshot_then_delete)
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="tour-gone")
        assert any(pid in w and "deleted after the DB snapshot" in w
                   for w in rec.warnings), rec.warnings
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        shutil.rmtree(adir, ignore_errors=True)
        db_session.expire_all()
        leftover = db_session.get(Tour, tour_id)
        if leftover is not None:
            db_session.delete(leftover)
            db_session.commit()


def test_a_live_tour_without_uploads_is_not_a_warning(db_session, slide):
    """No dir, still live, nothing referenced: the ordinary tour without
    attachments, which must stay silent."""
    tour = Tour(slide_id=slide.id, title="plain", description="no images here")
    db_session.add(tour)
    db_session.commit()
    rec = None
    try:
        assert not os.path.isdir(tour_asset_dir(tour.public_id))
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="plain")
        assert not any(tour.public_id in w for w in rec.warnings), rec.warnings
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        db_session.delete(tour)
        db_session.commit()


def test_a_live_tour_referencing_attachments_it_lacks_is_a_warning(db_session, slide):
    """Still live, but its markdown points at its own attachments and the
    folder is missing (removed by hand, lost in a copy): the restored tour's
    images would not load, so the job result says so. A reference into
    another tour's folder does not count for this one."""
    tour = Tour(slide_id=slide.id, title="refs")
    db_session.add(tour)
    db_session.commit()
    other = Tour(slide_id=slide.id, title="other",
                 description=f"![x](/assets/tours/{tour.public_id}/gone.png)")
    tour.description = "![y](/assets/tours/zzzzzzzz/elsewhere.png)"
    db_session.add(other)
    db_session.commit()
    from wsi_app.models import Step
    db_session.add(Step(tour_id=tour.id, order=0,
                        description_md=f"see ![img](/assets/tours/{tour.public_id}/a.png)"))
    db_session.commit()
    rec = None
    try:
        rec = _backup.create_backup(scope=_backup.SCOPE_METADATA, note="refs")
        mine = [w for w in rec.warnings if tour.public_id in w and "references" in w]
        assert len(mine) == 1, rec.warnings
        assert mine[0].startswith(f"tour {tour.public_id}'s"), mine
        # The other tour references *this* tour's files, not its own.
        assert not any(w.startswith(f"tour {other.public_id}") for w in rec.warnings), rec.warnings
    finally:
        if rec is not None:
            _backup.delete_backup(rec.id)
        db_session.delete(other)
        db_session.delete(tour)
        db_session.commit()
