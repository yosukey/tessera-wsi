"""Restore-apply swap: move a staged restore into ``DATA_DIR``, crash-safely.

The one implementation of the swap and of its crash recovery. Callers:

* ``infra/systemd/start-wsi-app.sh`` (production). The API writes the restore
  request and restarts wsi-app; before exec'ing uvicorn — no SQLite handle
  open, no request in flight — the script runs
  ``python -m wsi_app.restore_apply <request> <result> <journal>`` (:func:`main`).
* ``backup.inprocess_apply`` (dev, ``BACKUP_DEV_INPROCESS_APPLY``) and
  ``backup.recover_interrupted_apply`` (the dev lifespan's reconciliation),
  which call :func:`plan_journal` / :func:`apply_journal` /
  :func:`load_journal` / :func:`settle_journal` / :func:`close_journal`.

Standard library ONLY: never import from wsi_app or a third-party package
here. The boot-time run must not initialise the app's settings / DB engine
(their import has side effects, some on the very DB being swapped), and must
still be able to roll back a half-applied tree when the venv's packages are
what is broken. tests/test_restore_apply_journal.py enforces this.

Crash safety. The swap is a sequence of rename(2)s — park the live wsi.db (+
siblings), assets/, wsi/ and thumbs/ under ``.pre-restore-<UTC>``, promote the
staged ones — and a stop that bypasses exception handling (SIGKILL after
TimeoutStopSec, power loss, a reboot) can land between any two of them. An
in-memory rollback list cannot survive that, and the request cannot either
(/run is a tmpfs). So before the first move the whole plan is written to a
journal on the data volume, ``done`` is advanced durably BEFORE each move, and
``phase`` turns ``committed`` once every move has landed: after a crash
steps[:done-1] certainly ran and steps[done-1] may have. The next start
reconciles a leftover journal before anything else — uncommitted: undo
newest-first, lowering ``done`` durably after each undo so a crash during the
rollback resumes instead of guessing; committed: finish the bookkeeping — and
removes it only once the tree is consistent again. Nothing may open the DB
while it exists. Reconciliation replays the journal's own step list, never a
re-derived plan, so a journal written under an older move order still
reconciles; bump JOURNAL_VERSION only when its fields change meaning.
"""
from __future__ import annotations

import datetime
import errno
import json
import math
import os
import shutil
import sys
import time
import traceback
from typing import Optional

JOURNAL_VERSION = 1
# Default journal file name under DATA_DIR (backup.restore_journal_path and
# start-wsi-app.sh derive the same path; RESTORE_JOURNAL_PATH overrides both).
JOURNAL_NAME = ".restore-apply.journal"
# Mirrors backup.VALID_APPLY_SCOPES (which cannot be imported here).
APPLY_SCOPES = ("metadata", "full", "reset")
# The first 16 bytes of every SQLite database file.
SQLITE_MAGIC = b"SQLite format 3\x00"
# The files SQLite keeps beside a database and replays into it when it is next
# opened: the write-ahead log and its index, and a hot rollback journal. A
# swap parks the live database's along with it, and the promote of the staged
# database refuses to land beside any (see _refuse_occupied_db_promote).
DB_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")


class _RenameRefused(OSError):
    """``rename(2)`` itself failed: nothing moved (see :func:`move_durable`)."""


class NotReconciled(RuntimeError):
    """The tree could not be brought to a state the journal accounts for.

    The journal is kept (fail closed): the start script refuses to exec
    uvicorn and the app's lifespan refuses to open the DB while it exists, and
    the next start retries from the cursor. ``journal`` is the parsed journal,
    or None when it could not be read.
    """

    def __init__(self, message: str, journal: Optional[dict] = None) -> None:
        super().__init__(message)
        self.journal = journal


