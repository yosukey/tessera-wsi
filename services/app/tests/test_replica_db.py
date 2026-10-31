"""infra/lib/replica_db.py — the database side of infra/sync-from.sh.

The helper runs with the system python3 and cannot import the app, so it
keeps its own table classification, app_meta keys and version parser. These
tests tie each of them to the app's definitions, then run the helper the way
sync-from.sh does (``python3 -I - <subcommand>`` with the file on stdin)
against databases built from the real ORM schema.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wsi_app import admin_helpers, app_meta, dzi, slide_transfer
from wsi_app.db import Base
from wsi_app.models import (
    AppMeta,
    AuthSettings,
    AuthUser,
    MetadataOption,
    NetworkSettings,
    PageView,
    Slide,
    Step,
    StepPin,
    Tour,
    User,
    ViewerSettings,
)

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "infra" / "lib" / "replica_db.py"
RESTORE_APPLY = REPO / "services" / "app" / "wsi_app" / "restore_apply.py"

_spec = importlib.util.spec_from_file_location("replica_db", HELPER)
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)


# ---------------------------------------------------------------------------
# The helper's copies of app definitions
# ---------------------------------------------------------------------------

def test_every_orm_table_is_classified_exactly_once():
    content, local = set(rdb.CONTENT_TABLES), set(rdb.LOCAL_TABLES)
    assert not content & local
    assert rdb.META_TABLE not in content | local
    classified = content | local | {rdb.META_TABLE}
    orm = set(Base.metadata.tables)
    assert orm - classified == set(), (
        "new table(s) without a sync decision: add each to CONTENT_TABLES "
        "(taken from the source) or LOCAL_TABLES (kept per server) in "
        "infra/lib/replica_db.py")
    assert classified - orm == set(), "replica_db.py names table(s) the ORM no longer has"


def test_meta_keys_match_the_app():
    assert rdb.KEY_SLIDE_ID_HWM == admin_helpers._SLIDE_ID_HWM_KEY
    assert rdb.KEY_TILE_GEOMETRY == dzi.KEY_TILE_GEOMETRY
    assert rdb.KEY_APP_VERSION == app_meta.KEY_APP_VERSION
    assert rdb.KEY_SCHEMA_VERSION == app_meta.KEY_SCHEMA_VERSION


@pytest.mark.parametrize("v", [
    "v1.2.3", "1.2.3", "v0.1", "0.1.0-rc1", "0.1.0-rc.2", "0.1.0a2", "0.1.0b1",
    "0.1.0-3-gabc1234", "0.1.0-rc1-3-gabc1234", "unknown", "abc1234", "1234abc",
    "v2.0.0-pre", "10.0.0", " v3.4.5 ",
])
def test_parse_version_matches_app_meta(v):
    assert rdb.parse_version(v) == app_meta.parse_version(v)


def test_public_id_rule_matches_the_app():
    assert rdb.PUBLIC_ID_RE.pattern == admin_helpers.PUBLIC_ID_RE.pattern


def test_attachment_link_rule_matches_the_app():
    assert rdb.ASSET_REF_RE.pattern == slide_transfer._ASSET_REF_RE.pattern


def test_secret_keys_are_wsi_env_settings():
    example = (REPO / "infra" / "wsi.env.example").read_text(encoding="utf-8")
    for key in rdb.SECRET_KEYS:
        assert f"\n{key}=" in example


# ---------------------------------------------------------------------------
# Fixtures: a source and a destination built from the ORM schema
# ---------------------------------------------------------------------------

def _make_db(path: Path, version: str = "v1.4.0", schema: int = 7) -> None:
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    with Session(eng) as db:
        db.add_all([
            AppMeta(key="app_version", value=version),
            AppMeta(key="schema_version", value=str(schema)),
        ])
        db.commit()
    eng.dispose()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src.db"
    _make_db(path)
    eng = create_engine(f"sqlite:///{path}")
    with Session(eng) as db:
        s1 = Slide(id=1, public_id="slideAAA", title="Liver", wsi_filename="1_liver.svs",
                   thumb_filename="1_t1.jpg", wsi_generation=100, is_published=True)
        s2 = Slide(id=5, public_id="slideBBB", title="Lung", wsi_filename="5_lung.ndpi",
                   thumb_filename="5_t5.jpg", wsi_generation=200)
        db.add_all([s1, s2])
        db.flush()
        t = Tour(public_id="tourAAA", slide_id=1, title="Lobules")
        db.add(t)
        db.flush()
        st = Step(tour_id=t.id, label="Central vein")
        db.add(st)
        db.flush()
        db.add(StepPin(step_id=st.id, x=1.0, y=2.0, label="pin"))
        db.add_all([
            User(username="admin", password_hash="src-hash", role="admin"),
            User(username="teacher", password_hash="t-hash", role="instructor"),
            ViewerSettings(id=1, site_title="Histology"),
            MetadataOption(key="organ", value="liver"),
            NetworkSettings(id=1, mdns_enabled=True, ga_measurement_id="G-SOURCE"),
            AuthSettings(id=1, mode="google", google_client_id="src-client"),
            AuthUser(username="student", password_hash="s-hash"),
            PageView(kind="viewer", slide_id=1, client_ip="10.0.0.1"),
            AppMeta(key="slides_id_hwm", value="9"),
            AppMeta(key="dzi_tile_geometry", value="t254o1"),
        ])
        db.commit()
    eng.dispose()
    return path


@pytest.fixture
def dest(tmp_path):
    path = tmp_path / "dest.db"
    _make_db(path)
    eng = create_engine(f"sqlite:///{path}")
    with Session(eng) as db:
        db.add_all([
            Slide(id=3, public_id="oldSlide", title="Old", wsi_filename="3_old.svs",
                  thumb_filename="3_t3.jpg", wsi_generation=50),
            User(username="backup-admin", password_hash="b-hash", role="admin"),
            ViewerSettings(id=1, site_title="Backup"),
            NetworkSettings(id=1, mdns_enabled=False, ip_filter_enabled=True),
            AuthSettings(id=1, mode="none"),
            PageView(kind="top", client_ip="10.9.9.9"),
            PageView(kind="viewer", slide_id=1, client_ip="10.9.9.8"),
            AppMeta(key="slides_id_hwm", value="3"),
            AppMeta(key="dzi_tile_geometry", value="t512o0"),
            AppMeta(key="db_initialized_at", value="dest-init"),
        ])
        db.commit()
    eng.dispose()
    return path


def run_helper(*args, stdin_bytes=None, check=True):
    """Run the helper as sync-from.sh does: the program on stdin."""
    proc = subprocess.run(
        [sys.executable, "-I", "-", *map(str, args)],
        input=HELPER.read_bytes() if stdin_bytes is None else stdin_bytes,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"helper {args} failed: {proc.stderr.decode()}")
    return proc


def export(src: Path, out: Path, scope: str = "content") -> Path:
    out.write_bytes(run_helper("export", src, "--scope", scope).stdout)
    return out


def rows(path: Path, sql: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Source side
# ---------------------------------------------------------------------------

def test_state_reports_versions_slides_and_files(source, tmp_path):
    data = tmp_path / "data"
    (data / "wsi").mkdir(parents=True)
    (data / "thumbs").mkdir()
    (data / "wsi" / "1_liver.svs").write_bytes(b"x")
    tour = data / "assets" / "tours" / "tourAAA"
    (tour / "sub").mkdir(parents=True)
    (tour / "fig.png").write_bytes(b"12345")
    (tour / ".fig.png.upload-1").write_bytes(b"tmp")
    (tour / "link.png").symlink_to(tour / "fig.png")
    state = json.loads(run_helper("state", source, data).stdout)
    # What tour-filter lets rsync take: regular, non-hidden, top level.
    mtime = int((tour / "fig.png").stat().st_mtime)
    assert state["assets"] == {"tourAAA": {"fig.png": [5, mtime]}}
    by_id = {s["id"]: s for s in state["slides"]}
    assert by_id[1]["wsi_sig"] == [1, int((data / "wsi" / "1_liver.svs").stat().st_mtime)]
    assert by_id[1]["thumb_sig"] is None
    assert state["app_version"] == "v1.4.0" and state["schema_version"] == 7
    assert state["tours"] == ["tourAAA"]
    by_id = {s["id"]: s for s in state["slides"]}
    assert by_id[1]["wsi_ok"] is True and by_id[1]["thumb_ok"] is False
    assert by_id[5]["wsi_ok"] is False and by_id[1]["gen"] == 100


def test_content_export_leaves_the_per_server_tables_behind(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    # Built next to the source database (not in a small /tmp), then removed.
    assert not list(source.parent.glob(".wsi-sync-export.*"))
    tables = {r[0] for r in rows(snap, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(rdb.CONTENT_TABLES) <= tables and "app_meta" in tables
    assert not tables & set(rdb.LOCAL_TABLES)
    assert rows(snap, "PRAGMA journal_mode") == [("delete",)]
    assert rows(snap, "SELECT COUNT(*) FROM slides") == [(2,)]
    # The source's AUTOINCREMENT counters, once each.
    assert (sorted(rows(snap, "SELECT name, seq FROM sqlite_sequence"))
            == sorted(r for r in rows(source, "SELECT name, seq FROM sqlite_sequence")
                      if r[0] in rdb.CONTENT_TABLES))


def test_all_export_is_the_whole_database(source, tmp_path):
    snap = export(source, tmp_path / "snap.db", "all")
    assert rows(snap, "SELECT client_ip FROM page_views") == [("10.0.0.1",)]
    assert rows(snap, "SELECT mode FROM auth_settings") == [("google",)]
    assert rows(snap, "PRAGMA journal_mode") == [("delete",)]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def _v(app, schema):
    return {"app_version": app, "schema_version": schema}


def test_content_sync_needs_the_same_schema():
    assert rdb.check_versions("content", _v("v1.4.0", 7), _v("v1.4.0", 7)) == []
    with pytest.raises(rdb.Refusal, match="Update this server"):
        rdb.check_versions("content", _v("v1.5.0", 8), _v("v1.4.0", 7))
    with pytest.raises(rdb.Refusal, match="same schema"):
        rdb.check_versions("content", _v("v1.3.0", 6), _v("v1.4.0", 7))


def test_content_sync_refuses_a_newer_source_release_and_warns_on_an_older_one():
    with pytest.raises(rdb.Refusal, match="newer than this server"):
        rdb.check_versions("content", _v("v1.4.1", 7), _v("v1.4.0", 7))
    warnings = rdb.check_versions("content", _v("v1.4.0", 7), _v("v1.4.1", 7))
    assert len(warnings) == 1 and "one release" in warnings[0]


def test_migration_accepts_an_older_source_like_a_restore():
    assert rdb.check_versions("all", _v("v1.3.0", 6), _v("v1.4.0", 7)) == []
    with pytest.raises(rdb.Refusal):
        rdb.check_versions("all", _v("v1.4.0", 8), _v("v1.4.0", 7))
    with pytest.raises(rdb.Refusal):
        rdb.check_versions("all", _v("v1.5.0", 7), _v("v1.4.0", 7))


def test_versions_must_be_recorded():
    with pytest.raises(rdb.Refusal, match="source"):
        rdb.check_versions("content", _v("v1.4.0", None), _v("v1.4.0", 7))
    with pytest.raises(rdb.Refusal, match="this server"):
        rdb.check_versions("content", _v("v1.4.0", 7), _v("v1.4.0", None))


def test_check_versions_cli(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps(_v("v2.0.0", 7)))
    (tmp_path / "i.json").write_text(json.dumps(_v("v1.0.0", 7)))
    proc = run_helper("check-versions", "--scope", "content",
                      tmp_path / "s.json", tmp_path / "i.json", check=False)
    assert proc.returncode == 1 and b"update.sh" in proc.stderr


# ---------------------------------------------------------------------------
# Merge (content scope)
# ---------------------------------------------------------------------------

def test_merge_takes_content_and_keeps_this_servers_settings_and_records(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    run_helper("merge", dest, snap)

    # Content and admin accounts come from the source.
    assert rows(dest, "SELECT id, public_id, wsi_generation FROM slides ORDER BY id") == [
        (1, "slideAAA", 100), (5, "slideBBB", 200)]
    assert rows(dest, "SELECT public_id FROM tours") == [("tourAAA",)]
    assert rows(dest, "SELECT label FROM step_pins") == [("pin",)]
    assert rows(dest, "SELECT username, password_hash FROM users ORDER BY username") == [
        ("admin", "src-hash"), ("teacher", "t-hash")]
    assert rows(dest, "SELECT site_title FROM viewer_settings") == [("Histology",)]
    assert rows(dest, "SELECT value FROM metadata_options") == [("liver",)]
    # Access settings, app accounts and records stay this server's.
    assert rows(dest, "SELECT mdns_enabled, ip_filter_enabled, ga_measurement_id FROM network_settings") == [
        (0, 1, "")]
    assert rows(dest, "SELECT mode FROM auth_settings") == [("none",)]
    assert rows(dest, "SELECT COUNT(*) FROM auth_users") == [(0,)]
    assert rows(dest, "SELECT client_ip FROM page_views ORDER BY id") == [("10.9.9.9",), ("10.9.9.8",)]
    # app_meta: own version stamps and unknown keys, the content's geometry,
    # and the larger slide-id watermark.
    meta = dict(rows(dest, "SELECT key, value FROM app_meta"))
    assert meta["app_version"] == "v1.4.0" and meta["db_initialized_at"] == "dest-init"
    assert meta["dzi_tile_geometry"] == "t254o1"
    assert meta["slides_id_hwm"] == "9"
    assert rows(dest, "PRAGMA foreign_key_check") == []


def test_merge_takes_the_source_counters_and_sign_out_moves_them_past_both(source, dest, tmp_path):
    """A staff token is honoured while its tv equals users.token_version for
    its sub, an app cookie while its gen >= auth_settings.session_generation
    (wsi_app.auth / app_auth). After a swap neither the source's values nor
    this server's own may stand: one would revive a token revoked here, the
    other let the account that held an id before the sync pass for the one
    holding it after. sign-out runs the app's restore sign-out for that."""
    conn = sqlite3.connect(dest)
    conn.execute("UPDATE users SET token_version = 5000")
    conn.execute("UPDATE auth_settings SET session_generation = 7")
    conn.commit()
    conn.close()
    prev = tmp_path / "dest.db.pre-sync"
    run_helper("backup-db", dest, prev)
    src_tv = dict(rows(source, "SELECT id, token_version FROM users"))
    run_helper("merge", dest, export(source, tmp_path / "snap.db"))
    assert dict(rows(dest, "SELECT id, token_version FROM users")) == src_tv
    run_helper("sign-out", dest, prev, RESTORE_APPLY)
    after = dict(rows(dest, "SELECT id, token_version FROM users"))
    for uid, tv in after.items():
        assert tv > src_tv[uid]
        if uid == 1:  # the id this server's own admin held
            assert tv > 5000
    assert rows(dest, "SELECT session_generation FROM auth_settings") == [(8,)]
    assert not Path(str(dest) + ".session-revocation-pending").exists()


