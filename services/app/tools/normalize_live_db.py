#!/usr/bin/env python3
"""One-off normaliser for a tessera database upgraded in place.

Brings a database whose schema was evolved in place by the boot-time
reconciler back in line with what ``create_all`` would build today.

Why this exists
---------------
The app absorbs schema changes in place:
``main._apply_column_migrations`` reflects the live table shape and
``ALTER TABLE ADD COLUMN``s whatever the ORM declares but the DB lacks.
SQLite's ``ADD COLUMN`` cannot carry ``NOT NULL`` without a *constant*
default, so any column whose default
is function-valued (``func.now()``) or Python-side (``_mint_public_id``)
arrives **nullable and empty** on a DB that was upgraded in place, while a
freshly created DB gets the strict shape straight from ``create_all``.

The two shapes therefore drift apart silently, and code that reads these
columns must otherwise tolerate NULLs; this script removes that drift.

What it does
------------
1. Reads the ORM (``wsi_app.models``) as the sole source of truth for the
   target shape.
2. Back-fills NULL cells in every column the ORM marks NOT NULL.
3. Mints ``public_id`` tokens for slide/tour rows that lack one (the value is
   minted in Python, so there is no constant to back-fill).
4. Rebuilds any table whose live nullability disagrees with the ORM, using
   SQLite's documented table-rebuild procedure, and recreates the ORM's
   indexes on it.

``main._warn_nullability_divergence`` reports exactly the drift this repairs,
so a "schema drift" line in the boot log is the cue to run this.

Safety model
------------
Nothing is done in place. The script checkpoints the WAL, copies the DB
through SQLite's own backup API (so a live WAL cannot be missed), operates
**only on that copy**, verifies the result, and only then swaps it in — after
writing a timestamped ``.bak`` of the original alongside. Any failure leaves
the original file untouched.

It refuses to invent a value for a NOT NULL *foreign key* column: a NULL
parent id is an orphaned row, and deleting or re-parenting it is an operator
decision, not this script's. Such a case aborts with the offending row ids.

Usage
-----
    # Stop both services first — the DB must be quiescent, and wsi-sync holds
    # its own handles that would otherwise stay on the file swapped aside.
    sudo systemctl stop wsi-app wsi-sync

    # 1. Inspect only (default). Reports what would change, writes nothing.
    sudo -u wsi /opt/wsi/venv/bin/python normalize_live_db.py /var/lib/wsi/wsi.db

    # 2. Apply, after reading the report.
    sudo -u wsi /opt/wsi/venv/bin/python normalize_live_db.py /var/lib/wsi/wsi.db --apply

    sudo systemctl start wsi-app wsi-sync

Run it as the service user (``wsi``), not as root. The deployed venv and the
``wsi_app`` package are owned by that user, so root running them would be
running code the service user can rewrite; and the files this tool creates
beside the database (the ``.bak`` and the working copy, with predictable
names) sit in a directory only that user can write, where a planted symlink
would be followed by a root process. As the owner it needs no more rights than
the database already gives the service, and the swapped-in file keeps its
owner without a chown. The tool refuses to run as root on a database a
non-root user owns.

The script may live anywhere — copy it to /tmp on the server if that is
easier. It needs SQLAlchemy and the ``wsi_app`` package, which it locates in
this order: ``--app-dir``, ``$WSI_APP_DIR``, its own ``../`` (when it sits in
the repo's ``services/app/tools/``), its own directory (when dropped straight
into ``services/app/``), then the deployed
``/opt/wsi/services/app``. Running it with the deployed venv's interpreter
(``/opt/wsi/venv/bin/python``) satisfies the SQLAlchemy half.

It touches no other part of the app: no config, no auth, no engine — only
the ORM metadata and raw ``sqlite3``.
"""
from __future__ import annotations

import argparse
import datetime
import os
import pwd
import secrets
import shutil
import sqlite3
import sys
from typing import Iterable, Optional

# Populated by _load_orm() once the app directory is known. The ORM import is
# deferred (rather than done at module scope) so --help and the argument
# errors work on a machine where the package cannot be found.
Base = None
CreateIndex = CreateTable = None
_DIALECT = None
_column_default_literal = None

_DEPLOYED_APP_DIR = "/opt/wsi/services/app"