def _running_app_version() -> Optional[str]:
    """The release this start runs — ``<repo>/VERSION``, which install.sh
    stamps at deploy time and wsi_app.version reads first too — or None when
    there is none (a development checkout), and the version check is skipped.
    Read here rather than imported: this helper must stay standard-library
    only and import nothing of the app's."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "VERSION",
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            stamped = f.read().strip()
    except OSError:
        return None
    # As wsi_app.version._strip_v: "v1.2.3" and "1.2.3" are one release.
    stamped = stamped[1:] if stamped.startswith("v") else stamped
    return stamped or None


def utc_now_z() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(tzinfo=None)
        .isoformat()
        + "Z"
    )


def fsync_dir(path: str) -> None:
    # A rename / create / unlink is durable only once the directory holding
    # the name is flushed. Best effort: some filesystems (tmpfs) refuse it.
    try:
        fd = os.open(path or ".", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_durable(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # A failed write (ENOSPC, a failed rename) must not leave its .tmp.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    fsync_dir(os.path.dirname(path))


def remove_durable(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    fsync_dir(os.path.dirname(path))


def move_durable(src: str, dst: str) -> None:
    # Exactly one rename(2) per journaled step, so after any crash each step's
    # payload is wholly at ``src`` or wholly at ``dst`` — never half-copied.
    # shutil.move would silently degrade to copy+delete across filesystems
    # (request_apply refuses that layout up front; EXDEV here enforces it at
    # apply time, as an ordinary failure that rolls back). Both directory
    # entries are flushed before the next journal write says it happened.
    # A failure of the rename itself is raised as _RenameRefused: the step
    # provably moved nothing, which apply_journal's rollback relies on.
    try:
        os.rename(src, dst)
    except OSError as exc:
        raise _RenameRefused(exc.errno, exc.strerror, src, None, dst) from exc
    fsync_dir(os.path.dirname(src))
    fsync_dir(os.path.dirname(dst))


def _refuse_occupied_db_promote(step: dict, db_path: str) -> None:
    """Refuse the promote of the staged DB while anything sits at the live DB
    path or at one of its siblings (:data:`DB_SIBLING_SUFFIXES`).

    The swap parked whatever was there, so anything found now was created
    since — by a connection opened while the path was missing (see
    plan_journal). rename(2) would silently replace an intruding wsi.db, and
    the intruder's -wal / -shm would then be replayed onto the restored DB.
    Raised as :class:`_RenameRefused`, which is the truth: the step moved
    nothing, so apply_journal's rollback leaves it out and undoes only the
    parks before it. That rollback still fails closed (NotReconciled, the
    journal kept) when an intruder sits where a parked file must go back:
    which copy holds the data is then for the operator to judge, not for a
    rename to decide by overwriting one.

    The check and the rename are two calls, so a connection opened in the
    microseconds between them still slips through; there is no portable
    no-replace rename in the standard library. That is a far narrower window
    than the several moves the path is otherwise missing for.

    Directory promotes (assets/, wsi/, thumbs/) are not refused. rename(2)
    onto an existing directory fails (ENOTEMPTY) unless that directory is
    empty, and replacing an empty directory loses nothing — whereas refusing
    it would leave the undo of that directory's park facing "both exist" and
    fail closed for no data at all.
    """
    if step.get("op") != "promote" or step.get("dst") != db_path:
        return
    for path in (db_path, *(db_path + sfx for sfx in DB_SIBLING_SUFFIXES)):
        if os.path.lexists(path):
            raise _RenameRefused(
                errno.EEXIST,
                "refusing to promote the staged database: something created "
                "this path after the live database was parked",
                step["src"], None, path,
            )


# ---------------------------------------------------------------------------
# Plan, apply, undo
# ---------------------------------------------------------------------------

def plan_journal(*, stage_id: str, scope: str, stage_dir: str, data_dir: str,
                 db_path: str, app_version: Optional[str],
                 rollback_suffix: str = "") -> dict:
    """Plan an apply: its journal (``phase`` applying, ``done`` 0), listing
    every move up front so the whole swap is on disk before the first one runs.

    Touches nothing. The caller writes it with :func:`write_json_durable`,
    then runs :func:`apply_journal`.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rollback_root = os.path.join(data_dir, f".pre-restore-{ts}{rollback_suffix}")
    steps: list[dict] = []

    # ``lexists`` rather than ``exists`` so a dangling symlink at a live path
    # is parked too instead of blocking the promote.
    def plan(op: str, src: str, dst: str, want_dir: bool = False) -> None:
        if os.path.isdir(src) if want_dir else os.path.lexists(src):
            steps.append({"op": op, "src": src, "dst": dst})

    # Park the *entire* mutable live state — DB, assets/, wsi/ and thumbs/ —
    # for every scope. Restore replaces wholesale: a metadata-scope restore
    # deliberately wipes wsi/ + thumbs/ (the new DB's wsi_filename column
    # describes which files the operator must re-upload), a reset wipes
    # everything but admin/config rows, and a full restore lays down the
    # archive's own wsi/ + thumbs/ in place of whatever was there.
    plan("park", db_path, os.path.join(rollback_root, "wsi.db"))
    # ``-journal`` is unused under WAL mode, but a DB that ever ran under
    # rollback-journal mode may have left one behind, which would otherwise be
    # replayed against the swapped-in DB.
    for sfx in DB_SIBLING_SUFFIXES:
        plan("park", db_path + sfx, os.path.join(rollback_root, "wsi.db" + sfx))
    # Promote the staged DB right after the live one and its siblings are
    # parked. The live path is still missing for up to four moves (the DB,
    # then each sibling that exists), and a SQLite connection opened in that
    # window creates an empty wsi.db plus a fresh -wal / -shm there. (Only the
    # dev in-process apply can race it; the boot-time run has nothing
    # connected.) A rename(2) would replace that empty wsi.db silently but
    # leave its -wal behind, and a WAL that connection wrote into is replayed
    # onto the restored DB on its next open — its pages land in the wrong
    # file, and tables can vanish. So apply_journal refuses this promote while
    # anything sits at the DB path or beside it (_refuse_occupied_db_promote),
    # as a refused rename that moved nothing: the swap rolls back. Planned
    # unconditionally: a missing staged DB must fail the move and roll back,
    # not be skipped and leave no DB at all.
    steps.append({"op": "promote", "src": os.path.join(stage_dir, "wsi.db"), "dst": db_path})
    # thumbs/ is parked (hence wiped) for every scope, same as wsi/.
    # Thumbnails are keyed by slide id, so a stale tree would mis-render
    # restored slides whose id collides with a previous deployment's, and would
    # leave thumbnails behind for slides whose WSI body is being wiped.
    for name in ("assets", "wsi", "thumbs"):
        plan("park", os.path.join(data_dir, name), os.path.join(rollback_root, name))
    # assets/ rides along for every scope; wsi/ + thumbs/ only for full
    # (metadata / reset leave both empty until the operator re-uploads).
    plan("promote", os.path.join(stage_dir, "assets"), os.path.join(data_dir, "assets"), want_dir=True)
    if scope == "full":
        for name in ("wsi", "thumbs"):
            plan("promote", os.path.join(stage_dir, name), os.path.join(data_dir, name), want_dir=True)

    return {
        "version": JOURNAL_VERSION,
        "phase": "applying",
        "stage_id": stage_id,
        "scope": scope,
        "app_version": app_version,
        "stage_dir": stage_dir,
        "data_dir": data_dir,
        "db_path": db_path,
        "rollback_root": rollback_root,
        "started_at": utc_now_z(),
        "steps": steps,
        "done": 0,
    }


