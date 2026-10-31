"""Tests for the schema_version migration runner and its baseline logic.

The runner (``wsi_app.migrations.run_migrations``) complements the automatic
column reconciler in ``main._apply_column_migrations``: it applies ordered,
numbered migrations for changes the reconciler can't express and stamps the
integer ``app_meta.schema_version`` as it goes.

The ordering / atomicity / resume cases below drive the runner with
*synthetic* migrations rather than whatever happens to be registered, so they
keep the machinery honest independently of the real list.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from wsi_app import app_meta, main, migrations
from wsi_app.migrations import Migration, run_migrations


def _engine(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{tmp_path}/{name}")


def _create_app_meta(eng):
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS app_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )


# main.py logs under "wsi_app.startup", not its module path. Naming the wrong
# logger in caplog.at_level() is a silent no-op: caplog still captures via the
# root handler, so the positive drift test would pass anyway — but the
# *negative* one ("the real schema reports no drift") would pass vacuously the
# moment anything raised that logger's level.
_STARTUP_LOGGER = "wsi_app.startup"


def _synthetic(*versions, record=None):
    """Synthetic migrations with the given numbers, appending to ``record``
    (runner tests pass ``recorded_sv`` explicitly, so any numbering works)."""
    return [
        Migration(
            version=v,
            description=f"m{v}",
            upgrade=(lambda conn, _v=v: record.append(_v)) if record is not None
            else (lambda conn: None),
        )
        for v in versions
    ]


# ---------------------------------------------------------------------------
# Version constant invariants
# ---------------------------------------------------------------------------

def test_current_schema_version_is_max_of_baseline_and_migrations():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert migrations.CURRENT_SCHEMA_VERSION == max(
        [migrations.BASELINE_SCHEMA_VERSION, *versions]
    )
    # Unique + ascending: the runner and the downgrade guard both rely on it.
    assert versions == sorted(set(versions))


def test_every_migration_is_numbered_above_the_baseline():
    """A migration at or below the baseline could never run.

    ``BASELINE_SCHEMA_VERSION`` is the lowest version any database records,
    so the runner's ``version <= baseline`` skip would drop such a migration
    on every install — silently. The package asserts this at import time;
    this pins the rule so the assertion isn't deleted as noise.
    """
    for m in migrations.MIGRATIONS:
        assert m.version > migrations.BASELINE_SCHEMA_VERSION


def test_baseline_floor_is_pinned():
    """The baseline may rise, never fall — pin the floor as a literal.

    ``test_baseline_is_never_lowered`` below shows the *mechanism* that makes
    a reduction fatal, but it does so generically: with the constant edited
    down, CURRENT falls with it and the demonstration still passes. Only a
    literal catches the edit itself.

    5 is the first public release's shape, which every database in existence
    records. A binary shipping a lower baseline cannot produce what those
    databases claim, so ``assert_schema_not_downgrade`` refuses to open any
    of them — an outage, not a cosmetic change. Raise this only alongside a
    migration that genuinely moves the floor.
    """
    assert migrations.BASELINE_SCHEMA_VERSION >= 5
    assert migrations.CURRENT_SCHEMA_VERSION >= 5


def test_baseline_is_never_lowered(tmp_path):
    """Lowering the baseline locks the app out of every existing database.

    Every DB records at least ``BASELINE_SCHEMA_VERSION``, and the schema
    downgrade guard refuses to boot a binary that cannot produce what the DB
    records. So a baseline edited downward is not a cosmetic change — it is
    an outage. Demonstrate the mechanism rather than merely asserting a
    number, so the reason survives.
    """
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    run_migrations(eng, recorded_sv=None)  # stamps CURRENT, as a fresh DB
    assert (
        app_meta.read_recorded_schema_version(eng)
        == migrations.CURRENT_SCHEMA_VERSION
    )

    # A binary whose CURRENT sits one below what this DB records.
    with pytest.raises(RuntimeError, match="newer than this binary supports"):
        app_meta.assert_schema_not_downgrade(
            eng, migrations.CURRENT_SCHEMA_VERSION - 1
        )


# ---------------------------------------------------------------------------
# Baseline selection
# ---------------------------------------------------------------------------

def test_unstamped_db_runs_every_migration_from_the_baseline(tmp_path, monkeypatch):
    """No recorded version = a brand-new DB. It starts from the baseline, so
    every (defensive) migration above it runs — a no-op on the fresh shape —
    and the stamp ends at CURRENT."""
    base = migrations.BASELINE_SCHEMA_VERSION
    ran: list[int] = []
    monkeypatch.setattr(migrations, "MIGRATIONS", _synthetic(base + 1, base + 2, record=ran))
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", base + 2)

    eng = _engine(tmp_path)
    _create_app_meta(eng)
    applied = run_migrations(eng, recorded_sv=None)

    assert applied == [base + 1, base + 2]
    assert ran == [base + 1, base + 2]
    assert app_meta.read_recorded_schema_version(eng) == base + 2


def test_a_database_below_the_baseline_is_refused(tmp_path):
    """A database stamped below the baseline has no migration path to this
    shape, so the boot refuses it before any schema work instead of stamping
    it current."""
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, str(migrations.BASELINE_SCHEMA_VERSION - 1)),
        )
    with pytest.raises(RuntimeError, match="pre-release build"):
        main.reconcile_schema(eng)
    # Refused before create_all: nothing was built.
    with eng.connect() as conn:
        tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert tables == {"app_meta"}
    # The baseline itself and an unstamped DB pass.
    migrations.assert_schema_floor(migrations.BASELINE_SCHEMA_VERSION)
    migrations.assert_schema_floor(None)


def test_a_populated_database_without_a_schema_version_is_refused(tmp_path):
    """An app_version stamp without a schema_version one means the database
    was written before the key existed — not a brand-new DB — so it is
    refused rather than stamped current without the migrations it missed."""
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_APP_VERSION, "0.0.1"),
        )
    with pytest.raises(RuntimeError, match="no schema_version"):
        main.reconcile_schema(eng)
    with eng.connect() as conn:
        tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert tables == {"app_meta"}


def test_empty_migration_list_still_stamps(tmp_path, monkeypatch):
    """The registered list being empty is a legitimate state, not a bug: the
    baseline alone describes the schema and the stamp must still be written."""
    monkeypatch.setattr(migrations, "MIGRATIONS", [])
    monkeypatch.setattr(
        migrations, "CURRENT_SCHEMA_VERSION", migrations.BASELINE_SCHEMA_VERSION
    )
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    assert run_migrations(eng, recorded_sv=None) == []
    assert (
        app_meta.read_recorded_schema_version(eng)
        == migrations.CURRENT_SCHEMA_VERSION
    )
    # ...and on an existing DB already at the baseline.
    assert run_migrations(eng, recorded_sv=migrations.BASELINE_SCHEMA_VERSION) == []
    assert (
        app_meta.read_recorded_schema_version(eng)
        == migrations.CURRENT_SCHEMA_VERSION
    )


def test_resume_runs_only_pending(tmp_path):
    """A DB already stamped at CURRENT has nothing pending."""
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    applied = run_migrations(eng, recorded_sv=migrations.CURRENT_SCHEMA_VERSION)
    assert applied == []
    assert (
        app_meta.read_recorded_schema_version(eng)
        == migrations.CURRENT_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# Ordering + atomicity (driven with a synthetic migration list)
# ---------------------------------------------------------------------------

def test_runs_in_ascending_order_and_stamps_each(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    order: list[int] = []
    monkeypatch.setattr(migrations, "MIGRATIONS", _synthetic(2, 3, 4, record=order))
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 4)

    applied = run_migrations(eng, recorded_sv=1)
    assert order == [2, 3, 4]
    assert applied == [2, 3, 4]
    assert app_meta.read_recorded_schema_version(eng) == 4


def test_failure_leaves_version_at_last_good_step(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    _create_app_meta(eng)

    def ok(conn):
        pass

    def boom(conn):
        raise RuntimeError("migration 3 failed")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        Migration(version=2, description="ok", upgrade=ok),
        Migration(version=3, description="boom", upgrade=boom),
        Migration(version=4, description="ok", upgrade=ok),
    ])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 4)

    with pytest.raises(RuntimeError, match="migration 3 failed"):
        run_migrations(eng, recorded_sv=1)

    # Migration 2 committed with its stamp; 3 rolled back; 4 never ran; the
    # final CURRENT stamp is unreachable — so the version resumes from 2.
    assert app_meta.read_recorded_schema_version(eng) == 2


def test_failed_step_rolls_back_its_ddl(tmp_path, monkeypatch):
    """DDL a failing step already ran must roll back with it.

    pysqlite's default mode opens a transaction only before DML, so without an
    explicit BEGIN a CREATE/ALTER autocommits and outlives the failure.
    """
    eng = _engine(tmp_path)
    _create_app_meta(eng)

    def half_done(conn):
        conn.exec_driver_sql("CREATE TABLE half_built (id INTEGER)")
        conn.exec_driver_sql("ALTER TABLE app_meta ADD COLUMN stray TEXT")
        raise RuntimeError("migration 2 failed after its DDL")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        Migration(version=2, description="half", upgrade=half_done),
    ])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 2)

    with pytest.raises(RuntimeError, match="after its DDL"):
        run_migrations(eng, recorded_sv=1)

    with eng.connect() as conn:
        tables = {
            r[0] for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(app_meta)")}
    assert "half_built" not in tables
    assert "stray" not in cols
    assert app_meta.read_recorded_schema_version(eng) is None


def test_successful_step_commits_its_ddl(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    _create_app_meta(eng)

    def create(conn):
        conn.exec_driver_sql("CREATE TABLE built (id INTEGER)")
        conn.exec_driver_sql("INSERT INTO built(id) VALUES (1)")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        Migration(version=2, description="create", upgrade=create),
    ])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 2)

    assert run_migrations(eng, recorded_sv=1) == [2]
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SELECT id FROM built").scalar_one() == 1
    assert app_meta.read_recorded_schema_version(eng) == 2


# ---------------------------------------------------------------------------
# Foreign keys during a step (SQLite's "other ALTER TABLE" procedure)
# ---------------------------------------------------------------------------
# db.py turns foreign_keys ON for every pooled connection, and the pragma is a
# silent no-op inside a transaction. So unless the runner disables it BEFORE
# BEGIN IMMEDIATE, the standard table rebuild's DROP TABLE fires ON DELETE
# CASCADE and wipes every child row — and the step commits happily.

def _fk_engine(tmp_path):
    """An engine shaped like db.py's: FKs on per connection, one pooled
    connection so the test can see what state the runner hands back."""
    from sqlalchemy import event

    eng = create_engine(
        f"sqlite:///{tmp_path}/fk.db", pool_size=1, max_overflow=0
    )

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    _create_app_meta(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "NOT NULL REFERENCES parent(id) ON DELETE CASCADE)"
        )
        conn.exec_driver_sql("INSERT INTO parent(id, name) VALUES (1, 'a'), (2, 'b')")
        conn.exec_driver_sql(
            "INSERT INTO child(id, parent_id) VALUES (10, 1), (11, 1), (12, 2)"
        )
    return eng


def _fk_enabled(eng) -> int:
    with eng.connect() as conn:
        return conn.exec_driver_sql("PRAGMA foreign_keys").scalar()


def test_table_rebuild_step_keeps_cascade_children(tmp_path, monkeypatch):
    eng = _fk_engine(tmp_path)

    def rebuild_parent(conn):
        # The documented rebuild: new table, copy, drop old, rename.
        conn.exec_driver_sql(
            "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT, "
            "note TEXT NOT NULL DEFAULT '')"
        )
        conn.exec_driver_sql(
            "INSERT INTO parent_new(id, name) SELECT id, name FROM parent"
        )
        conn.exec_driver_sql("DROP TABLE parent")
        conn.exec_driver_sql("ALTER TABLE parent_new RENAME TO parent")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        Migration(version=2, description="rebuild parent", upgrade=rebuild_parent),
    ])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 2)

    assert run_migrations(eng, recorded_sv=1) == [2]

    with eng.connect() as conn:
        children = conn.exec_driver_sql(
            "SELECT id FROM child ORDER BY id"
        ).scalars().all()
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(parent)")}
    assert children == [10, 11, 12]  # not cascaded away by DROP TABLE parent
    assert "note" in cols
    assert app_meta.read_recorded_schema_version(eng) == 2
    # The pooled connection goes back with enforcement restored.
    assert _fk_enabled(eng) == 1


def test_step_leaving_fk_violation_is_rolled_back(tmp_path, monkeypatch):
    """With enforcement off for the step, foreign_key_check is the only thing
    standing between a buggy rebuild and committed dangling references."""
    eng = _fk_engine(tmp_path)

    def orphan(conn):
        conn.exec_driver_sql("CREATE TABLE step_marker (id INTEGER)")
        conn.exec_driver_sql("INSERT INTO child(id, parent_id) VALUES (99, 42)")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        Migration(version=2, description="orphan", upgrade=orphan),
    ])
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 2)

    with pytest.raises(RuntimeError, match="foreign key violation"):
        run_migrations(eng, recorded_sv=1)

    with eng.connect() as conn:
        ids = conn.exec_driver_sql("SELECT id FROM child").scalars().all()
        tables = {
            r[0] for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert 99 not in ids
    assert "step_marker" not in tables  # the whole step rolled back, DDL too
    assert app_meta.read_recorded_schema_version(eng) is None
    assert _fk_enabled(eng) == 1


def test_only_pending_run_from_recorded_version(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    ran: list[int] = []
    monkeypatch.setattr(migrations, "MIGRATIONS", _synthetic(2, 3, 4, record=ran))
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 4)

    applied = run_migrations(eng, recorded_sv=3)
    assert ran == [4]
    assert applied == [4]
    assert app_meta.read_recorded_schema_version(eng) == 4


def test_rerun_after_full_chain_is_a_noop(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    _create_app_meta(eng)
    ran: list[int] = []
    monkeypatch.setattr(migrations, "MIGRATIONS", _synthetic(2, 3, record=ran))
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", 3)

    run_migrations(eng, recorded_sv=1)
    assert ran == [2, 3]
    sv = app_meta.read_recorded_schema_version(eng)
    assert run_migrations(eng, recorded_sv=sv) == []
    assert ran == [2, 3]  # each migration ran exactly once


# ---------------------------------------------------------------------------
# "Is this DB new?" must not be answerable by accident
# ---------------------------------------------------------------------------

def test_missing_app_meta_table_reads_as_new_db(tmp_path):
    eng = _engine(tmp_path, "no-meta.db")
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE placeholder (id INTEGER)")
    assert app_meta.read_recorded_schema_version(eng) is None


def test_unreadable_db_raises_rather_than_reading_as_new(tmp_path):
    """A read failure must NOT be flattened into ``None``.

    ``run_migrations`` treats ``None`` as "fresh install: stamp CURRENT, run
    nothing". If a transient read error produced ``None`` on a populated DB,
    every pending migration would be skipped and the DB stamped as though
    they had run — corruption that no later boot can detect. Failing the boot
    is the recoverable outcome.
    """
    unopenable = tmp_path / "not-a-file.db"
    unopenable.mkdir()  # a directory where SQLite expects a file
    eng = create_engine(f"sqlite:///{unopenable}")
    with pytest.raises(Exception):
        app_meta.read_recorded_schema_version(eng)


@pytest.mark.parametrize("stored", ["", "   ", "1a", "v3", "null"])
def test_garbage_schema_version_raises_rather_than_reading_as_new(tmp_path, stored):
    """A present-but-unparseable stamp is corruption, not a fresh install.

    The key existing proves the DB has been written by this app before, so
    answering ``None`` (the "brand-new DB" signal) would skip every pending
    migration and re-stamp the DB at CURRENT — the same undetectable
    corruption the sibling test guards against, reached through a bad *value*
    instead of a bad *read*.
    """
    eng = _engine(tmp_path, f"garbage-{abs(hash(stored))}.db")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            (app_meta.KEY_SCHEMA_VERSION, stored),
        )
    with pytest.raises(RuntimeError, match="schema version"):
        app_meta.read_recorded_schema_version(eng)


# ---------------------------------------------------------------------------
# Lifespan schema sequence: create_all -> ADD -> run_migrations -> DROP
# ---------------------------------------------------------------------------

def test_lifespan_schema_sequence_stamps_version():
    """Reproduce the lifespan's schema-setup block (guards, create_all, the
    ADD/DROP phase split around ``run_migrations``) against the shared engine
    and confirm it ends with schema_version == CURRENT.

    Uses the real functions in the real order but skips ``bootstrap_admin``,
    which is not a schema step (its multi-admin boot is covered by
    test_startup_schema_lock). Order-independent: it clears the
    schema_version stamp first and the sequence is idempotent.
    """
    from wsi_app.db import engine

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM app_meta WHERE key=?", (app_meta.KEY_SCHEMA_VERSION,)
        )

    app_meta.assert_not_downgrade(engine)
    app_meta.assert_schema_not_downgrade(engine, migrations.CURRENT_SCHEMA_VERSION)
    recorded_sv = app_meta.read_recorded_schema_version(engine)
    assert recorded_sv is None  # just cleared
    main.Base.metadata.create_all(bind=engine)
    main._apply_column_migrations(phase="add")
    applied = migrations.run_migrations(engine, recorded_sv=recorded_sv)
    main._apply_column_migrations(phase="drop")
    main._reconcile_indexes()

    # A cleared stamp starts from the baseline, so every registered
    # migration runs again — each a no-op on the current shape.
    assert applied == [m.version for m in migrations.MIGRATIONS]
    assert (
        app_meta.read_recorded_schema_version(engine)
        == migrations.CURRENT_SCHEMA_VERSION
    )


def test_lifespan_runs_the_schema_steps_in_order(monkeypatch):
    """The real ``lifespan`` must invoke every schema step, in sequence.

    ``test_lifespan_schema_sequence_stamps_version`` above re-implements the
    sequence to assert what it produces; that cannot notice a step being
    dropped from ``lifespan`` itself. Deleting the drift check from the boot
    path, for instance, leaves its own unit tests passing while the detector
    silently stops running. Drive the actual lifespan and record the order.

    ``bootstrap_admin`` and the orphan reaper are neutralised: both write to
    the shared test DB, which by this point holds rows from the rest of the
    suite that neither step expects.
    """
    from fastapi.testclient import TestClient

    from wsi_app import admin_helpers

    calls: list[str] = []

    def spy(name, fn):
        def wrapper(*a, **kw):
            # _apply_column_migrations is called with phase= as a keyword, and
            # which half ran is the interesting part of this ordering.
            phase = kw.get("phase") or (a[0] if a and isinstance(a[0], str) else "")
            calls.append(f"{name}{phase}")
            return fn(*a, **kw)
        return wrapper

    monkeypatch.setattr(main, "_apply_column_migrations",
                        spy("apply:", main._apply_column_migrations))
    monkeypatch.setattr(main, "_reconcile_indexes",
                        spy("indexes", main._reconcile_indexes))
    monkeypatch.setattr(main, "_warn_nullability_divergence",
                        spy("drift", main._warn_nullability_divergence))
    monkeypatch.setattr(main._migrations, "run_migrations",
                        spy("migrations", main._migrations.run_migrations))
    monkeypatch.setattr(main, "bootstrap_admin", lambda: None)
    monkeypatch.setattr(admin_helpers, "reap_orphan_slide_reservations",
                        lambda db: None)

    with TestClient(main.app):
        pass

    assert calls == [
        "apply:add", "migrations", "apply:drop", "indexes", "drift"
    ], calls


# ---------------------------------------------------------------------------
# Nullability drift detection
# ---------------------------------------------------------------------------
# The reconciler only ADDs and DROPs columns; it cannot change an existing
# column's nullability (SQLite needs a table rebuild). So a column added in
# place with a non-constant default stays nullable forever while create_all
# builds it NOT NULL — a divergence nothing repairs and nothing would report.

_ANALYTICS_WINDOW_INDEXES = {
    "page_views": "ix_page_views_actor_role_occurred_at",
    "slide_dwell_events": "ix_slide_dwell_events_actor_role_started_at",
    "tour_sessions": "ix_tour_sessions_actor_role_started_at",
}


def test_reconcile_adds_analytics_window_indexes_to_existing_db():
    """An existing DB whose tables lack the composite indexes gains them at boot."""
    from wsi_app.db import engine

    try:
        with engine.begin() as conn:
            for idx in _ANALYTICS_WINDOW_INDEXES.values():
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{idx}"')
        main._reconcile_indexes()
        with engine.connect() as conn:
            for table, idx in _ANALYTICS_WINDOW_INDEXES.items():
                names = {
                    r[1] for r in conn.exec_driver_sql(f"PRAGMA index_list({table})")
                }
                assert idx in names, (table, names)
    finally:
        main._reconcile_indexes()


def test_default_dashboard_filter_uses_the_composite_index():
    """``actor_role IS NULL`` + date window must seek, not scan all history.

    With only single-column indexes SQLite would choose ix_page_views_actor_role (the
    equality-looking predicate), which matches nearly every row.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from wsi_app.db import engine
    from wsi_app.models import PageView
    from wsi_app.routers import analytics as an

    until = datetime.now(timezone.utc)
    stmt = an._apply_default_filters(
        an._window_filter(select(func.count()), until - timedelta(days=7), until),
        include_bots=False,
    ).select_from(PageView)
    sql = str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as conn:
        plan = " ".join(
            str(r[-1]) for r in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}")
        )
    assert "ix_page_views_actor_role_occurred_at" in plan, plan
    assert "occurred_at>" in plan, plan