def _candidate_app_dirs(explicit: Optional[str]) -> list[str]:
    """Where ``wsi_app`` might live, most specific first."""
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        d for d in (
            explicit,
            os.environ.get("WSI_APP_DIR"),
            os.path.dirname(here),   # repo checkout: services/app/tools/ -> services/app
            here,                    # dropped straight into services/app/
            _DEPLOYED_APP_DIR,
        ) if d
    ]


def _load_orm(explicit: Optional[str]) -> str:
    """Put the app directory on sys.path and import the ORM metadata.

    Imports ``wsi_app.models`` and the dependency-free
    ``wsi_app.column_defaults`` (the reconciler's own default renderer, so the
    back-fill writes exactly the literal a fresh ``ADD COLUMN`` would). That
    pulls in ``db`` (for ``Base``) and nothing that needs runtime
    configuration — no JWT secret, no data dir — so this stays a read-only
    look at the declared schema.
    """
    global Base, CreateIndex, CreateTable, _DIALECT, _column_default_literal

    tried = _candidate_app_dirs(explicit)
    app_dir = next(
        (d for d in tried if os.path.isdir(os.path.join(d, "wsi_app"))), None
    )
    if app_dir is None:
        raise SystemExit(
            "error: could not find the wsi_app package. Looked in:\n"
            + "\n".join(f"  {d}" for d in tried)
            + "\n\nPass --app-dir /opt/wsi/services/app (or set WSI_APP_DIR)."
        )
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    try:
        from sqlalchemy.dialects import sqlite as sqlite_dialect
        from sqlalchemy.schema import CreateIndex as _CreateIndex
        from sqlalchemy.schema import CreateTable as _CreateTable

        from wsi_app.column_defaults import column_default_literal
        from wsi_app.models import Base as _Base
    except ImportError as exc:
        raise SystemExit(
            f"error: {exc}\n\nRun this with the deployed virtualenv's "
            f"interpreter, which already has SQLAlchemy:\n"
            f"  sudo /opt/wsi/venv/bin/python {os.path.basename(__file__)} <db>"
        ) from exc

    Base = _Base
    CreateIndex, CreateTable = _CreateIndex, _CreateTable
    _column_default_literal = column_default_literal
    _DIALECT = sqlite_dialect.dialect()
    return app_dir


# ---------------------------------------------------------------------------
# Target shape
# ---------------------------------------------------------------------------

# Tables whose ``public_id`` is minted in Python rather than carrying a
# constant default, so a NULL there is filled with a fresh unique token
# instead of a back-filled literal.
_PUBLIC_ID_TABLES = ("slides", "tours")


def _target_not_null(table_name: str) -> set[str]:
    """Column names the target shape requires to be NOT NULL."""
    table = Base.metadata.tables[table_name]
    return {c.name for c in table.columns if not c.nullable}


# ---------------------------------------------------------------------------
# Back-fill value policy
# ---------------------------------------------------------------------------

class OrphanRowError(RuntimeError):
    """A NOT NULL foreign-key column holds NULL — an orphaned row."""


def _backfill_sql(col) -> str:
    """SQL expression to write into NULL cells of a NOT NULL column.

    Prefers the column's own declared default. Falls back to a
    type-appropriate empty value, and refuses outright for a foreign key —
    inventing a parent id would silently re-parent an orphan.
    """
    if col.foreign_keys:
        raise OrphanRowError(col.name)
    literal = _column_default_literal(col)
    if literal is not None:
        return literal
    affinity = str(col.type).upper()
    if "DATETIME" in affinity or "TIMESTAMP" in affinity:
        # The real timestamp is unrecoverable; "now" is the only honest
        # answer and is reported in the summary so it isn't silent.
        return "CURRENT_TIMESTAMP"
    if "DATE" in affinity:
        return "CURRENT_DATE"
    if "BLOB" in affinity:
        return "x''"
    if any(k in affinity for k in ("INT", "REAL", "FLOAT", "NUMERIC", "DECIMAL")):
        return "0"
    if "BOOL" in affinity:
        return "0"
    return "''"


# ---------------------------------------------------------------------------
# Live-shape reflection
# ---------------------------------------------------------------------------