def apply_journal(journal: dict, journal_path: str) -> None:
    """Run the moves of a journal already written to ``journal_path``, then
    commit it. On success the journal stays on disk, committed, for the caller
    to finish its bookkeeping and :func:`close_journal`.

    On any failure — even KeyboardInterrupt — the moves made so far are undone
    newest-first and the failure re-raised, the journal left at ``done`` 0 for
    the caller to report and close. If the undo itself stops,
    :class:`NotReconciled` (chained from the failure) is raised instead and the
    journal is kept. A kill that skips this frame entirely leaves the journal
    for :func:`settle_journal` on the next start.
    """
    try:
        if any(s["op"] == "park" for s in journal["steps"]):
            os.makedirs(journal["rollback_root"], exist_ok=True)
            fsync_dir(journal["data_dir"])
        for i, step in enumerate(journal["steps"]):
            # Cursor first, then the move: after a crash the journal never
            # under-reports what may have moved (see undo_steps).
            journal["done"] = i + 1
            write_json_durable(journal_path, journal)
            _refuse_occupied_db_promote(step, journal["db_path"])
            move_durable(step["src"], step["dst"])
        # The commit point. From here on a crash finishes the apply instead of
        # rolling it back.
        journal["phase"] = "committed"
        write_json_durable(journal_path, journal)
    except BaseException as exc:
        if isinstance(exc, _RenameRefused):
            # The failing step's rename moved nothing, so it is not part of
            # the prefix to undo. Left in, undo_steps would have to infer it
            # from which paths exist — and a step whose source was already
            # gone (a staged DB that vanished) leaves neither path, which it
            # must read as "can't tell" and fail closed on.
            journal["done"] -= 1
            try:
                # Durably, so a crash before the undo's first write doesn't
                # leave the journal naming this step as possibly run.
                write_json_durable(journal_path, journal)
            except OSError:
                pass  # undo_steps copes with the higher cursor, fail-closed
        # Kept in the journal so a crash during the rollback below still
        # reports the original failure once the next start finishes it.
        journal["error"] = str(exc)
        err = undo_steps(journal, journal_path)
        if err:
            raise NotReconciled(
                f"restore swap failed ({exc}) and its automatic rollback stopped: {err}",
                journal,
            ) from exc
        drop_empty_rollback_root(journal)
        raise


def undo_steps(journal: dict, journal_path: str) -> Optional[str]:
    """Reverse the applied prefix of the journal's moves, newest first.

    ``done`` bounds what may have run: steps[:done-1] certainly did, and
    steps[done-1] may or may not have (the crash can fall between advancing
    the cursor and its rename). Each undo is a single rename back, and the
    cursor is lowered durably after each one, so every inspection below sees
    the tree exactly as it stood right after that step — unambiguous even
    though one path (the live wsi.db, assets/, ...) is both the source of a
    park and the destination of a later promote. Stops at the first problem
    (fail closed) and returns its description; None once fully undone.
    """
    journal["phase"] = "rolling_back"
    steps = journal["steps"]
    done = int(journal.get("done", 0))
    while done > 0:
        src, dst = steps[done - 1]["src"], steps[done - 1]["dst"]
        src_there, dst_there = os.path.lexists(src), os.path.lexists(dst)
        if dst_there and not src_there:
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                move_durable(dst, src)
            except OSError as exc:
                return f"could not move {dst!r} back to {src!r}: {exc}"
        elif not (src_there and not dst_there):
            # (src there, dst not) is "this step never ran" — fine. Anything
            # else means something outside the apply touched the tree.
            state = "both exist" if src_there else "neither exists"
            return f"cannot undo the move of {src!r} to {dst!r}: {state}"
        done -= 1
        journal["done"] = done
        try:
            write_json_durable(journal_path, journal)
        except OSError as exc:
            return f"could not record rollback progress in {journal_path!r}: {exc}"
    return None


def drop_empty_rollback_root(journal: dict) -> None:
    """Remove the ``.pre-restore-*`` directory of a fully undone apply.

    Every park was moved back, so it is empty — and an empty one left behind
    is worse than none: it reads as the newest rollback copy, the one an
    operator undoing a restore would reach for. ``rmdir`` only removes an
    empty directory, so anything that did land there stays. Best effort.
    """
    root = journal.get("rollback_root")
    if not isinstance(root, str):
        return
    try:
        os.rmdir(root)
    except OSError:
        return
    fsync_dir(os.path.dirname(root))


# ---------------------------------------------------------------------------
# Reconciling a leftover journal
# ---------------------------------------------------------------------------

def load_journal(journal_path: str) -> dict:
    """Read and validate a leftover journal; :class:`NotReconciled` (with no
    journal attached) when it is unreadable or malformed — never guess."""
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            journal = json.load(f)
        if not isinstance(journal, dict) or journal.get("version") != JOURNAL_VERSION:
            raise ValueError("unrecognised journal format")
        if "phase" not in journal:  # its value is judged by settle_journal
            raise ValueError("missing phase")
        if not all(
            isinstance(s, dict) and isinstance(s.get("src"), str) and isinstance(s.get("dst"), str)
            for s in journal["steps"]
        ) or not 0 <= int(journal.get("done", 0)) <= len(journal["steps"]):
            raise ValueError("malformed step list or cursor")
        if not isinstance(journal.get("stage_dir"), str) or not isinstance(journal.get("db_path"), str):
            raise ValueError("missing stage_dir / db_path")
    except Exception as exc:
        raise NotReconciled(
            f"restore-apply journal {journal_path!r} is unreadable ({exc}); "
            "inspect the data directory and the newest .pre-restore-* tree by "
            "hand, then delete the journal"
        ) from exc
    return journal


def settle_journal(journal: dict, journal_path: str) -> str:
    """Bring the tree an interrupted apply left behind to a consistent state.

    ``"completed"`` when every move had landed (only the bookkeeping was cut
    short), ``"rolled_back"`` once an uncommitted apply is undone. Raises
    :class:`NotReconciled`, keeping the journal, when it cannot be. The
    journal is still on disk either way: the caller reports, then
    :func:`close_journal`.
    """
    phase = journal["phase"]
    if phase == "committed":
        return "completed"
    if phase in ("applying", "rolling_back"):
        err = undo_steps(journal, journal_path)
        if err:
            raise NotReconciled(f"rollback of the interrupted restore apply stopped: {err}", journal)
        drop_empty_rollback_root(journal)
        return "rolled_back"
    raise NotReconciled(f"restore-apply journal has unknown phase {phase!r}", journal)


