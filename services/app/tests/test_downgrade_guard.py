"""Regression test for the pre-migration downgrade guard.

_apply_column_migrations runs destructive DROP COLUMN for columns the running
ORM does not declare. If an older binary boots against a newer DB, that would
delete the newer release's data and commit BEFORE bootstrap_app_version's own
downgrade check runs. assert_not_downgrade must run first (inside the startup
schema lock, before create_all/_apply_column_migrations) and abort while the
data is still intact.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from wsi_app import app_meta, version
from wsi_app.version import APP_VERSION


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path}/dg.db")


def test_missing_table_reads_none(tmp_path):
    """A fresh DB (no app_meta table yet) must not raise — the guard fails open
    so a genuine fresh install boots and create_all builds the table."""
    eng = _engine(tmp_path)
    assert app_meta.read_recorded_app_version(eng) is None
    # Must not raise.
    app_meta.assert_not_downgrade(eng)


def test_newer_recorded_version_blocks_boot(tmp_path):
    eng = _engine(tmp_path)
    # Stamp a version far newer than the running binary.
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_APP_VERSION, "999.0.0"),
        )
    assert app_meta.read_recorded_app_version(eng) == "999.0.0"
    with pytest.raises(RuntimeError, match="Downgrades are not supported"):
        app_meta.assert_not_downgrade(eng)


def test_same_or_older_recorded_version_boots(tmp_path):
    eng = _engine(tmp_path)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_APP_VERSION, "0.0.1"),
        )
    # Older recorded version → an upgrade, not a downgrade → must not raise.
    app_meta.assert_not_downgrade(eng)
    # The running binary's own version must also never be treated as a downgrade.
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE app_meta SET value=? WHERE key=?",
            (APP_VERSION, app_meta.KEY_APP_VERSION),
        )
    app_meta.assert_not_downgrade(eng)


# ---------------------------------------------------------------------------
# Schema-precise downgrade guard (integer schema_version)
# ---------------------------------------------------------------------------

def test_schema_missing_key_fails_open(tmp_path):
    """No app_meta table / no schema_version key = unknown → must not raise
    (a fresh or hand-made DB still needs to boot)."""
    eng = _engine(tmp_path)
    assert app_meta.read_recorded_schema_version(eng) is None
    app_meta.assert_schema_not_downgrade(eng, current=5)


def test_newer_recorded_schema_blocks_boot(tmp_path):
    eng = _engine(tmp_path)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, "99"),
        )
    assert app_meta.read_recorded_schema_version(eng) == 99
    with pytest.raises(RuntimeError, match="Downgrades are not supported"):
        app_meta.assert_schema_not_downgrade(eng, current=3)


def test_same_or_older_recorded_schema_boots(tmp_path):
    eng = _engine(tmp_path)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, "2"),
        )
    # Older recorded schema (2 < 5) → forward upgrade → must not raise.
    app_meta.assert_schema_not_downgrade(eng, current=5)
    # Equal is not a downgrade.
    app_meta.assert_schema_not_downgrade(eng, current=2)


# ---------------------------------------------------------------------------
# A running version from the frontend/package.json fallback is not trusted
# ---------------------------------------------------------------------------
# A release archive deployed without a VERSION stamp reports
# frontend/package.json, which releases (git tags) do not bump, so it can
# report a version far older than the DB's stamp. Trusting it, such a deploy
# would refuse to boot against its own DB and refuse to restore its own
# backups.

def _stamped(tmp_path, recorded):
    eng = _engine(tmp_path)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_APP_VERSION, recorded),
        )
    return eng


def test_fallback_version_skips_boot_guard_with_warning(tmp_path, monkeypatch, caplog):
    eng = _stamped(tmp_path, "0.9.240-3-gabc1234")
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_PACKAGE_JSON)
    with caplog.at_level("WARNING", logger="wsi_app.app_meta"):
        app_meta.assert_not_downgrade(eng)  # must not raise
    assert "downgrade guard skipped for boot" in caplog.text
    assert "0.9.240-3-gabc1234" in caplog.text


def test_authoritative_version_still_blocks(tmp_path, monkeypatch):
    eng = _stamped(tmp_path, "0.9.240-3-gabc1234")
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.1.0")
    for source in (version.SOURCE_VERSION_FILE, version.SOURCE_GIT):
        monkeypatch.setattr(version, "APP_VERSION_SOURCE", source)
        with pytest.raises(RuntimeError, match="Downgrades are not supported"):
            app_meta.assert_not_downgrade(eng)


def test_fallback_bootstrap_keeps_the_newer_stamp(tmp_path, monkeypatch):
    """bootstrap_app_version must neither raise nor overwrite the newer stamp
    with the untrusted lower value (that would disarm the guard for the next
    build that does know its version)."""
    from wsi_app.models import Base

    eng = _engine(tmp_path)
    Base.metadata.create_all(eng)
    db_file = f"{tmp_path}/dg.db"
    app_meta.sqlite_set_app_version(db_file, "0.9.240-3-gabc1234")
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_PACKAGE_JSON)
    app_meta.bootstrap_app_version(eng)
    assert app_meta.sqlite_read_app_version(db_file) == "0.9.240-3-gabc1234"
    # An older stamp is still advanced normally.
    app_meta.sqlite_set_app_version(db_file, "0.0.9")
    app_meta.bootstrap_app_version(eng)
    assert app_meta.sqlite_read_app_version(db_file) == "0.1.0"


def test_fallback_restore_plan_skips_app_version_but_not_schema(monkeypatch):
    from wsi_app import backup, migrations

    monkeypatch.setattr(backup, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_PACKAGE_JSON)

    def manifest(schema_version):
        return backup.Manifest(
            scope="metadata",
            app_version="0.9.240-3-gabc1234",
            created_at="2026-01-01T00:00:00Z",
            slide_count=0,
            tour_count=0,
            includes_analytics=True,
            includes_history=True,
            schema_version=schema_version,
        )

    current_sv = migrations.CURRENT_SCHEMA_VERSION
    assert backup.plan_restore(manifest(current_sv)).blocking_code is None
    assert (
        backup.plan_restore(manifest(current_sv + 1)).blocking_code
        == "DOWNGRADE_NOT_SUPPORTED"
    )
    # With a trusted running version the app_version comparison applies again.
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_VERSION_FILE)
    assert (
        backup.plan_restore(manifest(current_sv)).blocking_code
        == "DOWNGRADE_NOT_SUPPORTED"
    )


def test_fallback_restore_keeps_the_archives_newer_stamp(monkeypatch):
    """Applying a newer archive under the fallback version must leave its
    stamp alone, like bootstrap_app_version does: restamping it with the
    untrusted lower value would let the next build that does know its
    version (and is older than the archive) boot against it."""
    import os
    import sqlite3

    from wsi_app import backup, migrations
    from wsi_app.config import settings

    def stage(stamp):
        stage_id = backup.new_stage_id()
        stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
        os.makedirs(stage_dir)
        db_file = os.path.join(stage_dir, backup.DB_ENTRY)
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.close()
        app_meta.sqlite_set_app_version(db_file, stamp)
        manifest = backup.Manifest(
            scope="metadata",
            app_version=stamp,
            created_at="2026-01-01T00:00:00Z",
            slide_count=0,
            tour_count=0,
            includes_analytics=True,
            includes_history=True,
            schema_version=migrations.CURRENT_SCHEMA_VERSION,
        )
        return stage_id, db_file, backup.plan_restore(manifest)

    monkeypatch.setattr(backup, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_PACKAGE_JSON)
    stage_id, db_file, plan = stage("0.9.240-3-gabc1234")
    try:
        assert plan.blocking_code is None
        backup.apply_migrations_to_stage(stage_id, plan)
        assert app_meta.sqlite_read_app_version(db_file) == "0.9.240-3-gabc1234"
        # Re-applying the same stage is still accepted and still a no-op.
        backup.apply_migrations_to_stage(stage_id, plan)
        assert app_meta.sqlite_read_app_version(db_file) == "0.9.240-3-gabc1234"
    finally:
        backup.cleanup_stage(stage_id)

    # An older archive is still advanced to the running version.
    stage_id, db_file, plan = stage("0.0.9")
    try:
        backup.apply_migrations_to_stage(stage_id, plan)
        assert app_meta.sqlite_read_app_version(db_file) == "0.1.0"
    finally:
        backup.cleanup_stage(stage_id)



def test_fallback_backup_and_reset_keep_the_live_dbs_newer_stamp(monkeypatch, db_session):
    """A backup snapshot and a reset image are copies of the live database,
    which keeps a newer build's stamp under the fallback version (see
    test_fallback_bootstrap_keeps_the_newer_stamp). Restamping either copy
    with the untrusted lower value would let an older build that knows its
    version restore or boot it; under an authoritative version the copies are
    stamped with the running version as usual."""
    import os

    from wsi_app import backup
    from wsi_app.config import settings

    def staged_stamp(stage_id):
        db_file = os.path.join(settings.backup_dir, ".staging", stage_id, backup.DB_ENTRY)
        return app_meta.sqlite_read_app_version(db_file)

    live_db = backup._live_db_path()
    before = app_meta.sqlite_read_app_version(live_db)
    newer = "0.9.240-3-gabc1234"
    monkeypatch.setattr(backup, "APP_VERSION", "0.1.0")
    monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_PACKAGE_JSON)
    app_meta.sqlite_set_app_version(live_db, newer)
    stages: list[str] = []
    rec = None
    try:
        rec = backup.create_backup(scope="metadata")
        assert rec.app_version == newer
        # The archived database carries the same stamp as its manifest.
        stage_id, manifest = backup.stage_restore(rec.path)
        stages.append(stage_id)
        assert manifest.app_version == newer
        assert staged_stamp(stage_id) == newer

        stages.append(backup.create_reset_stage())
        assert staged_stamp(stages[-1]) == newer

        monkeypatch.setattr(version, "APP_VERSION_SOURCE", version.SOURCE_VERSION_FILE)
        stages.append(backup.create_reset_stage())
        assert staged_stamp(stages[-1]) == "0.1.0"
    finally:
        for stage_id in stages:
            backup.cleanup_stage(stage_id)
        if rec is not None:
            backup.delete_backup(rec.id)
        if before is not None:
            app_meta.sqlite_set_app_version(live_db, before)