def _live_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """{name: {"notnull": bool, "type": str}} for a live table, {} if absent."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {r[1]: {"type": r[2], "notnull": bool(r[3])} for r in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count_nulls(conn: sqlite3.Connection, table: str, column: str) -> int:
    return conn.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL'
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class Finding:
    __slots__ = ("table", "column", "live_nullable", "null_rows", "reason")

    def __init__(self, table, column, live_nullable, null_rows, reason):
        self.table = table
        self.column = column
        self.live_nullable = live_nullable
        self.null_rows = null_rows
        self.reason = reason


def analyse(conn: sqlite3.Connection) -> tuple[list[Finding], list[str], list[str]]:
    """Compare the live DB against the target shape.

    Returns (findings, tables_needing_rebuild, missing_columns).
    """
    findings: list[Finding] = []
    rebuild: list[str] = []
    missing: list[str] = []

    for table in Base.metadata.sorted_tables:
        if not _table_exists(conn, table.name):
            # create_all builds it whole on the next boot; nothing to normalise.
            continue
        live = _live_columns(conn, table.name)
        required = _target_not_null(table.name)
        needs_rebuild = False
        for col_name in sorted(required):
            if col_name not in live:
                missing.append(f"{table.name}.{col_name}")
                continue
            live_nullable = not live[col_name]["notnull"]
            if not live_nullable:
                continue
            if table.name in _PUBLIC_ID_TABLES and col_name == "public_id":
                # An empty string satisfies NOT NULL but is not a usable
                # token, so it counts as unset here too.
                nulls = conn.execute(
                    f'SELECT COUNT(*) FROM "{table.name}" '
                    f'WHERE "{col_name}" IS NULL OR "{col_name}" = \'\''
                ).fetchone()[0]
                reason = "mint"
            else:
                nulls = _count_nulls(conn, table.name, col_name)
                reason = "backfill"
            needs_rebuild = True
            findings.append(Finding(table.name, col_name, True, nulls, reason))
        if needs_rebuild:
            rebuild.append(table.name)

    return findings, rebuild, missing


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def count_unset_public_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per table whose ``public_id`` is NULL or empty.

    Checked independently of the nullability findings: a token-less row is
    worth fixing even when the column is already NOT NULL, since an empty
    string satisfies the constraint without being a usable identifier.
    """
    out: dict[str, int] = {}
    for table in _PUBLIC_ID_TABLES:
        if not _table_exists(conn, table):
            continue
        if "public_id" not in _live_columns(conn, table):
            continue
        n = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" '
            f'WHERE public_id IS NULL OR public_id = \'\''
        ).fetchone()[0]
        if n:
            out[table] = n
    return out