def close_journal(journal: dict, journal_path: str) -> None:
    """The tree is consistent: drop the journal, then the stage. After a
    rollback the undo made the stage whole again, but it is dropped anyway —
    the operator re-stages to retry, and a full-scope stage can be huge."""
    remove_durable(journal_path)
    shutil.rmtree(journal["stage_dir"], ignore_errors=True)


# ---------------------------------------------------------------------------
# Signing everyone out after a committed swap
# ---------------------------------------------------------------------------

def _token_versions(conn) -> list[tuple[int, int]]:
    import sqlite3
    try:
        rows = conn.execute("SELECT id, token_version FROM users").fetchall()
    except sqlite3.OperationalError:  # no such table / column
        return []
    return [(int(uid), int(tv or 0)) for uid, tv in rows]


def _session_generation(conn) -> Optional[int]:
    """``auth_settings.session_generation``; None when there is no row (or
    no such table / column)."""
    import sqlite3
    try:
        row = conn.execute("SELECT MAX(session_generation) FROM auth_settings").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None or row[0] is None else int(row[0])


# Beside the live database (``<db_path>`` + this): the session generation a
# swap moved to but could not record, the swapped-in database having no
# auth_settings row (the archived install never saved its authentication
# settings) — or no such table or column. The app's boot creates what is
# missing (the row, or the column at its default 0), raises the generation to
# this floor and removes the file (backup.apply_session_generation_floor).
SESSION_FLOOR_SUFFIX = ".session-generation-floor"


