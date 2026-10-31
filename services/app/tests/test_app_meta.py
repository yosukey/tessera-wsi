"""Tests for app_meta: version parsing, downgrade detection, and the
stamp-only bootstrap.
"""
from __future__ import annotations

import sqlite3

import pytest

from wsi_app import app_meta


def test_parse_version_handles_suffixes_and_short():
    # Bare release: numeric tuple plus the final-release rank marker.
    assert app_meta.parse_version("0.1.0") == (0, 1, 0, app_meta._RANK_FINAL, 0)
    # The numeric prefix is padded to 3 components so a 2-component version
    # compares correctly against a 3-component one ("1.2" == "1.2.0").
    assert app_meta.parse_version("1.2") == (1, 2, 0, app_meta._RANK_FINAL, 0)
    assert app_meta.parse_version("1.2") == app_meta.parse_version("1.2.0")
    assert not app_meta.is_downgrade("1.2", "1.2.0")
    assert not app_meta.is_downgrade("1.2.0", "1.2")
    # Prereleases sort below the bare release and between each other.
    assert app_meta.parse_version("0.1.0-rc1") == (0, 1, 0, app_meta._RANK_RC, 1)
    assert app_meta.parse_version("0.1.0-rc2") == (0, 1, 0, app_meta._RANK_RC, 2)
    assert app_meta.parse_version("0.1.0-beta3") == (0, 1, 0, app_meta._RANK_BETA, 3)
    assert app_meta.parse_version("0.1.0a2") == (0, 1, 0, app_meta._RANK_ALPHA, 2)
    # git-describe post-tag suffix is a *post*-release: greater than the bare tag.
    assert app_meta.parse_version("0.1.0-3-gabc1234") == (0, 1, 0, app_meta._RANK_POST, 3)
    # Semver-style dotted prerelease, and a git-describe build past a
    # prerelease tag (above rc1, below rc2 and the final release).
    assert app_meta.parse_version("0.1.0-rc.1") == app_meta.parse_version("0.1.0-rc1")
    post_rc1 = app_meta.parse_version("0.1.0-rc1-3-gabc1234")
    assert (app_meta.parse_version("0.1.0-rc1") < post_rc1
            < app_meta.parse_version("0.1.0-rc2")
            < app_meta.parse_version("0.1.0"))
    # Unparseable strings — including a bare git hash, even a digit-led one
    # that would otherwise read as version 1234 — are not versions.
    assert app_meta.parse_version("unknown") is None
    assert app_meta.parse_version("1234abc") is None
    assert app_meta.parse_version("abcdef1") is None
    # Git tag names (the update check feeds them in) carry a leading "v".
    assert app_meta.parse_version("v0.1.0-rc1") == app_meta.parse_version("0.1.0-rc1")
    assert app_meta.parse_version("vabc1234") is None


def test_is_downgrade_strict():
    assert app_meta.is_downgrade("0.2.0", "0.1.0")
    assert not app_meta.is_downgrade("0.1.0", "0.2.0")
    # Equality is NOT a downgrade.
    assert not app_meta.is_downgrade("0.1.0", "0.1.0")
    # Prereleases compare correctly against each other and against the bare release.
    assert app_meta.is_downgrade("0.1.0-rc2", "0.1.0-rc1")
    assert not app_meta.is_downgrade("0.1.0-rc1", "0.1.0-rc2")
    assert app_meta.is_downgrade("0.1.0", "0.1.0-rc1")
    assert not app_meta.is_downgrade("0.1.0-rc1", "0.1.0")
    # git-describe post-release is greater than the bare tag.
    assert app_meta.is_downgrade("0.1.0-3-gabc1234", "0.1.0")
    assert not app_meta.is_downgrade("0.1.0", "0.1.0-3-gabc1234")
    # An unparseable side (dev "unknown", a tagless git-describe hash) never
    # reads as a downgrade in either direction.
    assert not app_meta.is_downgrade("0.5.0", "unknown")
    assert not app_meta.is_downgrade("0.5.0", "abc1234")
    assert not app_meta.is_downgrade("1234abc", "0.5.0")
    assert not app_meta.is_downgrade("0.5.0", "1234abc")
    # Deploying commits past an rc tag, then the next rc, is an upgrade.
    assert not app_meta.is_downgrade("0.1.0-rc1-3-gabc1234", "0.1.0-rc2")


