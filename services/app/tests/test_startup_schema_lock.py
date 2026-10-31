"""Boot-time schema setup must be cross-worker safe.

Production runs uvicorn with ``--workers N`` and the FastAPI lifespan runs in
every worker, so ``create_all`` + ``_apply_column_migrations`` race on the
first boot after an upgrade. ``main._startup_schema_lock`` serialises that
block with a host-local ``fcntl.flock``. These tests pin the behaviours that
setup must have: the lock is mutually exclusive; concurrent migration runs
under it heal a missing column without raising duplicate-column / snapshot
errors; each versioned migration runs exactly once across racing boots; the
add and drop phases are independent; and admin bootstrap survives a
concurrent insert.
"""
from __future__ import annotations

import dataclasses
import gc
import threading

import pytest
from sqlalchemy import inspect

from wsi_app import main
from wsi_app.db import engine


def _slide_columns() -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("slides")}


def _fresh_pool() -> None:
    """Close every pooled connection before a test reshapes ``slides``.

    The DB file is shared by the whole suite. A pooled connection with a
    statement an earlier test left unfinished (a result object still alive
    somewhere) keeps that statement's WAL read snapshot, and DDL run on it
    sees the snapshot's column list: dropping a column another connection
    has re-added since fails with "no such column".
    """
    gc.collect()
    engine.dispose()


def test_startup_schema_lock_is_mutually_exclusive():
    """Two threads inside the lock never overlap."""
    overlap = []  # records any observed concurrency
    active = 0
    active_guard = threading.Lock()
    start = threading.Barrier(2)

    def worker():
        nonlocal active
        start.wait()
        with main._startup_schema_lock():
            with active_guard:
                active += 1
                if active > 1:
                    overlap.append(active)
            # Hold the critical section briefly so a missing lock would let
            # the other thread observe active == 2.
            threading.Event().wait(0.02)
            with active_guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap == [], f"lock allowed concurrent entry: {overlap}"


def test_concurrent_migrations_under_lock_heal_missing_column():
    """A column missing from an upgraded DB is re-added exactly once even when
    several worker boots run the migration concurrently."""
    # Simulate a pre-upgrade DB: drop a model column from the live table.
    # ``objective_power_file`` carries a literal server_default, so the
    # additive ALTER re-adds it cleanly.
    _fresh_pool()
    assert "objective_power_file" in _slide_columns()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            'ALTER TABLE "slides" DROP COLUMN "objective_power_file"'
        )
    assert "objective_power_file" not in _slide_columns()

    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def boot():
        start.wait()
        try:
            with main._startup_schema_lock():
                main.Base.metadata.create_all(bind=engine)
                main._apply_column_migrations()
        except BaseException as exc:  # noqa: BLE001 — surface any race fallout
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent migration raised: {errors!r}"
    assert "objective_power_file" in _slide_columns()


def test_concurrent_boots_apply_each_migration_exactly_once(tmp_path, monkeypatch):
    """Under the lock, N worker boots run the migration chain once between them.

    Production starts uvicorn with ``--workers N`` and every worker runs the
    same lifespan. The runner decides what to apply purely from the DB's
    recorded ``schema_version``, read inside the lock — so the first worker
    through applies and stamps, and the rest read the stamp and skip. Without
    that ordering a migration body would run once per worker, which for
    anything non-idempotent means duplicated writes.

    Drives the real ``run_migrations`` with synthetic migrations, since the
    registered list is empty.
    """
    from sqlalchemy import create_engine

    from wsi_app import app_meta, migrations
    from wsi_app.migrations import Migration

    db = tmp_path / "concurrent.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, "1"),
        )

    runs: list[int] = []
    runs_guard = threading.Lock()

    def make(v):
        def up(conn, _v=v):
            with runs_guard:
                runs.append(_v)
        return Migration(version=v, description=f"m{v}", upgrade=up)

    monkeypatch.setattr(migrations, "MIGRATIONS", [make(2), make(3)])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 3)

    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def boot():
        start.wait()
        try:
            with main._startup_schema_lock():
                # Exactly what the lifespan does: read the stamp inside the
                # critical section, then hand it to the runner.
                recorded = app_meta.read_recorded_schema_version(eng)
                migrations.run_migrations(eng, recorded_sv=recorded)
        except BaseException as exc:  # noqa: BLE001 — surface any race fallout
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent boot raised: {errors!r}"
    assert sorted(runs) == [2, 3], f"migrations ran more than once: {runs}"
    assert app_meta.read_recorded_schema_version(eng) == 3