def _store_session_generation(conn, generation: int) -> bool:
    """Set ``auth_settings.session_generation``. False when there is no
    auth_settings row (or table, or column) to hold it."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(auth_settings)").fetchall()]
    if "session_generation" not in columns:
        return False
    cur = conn.execute("UPDATE auth_settings SET session_generation = ?", (generation,))
    return cur.rowcount > 0


def _previous_counters(previous_db: str) -> tuple[dict[int, int], Optional[int]]:
    """Every user's ``token_version`` and the session generation in the
    parked pre-restore database. Raises (``OSError`` / ``sqlite3.Error``)
    when it is gone or cannot be read; a table or column it never had reads
    as no values.

    Stricter than :func:`_token_versions` / :func:`_session_generation`,
    which take any ``OperationalError`` for "no such table / column": that is
    right for the restored database, which the app's own schema shapes, but
    an ``OperationalError`` is also what a parked database answers with
    ``disk I/O error`` or ``unable to open database file`` — and reading one
    of those as "no sessions to revoke" would fail open. So whether the
    table / column exists is asked of the schema first (``PRAGMA
    table_info``, which a file that is not a database already refuses), and
    every error after that is raised for :func:`invalidate_sessions` to fall
    back on its floor."""
    import sqlite3

    if not os.path.isfile(previous_db):
        raise FileNotFoundError(errno.ENOENT, "no such file", previous_db)
    conn = sqlite3.connect(previous_db)
    try:
        def columns(table: str) -> set:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

        prev_tv: dict[int, int] = {}
        if "token_version" in columns("users"):
            prev_tv = {
                int(uid): int(tv or 0)
                for uid, tv in conn.execute("SELECT id, token_version FROM users").fetchall()
            }
        prev_gen: Optional[int] = None
        if "session_generation" in columns("auth_settings"):
            row = conn.execute("SELECT MAX(session_generation) FROM auth_settings").fetchone()
            prev_gen = None if row is None or row[0] is None else int(row[0])
    finally:
        conn.close()
    return prev_tv, prev_gen


def unknown_previous_floor() -> int:
    """The value every session counter is moved past when the pre-restore
    database the sign-out must compare against cannot be read: the current
    time in milliseconds since the epoch.

    The sign-out has to put each counter past every value the pre-restore
    database could have held, and with that database damaged (the very
    reason to restore over it) or deleted (an operator tidying
    ``.pre-restore-*`` before a retry) nothing records those values any
    more. Refusing to start until someone deletes the retry marker by hand
    would block exactly the disaster recovery a
    restore is for, and the hand-deletion would then sign nobody out at all. A
    bound does exist, though:

    * ``users.token_version`` starts at a random value below 2**31
      (models._initial_token_version) and only ever moves on by one — per
      password change, disable or forced sign-out — or, across a restore, to
      one past a value one of the two databases held. ``auth_settings.
      session_generation`` starts at 0 and moves the same way. So, apart
      from this floor itself, no counter gets anywhere near 2**31 + the
      number of revocations ever made, and epoch milliseconds (about
      1.8 * 10**12 today) are far past that.
    * A counter a previous use of this floor moved to is the epoch
      milliseconds of that moment plus one bump per revocation since; a
      later floor is past it as long as the wall clock has moved forward by
      more milliseconds than there were revocations meanwhile (one takes a
      request, so always) — and was not set back by more than that in
      between. The restored database's own values are compared as well, so
      a clock set back only weakens the floor, never lowers a counter.

    The values stay comfortably within what every reader handles: SQLite
    INTEGER is 64-bit, the JWT ``tv`` / cookie ``gen`` claims are JSON
    numbers compared as Python ints (auth._token_version_matches requires
    an exact match, app_auth.decode_session_token ``gen >= generation``), and both
    stay below 2**53 for about 285,000 years, so even a JavaScript reader
    would see them exactly (none does: the frontend never parses either).
    """
    return int(time.time() * 1000)


def invalidate_sessions(db_path: str, previous_db: Optional[str]) -> None:
    """Revoke every staff token and app-access cookie issued before a swap.

    A staff JWT is honoured while its ``tv`` claim equals its user's
    ``users.token_version``, an app-access cookie while its ``gen`` claim is
    at least ``auth_settings.session_generation``. Both only ever move
    forward — except across a restore, which brings back the archive's older
    values and with them every token and cookie revoked since the backup was
    taken (a password change, a disabled account, a "sign everyone out"). So
    each user's token_version is put past both its restored value and the
    value the same id held before the swap, and the session generation past
    both generations: nothing issued before the swap, on either side of it,
    is accepted afterwards. Everyone signs in again.

    A swapped-in database without an auth_settings row would otherwise come
    up at generation 0 and accept every cookie revoked before the swap: with
    no row to hold the value, it is left beside the database as a floor (see
    :data:`SESSION_FLOOR_SUFFIX`).

    ``previous_db`` is the parked pre-restore database (None when nothing was
    parked: no live database before the swap, so no sessions on that side).
    One that is gone or cannot be read — a restore over a damaged database,
    the usual reason to restore at all, or a ``.pre-restore-*`` directory
    deleted before a retry — does not fail the sign-out: every counter is
    moved past :func:`unknown_previous_floor` as well, a value no counter of
    that database can have reached, so the sign-out still holds without it.
    A database without the users table has no staff tokens to revoke, and
    with no generation on either side there are no cookies to. Raises only
    when the swapped-in database cannot be read or updated, or a floor
    cannot be written — the failures no fallback can stand in for; the
    caller reports it.
    """
    import sqlite3

    prev_tv: dict[int, int] = {}
    prev_gen: Optional[int] = None
    # 0 (no floor) unless the pre-restore database is unknown.
    unknown_floor = 0
    if previous_db is not None:
        try:
            prev_tv, prev_gen = _previous_counters(previous_db)
        except (OSError, sqlite3.Error) as exc:
            unknown_floor = unknown_previous_floor()
            # Stderr is systemd's journal for the boot-time run, the app's
            # log for the dev apply and the startup retry.
            print(
                f"WSI: the pre-restore database {previous_db!r} cannot be read "
                f"({exc}); signing every session out by moving each session "
                f"counter past {unknown_floor} instead of past its values",
                file=sys.stderr,
            )
    floor: Optional[int] = None
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for uid, tv in _token_versions(conn):
                conn.execute(
                    "UPDATE users SET token_version = ? WHERE id = ?",
                    (max(tv, prev_tv.get(uid, tv), unknown_floor) + 1, uid),
                )
            gen = _session_generation(conn)
            if gen is not None:
                conn.execute(
                    "UPDATE auth_settings SET session_generation = ?",
                    (max(gen, prev_gen or 0, unknown_floor) + 1,),
                )
            elif prev_gen is not None or unknown_floor:
                # An unknown pre-restore database may have had a generation:
                # record a floor for it as for a known one.
                target = max(prev_gen or 0, unknown_floor) + 1
                if not _store_session_generation(conn, target):
                    floor = target
    finally:
        conn.close()
    if floor is not None:
        write_json_durable(db_path + SESSION_FLOOR_SUFFIX, {"session_generation": floor})


# Beside the live database (``<db_path>`` + this): a committed swap whose
# sign-out (:func:`invalidate_sessions`) failed. The swap stands — undoing a
# committed restore over a counter update would be far worse — but the app must
# not then serve the archive's counters, under which every token and cookie
# revoked since the backup was taken is valid again. So the failure leaves this
# marker, naming the parked pre-restore database the sign-out compares
# against, and the app's boot retries the sign-out before it serves anything,
# refusing to start while it keeps failing
# (backup.retry_pending_session_revocation). Since a pre-restore database that
# is gone or unreadable is signed out past a floor instead (invalidate_sessions),
# what is left to fail is the swapped-in database itself: locked, read-only,
# or not updatable — the cases where serving it would honour revoked
# credentials and nothing short of fixing it can revoke them. Stdlib-only
# JSON, like the floor.
SESSION_REVOCATION_SUFFIX = ".session-revocation-pending"


def invalidate_sessions_after(journal: dict) -> Optional[str]:
    """:func:`invalidate_sessions` for a committed journal; returns why it
    failed, or None. Running it again (a crash before the journal was
    closed) only moves the counters further on.

    A failure leaves :data:`SESSION_REVOCATION_SUFFIX` beside the database
    for the app's boot to retry; a success removes one an earlier attempt at
    the same swap left (and only that one: see below). Only if that marker
    cannot be written either does the sign-out stay undone — the returned
    reason then says so."""
    db_path = journal["db_path"]
    # The database to compare against is the one the journal parked: the
    # park step of the live DB says where it went. Not "whatever is at
    # rollback_root/wsi.db": with nothing parked (no live DB before the swap)
    # there were no sessions on that side, whereas a park whose file is gone
    # since (deleted by hand between a crash and this reconciliation) is an
    # unknown pre-restore database, which invalidate_sessions must sign out
    # past its floor — reading it as "nothing parked" would sign out against
    # the restored values alone, i.e. fail open.
    previous = next(
        (s["dst"] for s in journal["steps"] if s.get("op") == "park" and s.get("src") == db_path),
        None,
    )
    marker = db_path + SESSION_REVOCATION_SUFFIX
    try:
        invalidate_sessions(db_path, previous)
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        try:
            write_json_durable(marker, {
                "stage_id": journal.get("stage_id"),
                "previous_db": previous,
                "error": reason,
                "failed_at": utc_now_z(),
            })
        except Exception as marker_exc:
            return (
                f"{reason}; and the marker that makes the app retry it "
                f"({marker!r}) could not be written either: {marker_exc}"
            )
        return reason
    # Remove the marker only when it is this swap's own (an earlier attempt
    # at it, cut short): one another restore left is that restore's pending
    # sign-out, against ITS pre-restore database, which this one never
    # compared against — dropping it would quietly honour every credential
    # that restore should have revoked. The boot cannot get here with
    # another's marker (it refuses to start until the retry succeeds), and
    # the dev in-process apply refuses to start a swap while one is pending
    # (backup.inprocess_apply); this is the backstop.
    try:
        with open(marker, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None  # unreadable: left for the boot's retry to report
    if isinstance(pending, dict) and pending.get("stage_id") == journal.get("stage_id"):
        try:
            remove_durable(marker)
        except OSError:
            pass  # the retry only moves the counters further on
    return None


# ---------------------------------------------------------------------------
# How long a restore request stays applicable
# ---------------------------------------------------------------------------

# A request is applied only this long after it was written. Legitimately it
# reaches this helper within about a minute: right after writing it the API
# kicks the restart, which stops wsi-sync and wsi-app (each bounded by its
# unit's TimeoutStopSec=20), and start-wsi-app.sh runs this helper before
# uvicorn; the dashboard itself stops waiting for the result after 90 s.
# Writing the trigger file counts as a successful kick even when nothing acts
# on it (wsi-restart.path disabled, wsi-restart.service start-limited), so a
# request could otherwise wait for days — every backup, restore and import
# refused meanwhile — until an unrelated restart (set-workers.sh, update.sh,
# by hand) swapped in that old stage and parked every change made since
# under .pre-restore-*. Ten times the slowest legitimate path still bounds
# that. The API stops counting such a request as pending, and reports and
# drops it, by the same rule (routers/admin_backup.py).
REQUEST_MAX_AGE_SECONDS = 10 * 60

# The request is aged by the boot clock (CLOCK_BOOTTIME: seconds since boot,
# suspend included), which is never stepped. The wall clock is: NTP fixing a
# host that booted with a wrong RTC, a VM resumed, an operator's `date -s`.
# A step back of more than the limit between writing a request and the
# restart would make a legitimate restore look written in the future, to be
# refused; accepting such stamps instead would apply a request written while
# the clock ran ahead and then left waiting for hours, its wall-clock age
# reading short. The boot clock is right both ways. It only means anything
# within one boot, so the request records the boot's id beside it; a request
# read in another boot is one whose file outlived a reboot (/run is a tmpfs,
# so only under a non-default RESTORE_REQUEST_PATH) and is aged by the wall
# clock.
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _boot_id() -> Optional[str]:
    try:
        with open(_BOOT_ID_PATH, "r", encoding="ascii") as f:
            return f.read().strip() or None
    except (OSError, ValueError):
        return None


def _boot_seconds() -> Optional[float]:
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return None
    try:
        return time.clock_gettime(clock)
    except OSError:
        return None


def request_clock_stamp() -> dict:
    """The boot-clock fields a request is written with (backup.request_apply),
    or ``{}`` where there is no boot id / CLOCK_BOOTTIME (not Linux): such a
    request is aged by its wall-clock ``requested_at`` alone."""
    boot_id, now = _boot_id(), _boot_seconds()
    if boot_id is None or now is None:
        return {}
    return {"requested_boot_id": boot_id, "requested_boot_seconds": now}


def request_age_seconds(req: object, req_path: str) -> Optional[float]:
    """How long ago a request was written: by the boot clock when it was
    written in this boot (see :func:`request_clock_stamp`); else by its
    ``requested_at`` (UTC ISO with a ``Z``, as backup.request_apply stamps
    it); else — missing or garbled — by the file's mtime. None when none of
    those can be read."""
    if isinstance(req, dict):
        boot_secs = req.get("requested_boot_seconds")
        boot_id = req.get("requested_boot_id")
        if (
            isinstance(boot_secs, (int, float))
            and not isinstance(boot_secs, bool)
            and math.isfinite(boot_secs)
            and isinstance(boot_id, str)
            and boot_id == _boot_id()
        ):
            now = _boot_seconds()
            if now is not None:
                return now - boot_secs
    stamp = req.get("requested_at") if isinstance(req, dict) else None
    try:
        when = datetime.datetime.fromisoformat(str(stamp).removesuffix("Z"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        written = when.timestamp()
    except (TypeError, ValueError, OverflowError):
        try:
            written = os.stat(req_path).st_mtime
        except OSError:
            return None
    return datetime.datetime.now(datetime.timezone.utc).timestamp() - written


def request_expired(age: Optional[float]) -> bool:
    # Either way round. By the boot clock a real request's age is never
    # negative, so one that is was garbled. By the wall clock (no usable boot
    # stamp) a stamp well in the future means the clock moved since and the
    # real age is unknown: refusing costs the operator a re-apply (the stage
    # is kept), applying a stale request would park every change made since.
    return age is not None and abs(age) > REQUEST_MAX_AGE_SECONDS


def expired_request_result(req: object) -> dict:
    """The result-file report of a request dropped by :func:`request_expired`
    (by this helper at boot, or by the API)."""
    req = req if isinstance(req, dict) else {}
    scope = req.get("scope")
    what = "reset" if scope == "reset" else "restore"
    return {
        "ok": False,
        "stage_id": req.get("stage_id"),
        "scope": scope,
        "stage": "expired",
        "requested_at": req.get("requested_at"),
        "error": (
            f"the {what} requested at {req.get('requested_at') or 'an unknown time'} "
            f"was not applied: no service restart picked it up within "
            f"{REQUEST_MAX_AGE_SECONDS // 60} minutes (check that wsi-restart.path "
            "is enabled and wsi-restart.service has not failed), and applying it "
            "later would discard every change made since. The live data was left "
            f"as it is; {what} again from the Backup & Restore tab if you still want to."
        ),
    }


# ---------------------------------------------------------------------------
# start-wsi-app.sh entry point
# ---------------------------------------------------------------------------

def _count_pending_wsi_uploads(swapped_db: str, scope: Optional[str]) -> int:
    """Count slides whose wsi_filename will have no on-disk file post-swap.
    For full-scope this is 0 (staged wsi/ becomes the new live wsi/); for
    metadata/reset the live wsi/ is wiped, so every referenced filename is
    pending until the operator re-uploads.
    """
    if scope == "full":
        return 0
    # Imported here so the reconciliation itself never depends on _sqlite3.
    import sqlite3
    try:
        conn = sqlite3.connect(swapped_db)
    except Exception:
        return 0
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM slides "
            "WHERE wsi_filename IS NOT NULL AND wsi_filename != ''"
        ).fetchone()[0]
        return int(n)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def committed_result(journal: dict) -> dict:
    """The report of a committed swap, for the result file the dashboard
    reads. Signs out the sessions issued before it on the way
    (:func:`invalidate_sessions_after`), reporting a failure to as
    ``session_revocation_error``: the swap stands either way, and the app's
    boot retries the sign-out before serving (:data:`SESSION_REVOCATION_SUFFIX`)."""
    pending = _count_pending_wsi_uploads(journal["db_path"], journal.get("scope"))
    parked = any(s.get("op") == "park" for s in journal["steps"])
    result = {
        "ok": True,
        "stage_id": journal.get("stage_id"),
        "scope": journal.get("scope"),
        "app_version": journal.get("app_version"),
        "rollback_root": journal.get("rollback_root") if parked else None,
        "wsi_uploads_pending": pending,
    }
    revoke_error = invalidate_sessions_after(journal)
    if revoke_error:
        result["session_revocation_error"] = revoke_error
    return result


class _BootRun:
    """One start-wsi-app.sh run: reconcile a leftover journal, then apply the
    pending request. Outcomes go to the result file (the dashboard reads it
    once uvicorn is back) and to stderr (systemd's journal)."""

    def __init__(self, req_path: str, res_path: str, journal_path: str) -> None:
        self.req_path, self.res_path, self.journal_path = req_path, res_path, journal_path

    def run(self) -> int:
        # 1. Reconcile an interrupted apply before anything else. Its request
        #    file may be gone (reboot) or still present (service restart);
        #    either way the journal, not the request, describes what moved.
        if os.path.lexists(self.journal_path):
            rc = self._reconcile()
            if rc:
                return rc
        # 2. Apply the pending request, if any.
        if not os.path.lexists(self.req_path):
            return 0
        return self._apply_request()

    def _write_result(self, payload: dict) -> None:
        payload.setdefault("applied_at", utc_now_z())
        write_json_durable(self.res_path, payload)

    def _reject(self, payload: dict) -> int:
        # Report an unusable request and drop it: one attempt per request.
        # Left in place it would re-run on every later start, and meanwhile
        # jobs.restore_apply_pending() would 409 every backup,
        # restore and reset. Nothing has moved, so uvicorn may start.
        self._write_result(payload)
        try:
            os.remove(self.req_path)
        except FileNotFoundError:
            pass
        return 0

    def _consume_request_for(self, stage_id: str) -> None:
        # Drop the request only when it is the one the journal belongs to.
        # Removed BEFORE the journal, so a crash between the two can never
        # leave a request whose stage is already consumed with no journal to
        # explain it — the next start would misreport that as a missing staged
        # DB (validate_stage).
        try:
            with open(self.req_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(pending, dict) and pending.get("stage_id", "?") == stage_id:
            try:
                remove_durable(self.req_path)
            except OSError:
                # The tree is consistent either way; the shell's rm -f retries.
                traceback.print_exc(file=sys.stderr)

    def _fail_closed(self, message: str, journal: Optional[dict] = None) -> int:
        # Leave the journal in place: the shell refuses to exec uvicorn while
        # it exists, and the next start retries the reconciliation from the
        # cursor.
        print(f"WSI: RESTORE APPLY NOT RECONCILED: {message}", file=sys.stderr)
        payload = {
            "ok": False,
            "stage": "recovery",
            "error": message,
            "journal": self.journal_path,
        }
        if isinstance(journal, dict):
            payload["stage_id"] = journal.get("stage_id")
            payload["scope"] = journal.get("scope")
            payload["rollback_root"] = journal.get("rollback_root")
        try:
            self._write_result(payload)
        except Exception:
            traceback.print_exc(file=sys.stderr)
        return 3

    def _finish_rolled_back(self, journal: dict, stage: str, error: str,
                            tb: Optional[str] = None) -> None:
        result = {
            "ok": False,
            "stage_id": journal.get("stage_id"),
            "scope": journal.get("scope"),
            "stage": stage,
            "error": error,
        }
        # The undo emptied the rollback directory and normally removed it;
        # name it only if it is still there.
        root = journal.get("rollback_root")
        if isinstance(root, str) and os.path.isdir(root):
            result["rollback_root"] = root
        if tb:
            result["traceback"] = tb
        try:
            self._write_result(result)
        except Exception:
            print(f"WSI: failed to write restore result {self.res_path!r} (ok=False)", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        self._consume_request_for(journal.get("stage_id", "?"))
        close_journal(journal, self.journal_path)

    def _finish_committed(self, journal: dict, recovered: bool = False) -> None:
        # Signs the sessions out before the result is written, so a failure to
        # is reported with it.
        result = committed_result(journal)
        if recovered:
            result["recovered"] = "completed on the next start after an interrupted apply"
        revoke_error = result.get("session_revocation_error")
        if revoke_error:
            print(f"WSI: could not sign out the sessions issued before the restore: {revoke_error} "
                  "(the app retries it at startup and refuses to serve until it succeeds)",
                  file=sys.stderr)
        # A result-write failure (full /run, EACCES) after a completed swap
        # must not undo it — the moves are committed. Log the outcome to
        # systemd's journal instead and still finish below.
        try:
            self._write_result(result)
        except Exception:
            print(
                f"WSI: failed to write restore result {self.res_path!r} "
                f"(ok=True, stage_id={journal.get('stage_id')!r})",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
        self._consume_request_for(journal.get("stage_id", "?"))
        close_journal(journal, self.journal_path)

    def _reconcile(self) -> int:
        try:
            journal = load_journal(self.journal_path)
        except NotReconciled as exc:
            return self._fail_closed(str(exc))
        print(
            f"WSI: reconciling an interrupted restore apply "
            f"(stage {journal.get('stage_id')!r}, phase {journal['phase']!r})",
            file=sys.stderr,
        )
        try:
            outcome = settle_journal(journal, self.journal_path)
        except NotReconciled as exc:
            return self._fail_closed(str(exc), exc.journal)
        if outcome == "completed":
            self._finish_committed(journal, recovered=True)
        else:
            self._finish_rolled_back(
                journal,
                "interrupted",
                "the restore apply was interrupted (service stop, crash or power "
                "loss) before it completed; the pre-restore data was put back"
                + (f" (original failure: {journal['error']})" if journal.get("error") else ""),
            )
        return 0

    def _apply_request(self) -> int:
        try:
            with open(self.req_path, "r", encoding="utf-8") as f:
                req = json.load(f)
        except Exception as exc:
            return self._reject({"ok": False, "stage": "read_request", "error": str(exc)})

        # Same fail-and-clear path as a malformed JSON body above. A request
        # that parses but lacks a required key (truncated writer, hand-edited
        # file) would otherwise raise KeyError: the helper exits non-zero with
        # no result written, and the shell's rm -f is all that stops every
        # later start from re-running it.
        try:
            stage_dir = req["stage_dir"]
            data_dir = req["data_dir"]
            db_path = req["db_path"]
        except KeyError as exc:
            return self._reject({
                "ok": False,
                "stage": "read_request",
                "error": f"malformed restore request: missing key {exc}",
            })
        except TypeError as exc:
            # Valid JSON that isn't an object (a list, a bare string, ...).
            return self._reject({
                "ok": False,
                "stage": "read_request",
                "error": f"malformed restore request: not a JSON object ({exc})",
            })

        scope = req.get("scope")
        stage_id = req.get("stage_id", "?")
        if scope not in APPLY_SCOPES:
            return self._reject({
                "ok": False,
                "stage_id": stage_id,
                "stage": "read_request",
                "error": f"unknown apply scope: {scope!r}",
            })

        # A request no restart picked up in time: this start is not the one
        # it asked for (see REQUEST_MAX_AGE_SECONDS). Nothing has moved. The
        # stage is left for a re-apply; the 24 h staging sweep reclaims it.
        if request_expired(request_age_seconds(req, self.req_path)):
            print(f"WSI: restore request for stage {stage_id!r} expired; not applied",
                  file=sys.stderr)
            return self._reject(expired_request_result(req))

        # A request made by another release than the one starting now (an
        # update deployed while it waited for its restart): the stage was
        # checked, and its schema upgrade trialled, by the version that
        # requested it, so this one does not apply it on that one's word.
        # Nothing has moved; the stage is left for a re-stage.
        requested_version = req.get("app_version")
        running_version = _running_app_version()
        if (
            isinstance(requested_version, str) and requested_version
            and running_version and requested_version != running_version
        ):
            what = "reset" if scope == "reset" else "restore"
            print(
                f"WSI: restore request for stage {stage_id!r} was made under "
                f"{requested_version}, this start runs {running_version}; not applied",
                file=sys.stderr,
            )
            return self._reject({
                "ok": False,
                "stage_id": stage_id,
                "scope": scope,
                "stage": "version_changed",
                "requested_at": req.get("requested_at"),
                "error": (
                    f"the {what} was requested under version {requested_version}, "
                    f"but the service restarted into version {running_version} (an "
                    "update was deployed while it waited). The archive was checked "
                    "by the version that requested it, so it was not applied; the "
                    f"live data was left as it is. Stage the archive again and {what} "
                    "from the Backup & Restore tab."
                ),
            })

        # Validate the staged DB is present and looks like SQLite BEFORE
        # parking the live state. A stage reclaimed by the 24h
        # sweep_stale_staging TTL (or a partially-written one) would otherwise
        # fail only AFTER the live tree was parked, forcing an avoidable
        # rollback and the downtime it carries. A stage whose wsi.db an
        # interrupted apply already promoted never gets here: step 1
        # reconciled that apply from its journal and consumed its request, so
        # a missing staged DB at this point really is a missing stage.
        staged_db = os.path.join(stage_dir, "wsi.db")
        try:
            with open(staged_db, "rb") as f:
                magic = f.read(16)
        except OSError as exc:
            return self._reject({
                "ok": False,
                "stage_id": stage_id,
                "scope": scope,
                "stage": "validate_stage",
                "error": f"staged database missing or unreadable at {staged_db!r}: {exc}",
            })
        if magic != SQLITE_MAGIC:
            return self._reject({
                "ok": False,
                "stage_id": stage_id,
                "scope": scope,
                "stage": "validate_stage",
                "error": f"staged database at {staged_db!r} is not a SQLite file",
            })

        journal = plan_journal(
            stage_id=stage_id, scope=scope, stage_dir=stage_dir, data_dir=data_dir,
            db_path=db_path, app_version=req.get("app_version"),
        )
        try:
            write_json_durable(self.journal_path, journal)
        except Exception as exc:
            # Nothing has moved yet, so the live tree is intact: report and
            # let uvicorn start on it. Swapping without a journal is exactly
            # the unrecoverable case this exists to prevent.
            try:
                os.remove(self.journal_path + ".tmp")
            except OSError:
                pass
            return self._reject({
                "ok": False,
                "stage_id": stage_id,
                "scope": scope,
                "stage": "journal",
                "error": f"could not write the restore-apply journal {self.journal_path!r}: {exc}",
            })

        try:
            apply_journal(journal, self.journal_path)
        except NotReconciled as exc:
            return self._fail_closed(str(exc), journal)
        except Exception as exc:
            # The undo put the pre-restore tree back; report the failure and
            # drop the (whole again) stage — the operator re-stages to retry.
            self._finish_rolled_back(journal, "swap", str(exc), traceback.format_exc())
            return 0
        self._finish_committed(journal)
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m wsi_app.restore_apply <request> <result> <journal>``, as
    start-wsi-app.sh runs it before exec'ing uvicorn.

    Exit status: 0 whenever the tree is consistent — including every ordinary
    failure (bad request, missing stage, a swap that rolled back cleanly),
    which only the result file reports, since uvicorn must come up for the
    operator to see it; 3 when it could not be made consistent (fail closed:
    journal kept, result ``"stage": "recovery"``); 2 on a usage error; 1, as
    for any Python program, on an uncaught exception. The shell gates on the
    journal rather than the status: uvicorn never starts while it exists.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 3:
        print("usage: python -m wsi_app.restore_apply REQUEST RESULT JOURNAL", file=sys.stderr)
        return 2
    return _BootRun(*args).run()


if __name__ == "__main__":
    sys.exit(main())
