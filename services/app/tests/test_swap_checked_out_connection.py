"""A connection checked out while the dev in-process restore swaps wsi.db.

``backup.inprocess_apply`` disposes the engine's pool before it parks the
live DB, but dispose only closes the connections checked *in*: one a request
is still holding keeps the old file open across the swap. test_db_file_swap.py
covers a file swapped under an idle pool; this covers the real swap with a
connection in use (in WAL mode, as the app runs, so the parked -wal / -shm
move with it): afterwards new sessions read and write the restored DB, the
held connection never comes back into service, and nothing lands in the
parked pre-restore copy — nor is the restored DB's WAL disturbed when the
held connection finally closes.
"""
from __future__ import annotations

import gc
import os
import sqlite3

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from wsi_app import backup
from wsi_app.db import _apply_sqlite_pragmas, install_replaced_file_guard

from .test_restore_apply_journal import STAGE_ID, Env
from .test_restore_apply_journal import dev_settings  # noqa: F401 - fixture


def _rows(db) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT id, wsi_filename, width FROM slides ORDER BY id").fetchall()
    finally:
        con.close()


def _integrity(db) -> str:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def test_a_connection_held_across_the_inprocess_swap_never_serves_again(tmp_path, dev_settings):
    env = Env(tmp_path / "e")
    # Env's live -shm is opaque bytes for the rename matrix; a real WAL-mode
    # connection must create its own.
    os.remove(env.data / "wsi.db-shm")
    live = env.data / "wsi.db"
    eng = create_engine(f"sqlite:///{live}", pool_size=2, max_overflow=0,
                        connect_args={"check_same_thread": False})
    event.listen(eng, "connect", _apply_sqlite_pragmas)  # WAL, as the app runs
    install_replaced_file_guard(eng)
    Session = sessionmaker(bind=eng)
    dev_settings(env)
    try:
        held = eng.connect()
        held.execute(text("UPDATE slides SET width = 101 WHERE id = 1"))
        held.commit()
        # An open read transaction, as a request mid-query holds.
        held.begin()
        assert held.execute(text("SELECT width FROM slides WHERE id = 1")).scalar_one() == 101
        assert (env.data / "wsi.db-wal").exists()

        backup.inprocess_apply(STAGE_ID, "full", eng)
        (rollback,) = env.data.glob(".pre-restore-*")
        parked = rollback / "wsi.db"
        assert (rollback / "wsi.db-wal").exists()  # the WAL moved with its DB

        # New sessions see the restored database and write into it.
        with Session() as s:
            assert s.execute(text("SELECT width FROM slides WHERE id = 1")).scalar_one() == 200
            s.execute(text("INSERT INTO slides VALUES (2, 'after.svs', 300)"))
            s.commit()

        # The held connection still follows the inode it opened — the parked
        # copy — until it is returned; it must not come back into service.
        assert held.execute(text("SELECT width FROM slides WHERE id = 1")).scalar_one() == 101
        held.rollback()
        held.close()
        for i in range(4):  # more checkouts than the pool holds
            with eng.connect() as c:
                assert c.execute(text("SELECT width FROM slides WHERE id = 1")).scalar_one() == 200
                c.execute(text("UPDATE slides SET width = :w WHERE id = 2"), {"w": 300 + i})
                c.commit()
    finally:
        eng.dispose()
        gc.collect()  # the held connection's old pool closes it now

    # Nothing written after the swap reached the parked copy ...
    assert _rows(parked) == [(1, "1_old.svs", 101)]
    assert _integrity(parked) == "ok"
    # ... and the held connection's close (the last one on the parked inode)
    # did not checkpoint into, or delete the WAL of, the restored database:
    # SQLite skips that for a file renamed since it was opened.
    assert _rows(live) == [(1, "1_new.svs", 200), (2, "after.svs", 303)]
    assert _integrity(live) == "ok"