def test_sign_out_hands_a_failure_to_the_apps_boot(dest, tmp_path):
    """If the sign-out cannot run, the marker restore_apply leaves for that
    case makes the app retry it at startup (and refuse to serve until then)."""
    run_helper("backup-db", dest, tmp_path / "prev.db")
    broken = tmp_path / "restore_apply.py"
    broken.write_text(RESTORE_APPLY.read_text().replace(
        "def invalidate_sessions(db_path: str, previous_db: Optional[str]) -> None:",
        "def invalidate_sessions(db_path: str, previous_db: Optional[str]) -> None:\n"
        "    raise OSError('disk on fire')\n\n\ndef _unused(db_path, previous_db):"))
    proc = run_helper("sign-out", dest, tmp_path / "prev.db", broken, check=False)
    assert proc.returncode == rdb.EXIT_DEFERRED and b"retries it when it starts" in proc.stderr
    marker = json.loads(Path(str(dest) + ".session-revocation-pending").read_text())
    assert marker["previous_db"] == str(tmp_path / "prev.db") and "disk on fire" in marker["error"]


def test_check_sign_out_refuses_while_one_is_pending(dest):
    assert run_helper("check-sign-out", dest, RESTORE_APPLY).returncode == 0
    Path(str(dest) + ".session-revocation-pending").write_text("{}")
    proc = run_helper("check-sign-out", dest, RESTORE_APPLY, check=False)
    assert proc.returncode == 1 and b"still pending" in proc.stderr