def test_nullability_divergence_is_reported(caplog):
    from wsi_app.db import engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS drift_probe")
        conn.exec_driver_sql("CREATE TABLE drift_probe (id INTEGER PRIMARY KEY)")
    try:
        # Teach the ORM about a table whose live shape is deliberately looser
        # than declared: NOT NULL here, absent-then-nullable on disk.
        from sqlalchemy import Column, Integer, String, Table

        probe = Table(
            "drift_probe", main.Base.metadata,
            Column("id", Integer, primary_key=True),
            Column("tok", String(32), nullable=False),
        )
        try:
            main._apply_column_migrations(phase="add")  # adds `tok` nullable
            with caplog.at_level("WARNING", logger=_STARTUP_LOGGER):
                main._warn_nullability_divergence()
            assert "drift_probe.tok" in caplog.text
            assert "schema drift" in caplog.text
        finally:
            main.Base.metadata.remove(probe)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS drift_probe")


def test_no_divergence_reported_for_the_real_schema(caplog):
    """The shipped ORM and a create_all database must agree.

    Nullability must match between the ORM declarations and a database built
    by create_all; if a future column silently introduces drift, this fails
    rather than letting it accumulate.
    """
    with caplog.at_level("WARNING", logger=_STARTUP_LOGGER):
        main._warn_nullability_divergence()
    assert "schema drift" not in caplog.text