def test_apply_column_migrations_phases_are_independent():
    """ADD phase only adds; DROP phase only drops. The lifespan relies on the
    split so an ordered migration can run between them.

    Self-restoring: ends with ``objective_power_file`` present and the bogus
    stale column gone, exactly as it started (the DB file is shared across
    tests).
    """
    _fresh_pool()
    assert "objective_power_file" in _slide_columns()
    assert "_test_stale" not in _slide_columns()

    # Simulate a pre-upgrade DB (missing model column) AND a stale column the
    # ORM does not declare.
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE "slides" DROP COLUMN "objective_power_file"')
        conn.exec_driver_sql('ALTER TABLE "slides" ADD COLUMN "_test_stale" TEXT')
    assert "objective_power_file" not in _slide_columns()
    assert "_test_stale" in _slide_columns()

    # ADD phase: re-adds the model column, leaves the stale column untouched.
    main._apply_column_migrations(phase="add")
    cols = _slide_columns()
    assert "objective_power_file" in cols
    assert "_test_stale" in cols  # NOT dropped by the add phase

    # DROP phase: removes the stale column, leaves the model column in place.
    main._apply_column_migrations(phase="drop")
    cols = _slide_columns()
    assert "_test_stale" not in cols
    assert "objective_power_file" in cols  # restored to the starting state


def test_bootstrap_admin_survives_concurrent_insert(monkeypatch):
    """bootstrap_admin must not crash when another worker wins the
    no-admin-yet INSERT race: the unique-username IntegrityError at commit is
    caught and treated as a successful no-op.
    """
    from sqlalchemy import delete, select

    from wsi_app.config import settings
    from wsi_app.db import SessionLocal
    from wsi_app.models import User

    username = settings.admin_bootstrap_username

    # Start from a clean slate so bootstrap_admin takes the "no admin -> INSERT"
    # path (the file DB persists across tests).
    cleaner = SessionLocal()
    try:
        cleaner.execute(delete(User).where(User.username == username))
        cleaner.execute(delete(User).where(User.role == "admin"))
        cleaner.commit()
    finally:
        cleaner.close()

    real_hash = main.hash_password

    def racing_hash(pw: str) -> str:
        # Runs between bootstrap_admin's username SELECT and its commit: a
        # different "worker" commits the admin row first, so this commit hits
        # the users.username UNIQUE constraint.
        other = SessionLocal()
        try:
            exists = other.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if exists is None:
                other.add(User(username=username, password_hash=real_hash(pw), role="admin"))
                other.commit()
        finally:
            other.close()
        return real_hash(pw)

    monkeypatch.setattr(main, "hash_password", racing_hash)

    # Must not raise despite the collision at commit.
    main.bootstrap_admin()

    check = SessionLocal()
    try:
        admins = check.execute(
            select(User).where(User.username == username)
        ).scalars().all()
        assert len(admins) == 1, "exactly one admin row should exist after the race"
    finally:
        check.close()


def test_boot_with_multiple_admins(monkeypatch):
    """A second admin must not break the next boot.

    The admin UI lets an admin create further admins, so the existence probe in
    ``bootstrap_admin`` has to tolerate several matching rows. Drives the real
    lifespan (not just the function) with two admins present, since that is
    where a raise takes every worker down.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from wsi_app import admin_helpers
    from wsi_app.auth import hash_password
    from wsi_app.db import SessionLocal
    from wsi_app.models import User

    db = SessionLocal()
    try:
        for name in ("c1_admin_a", "c1_admin_b"):
            if db.execute(select(User).where(User.username == name)).scalar_one_or_none() is None:
                db.add(User(username=name, password_hash=hash_password("x" * 16), role="admin"))
        db.commit()
        before = db.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        ).scalar_one()
    finally:
        db.close()
    assert before >= 2

    # Direct call: no MultipleResultsFound, and no extra bootstrap row.
    main.bootstrap_admin()

    # The orphan reaper is neutralised for the same reason as in
    # test_migrations: it acts on shared-DB rows unrelated to this test.
    monkeypatch.setattr(admin_helpers, "reap_orphan_slide_reservations",
                        lambda db: None)
    with TestClient(main.app):
        pass

    db = SessionLocal()
    try:
        after = db.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        ).scalar_one()
    finally:
        db.close()
    assert after == before


@pytest.mark.parametrize("bad", ["x", "short", "x" * 1025])
def test_bootstrap_admin_rejects_out_of_policy_password(monkeypatch, bad):
    """ADMIN_BOOTSTRAP_PASSWORD goes through the same length policy as every
    other password: too short seeds a guessable admin, too long seeds one that
    can never log in. Startup fails instead, like the placeholder case."""
    from sqlalchemy import delete, select

    from wsi_app.config import settings
    from wsi_app.db import SessionLocal
    from wsi_app.models import User

    username = settings.admin_bootstrap_username
    db = SessionLocal()
    try:
        db.execute(delete(User).where(User.username == username))
        db.execute(delete(User).where(User.role == "admin"))
        db.commit()
    finally:
        db.close()

    # Settings is a frozen dataclass; patch the field the way other tests do.
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, admin_bootstrap_password=bad))
    with pytest.raises(RuntimeError, match="ADMIN_BOOTSTRAP_PASSWORD"):
        main.bootstrap_admin()

    check = SessionLocal()
    try:
        assert check.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none() is None
    finally:
        check.close()