def test_bump_generations_moves_every_slide_past_any_served_value(dest):
    before = dict(rows(dest, "SELECT id, wsi_generation FROM slides"))
    run_helper("bump-generations", dest)
    after = dict(rows(dest, "SELECT id, wsi_generation FROM slides"))
    for sid, gen in after.items():
        assert gen > before[sid] and gen > 1_600_000_000_000  # epoch ms, as fresh_wsi_generation


def test_merge_watermark_never_goes_below_this_servers(source, dest, tmp_path):
    conn = sqlite3.connect(dest)
    conn.execute("UPDATE app_meta SET value = '40' WHERE key = 'slides_id_hwm'")
    conn.commit()
    conn.close()
    run_helper("merge", dest, export(source, tmp_path / "snap.db"))
    assert rows(dest, "SELECT value FROM app_meta WHERE key = 'slides_id_hwm'") == [("40",)]


def test_merge_keeps_autoincrement_counters_moving_forward(source, dest, tmp_path):
    run_helper("merge", dest, export(source, tmp_path / "snap.db"))
    src_seq = dict(rows(source, "SELECT name, seq FROM sqlite_sequence"))
    dst_seq = dict(rows(dest, "SELECT name, seq FROM sqlite_sequence"))
    for name, seq in src_seq.items():
        if name in rdb.CONTENT_TABLES:
            assert dst_seq[name] >= seq


