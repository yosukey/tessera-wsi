"""A pooled connection must not outlive a swap of the SQLite file under it.

A restore moves a new wsi.db onto the path while wsi-sync (a separate
process) can still be running. Pooled handles that kept following the old
inode — the parked pre-restore copy — would go on answering auth / IP / staff
checks from the old data and write audit rows into the parked file.
"""
from __future__ import annotations

import os
import sqlite3

from sqlalchemy import create_engine, text

from wsi_app.db import install_replaced_file_guard


def _make_db(path: str, value: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


def test_checkout_reconnects_after_db_file_replaced(tmp_path):
    live = tmp_path / "wsi.db"
    _make_db(str(live), "old")
    eng = create_engine(f"sqlite:///{live}", pool_size=1, max_overflow=0)
    install_replaced_file_guard(eng)
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT v FROM t")).scalar_one() == "old"

        # The restore swap: park the live file, move the new one onto the path.
        staged = tmp_path / "staged.db"
        _make_db(str(staged), "new")
        os.replace(live, tmp_path / "parked.db")
        os.replace(staged, live)

        with eng.connect() as c:
            assert c.execute(text("SELECT v FROM t")).scalar_one() == "new"
            c.execute(text("INSERT INTO t VALUES ('written-after-swap')"))
            c.commit()
    finally:
        eng.dispose()

    parked = sqlite3.connect(str(tmp_path / "parked.db"))
    try:
        rows = [r[0] for r in parked.execute("SELECT v FROM t")]
    finally:
        parked.close()
    assert rows == ["old"], "a write after the swap landed in the parked file"


def test_production_engine_has_guard_installed():
    # The unit test above exercises install_replaced_file_guard on a private
    # engine; this pins that the module-level engine every process (wsi-app
    # AND wsi-sync) actually uses has it installed, so dropping the
    # install_replaced_file_guard(engine) call in db.py can't pass silently.
    from wsi_app import db as wsi_db

    checkout_names = {
        getattr(fn, "__name__", "") for fn in wsi_db.engine.pool.dispatch.checkout
    }
    assert "_drop_connection_to_replaced_db" in checkout_names

    # And the connect half: a fresh checkout records the file identity it
    # opened, which is what the checkout listener compares against.
    st = os.stat(wsi_db.engine.url.database)
    with wsi_db.engine.connect() as c:
        assert c.connection.info.get("wsi_db_file") == (st.st_dev, st.st_ino)
