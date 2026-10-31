"""Read/write the ``app_meta`` table and detect downgrade attempts.

The ``app_meta`` table is a tiny key-value store (see :class:`AppMeta` in
``models.py``). It records which APP_VERSION last opened this DB and the
integer ``schema_version`` (the data shape), primarily for diagnostics
(admin UI, backup manifests) and to block a restore/boot that would put a
newer-than-binary DB into place.

Schema sync between the ORM and the live DB is handled at startup by two
complementary mechanisms:

* ``main._apply_column_migrations`` reflects the actual table shape, ALTERs
  in any columns the ORM declares but the DB lacks, and drops columns the
  ORM no longer declares. Purely additive/removable changes need no
  hand-written migration; the live boot reconciles the difference.
* The ``migrations`` package holds ordered, numbered migrations for changes
  the reconciler can't express (back-fills, renames, type changes, table
  rebuilds). ``migrations.run_migrations`` stamps ``schema_version`` as it
  applies them.

Downgrade guards exist for both dimensions: :func:`assert_not_downgrade`
(app_version string) and :func:`assert_schema_not_downgrade` (schema_version
integer).

Every database this app writes carries both keys, so a missing ``app_meta``
table means one of two things and the readers are split accordingly. The
sqlite3-level readers work on staged archives, where it means the file was
not written by this application, and return ``None`` for the caller to
reject. The engine-level readers work on the live DB before ``create_all``,
where it means the DB is brand new.

:func:`read_recorded_schema_version` is the strict one: its ``None`` ("no
stamp") makes ``migrations.run_migrations`` start from the baseline and lets
:func:`assert_schema_not_downgrade` pass, so it raises on any failure that is
*not* "the key is absent" rather than let a bad read or an unreadable stamp —
which could be hiding a newer schema — pass as "no stamp".
:func:`read_recorded_app_version` keeps failing open — its result only feeds
a guard, never a decision about what to run, and ``bootstrap_app_version``
re-checks the same thing later against the reconciled schema.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import AppMeta, AuditLog
from . import version as _version
from .version import APP_VERSION

_logger = logging.getLogger("wsi_app.app_meta")

KEY_APP_VERSION = "app_version"
KEY_APP_VERSION_SEEN_AT = "app_version_seen_at"
KEY_DB_INITIALIZED_AT = "db_initialized_at"
# Monotonic integer describing the *data shape* this DB represents, bumped
# only when the schema changes (see the ``migrations`` package), independent
# of the human-facing app_version string. Stamped by
# ``migrations.run_migrations`` and carried into backup/export manifests.
KEY_SCHEMA_VERSION = "schema_version"


def get(db: Session, key: str) -> Optional[str]:
    row = db.get(AppMeta, key)
    return row.value if row else None


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(AppMeta, key)
    if row is None:
        db.add(AppMeta(key=key, value=value))
    else:
        row.value = value


# sqlite3-level helpers (used by backup/restore which work directly on a file).
def sqlite_read_app_version(path: str) -> Optional[str]:
    """Read ``app_meta.app_version`` directly from a SQLite file.

    Used during restore staging to inspect an archive's DB without opening it
    through SQLAlchemy. Returns ``None`` if the table or the key is absent —
    which :func:`backup.stage_restore` treats as a corrupt archive, since
    every DB this app writes carries both.
    """
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_meta'"
        )
        if cur.fetchone() is None:
            return None
        cur = conn.execute("SELECT value FROM app_meta WHERE key=?", (KEY_APP_VERSION,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def sqlite_set_app_version(path: str, version: str) -> None:
    """Write ``app_meta.app_version`` directly into a SQLite file.

    The target is always a staged archive DB that :func:`backup.stage_restore`
    has already validated, so ``app_meta`` is known to exist; a missing table
    here means the stage was tampered with after validation and the raised
    ``OperationalError`` is the right outcome.
    """
    conn = sqlite3.connect(path)
    try:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY_APP_VERSION, version),
        )
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY_APP_VERSION_SEEN_AT, now),
        )
        conn.commit()
    finally:
        conn.close()


def _coerce_schema_version(value: object) -> Optional[int]:
    """Parse a stored ``schema_version`` cell into an int, or None if the
    cell is missing/blank/non-numeric (treated as "unknown")."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def sqlite_read_schema_version(path: str) -> Optional[int]:
    """Read ``app_meta.schema_version`` directly from a SQLite file.

    Returns ``None`` when the table/key is absent or unparseable. Every DB
    this app writes is stamped, so ``None`` means a corrupt or foreign
    archive rather than a legitimate state; :func:`backup.stage_restore`
    rejects it.
    """
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_meta'"
        )
        if cur.fetchone() is None:
            return None
        cur = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (KEY_SCHEMA_VERSION,)
        )
        row = cur.fetchone()
        return _coerce_schema_version(row[0]) if row else None
    finally:
        conn.close()