def _empty_db_with_app_meta(path):
    """A SQLite file carrying the app_meta table but no rows.

    The sqlite3-level setters do not CREATE the table: their only caller
    is the restore path, which acts on a staged DB that stage_restore has
    already confirmed carries app_meta. Tests must supply the same
    precondition rather than rely on the setter to bootstrap it.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()


def test_sqlite_set_and_read_app_version(tmp_path):
    db = tmp_path / "v.db"
    _empty_db_with_app_meta(db)
    assert app_meta.sqlite_read_app_version(str(db)) is None

    app_meta.sqlite_set_app_version(str(db), "0.1.0")
    assert app_meta.sqlite_read_app_version(str(db)) == "0.1.0"

    # Idempotent: repeated set updates without error.
    app_meta.sqlite_set_app_version(str(db), "0.2.0")
    assert app_meta.sqlite_read_app_version(str(db)) == "0.2.0"


def test_sqlite_set_and_read_schema_version(tmp_path):
    db = tmp_path / "sv.db"
    _empty_db_with_app_meta(db)
    # Key absent → None. stage_restore treats that as a corrupt archive; the
    # reader itself just reports "not stamped".
    assert app_meta.sqlite_read_schema_version(str(db)) is None

    app_meta.sqlite_set_schema_version(str(db), 3)
    assert app_meta.sqlite_read_schema_version(str(db)) == 3

    # Idempotent update.
    app_meta.sqlite_set_schema_version(str(db), 7)
    assert app_meta.sqlite_read_schema_version(str(db)) == 7


def test_sqlite_readers_report_none_when_app_meta_is_absent(tmp_path):
    """A file with no app_meta at all: both readers say None rather than raise.

    This is the shape stage_restore rejects — an archive not written by this
    application — so the readers must return cleanly for it to be reported.
    """
    db = tmp_path / "foreign.db"
    sqlite3.connect(str(db)).close()
    assert app_meta.sqlite_read_app_version(str(db)) is None
    assert app_meta.sqlite_read_schema_version(str(db)) is None


def test_sqlite_setters_require_app_meta_to_exist(tmp_path):
    """The setters require app_meta to exist.

    A missing table means the staged file was swapped after validation, and
    raising is the correct, loud outcome.
    """
    db = tmp_path / "bare.db"
    sqlite3.connect(str(db)).close()
    with pytest.raises(sqlite3.OperationalError):
        app_meta.sqlite_set_app_version(str(db), "0.1.0")
    with pytest.raises(sqlite3.OperationalError):
        app_meta.sqlite_set_schema_version(str(db), 1)


def test_read_recorded_schema_version_tolerates_missing(tmp_path):
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{tmp_path}/rv.db")
    # No app_meta table yet (pre-create_all): None, not an error.
    assert app_meta.read_recorded_schema_version(eng) is None
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    # Table but no key: still None.
    assert app_meta.read_recorded_schema_version(eng) is None
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, "4"),
        )
    assert app_meta.read_recorded_schema_version(eng) == 4


def test_coerce_schema_version_handles_garbage():
    assert app_meta._coerce_schema_version("5") == 5
    assert app_meta._coerce_schema_version(" 6 ") == 6
    assert app_meta._coerce_schema_version(None) is None
    assert app_meta._coerce_schema_version("not-a-number") is None


def _engine_with_recorded(tmp_path, recorded_version):
    """Build a SQLite engine with ``app_meta.app_version`` pre-seeded."""
    from sqlalchemy import create_engine
    from wsi_app.models import Base

    db = tmp_path / "boot.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    if recorded_version is not None:
        app_meta.sqlite_set_app_version(str(db), recorded_version)
    return eng, db


def test_bootstrap_fresh_db_stamps_initialized_at(monkeypatch, tmp_path):
    """A DB that has never recorded an app_version gets db_initialized_at
    written on first boot.
    """
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.9.59")
    eng, db_path = _engine_with_recorded(tmp_path, recorded_version=None)

    app_meta.bootstrap_app_version(eng)

    assert app_meta.sqlite_read_app_version(str(db_path)) == "0.9.59"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (app_meta.KEY_DB_INITIALIZED_AT,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None  # initialization timestamp recorded


def test_bootstrap_advances_version_without_migrations(monkeypatch, tmp_path):
    """0.9.57 -> 0.9.59 boot stamps the new version without any
    hand-written migration: ``main._apply_column_migrations`` (called
    earlier in the lifespan) has already brought the schema into sync,
    so this function only updates the version metadata.
    """
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.9.59")
    eng, db_path = _engine_with_recorded(tmp_path, "0.9.57")

    app_meta.bootstrap_app_version(eng)

    assert app_meta.sqlite_read_app_version(str(db_path)) == "0.9.59"


def test_bootstrap_rejects_downgrade(monkeypatch, tmp_path):
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.9.57")
    eng, _ = _engine_with_recorded(tmp_path, "0.9.59")

    with pytest.raises(RuntimeError, match="Downgrades are not"):
        app_meta.bootstrap_app_version(eng)


def test_bootstrap_same_version_refreshes_seen_at(monkeypatch, tmp_path):
    """Re-boot at the same version is a no-op for app_version but bumps
    seen_at so admin diagnostics can show "last seen" wall time.
    """
    monkeypatch.setattr(app_meta, "APP_VERSION", "0.9.59")
    eng, db_path = _engine_with_recorded(tmp_path, "0.9.59")

    app_meta.bootstrap_app_version(eng)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (app_meta.KEY_APP_VERSION_SEEN_AT,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