def test_columns_whose_guards_were_removed_are_declared_not_null():
    """Pin the ORM declarations that unguarded call sites rely on.

    ``_serializers.thumb_url`` calls ``slide.created_at.timestamp()`` with no
    None check, and every ``public_id`` consumer assumes a token exists. Both
    are correct only while these columns are NOT NULL. Loosening one would
    turn the first into an AttributeError on the public slide catalogue — the
    most exposed endpoint there is — so fail here instead.
    """
    from wsi_app.models import Slide, Tour

    for table, column in (
        (Slide.__table__, "created_at"),
        (Slide.__table__, "public_id"),
        (Tour.__table__, "created_at"),
        (Tour.__table__, "public_id"),
    ):
        assert not table.c[column].nullable, (
            f"{table.name}.{column} became nullable; the call sites that "
            f"carry no None guard need it NOT NULL"
        )


# ---------------------------------------------------------------------------
# ALTER TABLE DEFAULT rendering
# ---------------------------------------------------------------------------
# The boot-time schema reconciler renders a column's server_default into an
# ``ALTER TABLE … ADD COLUMN … DEFAULT <literal>`` clause. A bare word emitted
# verbatim only works because SQLite reinterprets an unresolvable identifier
# in a DEFAULT clause as a string; the first server_default containing a
# space or apostrophe would be a syntax error, i.e. every worker failing to
# boot.