def mint_public_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Give every slide/tour row missing a ``public_id`` a fresh unique token.

    Uniqueness is checked against the tokens already present in the same
    table, so a re-run cannot collide with what a previous run minted.
    """
    minted: dict[str, int] = {}
    for table in _PUBLIC_ID_TABLES:
        if not _table_exists(conn, table):
            continue
        if "public_id" not in _live_columns(conn, table):
            continue
        taken = {
            r[0] for r in conn.execute(
                f'SELECT public_id FROM "{table}" WHERE public_id IS NOT NULL'
            ) if r[0]
        }
        rows = conn.execute(
            f'SELECT id FROM "{table}" WHERE public_id IS NULL OR public_id = \'\''
        ).fetchall()
        for (row_id,) in rows:
            for _ in range(100):
                token = secrets.token_urlsafe(8)
                if token not in taken:
                    break
            else:  # pragma: no cover — 100 collisions on a 64-bit space
                raise RuntimeError(f"could not mint a unique public_id for {table}.{row_id}")
            taken.add(token)
            conn.execute(
                f'UPDATE "{table}" SET public_id = ? WHERE id = ?', (token, row_id)
            )
        if rows:
            minted[table] = len(rows)
    return minted


def backfill_nulls(conn: sqlite3.Connection, findings: Iterable[Finding]) -> dict[str, int]:
    """Fill NULL cells in NOT NULL-target columns. Raises on an orphan FK."""
    filled: dict[str, int] = {}
    orphans: list[str] = []
    for f in findings:
        if f.null_rows == 0 or f.reason == "mint":
            continue  # minted columns are handled by mint_public_ids
        col = Base.metadata.tables[f.table].columns[f.column]
        try:
            expr = _backfill_sql(col)
        except OrphanRowError:
            ids = [
                str(r[0]) for r in conn.execute(
                    f'SELECT id FROM "{f.table}" WHERE "{f.column}" IS NULL LIMIT 20'
                )
            ]
            orphans.append(
                f"  {f.table}.{f.column}: {f.null_rows} orphaned row(s) "
                f"(id: {', '.join(ids) or 'n/a'}{'...' if f.null_rows > 20 else ''})"
            )
            continue
        cur = conn.execute(
            f'UPDATE "{f.table}" SET "{f.column}" = {expr} WHERE "{f.column}" IS NULL'
        )
        filled[f"{f.table}.{f.column}"] = cur.rowcount
    if orphans:
        raise OrphanRowError(
            "NOT NULL foreign-key columns hold NULL — these rows are orphans "
            "whose parent is unknown. Inventing a parent id would silently "
            "re-parent them, so this script stops here. Delete or re-parent "
            "them by hand, then re-run:\n" + "\n".join(orphans)
        )
    return filled


def _has_sqlite_sequence(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
    ).fetchone() is not None


def rebuild_table(conn: sqlite3.Connection, table_name: str) -> None:
    """Rebuild one table with the target shape (SQLite's documented procedure).

    ``legacy_alter_table=ON`` (set by the caller) keeps the initial RENAME
    from rewriting the FK clauses of child tables: they must go on referring
    to the original name, which the new table re-takes moments later.
    """
    table = Base.metadata.tables[table_name]
    live = _live_columns(conn, table_name)
    # Copy only the columns both shapes agree on. A stale live column is
    # dropped by the rebuild; a column the live table lacks was reported as
    # `missing` and aborted the run before this point.
    shared = [c.name for c in table.columns if c.name in live]
    col_list = ", ".join(f'"{c}"' for c in shared)

    # AUTOINCREMENT's high-water mark lives in sqlite_sequence under the table
    # name and is dropped with the old table; carry it over so the ids of rows
    # deleted from the top of the range are not handed out again.
    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table_name,)
    ).fetchone() if _has_sqlite_sequence(conn) else None
    old_seq = int(seq_row[0]) if seq_row else 0

    tmp = f"{table_name}__normalize_old"
    conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{tmp}"')
    conn.execute(str(CreateTable(table).compile(dialect=_DIALECT)))
    conn.execute(
        f'INSERT INTO "{table_name}" ({col_list}) SELECT {col_list} FROM "{tmp}"'
    )
    conn.execute(f'DROP TABLE "{tmp}"')
    if old_seq > 0 and table.dialect_options["sqlite"]["autoincrement"]:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
            (old_seq, table_name),
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table_name, old_seq),
            )
    # DROP took the old indexes with it; rebuild what the ORM declares.
    for index in table.indexes:
        conn.execute(str(CreateIndex(index).compile(dialect=_DIALECT)))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(conn: sqlite3.Connection) -> list[str]:
    """Re-check the repaired DB. Returns a list of problems (empty = good)."""
    problems: list[str] = []

    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        problems.append(f"integrity_check: {row[0] if row else 'no result'}")

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        problems.append(f"foreign_key_check: {len(fk_violations)} violation(s)")

    findings, rebuild, missing = analyse(conn)
    for f in findings:
        problems.append(
            f"still nullable: {f.table}.{f.column} ({f.null_rows} NULL row(s))"
        )
    for m in missing:
        problems.append(f"still missing: {m}")

    # The invariant the public URLs rest on: no unset tokens may remain.
    for table in _PUBLIC_ID_TABLES:
        if not _table_exists(conn, table):
            continue
        unset = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE public_id IS NULL OR public_id = \'\''
        ).fetchone()[0]
        if unset:
            problems.append(f"{table}: {unset} row(s) without a public_id")
        dupes = conn.execute(
            f'SELECT COUNT(*) FROM (SELECT public_id FROM "{table}" '
            f'GROUP BY public_id HAVING COUNT(*) > 1)'
        ).fetchone()[0]
        if dupes:
            problems.append(f"{table}: {dupes} duplicated public_id value(s)")

    return problems


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    return conn


def _snapshot(src_path: str, dest_path: str) -> None:
    """Copy a SQLite DB through its own backup API (WAL-safe).

    The copy inherits the caller's umask, not the source's mode — running
    under sudo that would leave a 0644 root-owned file holding password
    hashes, session state and analytics identities next to a DB the installer
    deliberately keeps at 0640. Mirror the source's mode and ownership onto
    every copy instead.
    """
    src = sqlite3.connect(src_path)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    _mirror_ownership(src_path, dest_path)


def _mirror_ownership(src_path: str, dest_path: str) -> None:
    """Give ``dest_path`` the mode and owner/group of ``src_path``."""
    st = os.stat(src_path)
    os.chmod(dest_path, st.st_mode & 0o7777)
    try:
        os.chown(dest_path, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        # Not root, or the target FS has no ownership. The mode is the part
        # that matters for confidentiality and is already applied.
        pass


def _report(findings, rebuild, missing, unset_tokens) -> None:
    if missing:
        print("Columns the ORM declares but the live DB lacks:")
        for m in missing:
            print(f"  {m}")
        print(
            "\n  -> Start the app once so its boot-time reconciler adds them, "
            "then re-run this script."
        )
        return

    if not findings and not unset_tokens:
        print("Live shape already matches the target. Nothing to do.")
        return

    if findings:
        print(f"{len(findings)} column(s) diverge from the target shape:\n")
        width = max(len(f"{f.table}.{f.column}") for f in findings)
        for f in findings:
            label = f"{f.table}.{f.column}".ljust(width)
            note = ("nullable on disk, NOT NULL in the ORM")
            rows = (f" | {f.null_rows} row(s) need a value" if f.null_rows
                    else " | no NULL rows")
            print(f"  {label}  {note}{rows}")

    if unset_tokens:
        print("\nRows without a usable public_id:")
        for table, n in unset_tokens.items():
            print(f"  {table}: {n} row(s) will be given a freshly minted token")

    if rebuild:
        print(f"\nTables to rebuild: {', '.join(rebuild)}")


def _refuse_root_on_service_db(db_path: str) -> Optional[str]:
    """An error message when this runs as root against a database, or in a
    directory, owned by someone else (the service user); None when it is fine
    to go on.

    Root would run the interpreter, site-packages and ``wsi_app`` the service
    user can rewrite, and follow symlinks it plants at the predictable names of
    the backup and working copy beside the database. Both the path itself
    (``lstat``: a link the service user planted is theirs, whatever it points
    at, and a root-owned database behind it would pass a ``stat``) and the
    directory the copies are written into are checked. See the module
    docstring."""
    if os.geteuid() != 0:
        return None
    directory = os.path.dirname(os.path.abspath(db_path))
    for what, look in (
        ("belongs to", lambda: os.lstat(db_path)),
        ("is a link to a database that belongs to", lambda: os.stat(db_path)),
        ("is in a directory owned by", lambda: os.stat(directory)),
    ):
        try:
            owner = look().st_uid
        except OSError:
            continue
        if owner == 0:
            continue
        try:
            name = pwd.getpwuid(owner).pw_name
        except KeyError:
            name = str(owner)
        return (
            f"refusing to run as root: {db_path} {what} {name}, and root must not run "
            f"the code {name} can rewrite (the venv and wsi_app) or follow links in {name}'s "
            f"directory.\nRun it as that user instead:\n"
            f"  sudo -u {name} {sys.executable} {os.path.abspath(__file__)} {db_path}"
            f"{' --apply' if '--apply' in sys.argv else ''}"
        )
    return None


def main() -> int:
    # The report is long enough that an operator will pipe it to head/less;
    # restore the default SIGPIPE so doing so exits quietly instead of dumping
    # a BrokenPipeError traceback.
    try:
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not POSIX, or not on the main thread

    parser = argparse.ArgumentParser(
        description="Normalise the live tessera DB to the shape create_all() builds.",
    )
    parser.add_argument("db", help="path to wsi.db (the service must be stopped)")
    parser.add_argument(
        "--apply", action="store_true",
        help="write the changes (default: report only, touch nothing)",
    )
    parser.add_argument(
        "--keep-work", action="store_true",
        help="on a report-only or failed run, keep the working copy for inspection "
             "(on success the working copy *becomes* the DB, so this is a no-op)",
    )
    parser.add_argument(
        "--app-dir", metavar="PATH",
        help="directory containing the wsi_app package (default: auto-detect, "
             f"falling back to {_DEPLOYED_APP_DIR})",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"error: no such file: {args.db}", file=sys.stderr)
        return 2

    refusal = _refuse_root_on_service_db(args.db)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 2

    app_dir = _load_orm(args.app_dir)
    print(f"Reading the target shape from {app_dir}/wsi_app")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # The pid keeps two concurrent runs off one scratch file. They would
    # otherwise both snapshot to the same path and interleave writes into it —
    # which happens to survive a small database and would not survive a large
    # one. Making the collision impossible is cheaper than reasoning about the
    # window.
    work = f"{args.db}.normalize-work.{os.getpid()}"
    backup = f"{args.db}.pre-normalize-{stamp}.bak"

    for stale in (work, work + "-wal", work + "-shm"):
        if os.path.exists(stale):
            os.remove(stale)

    # The run writes two more full copies of the DB beside the original (the
    # working copy, then the backup). Running out of room halfway leaves the
    # original intact but is worth refusing up front, with the numbers, rather
    # than after several minutes of copying.
    needed = os.path.getsize(args.db) * 2
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(args.db))).free
    if free < needed:
        print(
            f"error: not enough free space. This needs about "
            f"{needed // 1024} KiB beside {args.db} (a working copy plus a "
            f"backup of the original); {free // 1024} KiB are free.",
            file=sys.stderr,
        )
        return 2

    print(f"Snapshotting {args.db} -> working copy")
    _snapshot(args.db, work)

    # The swap happens inside the try block, so a failure anywhere before it
    # returns with the original still in place and the scratch copy removed.
    swapped = False
    conn = _connect(work)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            print(f"error: source DB fails integrity_check: {row}", file=sys.stderr)
            return 1

        findings, rebuild, missing = analyse(conn)
        unset_tokens = count_unset_public_ids(conn)
        print()
        _report(findings, rebuild, missing, unset_tokens)
        print()

        if missing:
            return 1
        if not findings and not unset_tokens:
            return 0
        if not args.apply:
            print("Report only — nothing was written. Re-run with --apply to proceed.")
            return 0

        minted = mint_public_ids(conn)
        for table, n in minted.items():
            print(f"minted {n} public_id token(s) in {table}")

        filled = backfill_nulls(conn, findings)
        for key, n in filled.items():
            if n:
                print(f"back-filled {n} NULL cell(s) in {key}")

        for table_name in rebuild:
            rebuild_table(conn, table_name)
            print(f"rebuilt {table_name} with the target shape")

        conn.commit()

        problems = verify(conn)
        if problems:
            print("\nVerification FAILED — the original DB was not touched:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        print("\nVerification passed (integrity, foreign keys, shape, public_id).")
        conn.close()

        # Only now is the original touched. Preserve ownership/permissions so
        # the service user can still open the swapped-in file.
        src_stat = os.stat(args.db)
        print(f"\nWriting backup of the original to {backup}")
        try:
            _snapshot(args.db, backup)
        except (OSError, sqlite3.Error) as exc:
            # Most likely the disk filled while copying. The original has not
            # been touched — say so plainly instead of letting a traceback
            # imply otherwise — and remove the half-written .bak, which would
            # otherwise sit there looking like a usable backup.
            if os.path.exists(backup):
                os.remove(backup)
            print(
                f"\nerror: could not write the backup of the original: {exc}\n"
                f"{args.db} was NOT modified. Free some space on its volume "
                f"(the backup is written next to it) and re-run.",
                file=sys.stderr,
            )
            return 1

        os.replace(work, args.db)
        swapped = True
        os.chmod(args.db, src_stat.st_mode & 0o7777)
        try:
            os.chown(args.db, src_stat.st_uid, src_stat.st_gid)
        except PermissionError:
            print(
                f"note: could not restore owner/group on {args.db} — re-run as "
                f"root, or chown it back before starting the service"
            )
        # The swapped-in file has no WAL of its own; a stale sidecar left by
        # the old file would be read against the new one.
        for sidecar in (args.db + "-wal", args.db + "-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)

        print(f"Done. {args.db} now matches the target shape.")
        print(f"Original preserved at {backup}")
        return 0
    except OrphanRowError as exc:
        print(f"\naborted: {exc}", file=sys.stderr)
        print("The original DB was not touched.", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError:
            pass  # already closed on the success path
        if not swapped and not args.keep_work:
            for scratch in (work, work + "-wal", work + "-shm"):
                if os.path.exists(scratch):
                    os.remove(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
