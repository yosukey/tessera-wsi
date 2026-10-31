#!/usr/bin/env python3
"""Database and file-list helper for infra/sync-from.sh.

sync-from.sh copies one Tessera install's content (the *source*, normally the
main server) onto another (the *destination*, the backup server it runs on).
It does the transfer (ssh, rsync) and the service handling; everything that
needs to understand the database is here. Standard library only, so the same
file runs with the system python3 on both hosts:

- on the source, fed over ssh on stdin (``python3 -I - state ...``) as the
  service user, so the source needs nothing installed beyond Tessera itself;
- on the destination, as the service user for everything that opens the live
  DB or writes under DATA_DIR, and as root only for the mirror directory.

Two scopes:

``content`` (standby, the default)
    The destination keeps its own access settings and records and takes only
    the content (and the admin accounts) from the source. Which table is
    which is decided by CONTENT_TABLES / LOCAL_TABLES below;
    services/app/tests/test_replica_db.py fails when an ORM table is in
    neither, so a new table cannot leak across (or be dropped) unnoticed.
``all`` (migration)
    The destination's database becomes a copy of the source's, every table
    included.

Every swap (merge, replace, and sync-from.sh --rollback) is followed by
``sign-out``, which runs the app's own post-restore sign-out
(wsi_app/restore_apply.py, stdlib-only like this file) against the database
that was replaced: every staff token and app-access cookie issued before the
swap is refused after it, so no revocation made on either server can be
undone by the copy.

Subcommands (exit 0 on success, 1 on a refusal or error, with the reason on
stderr; ``check`` also exits 3 when the source changed under the transfer,
``replace`` 4 when the copy landed but failed its check (the database WAS
replaced), ``sign-out`` 3 when it handed the sign-out to the app's next
start):

  state SOURCE_DB SOURCE_DATA_DIR      JSON: versions, slide files, tours and
                                       their attachments
  export SOURCE_DB --scope S           the snapshot DB, written to stdout
  info DB                              JSON: versions and counts of a live DB
  check-versions --scope S STATE INFO  refuse an incompatible pair
  files-from DB                        NUL-separated rsync --files-from list
  tour-filter DB                       rsync filter rules for assets/tours/
  check PRE POST DB MIRROR             transferred files match the snapshot?
  summary DB LIVE_DB --scope S         what applying DB would change
  backup-db SRC DST                    online copy of SRC to DST, checked
  merge LIVE_DB DB                     content scope: swap the content tables
  replace LIVE_DB DB                   all scope: copy DB over LIVE_DB
  sign-out LIVE_DB PREV_DB RESTORE_APPLY_PY
                                       revoke every session issued before a swap
  check-sign-out LIVE_DB RESTORE_APPLY_PY
                                       refuse while an earlier sign-out is pending
  bump-generations LIVE_DB             every slide to a fresh tile generation
  quick-check DB                       PRAGMA quick_check
  mfa-accounts DB                      how many accounts have two-factor sign-in on
  install-files DB MIRROR DATA_DIR     put the mirror's files in place
  install-bytes DB MIRROR DATA_DIR     bytes install-files would copy (no links)
  can-link FILE DIR                    1 when FILE can be hard-linked into DIR
  prune-mirror FILES_FROM MIRROR       drop mirror files no longer listed
  env-secrets ENV_FILE                 print the secrets a migration carries
  set-env ENV_FILE                     write KEY=value lines read from stdin
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Table classification (content scope)
# ---------------------------------------------------------------------------

# Taken from the source. The slides and everything hanging off them, what the
# public pages look like, and the admin accounts with the role permissions
# that go with them (an admin signs in to both servers with one password;
# ``sign-out`` then signs everyone out of this server, as a restore does).
CONTENT_TABLES = (
    "slides",
    "slide_attributes",
    "tours",
    "tour_attributes",
    "steps",
    "step_pins",
    "step_shapes",
    "viewer_settings",
    "seasonal_backgrounds",
    "metadata_options",
    "users",
    "role_permissions",
)

# Kept as the destination has them. How visitors reach and sign in to THIS
# server (its IP filter, mDNS name, Google Analytics tag, analytics mode, app
# login mode and accounts), and what happened on it (statistics, audit trail,
# error log, benchmark history), which a sync must not wipe.
LOCAL_TABLES = (
    "network_settings",
    "auth_settings",
    "auth_users",
    "page_views",
    "slide_dwell_events",
    "tour_sessions",
    "audit_logs",
    "error_logs",
    "perf_runs",
)

# app_meta is split by key. The destination keeps its own version stamps
# (they describe the binary that opened its DB) and anything unknown; two keys
# describe the content and follow it:
META_TABLE = "app_meta"
# admin_helpers._SLIDE_ID_HWM_KEY: slide ids are never reused, so the
# watermark becomes the larger of the two (and of the ids actually present).
KEY_SLIDE_ID_HWM = "slides_id_hwm"
# dzi.KEY_TILE_GEOMETRY: the tile grid the source's wsi_generation values
# were served under. Taking it with them lets the destination's next boot
# move every slide to a fresh generation when its own DZI_TILE_SIZE /
# DZI_OVERLAP differ, exactly as after a restore, so a generation never names
# tiles cut on two grids in the destination's edge cache.
KEY_TILE_GEOMETRY = "dzi_tile_geometry"
KEY_APP_VERSION = "app_version"
KEY_SCHEMA_VERSION = "schema_version"

# The wsi.env values a migration carries over: ANALYTICS_HASH_SALT keeps
# hashed-mode visitor pseudonyms continuous across the move, and WSI_MFA_KEY
# opens the staff two-factor secrets sealed in the copied users table (under
# another key every enrolled account would be refused until reset).
# (JWT_SECRET is not: every session is signed out after the swap anyway, so
# the new server keeps its own.)
SECRET_KEYS = ("ANALYTICS_HASH_SALT", "WSI_MFA_KEY")

# Same alphabet as admin_helpers.PUBLIC_ID_RE: a tour's public_id names its
# directory under assets/tours/, so anything else is refused before it is
# used in a path or an rsync rule.
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
# A stored WSI / thumbnail name is one path component, never hidden (names
# starting with "." are upload temporaries, rsync partials and this tool's own temp
# files), with no control characters (the rsync list is NUL-separated, the
# messages are line-based).
_BAD_NAME_RE = re.compile(r"[/\x00-\x1f\x7f]")
_SYNC_TMP_RE = re.compile(r"^\..+\.sync-[0-9a-f]{12}\Z")
# A tour attachment referenced from tour / step markdown (the app's own
# pattern: slide_transfer._ASSET_REF_RE, backup._TOUR_ASSET_REF_RE).
# Attachments are not rows, so this is how a database refers to them.
ASSET_REF_RE = re.compile(r"/assets/tours/([A-Za-z0-9_-]{1,32})/([^\s)\]\"'<>?#]+)")

EXIT_CHANGED = 3
EXIT_DEFERRED = 3     # sign-out: handed to the app's boot (the marker)
EXIT_SWAPPED_BAD = 4  # replace: the copy landed, then failed quick_check


class Refusal(Exception):
    """A condition the operator must fix; printed without a traceback."""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Versions (a copy of wsi_app.app_meta.parse_version: this file runs with the
# system python3 and cannot import the app; test_replica_db.py checks that the
# two agree)
# ---------------------------------------------------------------------------

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
_PRERELEASE_RE = re.compile(r"^-?([a-zA-Z]+)\.?(\d*)(?:-(\d+)-g[0-9a-f]+)?$")
_GIT_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")


def parse_version(v: str) -> tuple | None:
    s = v.strip()
    if s[:1] in ("v", "V") and s[1:2].isdigit():
        s = s[1:]
    if "." not in s and _GIT_HASH_RE.match(s):
        return None
    m = _NUM_PREFIX_RE.match(s)
    if m is None:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
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
                return nums + (rank, num, int(pr.group(3)))
            return nums + (rank, num)
    return nums + (_RANK_FINAL, 0)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _uri(path: str, mode: str) -> str:
    return f"file:{quote(os.path.abspath(path))}?mode={mode}"


def _connect(path: str, mode: str = "rw") -> sqlite3.Connection:
    """Open an existing database (a missing file is an error, never a new
    DB) in autocommit mode, URI handling on so ATTACH takes URIs too.

    ``rw`` for a live database even when only reading it: a WAL database
    needs its -shm writable, which a ``mode=ro`` handle may not get. The
    callers run as the file's owner (the service user), as the app does, so
    no sidecar is ever created with another owner. ``ro`` for the snapshot
    files this tool writes (rollback journal, never WAL)."""
    if not os.path.isfile(path):
        raise Refusal(f"no database at {path}")
    conn = sqlite3.connect(_uri(path, mode), uri=True, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _connect_ro(path: str) -> sqlite3.Connection:
    return _connect(path, "ro")


def _connect_rw(path: str) -> sqlite3.Connection:
    return _connect(path, "rw")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({_q(table)})")]


def _meta(conn: sqlite3.Connection, schema: str = "main") -> dict[str, str]:
    if META_TABLE not in _tables(conn, schema):
        return {}
    return {k: v for k, v in conn.execute(
        f"SELECT key, value FROM {schema}.{META_TABLE}")}


def _schema_version(meta: dict[str, str]) -> int | None:
    try:
        return int(str(meta.get(KEY_SCHEMA_VERSION, "")).strip())
    except ValueError:
        return None


def _quick_check(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA quick_check").fetchone()[0]


def _to_rollback_journal(conn: sqlite3.Connection) -> None:
    """A backup of a WAL database is itself marked WAL. Switch a copy this
    tool keeps or hands on to the rollback journal, so it is one
    self-contained file that a read-only handle (another user's, or this tool's on
    a root-owned file) can open."""
    conn.execute("PRAGMA journal_mode = DELETE").fetchone()


def _slide_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, public_id, wsi_filename, thumb_filename, wsi_generation,"
        " is_published FROM slides ORDER BY id").fetchall()
    return [{"id": r[0], "public_id": r[1], "wsi": r[2] or "", "thumb": r[3] or "",
             "gen": r[4], "published": bool(r[5])} for r in rows]


def _tour_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT public_id FROM tours ORDER BY id")]


def _check_name(name: str, what: str) -> str:
    if (not name or name in (".", "..") or name.startswith(".")
            or _BAD_NAME_RE.search(name) or len(name.encode("utf-8")) > 255):
        raise Refusal(f"refusing an unsafe {what} name in the database: {name!r}")
    return name


def _asset_refs(db_path: str) -> dict[str, set[str]]:
    """{tour public_id: attachment names} the database's markdown links to."""
    conn = _connect_ro(db_path)
    try:
        texts = [r[0] for r in conn.execute(
            "SELECT description FROM tours UNION ALL SELECT description_md FROM steps")]
    finally:
        conn.close()
    refs: dict[str, set[str]] = {}
    for text in texts:
        for m in ASSET_REF_RE.finditer(text or ""):
            refs.setdefault(m.group(1), set()).add(m.group(2))
    return refs


