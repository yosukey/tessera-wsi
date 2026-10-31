"""tools/normalize_live_db.py: rebuilding a table must not reset AUTOINCREMENT."""
from __future__ import annotations

import importlib.util
import signal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wsi_app.db import Base
from wsi_app.models import Slide, Tour

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "normalize_live_db.py"


@pytest.fixture(autouse=True)
def _restore_sigpipe():
    """main() puts SIGPIPE back to its default action (so `| head` ends quietly);
    run in this process, that would leave the whole test run to die of SIGPIPE
    at the next write to a closed socket (exit status 141)."""
    saved = signal.getsignal(signal.SIGPIPE) if hasattr(signal, "SIGPIPE") else None
    yield
    if saved is not None:
        signal.signal(signal.SIGPIPE, saved)


def _load_tool():
    spec = importlib.util.spec_from_file_location("normalize_live_db_under_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_orm(str(_TOOL.parents[1]))
    return mod


def test_rebuild_keeps_autoincrement_high_water_mark(tmp_path):
    path = str(tmp_path / "wsi.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        slide = Slide(title="s")
        db.add(slide)
        db.flush()
        for _ in range(5):
            db.add(Tour(slide_id=slide.id))
        db.commit()
        db.delete(db.get(Tour, 5))  # the top id is gone; 5 must never come back
        db.commit()
    engine.dispose()

    tool = _load_tool()
    conn = tool._connect(path)
    tool.rebuild_table(conn, "tours")
    conn.commit()
    assert conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tours'"
    ).fetchone() == (5,)
    conn.execute(
        "INSERT INTO tours(public_id, slide_id, title, description, is_published,"
        " unlisted, revision, created_at) VALUES ('new', 1, 't', '', 0, 0, 0, CURRENT_TIMESTAMP)"
    )
    assert conn.execute("SELECT id FROM tours WHERE public_id = 'new'").fetchone() == (6,)
    conn.close()


def test_backfill_uses_the_reconcilers_default_literal():
    """A private copy of the reconciler's default renderer that let Python's
    float() decide what is numeric would put 'nan', 'inf' and '1_0' into the
    UPDATE unquoted, where SQLite reads them as column names."""
    from sqlalchemy import Column, Integer, MetaData, String, Table, text

    from wsi_app.column_defaults import column_default_literal

    mod = _load_tool()
    t = Table(
        "t", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("a", String, nullable=False, server_default="nan"),
        Column("b", String, nullable=False, server_default="1_0"),
        Column("c", String, nullable=False, server_default=text("''")),
        Column("d", Integer, nullable=False, server_default="7"),
        Column("e", String, nullable=False, default="it's"),
    )
    got = {c.name: mod._backfill_sql(c) for c in t.columns if c.name != "id"}
    assert got == {"a": "'nan'", "b": "'1_0'", "c": "''", "d": "7", "e": "'it''s'"}
    for name in got:
        assert got[name] == column_default_literal(t.c[name])


# ---------------------------------------------------------------------------
# Root must not run this against the service user's database
# ---------------------------------------------------------------------------
def _owners(monkeypatch, tool, db, *, stat_uid, lstat_uid=None, dir_uid=None):
    """Make os.stat / os.lstat report other owners for the database path
    (``stat_uid`` following links, ``lstat_uid`` for the link itself) and for the
    directory holding it, whatever the real owners are."""
    import os as _os
    import types

    db = str(db)
    directory = str(_os.path.dirname(db))
    lstat_uid = stat_uid if lstat_uid is None else lstat_uid
    dir_uid = stat_uid if dir_uid is None else dir_uid

    def wrap(real, db_uid):
        def fake(path, *a, **kw):
            st = real(path, *a, **kw)
            p = str(path)
            owner = db_uid if p == db else dir_uid if p == directory else None
            if owner is None:
                return st
            # Everything the real stat says (os.path.isfile reads st_mode), as another owner's.
            fields = {n: getattr(st, n) for n in dir(st) if n.startswith("st_")}
            fields["st_uid"] = owner
            return types.SimpleNamespace(**fields)
        return fake

    monkeypatch.setattr(tool.os, "stat", wrap(_os.stat, stat_uid))
    monkeypatch.setattr(tool.os, "lstat", wrap(_os.lstat, lstat_uid))


def _service_user(monkeypatch, tool, name="wsi"):
    monkeypatch.setattr(tool.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": name})())


def test_root_is_refused_on_a_database_the_service_user_owns(tmp_path, monkeypatch):
    tool = _load_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, db, stat_uid=1234)
    _service_user(monkeypatch, tool)
    message = tool._refuse_root_on_service_db(str(db))
    assert message and "refusing to run as root" in message
    # ... and says exactly how to run it instead.
    assert f"sudo -u wsi {tool.sys.executable}" in message and str(db) in message


def test_root_may_run_it_on_a_root_owned_database_in_a_root_owned_directory(tmp_path, monkeypatch):
    tool = _load_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, db, stat_uid=0)
    assert tool._refuse_root_on_service_db(str(db)) is None


def test_a_root_owned_database_in_the_service_users_directory_is_refused(tmp_path, monkeypatch):
    """Root writes the backup and the working copy at predictable names beside
    the database; in a directory the service user owns it would follow what they
    planted there."""
    tool = _load_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, db, stat_uid=0, dir_uid=1234)
    _service_user(monkeypatch, tool)
    message = tool._refuse_root_on_service_db(str(db))
    assert message and "is in a directory owned by wsi" in message


def test_a_link_the_service_user_planted_is_refused_whatever_it_points_at(tmp_path, monkeypatch):
    """The service user replaces wsi.db with a link to a root-owned database: a
    stat follows it and sees root, the link itself is theirs."""
    tool = _load_tool()
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    link = tmp_path / "wsi.db"
    link.symlink_to(target)
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, link, stat_uid=0, lstat_uid=1234, dir_uid=0)
    _service_user(monkeypatch, tool)
    message = tool._refuse_root_on_service_db(str(link))
    assert message and "belongs to wsi" in message


def test_the_service_user_itself_is_never_refused(tmp_path, monkeypatch):
    tool = _load_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.os, "geteuid", lambda: 1234)
    _owners(monkeypatch, tool, db, stat_uid=1234)
    assert tool._refuse_root_on_service_db(str(db)) is None


def test_main_exits_before_loading_anything_when_refused(tmp_path, monkeypatch, capsys):
    tool = _load_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.sys, "argv", ["normalize_live_db.py", str(db)])
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, db, stat_uid=1234)
    _service_user(monkeypatch, tool)
    loaded = []
    monkeypatch.setattr(tool, "_load_orm", lambda explicit: loaded.append(explicit) or "")
    assert tool.main() == 2
    assert loaded == [], "the wsi_app package was imported by a process that is root"
    assert "refusing to run as root" in capsys.readouterr().err


def test_a_root_owned_link_to_the_service_users_database_is_refused(tmp_path, monkeypatch):
    """A link root made, in a directory root owns, pointing at the service
    user's database: root would import the code the service user can rewrite and
    replace the link, not the database (and print "Done")."""
    tool = _load_tool()
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    link = tmp_path / "wsi.db"
    link.symlink_to(target)
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    _owners(monkeypatch, tool, link, stat_uid=1234, lstat_uid=0, dir_uid=0)
    _service_user(monkeypatch, tool)
    message = tool._refuse_root_on_service_db(str(link))
    assert message and "is a link to a database that belongs to wsi" in message