def test_merge_matches_columns_by_name(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    # The same table with its columns in another order (an install whose
    # history added them in another sequence), and a column only the source
    # has (a newer release on the same schema number).
    conn = sqlite3.connect(dest)
    conn.executescript("""
        DROP TABLE metadata_options;
        CREATE TABLE metadata_options (value VARCHAR(255) NOT NULL, id INTEGER PRIMARY KEY,
                                       key VARCHAR(128) NOT NULL, sort_order INTEGER DEFAULT 0);
    """)
    conn.close()
    conn = sqlite3.connect(snap)
    conn.execute("ALTER TABLE viewer_settings ADD COLUMN only_on_source TEXT DEFAULT 'x'")
    conn.commit()
    conn.close()
    proc = run_helper("merge", dest, snap)
    assert b"only_on_source" in proc.stderr
    assert rows(dest, "SELECT key, value FROM metadata_options") == [("organ", "liver")]


def test_merge_refuses_broken_foreign_keys_and_changes_nothing(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    conn = sqlite3.connect(snap)
    conn.execute("DELETE FROM slides WHERE id = 1")  # the tour's slide
    conn.commit()
    conn.close()
    before = rows(dest, "SELECT id, public_id FROM slides")
    proc = run_helper("merge", dest, snap, check=False)
    assert proc.returncode == 1 and b"foreign key" in proc.stderr
    assert rows(dest, "SELECT id, public_id FROM slides") == before
    assert rows(dest, "SELECT username FROM users") == [("backup-admin",)]


def test_replace_refuses_a_damaged_snapshot_before_touching_anything(source, dest, tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    before = (Path(dest)).read_bytes()
    proc = run_helper("replace", dest, snap, check=False)
    assert proc.returncode == 1 and Path(dest).read_bytes() == before


def test_replace_copies_the_whole_database(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db", "all")
    run_helper("replace", dest, snap)
    assert rows(dest, "SELECT mode FROM auth_settings") == [("google",)]
    assert rows(dest, "SELECT client_ip FROM page_views") == [("10.0.0.1",)]


def test_backup_db_makes_a_checked_standalone_copy(dest, tmp_path):
    out = tmp_path / "dest.db.pre-sync"
    run_helper("backup-db", dest, out)
    assert rows(out, "PRAGMA journal_mode") == [("delete",)]
    assert rows(out, "SELECT public_id FROM slides") == [("oldSlide",)]
    assert not (tmp_path / "dest.db.pre-sync.tmp").exists()


def test_summary(source, dest, tmp_path):
    out = run_helper("summary", export(source, tmp_path / "snap.db"), dest,
                     "--scope", "content").stdout.decode()
    assert "1 here now -> 2 after the sync (1 published); 2 new, 1 removed" in out
    # This server's own admin (id 1) becomes the source's, and it has records.
    assert "0 slide id(s) and 1 user id(s) here come to mean" in out


def test_summary_says_nothing_of_ids_when_they_keep_their_meaning(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    same = tmp_path / "same.db"
    run_helper("backup-db", source, same)
    out = run_helper("summary", snap, same, "--scope", "content").stdout.decode()
    assert "come to mean" not in out


# ---------------------------------------------------------------------------
# File lists and the transfer check
# ---------------------------------------------------------------------------

def test_files_from_and_tour_filter(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    listed = run_helper("files-from", snap).stdout.split(b"\0")
    assert listed == [b"wsi/1_liver.svs", b"wsi/5_lung.ndpi",
                      b"thumbs/1_t1.jpg", b"thumbs/5_t5.jpg", b""]
    rules = run_helper("tour-filter", snap).stdout.decode().splitlines()
    assert rules == ["- .*", "- /*/*/", "+ /tourAAA/", "+ /tourAAA/*", "- *"]


@pytest.mark.parametrize("bad", ["../etc/passwd", ".hidden", "a/b", "x\ny"])
def test_unsafe_names_are_refused(source, tmp_path, bad):
    snap = export(source, tmp_path / "snap.db")
    conn = sqlite3.connect(snap)
    conn.execute("UPDATE slides SET wsi_filename = ? WHERE id = 1", (bad,))
    conn.commit()
    conn.close()
    proc = run_helper("files-from", snap, check=False)
    assert proc.returncode == 1 and b"unsafe" in proc.stderr


def test_unsafe_tour_ids_are_refused(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    conn = sqlite3.connect(snap)
    conn.execute("UPDATE tours SET public_id = '../x'")
    conn.commit()
    conn.close()
    assert run_helper("tour-filter", snap, check=False).returncode == 1


def _state_for(snap: Path, assets=None, mirror=None, **ok) -> dict:
    """The source's state as `state` reports it; the source's files are taken
    to be the ones in ``mirror`` (so a match unless a test changes them)."""
    def sig(sub, name):
        return rdb._signature(str(mirror / sub / name)) if mirror else None
    conn = sqlite3.connect(snap)
    slides = [{"id": r[0], "wsi": r[1], "thumb": r[2], "gen": r[3],
               "wsi_ok": ok.get("wsi_ok", True), "thumb_ok": ok.get("thumb_ok", True),
               "wsi_sig": sig("wsi", r[1]), "thumb_sig": sig("thumbs", r[2])}
              for r in conn.execute("SELECT id, wsi_filename, thumb_filename, wsi_generation FROM slides")]
    tours = [r[0] for r in conn.execute("SELECT public_id FROM tours")]
    conn.close()
    return {"slides": slides, "tours": tours, "assets": assets or {}}


def _check(pre, post, snap, mirror):
    return rdb.check_transfer(pre, post, str(snap), str(mirror))


def _fill_mirror(mirror: Path, snap: Path) -> None:
    for sub in ("wsi", "thumbs"):
        (mirror / sub).mkdir(parents=True, exist_ok=True)
    for name in run_helper("files-from", snap).stdout.split(b"\0"):
        if name:
            (mirror / name.decode()).write_bytes(b"data-" + name)


def test_check_passes_when_the_mirror_matches(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    _fill_mirror(tmp_path / "m", snap)
    m = tmp_path / "m"
    assert _check(_state_for(snap, mirror=m), _state_for(snap, mirror=m), snap, m) == (0, [])


def test_check_fails_for_a_stale_copy_under_the_same_name(source, tmp_path):
    """rsync exit 23 is left to the check: a WSI replaced on the source under
    its name whose transfer failed leaves the old copy, present but not the
    source's bytes."""
    snap = export(source, tmp_path / "snap.db")
    m = tmp_path / "m"
    _fill_mirror(m, snap)
    state = _state_for(snap, mirror=m)
    state["slides"][0]["wsi_sig"] = [999, 1]  # the source's new file
    code, msgs = _check(state, state, snap, m)
    assert code == 1 and "wsi/1_liver.svs" in msgs[0]


def test_check_asks_for_another_round_when_the_source_moved(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    _fill_mirror(tmp_path / "m", snap)
    state = _state_for(snap, mirror=tmp_path / "m")
    state["slides"][0]["gen"] += 1  # replaced during the transfer
    code, msgs = _check(_state_for(snap, mirror=tmp_path / "m"), state, snap, tmp_path / "m")
    assert code == rdb.EXIT_CHANGED and "1 slide(s) (ids 1)" in msgs[0]


def test_check_takes_a_file_rewritten_in_place_as_a_change(source, tmp_path):
    """A thumbnail rebuilt on the source keeps its name and generation."""
    snap = export(source, tmp_path / "snap.db")
    m = tmp_path / "m"
    _fill_mirror(m, snap)
    pre, post = _state_for(snap, mirror=m), _state_for(snap, mirror=m)
    post["slides"][0]["thumb_sig"] = [99, 1]
    code, msgs = _check(pre, post, snap, m)
    assert code == rdb.EXIT_CHANGED and "the files of 1 slide(s) (ids 1)" in msgs[0]


def test_check_names_tour_and_attachment_changes(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    _fill_mirror(tmp_path / "m", snap)
    m = tmp_path / "m"
    state = _state_for(snap, mirror=m)
    state["tours"].append("newTour")
    code, msgs = _check(_state_for(snap, mirror=m), state, snap, m)
    assert code == rdb.EXIT_CHANGED and "1 tour(s) added or deleted (newTour)" in msgs[0]
    code, msgs = _check(_state_for(snap, mirror=m),
                        _state_for(snap, {"tourAAA": {"new.png": [3, 1]}}, mirror=m), snap, m)
    assert code == rdb.EXIT_CHANGED and "attachments of 1 tour(s) (tourAAA)" in msgs[0]


def test_check_fails_for_an_attachment_that_did_not_arrive(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    m = tmp_path / "m"
    _fill_mirror(m, snap)
    d = m / "assets" / "tours" / "tourAAA"
    d.mkdir(parents=True)
    (d / "fig.png").write_bytes(b"png")
    assets = {"tourAAA": {"fig.png": rdb._signature(str(d / "fig.png")), "gone.png": [1, 1000]}}
    state = _state_for(snap, assets, mirror=m)
    code, msgs = _check(state, state, snap, m)
    assert code == 1 and "assets/tours/tourAAA/gone.png" in msgs[0]
    (d / "gone.png").write_bytes(b"truncated?")  # wrong size
    assert _check(state, state, snap, m)[0] == 1
    (d / "gone.png").write_bytes(b"x")
    os.utime(d / "gone.png", (1000, 1000))
    assert _check(state, state, snap, m) == (0, [])


def test_check_fails_for_a_file_that_did_not_arrive(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    _fill_mirror(tmp_path / "m", snap)
    (tmp_path / "m" / "wsi" / "5_lung.ndpi").unlink()
    pre = _state_for(snap, mirror=tmp_path / "m")
    pre["slides"][1]["wsi_sig"] = [3, 3]  # the source has it
    code, msgs = _check(pre, pre, snap, tmp_path / "m")
    assert code == 1 and "wsi/5_lung.ndpi" in msgs[0]
    # ...but mirrors a slide that is broken on the source too.
    broken = _state_for(snap, mirror=tmp_path / "m", wsi_ok=False)
    code, msgs = _check(broken, broken, snap, tmp_path / "m")
    assert code == 0
    # 5_lung.ndpi is missing on both sides; 1_liver.svs only on the source,
    # so this server keeps (and says it keeps) the copy it has.
    assert any("wsi/5_lung.ndpi is missing on the source too" in m for m in msgs)
    assert any("wsi/1_liver.svs is missing on the source; this server keeps" in m for m in msgs)


# ---------------------------------------------------------------------------
# Putting files in place
# ---------------------------------------------------------------------------

def test_install_files_links_replaces_and_sweeps(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    mirror, data = tmp_path / "mirror", tmp_path / "data"
    _fill_mirror(mirror, snap)
    (mirror / "assets" / "tours" / "tourAAA").mkdir(parents=True)
    (mirror / "assets" / "tours" / "tourAAA" / "fig.png").write_bytes(b"png")
    for sub in ("wsi", "thumbs"):
        (data / sub).mkdir(parents=True)
    (data / "wsi" / "1_liver.svs").write_bytes(b"stale bytes")   # replaced
    (data / "wsi" / "3_old.svs").write_bytes(b"old")             # the previous DB's
    (data / "wsi" / "9_orphan.svs").write_bytes(b"orphan")       # nobody's
    (data / "wsi" / ".rsync-partial").mkdir()                    # left alone
    (data / "wsi" / ".x.upload-1").write_bytes(b"tmp")           # left alone
    (data / "wsi" / ".1_liver.svs.sync-0123456789ab").write_bytes(b"crash")  # the tool's own: removed
    (data / "assets" / "tours" / "goneTour").mkdir(parents=True)
    (data / "assets" / "tours" / "goneTour" / "a.png").write_bytes(b"a")
    (data / "assets" / "tours" / "tourAAA").mkdir()
    (data / "assets" / "tours" / "tourAAA" / "removed.png").write_bytes(b"r")
    (data / "assets" / "tours" / "tourAAA" / "linked.png").write_bytes(b"l")
    # The database being replaced links to one attachment of a tour both have.
    eng = create_engine(f"sqlite:///{dest}")
    with Session(eng) as db:
        db.add(Tour(public_id="tourAAA", slide_id=3, title="Mine",
                    description="see ![x](/assets/tours/tourAAA/linked.png)"))
        db.commit()
    eng.dispose()

    stats = rdb.install_files(str(snap), str(mirror), str(data), keep_db=str(dest))

    wsi = data / "wsi"
    assert os.path.samefile(wsi / "1_liver.svs", mirror / "wsi" / "1_liver.svs")
    assert os.path.samefile(wsi / "5_lung.ndpi", mirror / "wsi" / "5_lung.ndpi")
    assert (wsi / "3_old.svs").exists(), "a file the replaced DB refers to is kept for --rollback"
    assert not (wsi / "9_orphan.svs").exists()
    assert not (wsi / ".1_liver.svs.sync-0123456789ab").exists()
    assert (wsi / ".x.upload-1").exists() and (wsi / ".rsync-partial").is_dir()
    tours = data / "assets" / "tours"
    assert os.path.samefile(tours / "tourAAA" / "fig.png", mirror / "assets" / "tours" / "tourAAA" / "fig.png")
    assert not (tours / "tourAAA" / "removed.png").exists()
    assert (tours / "tourAAA" / "linked.png").exists(), \
        "an attachment the replaced database links to is kept for --rollback"
    assert not (tours / "goneTour").exists()
    assert stats["placed"] == 5

    # A second run with nothing changed touches nothing.
    again = rdb.install_files(str(snap), str(mirror), str(data), keep_db=str(dest))
    assert again["placed"] == 0 and again["removed"] == 0
    # ...and one whose replaced database no longer links to it drops it.
    rdb.install_files(str(snap), str(mirror), str(data), keep_db=str(snap))
    assert not (tours / "tourAAA" / "linked.png").exists()


def test_install_files_keeps_what_any_keep_db_refers_to(source, dest, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    mirror, data = tmp_path / "mirror", tmp_path / "data"
    _fill_mirror(mirror, snap)
    (data / "wsi").mkdir(parents=True)
    (data / "wsi" / "3_old.svs").write_bytes(b"old")
    rollback_point = tmp_path / "pre-sync.db"
    run_helper("backup-db", dest, rollback_point)
    # The replaced database no longer names 3_old.svs; the rollback point does.
    run_helper("install-files", snap, mirror, data, "--keep-db", snap,
               "--keep-db", rollback_point)
    assert (data / "wsi" / "3_old.svs").exists()
    run_helper("install-files", snap, mirror, data, "--keep-db", snap)
    assert not (data / "wsi" / "3_old.svs").exists()


def test_a_copy_with_the_same_size_and_mtime_is_not_copied_again(tmp_path):
    """rsync's own quick check, on any device: two mounts of one filesystem
    share st_dev but copy (EXDEV), and must not re-copy every run."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"12345")
    b.write_bytes(b"54321")
    os.utime(a, (1000, 1000))
    os.utime(b, (1000, 1000))
    assert rdb._same_file(str(a), str(b))
    os.utime(b, (2000, 2000))
    assert not rdb._same_file(str(a), str(b))


def test_install_bytes_counts_what_a_copy_would_write(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    mirror, data = tmp_path / "mirror", tmp_path / "data"
    _fill_mirror(mirror, snap)
    (mirror / "assets" / "tours" / "tourAAA").mkdir(parents=True)
    (mirror / "assets" / "tours" / "tourAAA" / "fig.png").write_bytes(b"12345")
    total = sum(p.stat().st_size for p in mirror.rglob("*") if p.is_file())
    assert int(run_helper("install-bytes", snap, mirror, data).stdout) == total
    rdb.install_files(str(snap), str(mirror), str(data))
    assert int(run_helper("install-bytes", snap, mirror, data).stdout) == 0


def test_install_files_replaces_a_symlinked_tour_dir_instead_of_following_it(source, tmp_path):
    snap = export(source, tmp_path / "snap.db")
    mirror, data = tmp_path / "mirror", tmp_path / "data"
    _fill_mirror(mirror, snap)
    (mirror / "assets" / "tours" / "tourAAA").mkdir(parents=True)
    (mirror / "assets" / "tours" / "tourAAA" / "fig.png").write_bytes(b"png")
    (data / "assets" / "tours").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data / "assets" / "tours" / "tourAAA").symlink_to(elsewhere)
    rdb.install_files(str(snap), str(mirror), str(data))
    assert not (data / "assets" / "tours" / "tourAAA").is_symlink()
    assert list(elsewhere.iterdir()) == []


def test_can_link(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_bytes(b"x")
    assert run_helper("can-link", tmp_path / "f", tmp_path / "d").stdout.strip() == b"1"
    assert list((tmp_path / "d").iterdir()) == []


def test_place_copies_only_across_filesystems(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.write_bytes(b"bytes")
    (tmp_path / "d").mkdir()

    def refuse(errno_):
        def link(*a, **k):
            raise OSError(errno_, os.strerror(errno_))
        return link
    monkeypatch.setattr(rdb.os, "link", refuse(rdb.errno.EXDEV))
    rdb._place(str(src), str(tmp_path / "d"), "a")
    assert (tmp_path / "d" / "a").read_bytes() == b"bytes"
    # Any other refusal (fs.protected_hardlinks) is an error, not a copy.
    monkeypatch.setattr(rdb.os, "link", refuse(rdb.errno.EPERM))
    with pytest.raises(PermissionError):
        rdb._place(str(src), str(tmp_path / "d"), "b")
    assert sorted(p.name for p in (tmp_path / "d").iterdir()) == ["a"]


def test_sync_from_names_the_apps_pending_sign_out_marker():
    from wsi_app import restore_apply
    script = (REPO / "infra" / "sync-from.sh").read_text(encoding="utf-8")
    assert f"$DB_PATH{restore_apply.SESSION_REVOCATION_SUFFIX}" in script


def test_prune_mirror_drops_unlisted_files(tmp_path):
    mirror = tmp_path / "m"
    for sub in ("wsi", "thumbs"):
        (mirror / sub).mkdir(parents=True)
    (mirror / "wsi" / "keep.svs").write_bytes(b"k")
    (mirror / "wsi" / "gone.svs").write_bytes(b"g")
    (mirror / "thumbs" / "gone.jpg").write_bytes(b"g")
    (tmp_path / "list").write_bytes(b"wsi/keep.svs\0")
    run_helper("prune-mirror", tmp_path / "list", mirror)
    assert sorted(p.name for p in mirror.rglob("*") if p.is_file()) == ["keep.svs"]


# ---------------------------------------------------------------------------
# wsi.env secrets (migration)
# ---------------------------------------------------------------------------

def test_env_secrets_reads_the_live_assignment(tmp_path):
    env = tmp_path / "wsi.env"
    env.write_text("ANALYTICS_HASH_SALT=old\n#ANALYTICS_HASH_SALT=commented\n"
                   "ANALYTICS_HASH_SALT='quoted-salt'\r\nJWT_SECRET=not-carried\n")
    out = run_helper("env-secrets", env).stdout.decode().splitlines()
    assert out == ["ANALYTICS_HASH_SALT=quoted-salt"]


def test_set_env_writes_only_the_carried_key_from_stdin(tmp_path):
    env = tmp_path / "wsi.env"
    env.write_text("JWT_SECRET=mine\nANALYTICS_HASH_SALT=mine\nDATA_DIR=/var/lib/wsi\n")
    env.chmod(0o600)
    proc = subprocess.run(
        [sys.executable, "-I", str(HELPER), "set-env", str(env)],
        input=b"JWT_SECRET=theirs\nANALYTICS_HASH_SALT=salt\nDATA_DIR=/evil\nX=1\n",
        capture_output=True, check=True)
    assert proc.stdout.decode().strip() == "ANALYTICS_HASH_SALT"
    assert env.read_text() == "JWT_SECRET=mine\nANALYTICS_HASH_SALT=salt\nDATA_DIR=/var/lib/wsi\n"
    assert (env.stat().st_mode & 0o777) == 0o600


def test_set_env_follows_a_symlinked_env_file(tmp_path):
    """A wsi.env kept elsewhere and linked into /etc/wsi stays a link: the
    rename lands on the file it points to, not over the link."""
    real = tmp_path / "managed" / "wsi.env"
    real.parent.mkdir()
    real.write_text("ANALYTICS_HASH_SALT=mine\n")
    real.chmod(0o640)
    link = tmp_path / "wsi.env"
    link.symlink_to(real)
    subprocess.run(
        [sys.executable, "-I", str(HELPER), "set-env", str(link)],
        input=b"ANALYTICS_HASH_SALT=salt\n", capture_output=True, check=True)
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert real.read_text() == "ANALYTICS_HASH_SALT=salt\n"
    assert (real.stat().st_mode & 0o777) == 0o640
    assert sorted(p.name for p in real.parent.iterdir()) == ["wsi.env"]


def test_set_env_refuses_values_wsi_env_cannot_hold(tmp_path):
    env = tmp_path / "wsi.env"
    env.write_text("ANALYTICS_HASH_SALT=mine\n")
    proc = subprocess.run(
        [sys.executable, "-I", str(HELPER), "set-env", str(env)],
        input=b"ANALYTICS_HASH_SALT=has space\n", capture_output=True)
    assert proc.returncode == 1 and b"copy it by hand" in proc.stderr
    assert env.read_text() == "ANALYTICS_HASH_SALT=mine\n"


def test_the_helpers_public_id_check_refuses_a_trailing_newline():
    """A hostile source's tour id "abc\\n" would otherwise become a malformed
    line in the rsync filter list built for it."""
    assert rdb.PUBLIC_ID_RE.match("abc")
    assert rdb.PUBLIC_ID_RE.match("abc\n") is None
    assert rdb.PUBLIC_ID_RE.match("a" * 32 + "\n") is None