def _references(db_path: str) -> tuple[set[str], set[str], set[str]]:
    """(wsi names, thumbnail names, tour public_ids) a database refers to."""
    conn = _connect_ro(db_path)
    try:
        wsi, thumbs = set(), set()
        for s in _slide_rows(conn):
            if s["wsi"]:
                wsi.add(_check_name(s["wsi"], "WSI file"))
            if s["thumb"]:
                thumbs.add(_check_name(s["thumb"], "thumbnail"))
        tours = set()
        for pid in _tour_ids(conn):
            if not isinstance(pid, str) or not PUBLIC_ID_RE.match(pid):
                raise Refusal(f"refusing an unsafe tour public_id in the database: {pid!r}")
            tours.add(pid)
        return wsi, thumbs, tours
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Source side
# ---------------------------------------------------------------------------

def cmd_state(args) -> None:
    """Versions plus every slide's file fields and whether the files exist.

    Read twice per round by sync-from.sh: the files it transferred belong to
    the snapshot only if no slide's (wsi, thumb, generation) moved between
    the export and this read — a replace always moves the generation up.
    """
    conn = _connect_rw(args.db)
    try:
        conn.execute("BEGIN")
        meta = _meta(conn)
        slides = _slide_rows(conn)
        tours = _tour_ids(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    for s in slides:
        for key, sub in (("wsi", "wsi"), ("thumb", "thumbs")):
            name = s[key]
            sig = None
            if name and "/" not in name and not name.startswith("."):
                sig = _signature(os.path.join(args.data_dir, sub, name))
            s[key + "_ok"] = sig is not None
            s[key + "_sig"] = sig
    # Each tour's attachments as tour-filter lets rsync take them: the
    # regular, non-hidden files directly in its directory, with their
    # signature.
    assets = {}
    for pid in tours:
        if not isinstance(pid, str) or not PUBLIC_ID_RE.match(pid):
            continue
        d = os.path.join(args.data_dir, "assets", "tours", pid)
        files = {}
        try:
            with os.scandir(d) as it:
                for e in it:
                    if not e.name.startswith(".") and e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        files[e.name] = [st.st_size, int(st.st_mtime)]
        except FileNotFoundError:
            pass
        if files:
            assets[pid] = files
    json.dump({
        "app_version": meta.get(KEY_APP_VERSION),
        "schema_version": _schema_version(meta),
        "slides": slides,
        "tours": tours,
        "assets": assets,
    }, sys.stdout)


def _signature(path: str) -> list | None:
    """[size, whole-second mtime] of a regular file, None when there is none.
    rsync -t keeps the mtime, so a mirror copy matches its source's; a
    transfer that failed part-way (rsync exit 23, which the script lets the
    check decide) leaves the old copy, whose signature does not."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return [st.st_size, int(st.st_mtime)]


def _stream_file(path: str) -> None:
    out = sys.stdout.buffer
    with open(path, "rb") as f:
        shutil.copyfileobj(f, out, 1024 * 1024)
    out.flush()


def cmd_export(args) -> None:
    """Write a consistent snapshot of the source DB to stdout.

    ``all``: the whole database (SQLite's online-backup API). ``content``: a
    new database holding just app_meta and CONTENT_TABLES — the access
    statistics can run to gigabytes and the destination keeps its own — read
    inside one transaction, so every table comes from the same moment.
    Either way the copy is built in a private temp directory, quick_check'ed,
    streamed, and removed.
    """
    # Next to the database, not in /tmp: an --all snapshot is the whole
    # database (statistics can run to gigabytes), and /tmp is often a small
    # tmpfs. Removed below; the name says what left it behind otherwise.
    tmpdir = tempfile.mkdtemp(prefix=".wsi-sync-export.",
                              dir=os.path.dirname(os.path.abspath(args.db)))
    out_path = os.path.join(tmpdir, "snapshot.db")
    try:
        if args.scope == "all":
            src = _connect_rw(args.db)
            dst = sqlite3.connect(out_path)
            try:
                src.backup(dst)
                _to_rollback_journal(dst)
            finally:
                dst.close()
                src.close()
        else:
            if not os.path.isfile(args.db):
                raise Refusal(f"no database at {args.db}")
            dst = sqlite3.connect(out_path, isolation_level=None)
            try:
                # Attached read-write (a WAL database), and only read.
                dst.execute("ATTACH DATABASE ? AS src", (os.path.abspath(args.db),))
                dst.execute("BEGIN")
                have = _tables(dst, "src")
                missing = [t for t in CONTENT_TABLES + (META_TABLE,) if t not in have]
                if missing:
                    raise Refusal(
                        "the source database lacks table(s) " + ", ".join(missing)
                        + "; is it a Tessera database of a supported release?")
                for table in (META_TABLE,) + CONTENT_TABLES:
                    sql = dst.execute(
                        "SELECT sql FROM src.sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone()[0]
                    dst.execute(sql)
                    dst.execute(f"INSERT INTO main.{_q(table)} SELECT * FROM src.{_q(table)}")
                if "sqlite_sequence" in have and "sqlite_sequence" in _tables(dst):
                    marks = ",".join("?" * len(CONTENT_TABLES))
                    # The INSERTs above already left a row per AUTOINCREMENT
                    # table (at its max id); the source's own counter, which
                    # can be higher, replaces it.
                    dst.execute(f"DELETE FROM main.sqlite_sequence WHERE name IN ({marks})",
                                CONTENT_TABLES)
                    dst.execute(
                        "INSERT INTO main.sqlite_sequence(name, seq) SELECT name, seq"
                        f" FROM src.sqlite_sequence WHERE name IN ({marks})", CONTENT_TABLES)
                dst.execute("COMMIT")
                dst.execute("DETACH DATABASE src")
            finally:
                dst.close()
        check = sqlite3.connect(out_path)
        try:
            result = _quick_check(check)
        finally:
            check.close()
        if result != "ok":
            raise Refusal(f"the snapshot failed quick_check: {result}")
        _stream_file(out_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _env_value(lines: list[str], key: str) -> str | None:
    """The last live ``KEY=value`` of a wsi.env, quotes removed (as systemd
    and infra/lib/wsi-common.sh's wsi_env_value read it)."""
    value = None
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    for line in lines:
        m = pat.match(line.rstrip("\r\n"))
        if m is None:
            continue
        v = m.group(1)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        value = v
    return value


def cmd_env_secrets(args) -> None:
    with open(args.env_file, encoding="utf-8") as f:
        lines = f.readlines()
    for key in SECRET_KEYS:
        v = _env_value(lines, key)
        if v:
            print(f"{key}={v}")


# ---------------------------------------------------------------------------
# Destination side
# ---------------------------------------------------------------------------

def cmd_info(args) -> None:
    conn = _connect_rw(args.db)
    try:
        conn.execute("BEGIN")
        meta = _meta(conn)
        have = _tables(conn)
        slides = _slide_rows(conn) if "slides" in have else []
        tours = len(_tour_ids(conn)) if "tours" in have else 0
        conn.execute("COMMIT")
    finally:
        conn.close()
    json.dump({
        "app_version": meta.get(KEY_APP_VERSION),
        "schema_version": _schema_version(meta),
        "slides": len(slides),
        "tours": tours,
    }, sys.stdout)


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_versions(scope: str, source: dict, dest: dict) -> list[str]:
    """Raise Refusal for an incompatible pair; return warnings otherwise.

    content: the content tables are copied column by column into the
    destination's own schema, so both sides must be on the same schema_version
    and the source's release must not be newer (update this server first).
    all: like a restore of a backup — the destination's binary must be at
    least as new as the source in both numbers; its next boot brings an
    older database forward.
    """
    warnings = []
    s_schema, d_schema = source.get("schema_version"), dest.get("schema_version")
    s_app = source.get("app_version") or ""
    d_app = dest.get("app_version") or ""
    if not isinstance(s_schema, int):
        raise Refusal("the source database records no schema_version; "
                      "is it a Tessera database of a supported release?")
    if not isinstance(d_schema, int):
        raise Refusal("this server's database records no schema_version; "
                      "start Tessera here once (or re-run infra/install.sh) first")
    update_here = ("Update this server to the source's release first "
                   "(sudo bash infra/update.sh, with GIT_REF=<tag> to pick it).")
    if s_schema > d_schema:
        raise Refusal(f"the source's schema_version ({s_schema}) is newer than this "
                      f"server's ({d_schema}). " + update_here)
    s_v, d_v = parse_version(s_app), parse_version(d_app)
    if s_v is not None and d_v is not None and s_v > d_v:
        raise Refusal(f"the source runs {s_app}, newer than this server's {d_app}. "
                      + update_here)
    if scope == "content":
        if s_schema < d_schema:
            raise Refusal(
                f"the source's schema_version ({s_schema}) is older than this server's "
                f"({d_schema}). A standby sync needs both on the same schema: update the "
                "source to this server's release (this server cannot go back to an "
                "older one: its database has already been brought forward).")
        if s_app != d_app:
            warnings.append(f"the source runs {s_app or 'an unknown version'} and this "
                            f"server {d_app or 'an unknown version'} (same schema, so the "
                            "sync can go ahead); keep both on one release.")
    return warnings


def cmd_check_versions(args) -> None:
    for w in check_versions(args.scope, _load_json(args.state), _load_json(args.info)):
        _err(f"warning: {w}")


def cmd_files_from(args) -> None:
    wsi, thumbs, _ = _references(args.db)
    out = sys.stdout.buffer
    for name in sorted(wsi):
        out.write(f"wsi/{name}".encode("utf-8") + b"\0")
    for name in sorted(thumbs):
        out.write(f"thumbs/{name}".encode("utf-8") + b"\0")
    out.flush()


def cmd_tour_filter(args) -> None:
    """rsync filter rules for assets/tours/: the listed tours' directories
    and the regular files directly in them; nothing hidden, nothing deeper
    (uploads never create either, and backup.py does not archive them)."""
    _, _, tours = _references(args.db)
    print("- .*")
    print("- /*/*/")
    for pid in sorted(tours):
        print(f"+ /{pid}/")
        print(f"+ /{pid}/*")
    print("- *")


def _ids(items) -> str:
    items = sorted(items, key=str)
    return ", ".join(map(str, items[:10])) + (", ..." if len(items) > 10 else "")


def check_transfer(pre: dict, state: dict, db_path: str,
                   mirror: str) -> tuple[int, list[str]]:
    """(exit code, messages) for one transfer round: ``pre`` is the source's
    state read before the snapshot, ``state`` right after the rsync.
    0: the mirror holds the snapshot's files; EXIT_CHANGED: the source
    changed a slide's files, its tours or an attachment meanwhile (go round
    again); 1: a file the source has did not arrive."""
    conn = _connect_ro(db_path)
    try:
        snap = {s["id"]: (s["wsi"], s["thumb"], s["gen"]) for s in _slide_rows(conn)}
        snap_tours = set(_tour_ids(conn))
    finally:
        conn.close()
    now = {s["id"]: (s["wsi"], s["thumb"], s["gen"]) for s in state["slides"]}
    changes = []
    slides = [k for k in set(now) | set(snap) if now.get(k) != snap.get(k)]
    if slides:
        changes.append(f"{len(slides)} slide(s) (ids {_ids(slides)})")
    # A file rewritten in place keeps its name and generation (a thumbnail
    # rebuilt from the dashboard): its signature moving between the reads
    # before and after the transfer is a change too, not a failed transfer.
    pre_sigs = {s["id"]: (s.get("wsi_sig"), s.get("thumb_sig")) for s in pre.get("slides", [])}
    rewritten = [s["id"] for s in state["slides"] if s["id"] in pre_sigs
                 and pre_sigs[s["id"]] != (s.get("wsi_sig"), s.get("thumb_sig"))]
    if rewritten:
        changes.append(f"the files of {len(rewritten)} slide(s) (ids {_ids(rewritten)})")
    tours = snap_tours ^ set(state["tours"])
    if tours:
        changes.append(f"{len(tours)} tour(s) added or deleted ({_ids(tours)})")
    pre_assets, assets = pre.get("assets", {}), state.get("assets", {})
    moved = [p for p in set(pre_assets) | set(assets) if pre_assets.get(p) != assets.get(p)]
    if moved:
        changes.append(f"the attachments of {len(moved)} tour(s) ({_ids(moved)})")
    if changes:
        return EXIT_CHANGED, ["changed on the source during the transfer: " + "; ".join(changes)]
    messages, missing = [], []
    for pid, files in assets.items():
        for name, sig in files.items():
            if _signature(os.path.join(mirror, "assets", "tours", pid, name)) != list(sig):
                missing.append(f"assets/tours/{pid}/{name}")
    for s in state["slides"]:
        for key, sub in (("wsi", "wsi"), ("thumb", "thumbs")):
            name = s[key]
            if not name:
                continue
            have = _signature(os.path.join(mirror, sub, name))
            if s[key + "_ok"]:
                # Present, and the source's bytes (an older copy of a file
                # replaced under its name would also be "present").
                if have != list(s[key + "_sig"]):
                    missing.append(f"{sub}/{name}")
                continue
            if have is not None:
                messages.append(f"warning: slide {s['id']}: {sub}/{name} is missing on the "
                                "source; this server keeps the copy an earlier sync brought")
                continue
            else:
                messages.append(f"warning: slide {s['id']}: {sub}/{name} is missing on the "
                                "source too; the slide stays broken here as it is there")
    if missing:
        return 1, [f"{len(missing)} file(s) the source has did not arrive intact: "
                   + ", ".join(missing[:10]) + (", ..." if len(missing) > 10 else "")]
    return 0, messages


def cmd_check(args) -> None:
    code, messages = check_transfer(_load_json(args.pre), _load_json(args.state),
                                    args.db, args.mirror)
    for m in messages:
        _err(m)
    if code:
        sys.exit(code)


def cmd_summary(args) -> None:
    new = _connect_ro(args.db)
    try:
        n_slides = _slide_rows(new)
        n_tours = len(_tour_ids(new))
    finally:
        new.close()
    cur = _connect_rw(args.live_db)
    try:
        c_slides = _slide_rows(cur)
        c_tours = len(_tour_ids(cur))
    finally:
        cur.close()
    n_by, c_by = {s["public_id"]: s for s in n_slides}, {s["public_id"]: s for s in c_slides}
    added = [p for p in n_by if p not in c_by]
    removed = [p for p in c_by if p not in n_by]
    replaced = [p for p in n_by if p in c_by and n_by[p]["gen"] != c_by[p]["gen"]]
    published = sum(1 for s in n_slides if s["published"])
    print(f"slides: {len(c_slides)} here now -> {len(n_slides)} after the sync "
          f"({published} published); {len(added)} new, {len(removed)} removed, "
          f"{len(replaced)} with a replaced WSI")
    print(f"tours:  {c_tours} here now -> {n_tours} after the sync")
    # This server's records name slides / users by bare id (no foreign key).
    # Where an id comes to mean another slide or account (a first sync onto a
    # server that had content of its own), its earlier statistics and audit
    # rows would be credited to the new one: say so.
    if args.scope == "content":
        cur = _connect_rw(args.live_db)
        new = _connect_ro(args.db)
        try:
            records = sum(cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("page_views", "slide_dwell_events", "tour_sessions", "audit_logs")
                          if t in _tables(cur))
            moved_slides = sum(1 for sid, pid in cur.execute("SELECT id, public_id FROM slides")
                               if new.execute("SELECT 1 FROM slides WHERE id = ? AND public_id = ?",
                                              (sid, pid)).fetchone() is None
                               and new.execute("SELECT 1 FROM slides WHERE id = ?", (sid,)).fetchone())
            moved_users = sum(1 for uid, name in cur.execute("SELECT id, username FROM users")
                              if new.execute("SELECT 1 FROM users WHERE id = ? AND username IS NOT ?",
                                             (uid, name)).fetchone())
        finally:
            cur.close()
            new.close()
        if records and (moved_slides or moved_users):
            print(f"note:   {moved_slides} slide id(s) and {moved_users} user id(s) here come to mean "
                  "other slides / accounts; this server's statistics and activity history recorded "
                  "under them would then be shown for those (clear them if that "
                  "matters: the dashboard's analytics wipe)")


def cmd_backup_db(args) -> None:
    src = _connect_rw(args.src)
    tmp = args.dst + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        _to_rollback_journal(dst)
        result = _quick_check(dst)
    finally:
        dst.close()
        src.close()
    if result != "ok":
        os.unlink(tmp)
        raise Refusal(f"the copy of {args.src} failed quick_check: {result}")
    os.replace(tmp, args.dst)


def merge_content(live_path: str, content_path: str) -> list[str]:
    """Replace the destination's CONTENT_TABLES with the snapshot's, in one
    transaction; return warnings. Columns are matched by name (two installs
    on one schema can still order them differently, and a column only one
    side has is left to its default / dropped, as a restore of an older
    archive would). Nothing is committed unless every foreign key holds."""
    warnings = []
    conn = _connect_rw(live_path)
    try:
        # Off for the swap: deleting a slide must not cascade into its tours
        # before the snapshot's tours replace them anyway. Checked below.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ATTACH DATABASE ? AS src",
                     (f"file:{quote(os.path.abspath(content_path))}?mode=ro",))
        conn.execute("BEGIN IMMEDIATE")
        try:
            have, src_have = _tables(conn), _tables(conn, "src")
            for table in CONTENT_TABLES:
                if table not in have or table not in src_have:
                    side = "this server's database" if table not in have else "the snapshot"
                    raise Refusal(f"table {table} is missing from {side}")
                cols = _columns(conn, table)
                src_cols = _columns(conn, table, "src")
                both = [c for c in cols if c in src_cols]
                dropped = [c for c in src_cols if c not in cols]
                if dropped:
                    warnings.append(f"{table}: column(s) {', '.join(dropped)} exist only on "
                                    "the source and are not copied")
                collist = ", ".join(_q(c) for c in both)
                conn.execute(f"DELETE FROM main.{_q(table)}")
                conn.execute(f"INSERT INTO main.{_q(table)} ({collist}) "
                             f"SELECT {collist} FROM src.{_q(table)}")
            if "sqlite_sequence" in have and "sqlite_sequence" in src_have:
                for name, seq in conn.execute(
                        "SELECT name, seq FROM src.sqlite_sequence WHERE name IN ("
                        + ",".join("?" * len(CONTENT_TABLES)) + ")", CONTENT_TABLES).fetchall():
                    row = conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name = ?",
                                       (name,)).fetchone()
                    if row is None:
                        conn.execute("INSERT INTO main.sqlite_sequence(name, seq) VALUES (?, ?)",
                                     (name, seq))
                    elif seq > row[0]:
                        conn.execute("UPDATE main.sqlite_sequence SET seq = ? WHERE name = ?",
                                     (seq, name))
            src_meta, meta = _meta(conn, "src"), _meta(conn)
            hwm = max(int(v) for v in (
                [src_meta.get(KEY_SLIDE_ID_HWM, "0"), meta.get(KEY_SLIDE_ID_HWM, "0"),
                 conn.execute("SELECT COALESCE(MAX(id), 0) FROM main.slides").fetchone()[0]]
            ) if str(v).strip().isdigit())
            upsert = (f"INSERT INTO main.{META_TABLE}(key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
            conn.execute(upsert, (KEY_SLIDE_ID_HWM, str(hwm)))
            if src_meta.get(KEY_TILE_GEOMETRY):
                conn.execute(upsert, (KEY_TILE_GEOMETRY, src_meta[KEY_TILE_GEOMETRY]))
            bad = conn.execute("PRAGMA main.foreign_key_check").fetchall()
            if bad:
                raise Refusal(f"the merged database breaks {len(bad)} foreign key(s) "
                              f"(first: {bad[0]!r}); nothing was changed")
            # Checked inside the transaction (quick_check sees its pages), so
            # a failure rolls back: once COMMIT has run, the swap stands and
            # the caller must treat it as done (sign everyone out).
            result = conn.execute("PRAGMA main.quick_check").fetchone()[0]
            if result != "ok":
                raise Refusal(f"the merged database failed quick_check ({result}); "
                              "nothing was changed")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    return warnings


def cmd_merge(args) -> None:
    for w in merge_content(args.live_db, args.db):
        _err(f"warning: {w}")


def cmd_replace(args) -> None:
    """Copy the snapshot over the live DB through SQLite's backup API, so the
    copy goes through SQLite's own journalling and a -wal left behind can
    never be replayed on top of it (as with update.sh's snapshot restore)."""
    src = _connect_ro(args.db)
    try:
        # The copy cannot be checked before it lands, so check what is copied.
        result = _quick_check(src)
        if result != "ok":
            raise Refusal(f"{args.db} failed quick_check ({result}); nothing was changed")
        dst = _connect_rw(args.live_db)
        try:
            src.backup(dst)
            result = _quick_check(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if result != "ok":
        _err(f"error: {args.live_db} was replaced, then failed quick_check: {result}")
        sys.exit(EXIT_SWAPPED_BAD)


def _load_restore_apply(path: str):
    if not os.path.isfile(path):
        raise Refusal(f"no {path}; is Tessera installed here?")
    spec = importlib.util.spec_from_file_location("_wsi_restore_apply", path)
    ra = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ra)
    return ra


def cmd_check_sign_out(args) -> None:
    """Refuse while a sign-out the app has still to finish is pending (a
    restore's, or an earlier sync's). Another swap now would replace the
    database it compares against (wsi.db.pre-sync) or its marker, and the
    retry would then sign out only past the newer counters: the credentials
    revoked before the first swap would work again. backup.py refuses a
    second restore for the same reason."""
    ra = _load_restore_apply(args.restore_apply)
    marker = args.live_db + ra.SESSION_REVOCATION_SUFFIX
    if os.path.lexists(marker):
        raise Refusal(
            f"{marker} says an earlier swap's sign-out is still pending. Start the "
            "services (sudo systemctl start wsi-app wsi-sync) so the app finishes it; if "
            "they do not come up, the log says why (sudo journalctl -u wsi-app). Then run "
            "this again.")


def cmd_sign_out(args) -> None:
    """Revoke every staff token and app-access cookie issued before a swap,
    with the app's own post-restore sign-out (restore_apply.invalidate_sessions,
    loaded from the installed app: stdlib-only, so this python3 runs it), and
    against ``prev_db``, the database the swap replaced.

    Without it a swap brings back the source's (or, for a rollback, the
    older) users.token_version / auth_settings.session_generation, under
    which a token revoked here is honoured again, and one issued to the
    account that held an id before the swap passes for the account holding
    it after. Should the sign-out fail, the marker restore_apply leaves in
    that case makes the app retry it when it starts, and refuse to serve
    while it keeps failing (backup.retry_pending_session_revocation)."""
    ra = _load_restore_apply(args.restore_apply)
    try:
        ra.invalidate_sessions(args.live_db, args.prev_db)
    except Exception as exc:  # noqa: BLE001 -- any failure is handed to the boot
        reason = str(exc) or type(exc).__name__
        marker = args.live_db + ra.SESSION_REVOCATION_SUFFIX
        try:
            ra.write_json_durable(marker, {
                "stage_id": "infra/sync-from.sh",
                "previous_db": args.prev_db,
                "error": reason,
                "failed_at": ra.utc_now_z(),
            })
        except Exception as marker_exc:  # noqa: BLE001
            raise Refusal(f"could not sign out the sessions issued before the swap ({reason}), "
                          f"nor leave {marker} for the app to retry it ({marker_exc})")
        _err(f"warning: could not sign out the sessions issued before the swap ({reason}); "
             "the app retries it when it starts, and does not serve until it succeeds")
        sys.exit(EXIT_DEFERRED)


def cmd_bump_generations(args) -> None:
    """Move every slide to a fresh tile generation, as a tile-geometry change
    does at boot (dzi.reconcile_tile_geometry): the same max(generation + 1,
    now in ms) as models.fresh_wsi_generation, so the number was never served.
    For when files were put in place under a database that does not describe
    them (a swap that failed after it, a rollback): a generation must never
    name tiles of two different files in the edge cache."""
    conn = _connect_rw(args.live_db)
    try:
        now_ms = max(1, time.time_ns() // 1_000_000)
        conn.execute("UPDATE slides SET wsi_generation = MAX(wsi_generation + 1, ?)", (now_ms,))
    finally:
        conn.close()


def cmd_mfa_accounts(args) -> None:
    """Print how many staff accounts in DB have two-factor sign-in on. Their
    secrets open only under the WSI_MFA_KEY they were sealed with, so a
    standby sync that brings such accounts over needs the source's key too."""
    conn = _connect_ro(args.db)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("users")')}
        n = 0
        if "totp_secret_enc" in cols:
            n = conn.execute(
                "SELECT COUNT(*) FROM users WHERE totp_secret_enc IS NOT NULL"
            ).fetchone()[0]
    finally:
        conn.close()
    print(n)


def cmd_quick_check(args) -> None:
    conn = _connect_rw(args.db)
    try:
        result = _quick_check(conn)
    finally:
        conn.close()
    if result != "ok":
        raise Refusal(f"{args.db} failed quick_check: {result}")


def _place(src: str, dst_dir: str, name: str) -> None:
    """Make ``dst_dir/name`` the file ``src``: a hard link when both are on
    one filesystem (no second copy of a multi-GB WSI), a copy otherwise.
    Written under a temp name and renamed, so the old file is replaced in one
    step and a crash leaves at most a temp file the next run removes."""
    tmp = os.path.join(dst_dir, f".{name}.sync-{secrets.token_hex(6)}")
    try:
        try:
            os.link(src, tmp, follow_symlinks=False)
        except OSError as e:
            # Only across filesystems (the case sync-from.sh sized with
            # install-bytes). Any other refusal (a mirror file not the
            # service user's: fs.protected_hardlinks) is an error, never a
            # silent multi-GB copy nobody checked the space for.
            if e.errno != errno.EXDEV:
                raise
            shutil.copy2(src, tmp, follow_symlinks=False)
        os.replace(tmp, os.path.join(dst_dir, name))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _same_file(a: str, b: str) -> bool:
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except FileNotFoundError:
        return False
    # The same inode (a link), or rsync's own "unchanged" test: same size and
    # mtime (a copy: copy2 keeps the mtime, as rsync -t does). Whatever the
    # devices: two mounts of one filesystem share st_dev but copy (EXDEV).
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino) or (
        sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime))


def _regular_files(d: str) -> list[str]:
    try:
        with os.scandir(d) as it:
            return [e.name for e in it if e.is_file(follow_symlinks=False)]
    except FileNotFoundError:
        return []


def _sweep(d: str, keep: set[str]) -> int:
    """Remove the regular files in ``d`` that are not in ``keep``. Hidden
    files are left alone (rsync partials, upload temporaries) except a temp
    file of this tool's that a crash left behind."""
    removed = 0
    for name in _regular_files(d):
        if name in keep:
            continue
        if name.startswith(".") and not _SYNC_TMP_RE.match(name):
            continue
        os.unlink(os.path.join(d, name))
        removed += 1
    return removed


def install_files(db_path: str, mirror: str, data_dir: str,
                  keep_db: str | list[str] | None = None) -> dict:
    """Put the mirror's files in place under DATA_DIR for the database at
    ``db_path``, then remove what neither it nor any ``keep_db`` refers to:
    slide files by name, attachments by the links in its markdown. The keep
    databases are the one being replaced (a swap that fails still finds its
    files) and the current rollback point (which a failed sync keeps).
    Runs as the service user: nothing here follows a path as root."""
    wsi, thumbs, tours = _references(db_path)
    keeps = [keep_db] if isinstance(keep_db, str) else list(keep_db or [])
    keep_wsi, keep_thumbs, keep_tours = set(), set(), set()
    keep_assets: dict[str, set[str]] = {}
    for k in keeps:
        if not (k and os.path.isfile(k)):
            continue
        w, t, tr = _references(k)
        keep_wsi |= w
        keep_thumbs |= t
        keep_tours |= tr
        for pid, names in _asset_refs(k).items():
            keep_assets.setdefault(pid, set()).update(names)
    stats = {"placed": 0, "removed": 0, "missing": 0}
    for sub, names, keep in (("wsi", wsi, keep_wsi), ("thumbs", thumbs, keep_thumbs)):
        src_dir, dst_dir = os.path.join(mirror, sub), os.path.join(data_dir, sub)
        os.makedirs(dst_dir, exist_ok=True)
        for name in sorted(names):
            src = os.path.join(src_dir, name)
            if not os.path.isfile(src):
                stats["missing"] += 1
                continue
            if not _same_file(src, os.path.join(dst_dir, name)):
                _place(src, dst_dir, name)
                stats["placed"] += 1
        stats["removed"] += _sweep(dst_dir, names | keep)
    tours_src = os.path.join(mirror, "assets", "tours")
    tours_dst = os.path.join(data_dir, "assets", "tours")
    os.makedirs(tours_dst, exist_ok=True)
    for pid in sorted(tours):
        src_dir, dst_dir = os.path.join(tours_src, pid), os.path.join(tours_dst, pid)
        names = {n for n in _regular_files(src_dir) if not n.startswith(".")}
        if not names and not os.path.isdir(dst_dir):
            continue
        if os.path.islink(dst_dir) or (os.path.lexists(dst_dir) and not os.path.isdir(dst_dir)):
            os.unlink(dst_dir)
        os.makedirs(dst_dir, exist_ok=True)
        for name in sorted(names):
            src = os.path.join(src_dir, name)
            if not _same_file(src, os.path.join(dst_dir, name)):
                _place(src, dst_dir, name)
                stats["placed"] += 1
        stats["removed"] += _sweep(dst_dir, names | keep_assets.get(pid, set()))
    with os.scandir(tours_dst) as it:
        stale = [e for e in it if e.name not in tours and e.name not in keep_tours
                 and not e.name.startswith(".")]
    for e in stale:
        if e.is_dir(follow_symlinks=False):
            shutil.rmtree(e.path)
        else:
            os.unlink(e.path)
        stats["removed"] += 1
    return stats


def install_bytes(db_path: str, mirror: str, data_dir: str) -> int:
    """What install_files would write when it has to copy rather than link
    (mirror and DATA_DIR on different filesystems): every mirror file the
    database refers to that DATA_DIR does not already hold, counted whether
    it arrived in this run or an earlier one that was never applied."""
    wsi, thumbs, tours = _references(db_path)
    total = 0
    pairs = [(os.path.join(mirror, sub, n), os.path.join(data_dir, sub, n))
             for sub, names in (("wsi", wsi), ("thumbs", thumbs)) for n in names]
    for pid in tours:
        src_dir = os.path.join(mirror, "assets", "tours", pid)
        pairs += [(os.path.join(src_dir, n), os.path.join(data_dir, "assets", "tours", pid, n))
                  for n in _regular_files(src_dir) if not n.startswith(".")]
    for src, dst in pairs:
        if os.path.isfile(src) and not _same_file(src, dst):
            total += os.stat(src).st_size
    return total


def cmd_can_link(args) -> None:
    """Whether FILE hard-links into DIR: sync-from.sh's test of whether
    install-files links or copies. Same st_dev is not enough: two mounts of
    one filesystem (a bind mount) refuse link() across them (EXDEV)."""
    # Named like _place's temp files, so a sweep removes one a crash left.
    probe = os.path.join(args.dir, f".link-probe.sync-{secrets.token_hex(6)}")
    try:
        os.link(args.file, probe, follow_symlinks=False)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        print(0)
        return
    os.unlink(probe)
    print(1)


def cmd_install_bytes(args) -> None:
    print(install_bytes(args.db, args.mirror, args.data_dir))


def cmd_install_files(args) -> None:
    stats = install_files(args.db, args.mirror, args.data_dir, args.keep_db)
    print(f"{stats['placed']} file(s) put in place, {stats['removed']} removed"
          + (f", {stats['missing']} missing on the source too" if stats["missing"] else ""))


def cmd_prune_mirror(args) -> None:
    """Drop mirror files (wsi/, thumbs/) the files-from list no longer names.
    The mirror is root's, so this runs as root; assets/tours/ is kept exact
    by rsync's --delete-excluded."""
    with open(args.files_from, "rb") as f:
        listed = [p.decode("utf-8") for p in f.read().split(b"\0") if p]
    by_dir: dict[str, set[str]] = {"wsi": set(), "thumbs": set()}
    for p in listed:
        sub, _, name = p.partition("/")
        if sub in by_dir:
            by_dir[sub].add(name)
    removed = sum(_sweep(os.path.join(args.mirror, sub), names)
                  for sub, names in by_dir.items())
    if removed:
        print(f"{removed} file(s) no longer referenced removed from the mirror")


def cmd_set_env(args) -> None:
    """Write the SECRET_KEYS assignments read from stdin (``KEY=value``
    lines, as env-secrets prints them) into ENV_FILE, using envfile_set.py's
    rewrite. Values come on stdin, never on a command line ps could show."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import envfile_set  # noqa: E402  (sibling file in infra/lib)

    values, unusable = {}, []
    for line in sys.stdin.read().splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in SECRET_KEYS or not value:
            continue
        # What wsi_env_value / systemd read back unquoted and unchanged.
        if re.search(r"[\s#'\"\\]", value):
            unusable.append(key)
        else:
            values[key] = value
    for key in unusable:
        _err(f"warning: the source's {key} holds characters this tool does not write "
             "into wsi.env; copy it by hand")
    if not values:
        raise Refusal("no secret to write")
    with open(args.env_file, encoding="utf-8") as f:
        lines = f.readlines()
    for key, value in values.items():
        lines = envfile_set.rewrite(lines, key, value, "sync-from.sh",
                                    "Carried over from the source server by infra/sync-from.sh",
                                    None)
    envfile_set.write_atomic(args.env_file, lines, keep_owner=True)
    print(", ".join(sorted(values)))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="replica_db.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, func, *params, scope=False, keep=False):
        p = sub.add_parser(name)
        for param in params:
            p.add_argument(param)
        if scope:
            p.add_argument("--scope", choices=("content", "all"), required=True)
        if keep:
            p.add_argument("--keep-db", action="append", default=[])
        p.set_defaults(func=func)

    add("state", cmd_state, "db", "data_dir")
    add("export", cmd_export, "db", scope=True)
    add("info", cmd_info, "db")
    add("check-versions", cmd_check_versions, "state", "info", scope=True)
    add("files-from", cmd_files_from, "db")
    add("tour-filter", cmd_tour_filter, "db")
    add("check", cmd_check, "pre", "state", "db", "mirror")
    add("summary", cmd_summary, "db", "live_db", scope=True)
    add("backup-db", cmd_backup_db, "src", "dst")
    add("merge", cmd_merge, "live_db", "db")
    add("replace", cmd_replace, "live_db", "db")
    add("sign-out", cmd_sign_out, "live_db", "prev_db", "restore_apply")
    add("check-sign-out", cmd_check_sign_out, "live_db", "restore_apply")
    add("bump-generations", cmd_bump_generations, "live_db")
    add("quick-check", cmd_quick_check, "db")
    add("mfa-accounts", cmd_mfa_accounts, "db")
    add("install-files", cmd_install_files, "db", "mirror", "data_dir", keep=True)
    add("install-bytes", cmd_install_bytes, "db", "mirror", "data_dir")
    add("can-link", cmd_can_link, "file", "dir")
    add("prune-mirror", cmd_prune_mirror, "files_from", "mirror")
    add("env-secrets", cmd_env_secrets, "env_file")
    add("set-env", cmd_set_env, "env_file")
    args = ap.parse_args(argv)
    try:
        args.func(args)
    except Refusal as e:
        _err(f"error: {e}")
        sys.exit(1)
    except (sqlite3.Error, OSError) as e:
        _err(f"error: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
