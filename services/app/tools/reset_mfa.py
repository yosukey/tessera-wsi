#!/usr/bin/env python3
"""Turn off a staff account's two-factor sign-in from the server.

The last resort for when nobody can sign in to reset it from the dashboard
(the only admin lost both the authenticator device and the recovery codes, or
WSI_MFA_KEY was lost). It does what an admin's "Reset two-factor" does: clears
the account's TOTP secret and recovery codes and bumps its token_version, so
any session it still has ends and the next sign-in takes the password alone
(a lockout left by attempts at old codes is lifted too).
The reset is recorded in the activity history (``admin.mfa.reset``, under
System, with ``via: cli``).

Usage
-----
Run it from your own clone of the repository (the one you run install.sh /
update.sh from), never from the deployed /opt/wsi tree: the service user owns
that tree, and root must not run code the service user can rewrite (the
tool refuses to). ``-I`` keeps Python from importing anything that sits next
to the script.

    cd ~/wsi                    # your clone
    sudo python3 -I services/app/tools/reset_mfa.py <username>

    # A database somewhere else
    sudo python3 -I services/app/tools/reset_mfa.py <username> --db /path/to/wsi.db

The database is ``--db``, else ``DATABASE_URL`` from the environment, else
the one /etc/wsi/wsi.env names (``WSI_ENV_FILE`` overrides that path; the
file is root-only, hence sudo), else /var/lib/wsi/wsi.db; the path used is
printed. Run as root, the tool switches to the database file's owner before
opening it, so SQLite's -wal / -shm files keep their owner.

The services may keep running. Standard library only: it touches nothing but
the ``users`` and ``audit_logs`` tables, through ``sqlite3``.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sqlite3
import sys

DEFAULT_DB = "/var/lib/wsi/wsi.db"
DEFAULT_ENV_FILE = "/etc/wsi/wsi.env"
_MFA_COLUMNS = ("totp_secret_enc", "totp_enabled_at", "totp_last_step", "mfa_recovery_codes")


class _Unreadable(Exception):
    pass


# What config.py and SQLAlchemy accept, as infra/lib/wsi-common.sh's
# wsi_sqlite_db_path reads it: an absolute path after sqlite:/// (a driver
# suffix allowed), any ?query dropped.
_SQLITE_URL = re.compile(r"^sqlite(?:\+\w+)?:///(/[^?]*)(?:\?.*)?$")


def _env_file_database_url(path: str) -> str:
    """DATABASE_URL as systemd reads it from wsi.env: the last uncommented
    assignment, surrounding quotes dropped. "" when there is no such file or
    no assignment; _Unreadable when the file exists but cannot be read (it is
    root-only), since guessing the default then could reset the wrong
    database."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise _Unreadable(f"cannot read {path} ({exc.strerror}); run with sudo, or pass --db") from None
    value = ""
    for line in lines:
        key, sep, val = line.strip().partition("=")
        if sep and key.strip() == "DATABASE_URL":
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            value = val
    return value


def _default_db() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip() or _env_file_database_url(
        os.environ.get("WSI_ENV_FILE") or DEFAULT_ENV_FILE
    )
    if not url:
        return DEFAULT_DB
    m = _SQLITE_URL.match(url)
    if not m:
        # Never fall back to the default here: that may be a stale copy, and
        # "reset" there would report success while the live one is untouched.
        raise _Unreadable(f"cannot tell the database file from DATABASE_URL={url!r}; pass --db")
    return m.group(1)


def _service_user_owns_this_script(db_owner: int) -> bool:
    """True when the database's owner (the service user) owns this file or
    any directory above it (symlinks resolved), i.e. could have swapped in
    what root is running. A tripwire for running the deployed /opt/wsi copy,
    not a boundary: a copy the service user already rewrote need not carry
    it. Running from your own clone is the rule (see the module docstring)."""
    path = os.path.realpath(__file__)
    while True:
        if os.stat(path).st_uid == db_owner:
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def _become_owner(db_path: str) -> None:
    """Run as root, switch to the database file's owner: a root-owned -wal /
    -shm left behind would lock the service out of its own database."""
    if os.geteuid() != 0:
        return
    st = os.stat(db_path)
    if st.st_uid == 0:
        return
    if _service_user_owns_this_script(st.st_uid):
        raise _Unreadable(
            "refusing to run as root from a copy the service user owns "
            f"({os.path.abspath(__file__)}); run it from your own clone of the repository"
        )
    try:
        # The owner's own supplementary groups, as the service has them: the
        # database's directory may be reachable only through one.
        os.initgroups(pwd.getpwuid(st.st_uid).pw_name, st.st_gid)
    except KeyError:
        os.setgroups([])
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def reset(db_path: str, username: str) -> int:
    if not os.path.isfile(db_path):
        print(f"error: no database at {db_path}", file=sys.stderr)
        return 2
    try:
        _become_owner(db_path)
    except _Unreadable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Database: {db_path}")
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        columns = _columns(conn, "users")
    except sqlite3.Error as exc:
        print(f"error: cannot open {db_path}: {exc}", file=sys.stderr)
        return 2
    try:
        if not set(_MFA_COLUMNS) <= columns:
            print("This database has no two-factor columns yet; nothing to reset.")
            return 0
        row = conn.execute(
            "SELECT id, role, totp_secret_enc FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            print(f"error: no account named {username!r}", file=sys.stderr)
            return 1
        user_id, role, sealed = row
        if sealed is None:
            print(f"Two-factor sign-in is already off for {username!r}.")
            return 0
        with conn:
            conn.execute(
                "UPDATE users SET totp_secret_enc = NULL, totp_enabled_at = NULL, "
                "totp_last_step = NULL, mfa_recovery_codes = NULL, "
                "failed_login_count = 0, lockout_until = NULL, "
                "token_version = token_version + 1 WHERE id = ?",
                (user_id,),
            )
            if "audit_logs" in {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }:
                conn.execute(
                    "INSERT INTO audit_logs (actor_id, actor_username, actor_role, action, target, details) "
                    "VALUES (NULL, '', NULL, 'admin.mfa.reset', ?, ?)",
                    (str(user_id), json.dumps(
                        {"target_username": username, "role": role, "via": "cli"},
                        ensure_ascii=False,
                    )),
                )
    finally:
        conn.close()
    print(
        f"Two-factor sign-in turned off for {username!r}; its open sessions have ended. "
        "It can sign in with the password alone and enrol again from the account page."
    )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Turn off a staff account's two-factor sign-in.")
    ap.add_argument("username", help="the account's sign-in name (exact, case-sensitive)")
    ap.add_argument(
        "--db", default=None,
        help=f"SQLite file (default: DATABASE_URL from the environment, else from "
             f"{DEFAULT_ENV_FILE} or $WSI_ENV_FILE, else {DEFAULT_DB})",
    )
    args = ap.parse_args(argv)
    try:
        db_path = args.db or _default_db()
    except _Unreadable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return reset(db_path, args.username)


if __name__ == "__main__":
    sys.exit(main())