def sqlite_set_schema_version(path: str, version: int) -> None:
    """Write ``app_meta.schema_version`` directly into a SQLite file.

    Same precondition as :func:`sqlite_set_app_version`: the file is a staged
    archive DB whose ``app_meta`` table has already been validated.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY_SCHEMA_VERSION, str(int(version))),
        )
        conn.commit()
    finally:
        conn.close()


# Sortable ranks for prerelease / final / post-release modifiers. Tuple
# comparison then orders versions correctly: alpha < beta < rc < final < post.
_RANK_ALPHA = 1
_RANK_BETA = 2
_RANK_RC = 3
_RANK_FINAL = 4
_RANK_POST = 5

_PRERELEASE_RANKS = {
    "a": _RANK_ALPHA, "alpha": _RANK_ALPHA,
    "b": _RANK_BETA, "beta": _RANK_BETA,
    "rc": _RANK_RC, "c": _RANK_RC, "pre": _RANK_RC,
}

_NUM_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_GIT_DESCRIBE_RE = re.compile(r"^-(\d+)-g[0-9a-f]+$")
# Optional dot so semver-style "-rc.1" parses like "-rc1"; the optional
# trailing group is a git-describe build past a prerelease tag
# ("0.2.0-rc1-3-gabc1234").
_PRERELEASE_RE = re.compile(r"^-?([a-zA-Z]+)\.?(\d*)(?:-(\d+)-g[0-9a-f]+)?$")
# ``git describe --tags --always`` falls back to a bare abbreviated hash when
# no tag is reachable (e.g. a shallow clone). One that starts with digits
# ("1234abc") would otherwise parse as version 1234 and, once recorded, make
# every real release look like a downgrade.
_GIT_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")


def parse_version(v: str) -> Optional[tuple]:
    """Parse a version string into a comparable tuple, or None if it isn't one.

    The one version ordering in the app: the downgrade guards here and the
    upstream release check (``update_check``) both use it. A leading ``v``
    (a git tag name, ``"v0.1.0"``) is ignored.

    Recognised suffix forms after the numeric ``X.Y.Z`` prefix:

    * Bare release            ``"0.1.0"``           → ``(0, 1, 0, 4, 0)``
    * Prerelease              ``"0.1.0-rc1"``       → ``(0, 1, 0, 3, 1)``
                              ``"0.1.0-rc.1"``      → ``(0, 1, 0, 3, 1)``
                              ``"0.1.0a2"``         → ``(0, 1, 0, 1, 2)``
    * git-describe post-tag   ``"0.1.0-3-gabc1234"``→ ``(0, 1, 0, 5, 3)``
    * … past a prerelease     ``"0.1.0-rc1-3-gabc"``→ ``(0, 1, 0, 3, 1, 3)``
      (above rc1, below rc2 and the final release)

    None for anything without a numeric prefix ("unknown") and for a bare
    git hash, including one that happens to start with digits.
    """
    s = v.strip()
    if s[:1] in ("v", "V") and s[1:2].isdigit():
        s = s[1:]
    if "." not in s and _GIT_HASH_RE.match(s):
        return None
    m = _NUM_PREFIX_RE.match(s)
    if m is None:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    # Pad the numeric prefix to a fixed 3 components before appending the rank
    # pair, so a 2-component version compares correctly against a 3-component
    # one. Without this, "0.2" -> (0,2,RANK_FINAL,0) and "0.2.0" ->
    # (0,2,0,RANK_FINAL,0) and tuple comparison ranks (0,2) ABOVE (0,2,0)
    # because RANK_FINAL > 0 at index 2 — making is_downgrade("0.2","0.2.0")
    # wrongly True and refusing to boot against the DB's own version.
    if len(nums) < 3:
        nums = nums + (0,) * (3 - len(nums))
    rest = s[m.end():]
    if not rest:
        return nums + (_RANK_FINAL, 0)
    gd = _GIT_DESCRIBE_RE.match(rest)
    if gd is not None:
        return nums + (_RANK_POST, int(gd.group(1)))
    pr = _PRERELEASE_RE.match(rest)
    if pr is not None:
        rank = _PRERELEASE_RANKS.get(pr.group(1).lower())
        if rank is not None:
            num = int(pr.group(2)) if pr.group(2) else 0
            if pr.group(3):
                # A longer tuple with an equal prefix sorts after the shorter
                # one, so this lands between the tag and the next prerelease.
                return nums + (rank, num, int(pr.group(3)))
            return nums + (rank, num)
    return nums + (_RANK_FINAL, 0)


def is_downgrade(recorded: str, current: str) -> bool:
    """True if ``recorded`` > ``current`` (a downgrade attempt). Equality is False.

    Unparseable versions (see :func:`parse_version`) return False so the
    mismatch surfaces through another code path rather than a hard downgrade
    error — a dev build stamped "unknown" or a bare git hash must not refuse
    to boot against a DB a release opened, nor the reverse.
    """
    try:
        r = parse_version(recorded)
        c = parse_version(current)
        if r is None or c is None:
            return False
        return r > c
    except Exception:
        return False


def refuses_app_downgrade(recorded: str, current: str, *, what: str) -> bool:
    """Whether moving ``recorded`` onto the running binary (``current``) must
    be refused as an app_version downgrade.

    :func:`is_downgrade`, except when the running version is only the
    ``frontend/package.json`` fallback (no ``VERSION`` stamp, no reachable git
    tag — see ``version.resolve_version``). That value does not follow the
    release tags and can sit far below the code actually running, so
    trusting it would refuse to boot against a DB this very build stamped, or
    to restore its own backups. The comparison is skipped with a WARNING
    instead; the ``schema_version`` guards, which compare the data shape the
    binary really produces, still apply.

    ``what`` names the operation for that warning ("boot", "restore", ...).
    """
    if not is_downgrade(recorded, current):
        return False
    if _version.is_authoritative(_version.APP_VERSION_SOURCE):
        return True
    _logger.warning(
        "app_version downgrade guard skipped for %s: %r is newer than the "
        "running %r, but the running version is only the %s fallback (no "
        "VERSION file and no git tag), which cannot be trusted to order "
        "releases. The schema_version guard still applies.",
        what, recorded, current, _version.APP_VERSION_SOURCE,
    )
    return False


def read_recorded_app_version(engine: Engine) -> Optional[str]:
    """Read ``app_meta.app_version`` through ``engine``, tolerating a missing
    table.

    Unlike :func:`get`, this must work *before* ``create_all`` /
    ``_apply_column_migrations`` have run, when the ``app_meta`` table may not
    exist yet (a genuinely fresh DB) and an ORM ``select(AppMeta)`` would raise.
    Any read error is treated as "no recorded version" so a transient problem
    never blocks boot (the downgrade guard fails open; the later
    ``bootstrap_app_version`` check still runs on the reconciled schema).
    """
    try:
        with engine.connect() as conn:
            exists = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='app_meta'"
            ).fetchone()
            if exists is None:
                return None
            row = conn.exec_driver_sql(
                "SELECT value FROM app_meta WHERE key=?", (KEY_APP_VERSION,)
            ).fetchone()
            return row[0] if row else None
    except Exception:
        _logger.warning(
            "could not read recorded app_version for downgrade guard",
            exc_info=True,
        )
        return None


def assert_not_downgrade(engine: Engine) -> None:
    """Refuse to boot when the DB was last opened by a newer binary.

    MUST be called *before* ``create_all`` / ``_apply_column_migrations``: the
    reconciler drops every column the running ORM no longer declares, so an
    older binary run against a newer DB would ``DROP COLUMN`` (and destroy the
    data in) the newer release's columns and commit — all *before*
    ``bootstrap_app_version``'s own downgrade check gets a chance to raise.
    Checking here makes the boot abort while the data is still intact.
    """
    recorded = read_recorded_app_version(engine)
    if recorded is not None and refuses_app_downgrade(
        recorded, APP_VERSION, what="boot"
    ):
        raise RuntimeError(
            f"DB was last opened by app version {recorded!r}, which is newer "
            f"than the running binary {APP_VERSION!r}. Downgrades are not "
            f"supported; refusing to run destructive schema migrations. "
            f"Either upgrade the binary, or restore from a backup created "
            f"with the older version."
        )


def read_recorded_schema_version(engine: Engine) -> Optional[int]:
    """Read ``app_meta.schema_version`` through ``engine``.

    Like :func:`read_recorded_app_version`, this must work *before*
    ``create_all`` has run — the ``app_meta`` table genuinely does not exist
    on a brand-new DB.

    ``None`` means exactly one thing: **the DB carries no stamp**, because the
    table or the key is absent — a brand-new DB.
    ``migrations.run_migrations`` runs every migration above the baseline for
    it (they are written to be no-ops on a shape that does not need them).
    It is NOT a catch-all for "could not read". Any other failure propagates,
    deliberately: a swallowed error would also read as "no stamp", letting a
    DB stamped by a *newer* binary past :func:`assert_schema_not_downgrade`
    and into this binary's migrations and DROP phase. Refusing to boot is the
    recoverable outcome; a reshaped-down database is not.
    """
    with engine.connect() as conn:
        exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='app_meta'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.exec_driver_sql(
            "SELECT value FROM app_meta WHERE key=?", (KEY_SCHEMA_VERSION,)
        ).fetchone()
        if row is None:
            return None
        recorded = _coerce_schema_version(row[0])
        if recorded is None:
            # The key is present, so this DB is NOT new — it is stamped with
            # something unreadable (a botched restore, a hand-edited cell).
            # Returning None here would read as "no stamp": the schema
            # downgrade guard would pass whatever version the cell once held
            # and the DB would be re-stamped at CURRENT, with no later boot
            # able to tell. Refuse instead: that stays recoverable.
            raise RuntimeError(
                f"app_meta.{KEY_SCHEMA_VERSION} holds {row[0]!r}, which is not "
                f"a schema version. Refusing to run migrations against a "
                f"database whose recorded version cannot be established; "
                f"restore from a backup taken with a known-good version."
            )
        return recorded


def assert_schema_not_downgrade(engine: Engine, current: int) -> None:
    """Refuse to boot when the DB records a *higher* ``schema_version`` than
    this binary can produce.

    The schema-precise sibling of :func:`assert_not_downgrade` (which compares
    the human app_version string). A newer binary bumps ``schema_version``
    when it reshapes tables; an older binary run against that DB would let
    ``main._apply_column_migrations`` DROP the newer columns. Checked here,
    before any DDL, so the boot aborts while the data is still intact.

    A ``None`` recorded version means an unstamped, brand-new DB, which has
    nothing to downgrade from and must still boot.
    """
    recorded = read_recorded_schema_version(engine)
    if recorded is not None and recorded > current:
        raise RuntimeError(
            f"DB schema_version {recorded} is newer than this binary supports "
            f"({current}). Downgrades are not supported; refusing to run "
            f"destructive schema migrations. Either upgrade the binary, or "
            f"restore from a backup created with the older version."
        )


def bootstrap_app_version(engine: Engine) -> None:
    """Record the running binary's version in ``app_meta`` and refuse to
    start when the DB was last opened by a newer binary.

    Schema reconciliation is done earlier in the lifespan by
    ``main._apply_column_migrations`` (additive ALTER TABLE / DROP COLUMN
    against the live ORM), so this function only handles the metadata
    side: stamp + downgrade guard.
    """
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    with Session(engine) as db:
        recorded = get(db, KEY_APP_VERSION)
        if recorded is not None and is_downgrade(recorded, APP_VERSION):
            if _version.is_authoritative(_version.APP_VERSION_SOURCE):
                raise RuntimeError(
                    f"DB was last opened by app version {recorded!r}, which is "
                    f"newer than the running binary {APP_VERSION!r}. Downgrades "
                    f"are not supported. Either upgrade the binary, or restore "
                    f"from a backup created with the older version."
                )
            # Only the package.json fallback gets here, and
            # assert_not_downgrade has already logged the skipped guard. Keep
            # the newer stamp: overwriting it with an untrusted lower value
            # would disarm the guard for the next build that knows its version.
            return
        if recorded is None and get(db, KEY_DB_INITIALIZED_AT) is None:
            set_value(db, KEY_DB_INITIALIZED_AT, now)
        if recorded != APP_VERSION:
            _logger.info(
                "app_meta: stamping app_version %s -> %s",
                recorded or "(fresh)", APP_VERSION,
            )
            if recorded is not None:
                # Genuine upgrade (not a fresh install): record it in the
                # admin audit history. Written directly (not via audit())
                # so it shares the bootstrap transaction; the table exists
                # because create_all runs earlier in the lifespan, and the
                # startup schema lock guarantees only one worker observes
                # the version change.
                db.add(AuditLog(
                    actor_id=None,
                    actor_username="",
                    actor_role=None,
                    action="system.version_upgrade",
                    target=APP_VERSION,
                    details=json.dumps({"from": recorded, "to": APP_VERSION}),
                ))
        set_value(db, KEY_APP_VERSION, APP_VERSION)
        set_value(db, KEY_APP_VERSION_SEEN_AT, now)
        db.commit()
