"""Ordered, explicit schema migrations — the escape hatch the automatic
column reconciler can't cover.

Two mechanisms keep the SQLite schema in step with the ORM:

* ``main._apply_column_migrations`` — reflects the live table shape and
  ALTERs in any *added* column / drops any *removed* column automatically.
  This handles the common, purely-additive case with zero bookkeeping: add
  a ``mapped_column`` and it appears on every existing DB at the next boot.

* This package — an ordered list of numbered migrations for changes the
  reconciler cannot express: data back-fills, column renames/splits, type
  changes, table rebuilds (UNIQUE/FK reshapes SQLite can't ALTER in place),
  and one-off data repair.

The two are complementary. ``lifespan`` runs the reconciler's ADD phase,
then these migrations, then the reconciler's DROP phase, so a migration can
populate a freshly-added column *and* read a soon-to-be-dropped one before
it disappears.

Deciding where a change goes
----------------------------
* Add a column / table          → just declare it on the ORM model. The
  reconciler adds it everywhere; no migration needed.
* Remove a column               → delete it from the model; the reconciler's
  DROP phase removes it.
* Remove a table                → delete the model AND append a migration
  that drops it (the reconciler never drops tables).
* Anything transformational     → append a numbered migration here.

Versioning
----------
``schema_version`` is a monotonic integer stored in ``app_meta`` (key
``app_meta.KEY_SCHEMA_VERSION``), independent of the human-facing
``app_version`` string: ``app_version`` can move without any schema change,
``schema_version`` moves only when the data shape does.

``BASELINE_SCHEMA_VERSION`` is the shape this codebase ships as its starting
point — the shape ``Base.metadata.create_all`` builds today. Migrations are
numbered *above* it, and ``CURRENT_SCHEMA_VERSION`` is the highest version
this binary knows how to produce, so appending a migration raises it
automatically (no separate constant to forget to bump).

The baseline is a floor, not a decoration: it must never be lowered. Every
DB in existence records at least that value, and
``app_meta.assert_schema_not_downgrade`` refuses to boot a binary whose
``CURRENT_SCHEMA_VERSION`` is below what the DB records — so lowering the
floor locks the app out of its own database.

Authoring rules (IMPORTANT)
---------------------------
Migrations run against the *reconciled, current-shape* schema (create_all +
the ADD phase already ran), NOT a reconstructed historical snapshot the way
Alembic replays each revision. Therefore every migration MUST be:

* **defensive** — guard on the actual presence of the columns/tables it
  touches (``PRAGMA table_info`` / ``sqlite_master``); a fresh install
  builds the current shape directly and must be a clean no-op. The runner
  does apply every migration to it: a DB without a recorded
  ``schema_version`` starts from the baseline, fresh or not.
* **idempotent** — safe to re-run; gate writes on the data conditions that
  make them necessary, so a re-run doesn't clobber operator intent.

Each migration commits together with its ``schema_version`` bump in one
transaction (opened with ``BEGIN IMMEDIATE``, so DDL is covered too), so a
crash mid-chain leaves the version at the last fully applied step and the
next boot resumes from there. Consequently a migration cannot rely on
statements that SQLite ignores or refuses inside a transaction (``VACUUM``
fails; ``PRAGMA foreign_keys`` is a silent no-op).

Foreign keys: the runner follows SQLite's documented procedure for changes
ALTER TABLE cannot express (https://www.sqlite.org/lang_altertable.html#otheralter)
— it turns ``foreign_keys`` OFF on the connection *before* opening each
step's transaction, runs ``PRAGMA foreign_key_check`` inside the transaction
before COMMIT (any violation raises and rolls the step back), then restores
the previous setting. So a migration may rebuild a referenced table (create
``new_x``, copy, ``DROP TABLE x``, ``ALTER TABLE new_x RENAME TO x``) without
``ON DELETE CASCADE`` wiping the child rows; it must simply leave every
reference valid by the time it returns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from sqlalchemy.engine import Connection, Engine

from .. import app_meta

_logger = logging.getLogger("wsi_app.migrations")


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    upgrade: Callable[[Connection], None]


# The floor every database records; the migrations below raise the shape
# ``create_all`` builds from here. Never edit it downward (see the module
# docstring). A database recording a lower version is refused
# (``assert_schema_floor``). The first public release ships at 6 (migration
# 6 included), the oldest schema a released build writes.
BASELINE_SCHEMA_VERSION: int = 5


def _fence_staff_mfa_columns(conn: Connection) -> None:
    """Raises ``schema_version`` to 6 for the staff two-factor and perf
    summary columns (``users.totp_secret_enc``, ``totp_enabled_at``,
    ``totp_last_step``, ``mfa_recovery_codes`` and ``perf_runs.summary_json``).
    Nothing to transform: the reconciler adds them, NULL on every existing
    row, before the migrations run. Slide bundles carry none of these
    columns, so ``slide_transfer.MIN_BUNDLE_SCHEMA_VERSION`` stays 5.
    """


# Ordered ascending by version. Append new migrations here, numbered above
# BASELINE_SCHEMA_VERSION; never renumber or reorder a migration that may
# already have run in the field.
MIGRATIONS: List[Migration] = [
    Migration(6, "add staff two-factor and perf summary columns", _fence_staff_mfa_columns),
]

# Fail fast on a duplicate / out-of-order registration.
assert [m.version for m in MIGRATIONS] == sorted(
    {m.version for m in MIGRATIONS}
), "MIGRATIONS must have unique, ascending version numbers"

# A migration at or below the baseline could never run: the baseline is the
# lowest version any DB records, so the runner would always skip it.
assert all(m.version > BASELINE_SCHEMA_VERSION for m in MIGRATIONS), (
    f"every migration must be numbered above BASELINE_SCHEMA_VERSION "
    f"({BASELINE_SCHEMA_VERSION}); such a migration would never run"
)

CURRENT_SCHEMA_VERSION: int = max(
    [BASELINE_SCHEMA_VERSION, *(m.version for m in MIGRATIONS)]
)


def _stamp(conn: Connection, version: int) -> None:
    conn.exec_driver_sql(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (app_meta.KEY_SCHEMA_VERSION, str(int(version))),
    )


def _foreign_keys_enabled(conn: Connection) -> bool:
    # No row at all means SQLite was built without FK support; treat that as
    # "off" (there is nothing to toggle or restore).
    return bool(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())


def _assert_no_fk_violations(conn: Connection, m: Migration) -> None:
    """Refuse to commit a step that left dangling foreign keys.

    Runs INSIDE the step's transaction, before COMMIT, so raising here rolls
    the step back together with its stamp. With enforcement off for the step
    (see ``_run_step``) nothing else would notice a rebuild that dropped or
    renumbered parent rows.
    """
    rows = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if rows:
        # (table, rowid, parent, fkid) — a handful is enough to diagnose.
        sample = ", ".join(
            f"{r[0]} rowid={r[1]} -> {r[2]}" for r in rows[:5]
        )
        raise RuntimeError(
            f"schema migration {m.version} ({m.description}) left "
            f"{len(rows)} foreign key violation(s): {sample}; rolled back"
        )


def _run_step(engine: Engine, m: Migration) -> None:
    """Apply one migration and its ``schema_version`` stamp atomically.

    Follows SQLite's documented procedure for schema changes that ALTER
    TABLE cannot express (https://www.sqlite.org/lang_altertable.html#otheralter):

    1. ``PRAGMA foreign_keys=OFF`` — OUTSIDE the transaction, because the
       pragma is a silent no-op inside one. ``db.py`` turns enforcement on
       for every pooled connection, so without this a table rebuild (create
       new, copy, ``DROP`` old, rename) would fire ``ON DELETE CASCADE`` on
       the ``DROP`` and delete every child row — and commit it silently.
    2. ``BEGIN IMMEDIATE``, the migration, the stamp.
    3. ``PRAGMA foreign_key_check`` before COMMIT, so a step that left
       dangling references rolls back instead of committing corruption that
       enforcement would otherwise have caught.
    4. Restore the connection's previous ``foreign_keys`` setting after the
       transaction ends (commit or rollback), because the connection goes
       back to the shared pool.
    """
    with engine.connect() as conn:
        fk_was_on = _foreign_keys_enabled(conn)
        try:
            # pysqlite's legacy transaction mode does not open a transaction
            # for a PRAGMA, so this runs in autocommit; SQLAlchemy's own
            # (autobegun) transaction is empty and is closed by the commit()
            # below before conn.begin().
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            if _foreign_keys_enabled(conn):
                # Only possible if a transaction was somehow already open;
                # proceeding would re-expose the cascade this guards against.
                raise RuntimeError(
                    "could not disable foreign key enforcement for schema "
                    f"migration {m.version}"
                )
            conn.commit()
            # The migration and its version bump commit atomically: a failure
            # rolls back both, leaving schema_version at the last good step.
            with conn.begin():
                # pysqlite's default (legacy) transaction mode opens a
                # transaction only before INSERT/UPDATE/DELETE, so DDL issued
                # first would run in autocommit and survive a later failure.
                # Open the transaction explicitly; the driver's
                # commit/rollback then covers every statement of the step,
                # DDL included.
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                m.upgrade(conn)
                _assert_no_fk_violations(conn, m)
                _stamp(conn, m.version)
        finally:
            if fk_was_on:
                try:
                    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                    conn.commit()
                except Exception:
                    # Never hand an FK-off connection back to the pool: every
                    # later request on it would skip ON DELETE CASCADE.
                    _logger.warning(
                        "could not re-enable foreign keys after schema "
                        "migration %d; discarding the connection", m.version,
                        exc_info=True,
                    )
                    conn.invalidate()


def run_migrations(engine: Engine, *, recorded_sv: Optional[int]) -> List[int]:
    """Apply pending migrations and stamp ``schema_version``.

    Must run under ``main._startup_schema_lock`` (one worker only), AFTER
    ``create_all`` + the ADD phase of ``_apply_column_migrations`` and BEFORE
    its DROP phase.

    ``recorded_sv`` is the DB's stamped ``schema_version``, or ``None`` when
    the ``app_meta`` table / key does not exist — a brand-new DB that
    ``create_all`` has just built in the current shape. It starts from
    ``BASELINE_SCHEMA_VERSION``: every migration is defensive and idempotent
    (see the module docstring), so on a fresh shape each one is a no-op.
    A recorded version below the baseline never reaches this function
    (``assert_schema_floor`` refuses it first).
    (``app_meta.read_recorded_schema_version`` raises rather than returning
    ``None`` if the key cannot be read for any *other* reason.)

    Returns the list of versions actually applied (for logging/tests).
    """
    baseline = BASELINE_SCHEMA_VERSION if recorded_sv is None else recorded_sv
    applied: List[int] = []
    for m in MIGRATIONS:
        if m.version <= baseline:
            continue
        _logger.info("applying schema migration %d: %s", m.version, m.description)
        _run_step(engine, m)
        applied.append(m.version)

    # Guarantee the key equals CURRENT even when no migration ran (e.g.
    # recorded_sv already == CURRENT). Only reached on full success — a
    # raising migration propagates out of the loop above. Never lowers the
    # value (a recorded newer version is already blocked by the downgrade
    # guard, but be defensive).
    with engine.begin() as conn:
        _stamp(conn, max(baseline, CURRENT_SCHEMA_VERSION))
    return applied


def assert_schema_floor(
    recorded_sv: Optional[int], *, app_version_recorded: bool = False
) -> None:
    """Refuse a database stamped below ``BASELINE_SCHEMA_VERSION``.

    No migration below the baseline exists, so such a database cannot be
    brought to this binary's shape; stamping it current without the transformations it missed would
    serve it in a shape nothing was written for. ``None`` passes only for a
    brand-new DB: one that already records an ``app_version`` but no
    ``schema_version`` was not written by this binary, and is refused like
    any other database below the floor. (The boot stamps
    ``schema_version`` before ``app_version``, so no database this binary
    writes ends up with the one and not the other.)
    """
    if recorded_sv is None and app_version_recorded:
        raise RuntimeError(
            "database records an app_version but no schema_version; it was "
            "written by a pre-release build and cannot be upgraded"
        )
    if recorded_sv is not None and recorded_sv < BASELINE_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema_version {recorded_sv} is older than the oldest "
            f"this build supports ({BASELINE_SCHEMA_VERSION}); it was written "
            f"by a pre-release build and cannot be upgraded"
        )