def test_as_sql_literal_quotes_bare_strings():
    from wsi_app.column_defaults import as_sql_literal

    # Users.role ships server_default="instructor" today.
    assert as_sql_literal("instructor") == "'instructor'"
    assert as_sql_literal("no auth") == "'no auth'"
    assert as_sql_literal("it's") == "'it''s'"


def test_as_sql_literal_leaves_numbers_and_quoted_strings_alone():
    from wsi_app.column_defaults import as_sql_literal

    assert as_sql_literal("0") == "0"
    assert as_sql_literal("2000") == "2000"
    assert as_sql_literal("-1") == "-1"
    assert as_sql_literal("1.5") == "1.5"
    # Already-quoted literals (server_default=text("''")) pass through.
    assert as_sql_literal("''") == "''"
    assert as_sql_literal("""'["stain","organ"]'""") == """'["stain","organ"]'"""


def test_as_sql_literal_preserves_sql_constant_keywords():
    from wsi_app.column_defaults import as_sql_literal

    # Quoting these would turn a constant into a string literal.
    assert as_sql_literal("CURRENT_TIMESTAMP") == "CURRENT_TIMESTAMP"
    assert as_sql_literal("NULL") == "NULL"
    # 'inf'/'nan' are NOT numeric literals in SQL and must be quoted.
    assert as_sql_literal("inf") == "'inf'"
    assert as_sql_literal("nan") == "'nan'"


def test_column_add_clause_for_role_is_valid_sqlite_ddl():
    """End-to-end: the generated clause must actually execute, and preserve
    the default value on both existing and newly inserted rows."""
    from wsi_app.main import _column_add_clause
    from wsi_app.models import User

    clause = _column_add_clause(User.__table__.c.role)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO users (id) VALUES (1)")
    db.execute(f"ALTER TABLE users ADD COLUMN {clause}")
    db.execute("INSERT INTO users (id) VALUES (2)")
    rows = dict(db.execute("SELECT id, role FROM users").fetchall())
    assert rows == {1: "instructor", 2: "instructor"}


def test_column_add_clause_leaves_function_defaults_nullable():
    """Why the drift exists at all, pinned as behaviour.

    SQLite's ALTER TABLE rejects a function-valued DEFAULT, so the reconciler
    drops it and adds the column nullable — even where the ORM says NOT NULL.
    ``_warn_nullability_divergence`` is the counterpart that surfaces the
    result; this documents the cause.
    """
    from wsi_app.main import _column_add_clause
    from wsi_app.models import Slide

    clause = _column_add_clause(Slide.__table__.c.created_at)
    assert "DEFAULT" not in clause.upper()
    assert "NOT NULL" not in clause.upper()
