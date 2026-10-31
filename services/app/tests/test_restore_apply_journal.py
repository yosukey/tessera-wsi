"""Crash safety of the restore-apply swap.

wsi_app/restore_apply.py applies a staged restore as a sequence of renames:
park the live wsi.db / assets / wsi / thumbs under ``.pre-restore-*``, then
promote the staged ones. A rollback list kept only in process memory would
let a stop that bypassed ``except Exception`` (SIGKILL, power loss, a
reboot) after the new DB was promoted but before wsi/ leave a half-applied
tree; the next start would then see the staged wsi.db missing, record a
``validate_stage`` failure, delete the request, and start uvicorn on it.

The module is the one implementation of the swap, with two entry points, and
the crash matrix runs against both (the ``runner`` fixture):

* ``cli`` — ``python -m wsi_app.restore_apply`` as start-wsi-app.sh runs it at
  boot, killed with ``os._exit`` (no ``except``/``finally``/atexit — the
  SIGKILL shape) right after every rename / journal write, then run again the
  way the next boot would.
* ``inprocess`` — ``backup.inprocess_apply`` (the dev swap), "killed" at the
  same points by an exception no handler may act on, then
  ``backup.recover_interrupted_apply`` (the dev lifespan's reconciliation).

Whatever the kill point, the next start must leave the tree wholly pre-restore
(and say the apply failed) or wholly restored (and say it succeeded) — never
mixed — and must consume the journal so the app may start. A reconciliation
that cannot finish must keep the journal, and the shell must then refuse to
exec uvicorn — even when the helper cannot run at all.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from wsi_app import restore_apply

REPO = Path(__file__).resolve().parents[3]
APP_DIR = REPO / "services" / "app"
HELPER = APP_DIR / "wsi_app" / "restore_apply.py"
START_SCRIPT = REPO / "infra" / "systemd" / "start-wsi-app.sh"
# The release the boot under test runs (its VERSION stamp; none in a checkout).
_RUNNING_VERSION = restore_apply._running_app_version() or "test"
STAGE_ID = "stage-0123456789ab"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

# ``python -m wsi_app.restore_apply`` (runpy's -m machinery) with os.rename /
# os.replace wrapped: every call is counted and, when the count reaches
# KILL_AT, the process dies on the spot right AFTER that rename landed.
# FAIL_DST instead raises a plain OSError before the first rename onto that
# destination (the ordinary, caught failure). A run that is not killed
# reports the count and every wsi_app module it imported.
_DRIVER = r'''
import errno, os, runpy, sys
kill_at = int(os.environ.get("KILL_AT", "0") or 0)
fail_dst = os.environ.get("FAIL_DST") or None
count = [0]
real_rename, real_replace = os.rename, os.replace

def _tick():
    count[0] += 1
    if count[0] == kill_at:
        os._exit(137)

def rename(src, dst, *a, **kw):
    global fail_dst
    if fail_dst is not None and os.fspath(dst) == fail_dst:
        fail_dst = None  # once: the rollback renames back onto the same path
        raise OSError(errno.EIO, "injected rename failure", dst)
    real_rename(src, dst, *a, **kw)
    _tick()

def replace(src, dst, *a, **kw):
    real_replace(src, dst, *a, **kw)
    _tick()

os.rename, os.replace = rename, replace
try:
    runpy.run_module("wsi_app.restore_apply", run_name="__main__", alter_sys=True)
finally:
    loaded = ",".join(sorted(m for m in sys.modules if m.startswith("wsi_app")))
    sys.stderr.write(f"\nRENAME_COUNT={count[0]}\nWSI_APP_MODULES={loaded}\n")
'''


def _make_db(path: Path, width: int, wsi_filename: str) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE slides (id INTEGER PRIMARY KEY, wsi_filename TEXT, width INTEGER)")
        con.execute("INSERT INTO slides VALUES (1, ?, ?)", (wsi_filename, width))
        con.commit()
    finally:
        con.close()


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


class Env:
    """A scratch install: live data dir, one staged restore, IPC files."""

    def __init__(self, root: Path, scope: str = "full") -> None:
        self.root = root
        self.scope = scope
        self.data = root / "data"
        self.stage = root / "backups" / ".staging" / STAGE_ID
        self.run = root / "run"
        self.req = self.run / "restore.request"
        self.res = self.run / "restore.result"
        self.journal = self.data / ".restore-apply.journal"
        root.mkdir(parents=True)
        self.driver = root / "driver.py"
        self.driver.write_text(_DRIVER, encoding="utf-8")
        for d in (self.data, self.stage, self.run):
            d.mkdir(parents=True)

        # Live (pre-restore) state. The -shm sibling is opaque bytes: the DB is
        # in rollback-journal mode, so SQLite ignores it, but the swap must park
        # and restore it like the real WAL siblings.
        _make_db(self.data / "wsi.db", 100, "1_old.svs")
        _write(self.data / "wsi.db-shm", "old-shm")
        _write(self.data / "assets" / "tours" / "a" / "x.png", "old-asset")
        _write(self.data / "wsi" / "1_old.svs", "old-wsi")
        _write(self.data / "thumbs" / "1.jpg", "old-thumb")

        # Staged (restore) state.
        _make_db(self.stage / "wsi.db", 200, "1_new.svs")
        _write(self.stage / "manifest.json", "{}")
        _write(self.stage / "assets" / "tours" / "b" / "y.png", "new-asset")
        _write(self.stage / "wsi" / "1_new.svs", "new-wsi")
        _write(self.stage / "thumbs" / "1.jpg", "new-thumb")
        # What stage_restore writes last, once the archive passed its checks.
        _write(self.stage / ".staged-ok", "{}")

        self.req.write_text(json.dumps({
            "stage_id": STAGE_ID,
            "stage_dir": str(self.stage),
            "scope": scope,
            "data_dir": str(self.data),
            "db_path": str(self.data / "wsi.db"),
            # The version this start runs: the boot refuses a request made
            # under another (see test_a_request_from_another_release_...).
            "app_version": _RUNNING_VERSION,
        }), encoding="utf-8")

    def result(self) -> dict:
        return json.loads(self.res.read_text(encoding="utf-8"))

    def state(self) -> str:
        """"old", "new" (for this scope), or a description of the inconsistency."""
        db = self.data / "wsi.db"
        if not db.exists():
            return "mixed: no live wsi.db"
        con = sqlite3.connect(str(db))
        try:
            width = con.execute("SELECT width FROM slides WHERE id = 1").fetchone()[0]
        finally:
            con.close()

        def listing(name: str) -> list[str]:
            base = self.data / name
            if not base.is_dir():
                return []
            return sorted(
                f"{p.relative_to(base)}={p.read_text()}" for p in base.rglob("*") if p.is_file()
            )

        shm = (self.data / "wsi.db-shm").read_text() if (self.data / "wsi.db-shm").exists() else None
        got = (width, listing("assets"), listing("wsi"), listing("thumbs"), shm)
        old = (100, ["tours/a/x.png=old-asset"], ["1_old.svs=old-wsi"], ["1.jpg=old-thumb"], "old-shm")
        # metadata (and reset) promote only the DB + assets: wsi/ and thumbs/
        # end up empty, for the operator to re-upload.
        full = self.scope == "full"
        new = (200, ["tours/b/y.png=new-asset"],
               ["1_new.svs=new-wsi"] if full else [], ["1.jpg=new-thumb"] if full else [], None)
        if got == old:
            return "old"
        if got == new:
            return "new"
        return f"mixed: {got!r}"


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------

@dataclass
class Run:
    killed: bool
    renames: int = 0                # rename / replace calls made (when not killed)
    exit_code: Optional[int] = None  # cli
    outcome: Optional[str] = None    # inprocess: "completed" / "rolled_back"
    error: str = ""                  # inprocess: the exception it raised
    stderr: str = ""


class Cli:
    """``python -m wsi_app.restore_apply`` from the app dir, as start-wsi-app.sh
    runs it. One invocation is one boot: reconcile any journal, then apply the
    pending request — so ``apply`` and ``boot`` are the same call."""

    name = "cli"

    def apply(self, env: Env, *, kill_at: int = 0, fail_dst: str = "") -> Run:
        proc = subprocess.run(
            [sys.executable, str(env.driver), str(env.req), str(env.res), str(env.journal)],
            cwd=APP_DIR,
            env=dict(os.environ, PYTHONPATH=str(APP_DIR), KILL_AT=str(kill_at), FAIL_DST=fail_dst),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 137:
            return Run(killed=True, exit_code=137, stderr=proc.stderr)
        m = re.search(r"RENAME_COUNT=(\d+)\nWSI_APP_MODULES=(.*)\n", proc.stderr)
        assert m, proc.stderr
        # The boot-time run must not drag in the app (settings, DB engine).
        assert set(filter(None, m.group(2).split(","))) <= {"wsi_app", "wsi_app.restore_apply"}, m.group(2)
        return Run(killed=False, renames=int(m.group(1)), exit_code=proc.returncode, stderr=proc.stderr)

    def boot(self, env: Env, *, kill_at: int = 0) -> Run:
        return self.apply(env, kill_at=kill_at)

    def says_restored(self, env: Env, run: Run) -> bool:
        assert run.exit_code == 0, run.stderr
        res = env.result()
        assert res["stage_id"] == STAGE_ID
        assert not env.req.exists(), "request left behind: it would be re-applied"
        if res["ok"]:
            assert res["wsi_uploads_pending"] == (0 if env.scope == "full" else 1)
        return res["ok"]

    def failure(self, env: Env, run: Run, stage: str) -> str:
        # Fail closed (the journal kept) exits 3; any other failure is
        # ordinary and must let uvicorn start (exit 0).
        assert run.exit_code == (3 if stage == "recovery" else 0), run.stderr
        res = env.result()
        assert res["ok"] is False and res["stage"] == stage, res
        return res["error"]


class _Killed(BaseException):
    """Stands in for SIGKILL in-process: escapes every ``except Exception``."""


@contextmanager
def _dies_after(kill_at: int, fail_dst: str = ""):
    """The in-process twin of _DRIVER: counts os.rename / os.replace and
    raises _Killed right after the kill_at-th lands. From then on every
    rename / replace / remove raises it too — a killed process runs no
    rollback, so the apply's own ``except BaseException`` undo must not get to
    touch the tree either."""
    count, dead, fail = [0], [False], [fail_dst or None]
    real = {name: getattr(os, name) for name in ("rename", "replace", "remove", "unlink")}

    def wrap(name):
        def inner(*a, **kw):
            if dead[0]:
                raise _Killed()
            if name == "rename" and fail[0] is not None and os.fspath(a[1]) == fail[0]:
                fail[0] = None  # once: the rollback renames back onto the same path
                raise OSError(errno.EIO, "injected rename failure", a[1])
            real[name](*a, **kw)
            if name in ("rename", "replace"):
                count[0] += 1
                if count[0] == kill_at:
                    dead[0] = True
                    raise _Killed()
        return inner

    with pytest.MonkeyPatch.context() as mp:
        for name in real:
            mp.setattr(os, name, wrap(name))
        yield count


class _DummyEngine:
    def dispose(self) -> None:
        pass


class InProcess:
    """``backup.inprocess_apply`` (dev swap); the next start is
    ``backup.recover_interrupted_apply`` (the dev lifespan's reconciliation)."""

    name = "inprocess"

    def __init__(self, use_settings) -> None:
        self.use = use_settings

    def apply(self, env: Env, *, kill_at: int = 0, fail_dst: str = "") -> Run:
        from wsi_app import backup

        self.use(env)
        with _dies_after(kill_at, fail_dst) as count:
            try:
                backup.inprocess_apply(STAGE_ID, env.scope, _DummyEngine())
            except _Killed:
                return Run(killed=True)
            except Exception as exc:
                return Run(killed=False, renames=count[0], error=str(exc))
        return Run(killed=False, renames=count[0], outcome="completed")

    def boot(self, env: Env, *, kill_at: int = 0) -> Run:
        from wsi_app import backup

        self.use(env)
        with _dies_after(kill_at) as count:
            try:
                outcome = backup.recover_interrupted_apply()
            except _Killed:
                return Run(killed=True)
            except RuntimeError as exc:
                return Run(killed=False, renames=count[0], error=str(exc))
        return Run(killed=False, renames=count[0], outcome=outcome)

    def says_restored(self, env: Env, run: Run) -> bool:
        assert not run.error, run.error
        return {"completed": True, "rolled_back": False}[run.outcome]

    def failure(self, env: Env, run: Run, stage: str) -> str:
        assert run.error, run
        return run.error


@pytest.fixture
def dev_settings():
    """Point the app settings at a scratch Env for one test."""
    from wsi_app.config import settings

    names = ("data_dir", "backup_dir", "database_url", "restore_journal_path",
             "restore_result_path", "backup_dev_inprocess_apply")
    saved = {n: getattr(settings, n) for n in names}

    def use(env: Env, *, dev: bool = True) -> None:
        object.__setattr__(settings, "data_dir", str(env.data))
        object.__setattr__(settings, "backup_dir", str(env.root / "backups"))
        object.__setattr__(settings, "database_url", f"sqlite:///{env.data / 'wsi.db'}")
        object.__setattr__(settings, "restore_journal_path", "")
        object.__setattr__(settings, "restore_result_path", str(env.res))
        object.__setattr__(settings, "backup_dev_inprocess_apply", dev)

    yield use
    for n, v in saved.items():
        object.__setattr__(settings, n, v)


@pytest.fixture(params=["cli", "inprocess"])
def runner(request, dev_settings):
    return Cli() if request.param == "cli" else InProcess(dev_settings)


def test_a_request_without_a_scope_is_rejected_and_nothing_moves(tmp_path):
    """A restore request that names no scope is malformed: it is rejected
    (and removed) rather than applied under a guessed scope, and the live
    data is left as it was."""
    env = Env(tmp_path / "e")
    req = json.loads(env.req.read_text(encoding="utf-8"))
    del req["scope"]
    env.req.write_text(json.dumps(req), encoding="utf-8")
    run = Cli().apply(env)
    assert run.exit_code == 0, run.stderr
    res = env.result()
    assert res["ok"] is False and res["stage"] == "read_request", res
    assert "unknown apply scope" in res["error"]
    assert not env.req.exists()
    assert env.state() == "old"


def _assert_reconciled(env: Env, runner, run: Run) -> str:
    """The post-boot invariants: consistent tree, a report that matches it, no
    leftovers."""
    assert not run.killed
    state = env.state()
    assert state in ("old", "new"), state
    assert runner.says_restored(env, run) is (state == "new"), state
    assert not env.journal.exists(), "journal left behind: the app would refuse to start"
    assert not env.stage.exists()
    if state == "old":
        # A rolled-back apply's .pre-restore-* would be empty: left behind, it
        # reads as the newest rollback copy, the one an undo would reach for.
        assert not list(env.data.glob(".pre-restore-*"))
    return state


def _apply_count(runner, tmp_path: Path, scope: str = "full") -> int:
    """Rename/replace calls in an uninterrupted apply — the kill points."""
    env = Env(tmp_path / "count", scope)
    run = runner.apply(env)
    assert _assert_reconciled(env, runner, run) == "new"
    return run.renames


def _kill_point_after_db_promote(env: Env) -> int:
    """The rename count at which the staged wsi.db has just been promoted
    (assets/wsi/thumbs not yet), read off the plan both entry points journal."""
    from wsi_app import restore_apply

    steps = restore_apply.plan_journal(
        stage_id=STAGE_ID, scope=env.scope, stage_dir=str(env.stage),
        data_dir=str(env.data), db_path=str(env.data / "wsi.db"), app_version=None,
    )["steps"]
    db_promote = next(
        i for i, s in enumerate(steps)
        if s["op"] == "promote" and s["dst"].endswith("wsi.db")
    )
    # Calls: 1 initial journal write, then (cursor write, rename) per step.
    return 1 + 2 * (db_promote + 1)


def _make_unrecoverable(env: Env) -> None:
    """Delete the parked live DB, as an operator tidying ``.pre-restore-*`` by
    hand might: the rollback can no longer put it back."""
    journal = json.loads(env.journal.read_text())
    os.remove(Path(journal["rollback_root"]) / "wsi.db")


# ---------------------------------------------------------------------------
# The crash matrix, against both entry points
# ---------------------------------------------------------------------------

def test_full_apply_succeeds_and_leaves_rollback_tree(tmp_path, runner):
    env = Env(tmp_path / "e")
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    (rollback,) = env.data.glob(".pre-restore-*")
    assert (rollback / "wsi" / "1_old.svs").read_text() == "old-wsi"
    assert (rollback / "wsi.db-shm").read_text() == "old-shm"


@pytest.mark.parametrize("scope", ["full", "metadata"])
def test_every_kill_point_reconciles_to_a_consistent_tree(tmp_path, runner, scope):
    total = _apply_count(runner, tmp_path, scope)
    outcomes = {}
    for k in range(1, total + 1):
        env = Env(tmp_path / f"k{k}", scope)
        assert runner.apply(env, kill_at=k).killed, k
        outcomes[k] = _assert_reconciled(env, runner, runner.boot(env))
    # Once the commit marker is durable the apply is finished, not undone:
    # the last kill points must land on the restored tree.
    assert outcomes[total] == "new"
    assert outcomes[1] == "old"
    assert set(outcomes.values()) == {"old", "new"}


def test_kill_during_reconciliation_is_itself_recoverable(tmp_path, runner):
    """A second crash while rolling back must not wedge or corrupt the tree:
    the cursor is lowered durably after every undo, so a third start resumes."""
    total = _apply_count(runner, tmp_path)
    for k in (total // 2, total // 2 + 1):
        probe = Env(tmp_path / f"probe{k}")
        assert runner.apply(probe, kill_at=k).killed
        rec_total = runner.boot(probe).renames
        assert rec_total > 0
        for j in range(1, rec_total + 1):
            env = Env(tmp_path / f"k{k}j{j}")
            assert runner.apply(env, kill_at=k).killed
            assert runner.boot(env, kill_at=j).killed, (k, j)
            _assert_reconciled(env, runner, runner.boot(env))


def test_ordinary_swap_failure_rolls_back_and_drops_journal(tmp_path, runner):
    env = Env(tmp_path / "e")
    run = runner.apply(env, fail_dst=str(env.data / "wsi"))
    assert not run.killed
    assert env.state() == "old"
    assert not env.journal.exists() and not env.stage.exists()
    assert "injected rename failure" in runner.failure(env, run, stage="swap")
    assert not list(env.data.glob(".pre-restore-*"))
    if runner.name == "cli":
        assert "rollback_root" not in env.result()


def test_unrecoverable_journal_is_kept_and_fails_closed(tmp_path, runner):
    """If the parked tree was tampered with, the reconciliation must not
    guess: it keeps the journal (so nothing starts on the tree) and says why."""
    env = Env(tmp_path / "e")
    assert runner.apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    _make_unrecoverable(env)
    run = runner.boot(env)
    assert env.journal.exists()
    assert "neither exists" in runner.failure(env, run, stage="recovery")


def _other_order_journal(env: Env, done: int, moved: int, phase: str = "applying") -> int:
    """A journal with the same fields and JOURNAL_VERSION but its own move
    order (every park, then every promote), as another release's start script
    might write it — with the first ``moved`` of its steps carried out."""
    root = env.data / ".pre-restore-20260101T000000Z"
    d, s = env.data, env.stage
    moves = [("park", d / "wsi.db", root / "wsi.db"), ("park", d / "wsi.db-shm", root / "wsi.db-shm")]
    moves += [("park", d / n, root / n) for n in ("assets", "wsi", "thumbs")]
    moves += [("promote", s / n, d / n) for n in ("wsi.db", "assets", "wsi", "thumbs")]
    steps = [{"op": op, "src": str(src), "dst": str(dst)} for op, src, dst in moves]
    root.mkdir()
    for step in steps[:moved]:
        os.rename(step["src"], step["dst"])
    env.journal.write_text(json.dumps({
        "version": 1, "phase": phase, "stage_id": STAGE_ID, "scope": "full",
        "app_version": "test", "stage_dir": str(s), "data_dir": str(d),
        "db_path": str(d / "wsi.db"), "rollback_root": str(root),
        "started_at": "2026-01-01T00:00:00Z", "steps": steps, "done": done,
    }), encoding="utf-8")
    return len(steps)


def test_journal_with_another_move_order_still_reconciles(tmp_path, runner):
    """Reconciliation replays the journal's own steps, so a journal another
    release left behind (a different move order) still reconciles."""
    n = _other_order_journal(Env(tmp_path / "count"), 0, 0)
    for done in range(n + 1):
        # The cursor's step may or may not have run.
        for moved in {max(done - 1, 0), done}:
            env = Env(tmp_path / f"d{done}m{moved}")
            _other_order_journal(env, done, moved)
            assert _assert_reconciled(env, runner, runner.boot(env)) == "old", (done, moved)
    env = Env(tmp_path / "committed")
    _other_order_journal(env, n, n, phase="committed")
    assert _assert_reconciled(env, runner, runner.boot(env)) == "new"


def test_cli_kill_right_after_db_promote_rolls_back_instead_of_validate_stage(tmp_path):
    """Die once the staged wsi.db has been
    renamed onto the live path, before assets/wsi/thumbs follow. The request
    survives a service restart with its staged wsi.db gone — which the next
    boot must roll back rather than report as validate_stage."""
    cli = Cli()
    env = Env(tmp_path / "e")
    assert cli.apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    assert env.state().startswith("mixed"), env.state()
    assert env.journal.exists() and env.req.exists()
    assert not (env.stage / "wsi.db").exists()

    assert _assert_reconciled(env, cli, cli.boot(env)) == "old"
    res = env.result()
    assert res["stage"] == "interrupted", res
    assert "validate_stage" not in json.dumps(res)


def _age_request(env: Env, seconds: float, *, stamp: bool = True) -> None:
    """Make env's request ``seconds`` old: by its requested_at, or (``stamp``
    False, as a request without one is aged) by the file's mtime."""
    import datetime as _dt
    import time as _time

    if stamp:
        req = json.loads(env.req.read_text(encoding="utf-8"))
        when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
        req["requested_at"] = when.replace(tzinfo=None).isoformat() + "Z"
        env.req.write_text(json.dumps(req), encoding="utf-8")
    else:
        t = _time.time() - seconds
        os.utime(env.req, (t, t))


@pytest.mark.parametrize("stamp", [True, False])
def test_a_request_no_restart_picked_up_in_time_is_not_applied(tmp_path, stamp):
    """Writing the trigger file counts as success even when nothing acts on it
    (wsi-restart.path disabled, its service start-limited), so a request
    could wait for days — with backups, restores and imports refused — until
    an unrelated restart (set-workers.sh, update.sh, by hand) swapped in that
    old stage and parked everything done since under .pre-restore-*. Past
    REQUEST_MAX_AGE_SECONDS it is reported and dropped, the live data left as
    it is; within it, applied normally."""
    from wsi_app import restore_apply

    cli = Cli()
    limit = restore_apply.REQUEST_MAX_AGE_SECONDS
    env = Env(tmp_path / "late")
    _age_request(env, limit + 60, stamp=stamp)
    run = cli.boot(env)
    error = cli.failure(env, run, stage="expired")
    assert "was not applied" in error and f"{limit // 60} minutes" in error, error
    assert env.result()["stage_id"] == STAGE_ID
    assert env.state() == "old"
    assert not env.req.exists() and not env.journal.exists()
    assert not list(env.data.glob(".pre-restore-*"))
    # The stage is left for a re-apply from the dashboard (the 24 h sweep
    # reclaims it otherwise).
    assert (env.stage / "wsi.db").exists()

    env = Env(tmp_path / "in-time")
    _age_request(env, limit - 60, stamp=stamp)
    assert _assert_reconciled(env, cli, cli.boot(env)) == "new"


def _iso_in(seconds: float) -> str:
    import datetime as _dt

    when = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
    return when.replace(tzinfo=None).isoformat() + "Z"


def test_a_request_is_aged_by_the_boot_clock_across_wall_clock_steps():
    """The wall clock can be stepped (NTP, a VM resumed, by hand) between
    writing a request and the restart; the boot clock cannot. A request
    stamped with this boot's clock is aged by it alone: one the wall clock
    puts 20 minutes in the future (the clock went back) is applied, and one
    the boot clock says has waited too long expires whatever the wall clock
    says. Only a request without a usable boot stamp (another boot, an
    a writer that omits it, a garbled field) falls back to the wall clock, and there a
    stamp too far off either way is refused: its real age is unknown, and
    refusing costs a re-apply where applying a stale one parks every change
    made since."""
    from wsi_app import restore_apply

    stamp = restore_apply.request_clock_stamp()
    if not stamp:
        pytest.skip("no boot id / CLOCK_BOOTTIME on this platform")
    assert set(stamp) == {"requested_boot_id", "requested_boot_seconds"}
    limit = restore_apply.REQUEST_MAX_AGE_SECONDS

    def expired(req: dict) -> bool:
        return restore_apply.request_expired(
            restore_apply.request_age_seconds(req, "/nonexistent/restore.request")
        )

    assert not expired({"requested_at": _iso_in(limit + 600), **stamp})
    assert not expired({"requested_at": _iso_in(-(limit + 600)), **stamp})
    waited = dict(stamp, requested_boot_seconds=stamp["requested_boot_seconds"] - limit - 60)
    assert expired({"requested_at": _iso_in(0), **waited})

    for unusable in (
        dict(stamp, requested_boot_id="0" * 8 + "-0000-0000-0000-" + "0" * 12),
        dict(stamp, requested_boot_seconds="soon"),
        dict(stamp, requested_boot_seconds=float("nan")),
        {"requested_boot_id": stamp["requested_boot_id"]},
        {},
    ):
        assert not expired({"requested_at": _iso_in(0), **unusable}), unusable
        assert expired({"requested_at": _iso_in(limit + 60), **unusable}), unusable
        assert expired({"requested_at": _iso_in(-(limit + 60)), **unusable}), unusable


def test_a_restore_survives_the_clock_going_back_before_the_restart(tmp_path):
    """End to end through the boot-time helper: a request written with the
    boot stamp request_apply adds, whose wall-clock stamp the clock has since
    gone 20 minutes back past, is applied."""
    from wsi_app import restore_apply

    stamp = restore_apply.request_clock_stamp()
    if not stamp:
        pytest.skip("no boot id / CLOCK_BOOTTIME on this platform")
    cli = Cli()
    env = Env(tmp_path / "stepped")
    req = json.loads(env.req.read_text(encoding="utf-8"))
    req.update(stamp, requested_at=_iso_in(restore_apply.REQUEST_MAX_AGE_SECONDS + 600))
    env.req.write_text(json.dumps(req), encoding="utf-8")
    assert _assert_reconciled(env, cli, cli.boot(env)) == "new"


def test_helper_imports_only_the_standard_library():
    """start-wsi-app.sh runs the helper before the app exists: it must never
    initialise settings / the DB engine, and must work when the venv's
    packages are broken. ``-m wsi_app.restore_apply`` executes the package's
    ``__init__`` first, so that is held to the same rule."""
    for path in (HELPER, HELPER.parent / "__init__.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{where}: relative import"
                names = [node.module]
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                assert name not in ("__import__", "import_module"), f"{where}: dynamic import"
                continue
            else:
                continue
            for name in names:
                assert name.split(".")[0] in sys.stdlib_module_names, f"{where}: imports {name!r}"


# ---------------------------------------------------------------------------
# The shell gate: uvicorn must never be exec'd while a journal remains.
# ---------------------------------------------------------------------------

def _shell_copy(env: Env, app_dir: Path = APP_DIR) -> Path:
    """start-wsi-app.sh with the venv python pointed at this interpreter, the
    app dir at ``app_dir``, and the final ``exec uvicorn`` replaced by a
    marker."""
    text = START_SCRIPT.read_text(encoding="utf-8")
    for old, new in (
        ("PYTHON_BIN=/opt/wsi/venv/bin/python3", f"PYTHON_BIN={sys.executable}"),
        ("APP_DIR=/opt/wsi/services/app", f"APP_DIR={app_dir}"),
        ("exec /opt/wsi/venv/bin/uvicorn", "echo UVICORN-WOULD-START; exit 0; :"),
    ):
        assert old in text, old
        text = text.replace(old, new, 1)
    sh_dir = env.root / "sh"
    sh_dir.mkdir(exist_ok=True)
    shutil.copy(START_SCRIPT.parent / "wsi-calc.sh", sh_dir / "wsi-calc.sh")
    script = sh_dir / "start-wsi-app.sh"
    script.write_text(text, encoding="utf-8")
    return script


def _run_shell(env: Env, script: Path) -> subprocess.CompletedProcess:
    # Started from (and with PYTHONPATH on) a directory holding a decoy
    # wsi_app package: the script must point the helper at APP_DIR itself,
    # not trust the inherited cwd / environment.
    cwd = env.root / "cwd"
    (cwd / "wsi_app").mkdir(parents=True, exist_ok=True)
    (cwd / "wsi_app" / "__init__.py").write_text("", encoding="utf-8")
    (cwd / "wsi_app" / "restore_apply.py").write_text(
        'raise SystemExit("DECOY wsi_app imported from the inherited cwd")\n', encoding="utf-8",
    )
    run_env = dict(
        os.environ,
        DATA_DIR=str(env.data),
        RESTORE_REQUEST_PATH=str(env.req),
        RESTORE_RESULT_PATH=str(env.res),
        RESTART_TRIGGER_PATH=str(env.run / "restart.trigger"),
        PYTHONPATH=str(cwd),
    )
    run_env.pop("RESTORE_JOURNAL_PATH", None)
    proc = subprocess.run(
        ["bash", str(script)], cwd=cwd, env=run_env,
        capture_output=True, text=True, timeout=60,
    )
    assert "DECOY" not in proc.stderr, proc.stderr
    return proc


@needs_bash
def test_shell_refuses_to_start_while_journal_remains(tmp_path):
    env = Env(tmp_path / "e")
    assert Cli().apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    _make_unrecoverable(env)
    proc = _run_shell(env, _shell_copy(env))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" not in proc.stdout
    assert "RESTORE APPLY NOT RECONCILED" in proc.stderr
    assert "refusing to start uvicorn" in proc.stderr
    assert env.journal.exists()


@needs_bash
def test_shell_reconciles_an_interrupted_apply_without_a_request(tmp_path):
    """After a reboot /run is empty: the journal alone must trigger the
    reconciliation (default journal path derived from DATA_DIR), and the app
    starts only once the tree is whole again."""
    env = Env(tmp_path / "e")
    assert Cli().apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    env.req.unlink()  # tmpfs wiped by the reboot
    proc = _run_shell(env, _shell_copy(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" in proc.stdout
    assert env.state() == "old"
    assert not env.journal.exists()
    assert env.result()["stage"] == "interrupted"


@needs_bash
def test_shell_applies_a_request_through_the_module(tmp_path):
    env = Env(tmp_path / "e")
    proc = _run_shell(env, _shell_copy(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" in proc.stdout
    assert env.state() == "new"
    assert env.result()["ok"] is True
    assert not env.req.exists() and not env.journal.exists()


@needs_bash
@pytest.mark.parametrize("broken", ["module missing", "app dir missing"])
def test_shell_fails_closed_when_the_helper_cannot_run(tmp_path, broken):
    """A broken deploy where the venv python cannot run the module at all:
    the gate still holds on a journal; a bare request is dropped with the live
    tree untouched; a boot with neither never runs the helper."""
    app_dir = tmp_path / "app"
    if broken == "module missing":
        (app_dir / "wsi_app").mkdir(parents=True)
        (app_dir / "wsi_app" / "__init__.py").write_text("", encoding="utf-8")

    env = Env(tmp_path / "journal")
    assert Cli().apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    proc = _run_shell(env, _shell_copy(env, app_dir))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" not in proc.stdout
    assert "restore-apply helper" in proc.stderr and "refusing to start uvicorn" in proc.stderr
    assert env.journal.exists() and env.state().startswith("mixed")

    env = Env(tmp_path / "request")
    proc = _run_shell(env, _shell_copy(env, app_dir))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" in proc.stdout
    assert env.state() == "old"
    assert not env.req.exists() and not env.journal.exists()

    env = Env(tmp_path / "idle")
    env.req.unlink()
    proc = _run_shell(env, _shell_copy(env, app_dir))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UVICORN-WOULD-START" in proc.stdout
    assert "restore-apply helper" not in proc.stderr


# ---------------------------------------------------------------------------
# The app side: refusing to stack applies, and the lifespan gate.
# ---------------------------------------------------------------------------

def test_inprocess_apply_refuses_while_a_journal_remains(tmp_path, dev_settings):
    from wsi_app import backup as _backup

    env = Env(tmp_path / "e")
    dev_settings(env)
    env.journal.write_text("{}")
    with pytest.raises(RuntimeError, match="interrupted restore apply"):
        _backup.inprocess_apply(STAGE_ID, "full", _DummyEngine())
    assert env.state() == "old"


def test_lifespan_gate_refuses_outside_dev_and_reconciles_in_dev(tmp_path, dev_settings):
    from wsi_app import main

    env = Env(tmp_path / "e")
    dev_settings(env, dev=False)
    main._reconcile_interrupted_restore_apply()  # no journal: no-op

    # New DB promoted, staged assets/wsi/thumbs not yet in.
    assert InProcess(dev_settings).apply(env, kill_at=_kill_point_after_db_promote(env)).killed
    assert env.state().startswith("mixed")

    # Production (no dev flag): the start script owns reconciliation; the app
    # (and wsi-sync, which runs the same app) must not open the mixed tree.
    dev_settings(env, dev=False)
    with pytest.raises(RuntimeError, match="interrupted restore apply"):
        main._reconcile_interrupted_restore_apply()
    assert env.journal.exists() and env.state().startswith("mixed")

    dev_settings(env, dev=True)
    main._reconcile_interrupted_restore_apply()
    assert env.state() == "old"
    assert not env.journal.exists()


def test_start_script_backup_and_helper_agree(tmp_path, dev_settings):
    from wsi_app import backup as _backup
    from wsi_app import restore_apply

    env = Env(tmp_path / "e")
    dev_settings(env)
    text = START_SCRIPT.read_text(encoding="utf-8")
    assert "RESTORE_JOURNAL=${RESTORE_JOURNAL_PATH:-${DATA_DIR:-/var/lib/wsi}/.restore-apply.journal}" in text
    assert _backup.restore_journal_path() == str(env.data / restore_apply.JOURNAL_NAME)
    assert restore_apply.JOURNAL_NAME == ".restore-apply.journal"
    assert restore_apply.APPLY_SCOPES == _backup.VALID_APPLY_SCOPES


def test_a_move_whose_rename_failed_is_not_undone(tmp_path):
    """The staged DB vanished between validation and the swap: its promote
    fails with the live DB already parked. That rename moved nothing, so the
    rollback must just un-park the live DB — not read the step's "neither
    path exists" as an unreconcilable state and keep the journal (which
    blocks every later start)."""
    from wsi_app import restore_apply as ra

    data, stage = tmp_path / "data", tmp_path / "stage"
    (data / "wsi").mkdir(parents=True)
    (stage / "wsi").mkdir(parents=True)
    (data / "wsi.db").write_text("live")
    jp = str(data / ra.JOURNAL_NAME)
    journal = ra.plan_journal(
        stage_id="s", scope="full", stage_dir=str(stage), data_dir=str(data),
        db_path=str(data / "wsi.db"), app_version="t",
    )
    ra.write_json_durable(jp, journal)
    with pytest.raises(OSError) as ei:
        ra.apply_journal(journal, jp)
    assert not isinstance(ei.value, ra.NotReconciled)
    assert (data / "wsi.db").read_text() == "live"
    assert journal["done"] == 0


def test_stage_on_another_mount_is_refused_before_the_restart(tmp_path, monkeypatch):
    """Two ReadWritePaths= bind mounts share st_dev, yet a rename (or link)
    between them fails with EXDEV — so the up-front check has to try one."""
    from wsi_app import backup as _backup
    from wsi_app.config import settings

    data, stage = tmp_path / "data", tmp_path / "stage"
    data.mkdir()
    stage.mkdir()
    saved = settings.data_dir
    object.__setattr__(settings, "data_dir", str(data))
    try:
        _backup._assert_stage_on_data_volume(str(stage))  # same mount: fine
        assert os.listdir(data) == [] and os.listdir(stage) == []

        real_link = os.link

        def cross_mount(src, dst, *a, **kw):
            if os.path.dirname(src) == str(stage) and os.path.dirname(dst) == str(data):
                raise OSError(errno.EXDEV, "Invalid cross-device link", src)
            return real_link(src, dst, *a, **kw)

        monkeypatch.setattr(_backup.os, "link", cross_mount)
        with pytest.raises(RuntimeError, match="same mount"):
            _backup._assert_stage_on_data_volume(str(stage))
        assert os.listdir(data) == [] and os.listdir(stage) == []
    finally:
        object.__setattr__(settings, "data_dir", saved)



# ---------------------------------------------------------------------------
# A committed swap signs every session out
# ---------------------------------------------------------------------------

def _add_auth(db: Path, token_versions: dict, generation: Optional[int]) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, token_version INTEGER NOT NULL)")
        con.executemany("INSERT INTO users VALUES (?, ?)", token_versions.items())
        con.execute("CREATE TABLE auth_settings (id INTEGER PRIMARY KEY, session_generation INTEGER NOT NULL)")
        if generation is not None:
            con.execute("INSERT INTO auth_settings VALUES (1, ?)", (generation,))
        con.commit()
    finally:
        con.close()


def _auth_state(db: Path) -> tuple[dict, Optional[int]]:
    con = sqlite3.connect(str(db))
    try:
        tvs = dict(con.execute("SELECT id, token_version FROM users").fetchall())
        row = con.execute("SELECT session_generation FROM auth_settings").fetchone()
    finally:
        con.close()
    return tvs, (row[0] if row else None)


def test_committed_swap_moves_every_session_counter_past_both_sides(tmp_path, runner):
    """A restore brings back the archive's token_version / session generation
    — and with them every staff token and app cookie revoked since the backup
    (user 1 below). Each counter ends up past both the restored value and the
    value the same id held before the swap."""
    env = Env(tmp_path / "e")
    # Live: user 1 revoked twice since the backup; user 2 not in the archive.
    _add_auth(env.data / "wsi.db", {1: 52, 2: 7}, generation=9)
    # Archive: user 1 at its old value; user 3 not live any more.
    _add_auth(env.stage / "wsi.db", {1: 50, 3: 4}, generation=5)
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    assert _auth_state(env.data / "wsi.db") == ({1: 53, 3: 5}, 10)
    # The parked pre-restore database keeps its values.
    (rollback,) = env.data.glob(".pre-restore-*")
    assert _auth_state(rollback / "wsi.db") == ({1: 52, 2: 7}, 9)


def test_interrupted_but_committed_swap_still_signs_sessions_out(tmp_path, runner):
    from wsi_app import restore_apply

    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    _add_auth(env.stage / "wsi.db", {1: 50}, generation=5)
    steps = restore_apply.plan_journal(
        stage_id=STAGE_ID, scope=env.scope, stage_dir=str(env.stage),
        data_dir=str(env.data), db_path=str(env.data / "wsi.db"), app_version=None,
    )["steps"]
    # Killed right after the commit marker: the initial journal write, a
    # cursor write and a rename per step, then the commit write.
    assert runner.apply(env, kill_at=1 + 2 * len(steps) + 1).killed
    assert _assert_reconciled(env, runner, runner.boot(env)) == "new"
    assert _auth_state(env.data / "wsi.db") == ({1: 53}, 10)


def _session_floor(data: Path) -> Path:
    from wsi_app.restore_apply import SESSION_FLOOR_SUFFIX

    return data / ("wsi.db" + SESSION_FLOOR_SUFFIX)


def _auth_settings_columns(db: Path) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(r[1:5]) for r in con.execute("PRAGMA table_info(auth_settings)")]
    finally:
        con.close()


def test_a_database_without_the_session_generation_column_leaves_a_floor(tmp_path, runner):
    """A swapped-in auth_settings without the session_generation column (a
    hand-made database): the boot would add the column at its default 0,
    accepting every cookie revoked before the restore. The swap does not
    reshape the table; it leaves the generation beside the database as a
    floor for the boot to apply once the column exists."""
    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    con = sqlite3.connect(str(env.stage / "wsi.db"))
    try:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, token_version INTEGER NOT NULL)")
        con.execute("INSERT INTO users VALUES (1, 50)")
        con.execute("CREATE TABLE auth_settings (id INTEGER PRIMARY KEY, mode VARCHAR(16) NOT NULL)")
        con.execute("INSERT INTO auth_settings VALUES (1, 'ldap')")
        con.commit()
    finally:
        con.close()
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    con = sqlite3.connect(str(env.data / "wsi.db"))
    try:
        assert dict(con.execute("SELECT id, token_version FROM users").fetchall()) == {1: 53}
    finally:
        con.close()
    assert "session_generation" not in [c[0] for c in _auth_settings_columns(env.data / "wsi.db")]
    assert json.loads(_session_floor(env.data).read_text()) == {"session_generation": 10}


def test_a_database_without_an_auth_settings_row_leaves_a_floor_for_the_boot(tmp_path, runner):
    """No auth_settings row (the archived install never saved its
    authentication settings) means nowhere to record the generation: the app
    would create the row at 0. The value is left beside the database for the
    boot to raise that row to."""
    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    _add_auth(env.stage / "wsi.db", {1: 50}, generation=None)
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    assert _auth_state(env.data / "wsi.db") == ({1: 53}, None)
    floor = _session_floor(env.data)
    assert json.loads(floor.read_text()) == {"session_generation": 10}
    # Both report the swap in the result file (the dev apply as the boot does).
    res = env.result()
    assert res["ok"] is True and res["stage_id"] == STAGE_ID, res
    assert "session_revocation_error" not in res


def test_the_boot_records_a_session_generation_floor(tmp_path):
    """End to end through the real lifespan: the floor a restore left beside
    a database without an auth_settings row becomes that row's generation,
    and is removed."""
    db = tmp_path / "wsi.db"
    floor = _session_floor(tmp_path)
    script = (
        "from fastapi.testclient import TestClient\n"
        "from wsi_app.main import app\n"
        "with TestClient(app): pass\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": f"sqlite:///{db}",
        "DATA_DIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "RESTORE_REQUEST_PATH": str(tmp_path / "run" / "restore.request"),
        "JWT_SECRET": "x" * 40,
        "ADMIN_BOOTSTRAP_PASSWORD": "Sup3r-Secret-Passw0rd!",
        "WSI_SETTINGS_REFRESH_SECONDS": "0",
    }

    def boot() -> None:
        subprocess.run([sys.executable, "-c", script], env=env, cwd=APP_DIR,
                       check=True, capture_output=True, text=True, timeout=120)

    boot()
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM auth_settings")
        con.commit()
    finally:
        con.close()
    floor.write_text(json.dumps({"session_generation": 7}), encoding="utf-8")
    boot()
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT id, session_generation, mode FROM auth_settings").fetchall() == [(1, 7, "none")]
    finally:
        con.close()
    assert not floor.exists()


def test_a_failed_sign_out_is_reported_without_undoing_the_swap(tmp_path):
    cli = Cli()
    env = Env(tmp_path / "e")
    con = sqlite3.connect(str(env.stage / "wsi.db"))
    try:  # a users relation that cannot be updated
        con.execute("CREATE VIEW users AS SELECT 1 AS id, 0 AS token_version")
        con.commit()
    finally:
        con.close()
    assert _assert_reconciled(env, cli, cli.apply(env)) == "new"
    res = env.result()
    assert res["ok"] is True
    assert "users" in res["session_revocation_error"]
    # ...and not forgotten: the app's boot retries it from this marker.
    (rollback,) = env.data.glob(".pre-restore-*")
    marker = json.loads(_revocation_marker(env.data).read_text())
    assert marker["previous_db"] == str(rollback / "wsi.db")
    assert "users" in marker["error"]


def test_databases_without_auth_tables_are_left_alone(tmp_path, runner):
    env = Env(tmp_path / "e")  # neither side has users / auth_settings
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    # No generation on either side, so no cookies to revoke either.
    assert not _session_floor(env.data).exists()
    if runner.name == "cli":
        assert "session_revocation_error" not in env.result()


# ---------------------------------------------------------------------------
# A database on another mount, an intruder beside the
# parked database, a sign-out that fails open, a half-staged stage.
# ---------------------------------------------------------------------------

@contextmanager
def _settings(**values):
    from wsi_app.config import settings

    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield settings
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


@pytest.mark.parametrize("pair", ["stage-db", "db-data"])
def test_a_database_on_another_mount_is_refused_before_the_restart(tmp_path, monkeypatch, pair):
    """DATABASE_URL may put wsi.db outside DATA_DIR. The swap renames it into
    DATA_DIR/.pre-restore-* and the staged DB onto its path, so the up-front
    check must probe the database's directory against the stage and DATA_DIR
    too — not only stage <-> DATA_DIR, or every apply fails EXDEV at boot."""
    from wsi_app import backup as _backup

    data, stage, dbdir = tmp_path / "data", tmp_path / "stage", tmp_path / "dbvol"
    for d in (data, stage, dbdir):
        d.mkdir()
    with _settings(data_dir=str(data), database_url=f"sqlite:///{dbdir / 'wsi.db'}"):
        _backup._assert_stage_on_data_volume(str(stage))  # one mount: fine
        assert [os.listdir(d) for d in (data, stage, dbdir)] == [[], [], []]

        far = {"stage-db": {str(stage), str(dbdir)}, "db-data": {str(dbdir), str(data)}}[pair]
        real_link = os.link

        def cross_mount(src, dst, *a, **kw):
            if {os.path.dirname(src), os.path.dirname(dst)} == far:
                raise OSError(errno.EXDEV, "Invalid cross-device link", src)
            return real_link(src, dst, *a, **kw)

        monkeypatch.setattr(_backup.os, "link", cross_mount)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            _backup._assert_stage_on_data_volume(str(stage))
        assert [os.listdir(d) for d in (data, stage, dbdir)] == [[], [], []]


def test_every_apply_path_refuses_a_database_on_another_mount(tmp_path, monkeypatch, dev_settings):
    """request_apply (restore and reset in production) and inprocess_apply
    (both in dev) run the same check, before anything is written or moved."""
    from wsi_app import backup as _backup

    env = Env(tmp_path / "e")
    dev_settings(env)
    dbdir = tmp_path / "dbvol"
    dbdir.mkdir()
    real_link = os.link

    def cross_mount(src, dst, *a, **kw):
        if str(dbdir) in (os.path.dirname(src), os.path.dirname(dst)):
            raise OSError(errno.EXDEV, "Invalid cross-device link", src)
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(_backup.os, "link", cross_mount)
    req = tmp_path / "run" / "restore.request"
    with _settings(database_url=f"sqlite:///{dbdir / 'wsi.db'}", restore_request_path=str(req)):
        for scope in ("full", "reset"):
            with pytest.raises(RuntimeError, match="DATABASE_URL"):
                _backup.request_apply(STAGE_ID, scope, "someone")
            assert not req.exists()
            with pytest.raises(RuntimeError, match="DATABASE_URL"):
                _backup.inprocess_apply(STAGE_ID, scope, _DummyEngine())
    assert env.state() == "old"
    assert env.stage.is_dir() and not env.journal.exists()
    assert not list(env.data.glob(".pre-restore-*"))


def _plan_and_write(env: Env):
    from wsi_app import restore_apply as ra

    journal = ra.plan_journal(
        stage_id=STAGE_ID, scope=env.scope, stage_dir=str(env.stage),
        data_dir=str(env.data), db_path=str(env.data / "wsi.db"), app_version="t",
    )
    ra.write_json_durable(str(env.journal), journal)
    return journal


def _intrude_after_move(monkeypatch, dst: Path, intruder: Path, data: str):
    """Create ``intruder`` right after the journaled move onto ``dst`` — what
    a connection opened while the live DB path is missing would leave."""
    from wsi_app import restore_apply as ra

    real = ra.move_durable

    def move(src, to):
        real(src, to)
        if to == str(dst):
            intruder.write_text(data)

    monkeypatch.setattr(ra, "move_durable", move)


def test_a_wal_appearing_beside_the_parked_db_rolls_the_swap_back(tmp_path, monkeypatch):
    """A -wal created after the live DB and its siblings were parked would sit
    beside the promoted DB and be replayed onto it. The promote refuses it as
    a rename that moved nothing, so the swap rolls back cleanly and the live
    data is intact."""
    from wsi_app import restore_apply as ra

    env = Env(tmp_path / "e")
    live_db = (env.data / "wsi.db").read_bytes()
    staged_db = (env.stage / "wsi.db").read_bytes()
    journal = _plan_and_write(env)
    # wsi.db-shm is the last park before the DB promote in this Env.
    _intrude_after_move(monkeypatch, Path(journal["rollback_root"]) / "wsi.db-shm",
                        env.data / "wsi.db-wal", "stray-wal")
    with pytest.raises(OSError) as ei:
        ra.apply_journal(journal, str(env.journal))
    assert not isinstance(ei.value, ra.NotReconciled)
    assert ei.value.errno == errno.EEXIST
    assert journal["done"] == 0 and journal["phase"] == "rolling_back"
    # Byte for byte, before anything opens the DB: SQLite adopts a -wal it
    # finds beside a database on open (that is the hazard), so Env.state()
    # comes last, after the intruder is cleared away.
    assert (env.data / "wsi.db").read_bytes() == live_db
    assert (env.data / "wsi.db-shm").read_text() == "old-shm"
    assert (env.stage / "wsi.db").read_bytes() == staged_db  # never promoted
    assert (env.data / "wsi.db-wal").read_text() == "stray-wal"  # left for the operator
    assert not list(env.data.glob(".pre-restore-*"))
    (env.data / "wsi.db-wal").unlink()
    assert env.state() == "old"


def test_an_intruding_db_is_never_silently_replaced(tmp_path, monkeypatch):
    """An empty wsi.db created at the live path after the park must not be
    replaced by the promote's rename without a trace. The promote refuses,
    and the rollback, finding the intruder where the parked DB must go back,
    fails closed (journal kept) rather than overwrite either one."""
    from wsi_app import restore_apply as ra

    env = Env(tmp_path / "e")
    journal = _plan_and_write(env)
    rollback = Path(journal["rollback_root"])
    _intrude_after_move(monkeypatch, rollback / "wsi.db", env.data / "wsi.db", "")
    with pytest.raises(ra.NotReconciled):
        ra.apply_journal(journal, str(env.journal))
    assert env.journal.exists()
    assert (env.data / "wsi.db").read_text() == ""          # the intruder
    assert (env.stage / "wsi.db").is_file()                 # never promoted
    con = sqlite3.connect(str(rollback / "wsi.db"))         # the live data, parked intact
    try:
        assert con.execute("SELECT width FROM slides").fetchone()[0] == 100
    finally:
        con.close()


def _revocation_marker(data: Path) -> Path:
    from wsi_app.restore_apply import SESSION_REVOCATION_SUFFIX

    return data / ("wsi.db" + SESSION_REVOCATION_SUFFIX)


def test_a_failed_sign_out_leaves_a_marker_the_app_retries(tmp_path, monkeypatch, dev_settings):
    """A sign-out that fails after the swap committed must not be reported and
    forgotten: the app would then serve the archive's counters, under which
    every token revoked since the backup works again. It leaves a marker, and
    the retry (run by the boot before serving) moves the counters past both
    sides as the swap would have."""
    from wsi_app import backup as _backup
    from wsi_app import restore_apply as ra

    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    _add_auth(env.stage / "wsi.db", {1: 50}, generation=5)
    real = ra.invalidate_sessions
    calls = [0]

    def flaky(db_path, previous_db):
        calls[0] += 1
        if calls[0] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(db_path, previous_db)

    monkeypatch.setattr(ra, "invalidate_sessions", flaky)
    run = InProcess(dev_settings).apply(env)
    assert _assert_reconciled(env, InProcess(dev_settings), run) == "new"
    assert env.result()["session_revocation_error"] == "database is locked"
    assert _auth_state(env.data / "wsi.db") == ({1: 50}, 5)  # failed open, for now
    marker = json.loads(_revocation_marker(env.data).read_text())
    (rollback,) = env.data.glob(".pre-restore-*")
    assert marker["previous_db"] == str(rollback / "wsi.db")
    assert marker["stage_id"] == STAGE_ID

    _backup.retry_pending_session_revocation()
    assert _auth_state(env.data / "wsi.db") == ({1: 53}, 10)
    assert not _revocation_marker(env.data).exists()
    _backup.retry_pending_session_revocation()  # no marker: no-op
    assert _auth_state(env.data / "wsi.db") == ({1: 53}, 10)


def test_a_sign_out_retry_that_cannot_succeed_refuses_to_serve(tmp_path, dev_settings):
    from wsi_app import backup as _backup

    env = Env(tmp_path / "e")
    dev_settings(env)
    _add_auth(env.data / "wsi.db", {1: 50}, generation=5)
    marker = _revocation_marker(env.data)

    # The marker itself unreadable.
    marker.write_text("{not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        _backup.retry_pending_session_revocation()
    assert marker.exists()

    # The sign-out fails again (a users relation that cannot be updated).
    con = sqlite3.connect(str(env.data / "wsi.db"))
    try:
        con.execute("DROP TABLE users")
        con.execute("CREATE VIEW users AS SELECT 1 AS id, 0 AS token_version")
        con.commit()
    finally:
        con.close()
    marker.write_text(json.dumps({"previous_db": None, "error": "x"}))
    with pytest.raises(RuntimeError, match="refusing to serve"):
        _backup.retry_pending_session_revocation()
    assert marker.exists()


def test_the_boot_retries_a_failed_sign_out_and_refuses_to_serve_without_it(tmp_path):
    """End to end through the real lifespan: the marker a failed sign-out
    left is acted on before anything is served — counters moved past the
    parked database's, marker removed — and a boot whose retry cannot
    succeed fails instead of serving."""
    db = tmp_path / "wsi.db"
    previous = tmp_path / "parked.db"
    marker = _revocation_marker(tmp_path)
    script = (
        "from fastapi.testclient import TestClient\n"
        "from wsi_app.main import app\n"
        "with TestClient(app): pass\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": f"sqlite:///{db}",
        "DATA_DIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "RESTORE_REQUEST_PATH": str(tmp_path / "run" / "restore.request"),
        "JWT_SECRET": "x" * 40,
        "ADMIN_BOOTSTRAP_PASSWORD": "Sup3r-Secret-Passw0rd!",
        "WSI_SETTINGS_REFRESH_SECONDS": "0",
    }

    def boot() -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", script], env=env, cwd=APP_DIR,
                              capture_output=True, text=True, timeout=120)

    assert boot().returncode == 0
    con = sqlite3.connect(str(db))
    try:
        (uid, tv), = con.execute("SELECT id, token_version FROM users").fetchall()
        # A fresh install has saved no authentication settings yet, so the
        # retry has no row to record the generation in: it leaves a floor,
        # which the same boot then applies.
        assert con.execute("SELECT COUNT(*) FROM auth_settings").fetchone()[0] == 0
    finally:
        con.close()
    # The pre-restore database had moved on since the "backup": the live one
    # stands in for the restored archive.
    _add_auth(previous, {uid: tv + 40}, generation=6)

    marker.write_text(json.dumps({"previous_db": str(previous), "error": "locked", "stage_id": "s"}))
    proc = boot()
    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT token_version FROM users WHERE id = ?", (uid,)).fetchone()[0] == tv + 41
        assert con.execute("SELECT session_generation FROM auth_settings").fetchone()[0] == 7
    finally:
        con.close()
    assert not marker.exists() and not _session_floor(tmp_path).exists()

    # A retry that cannot update the live database refuses to serve.
    marker.write_text(json.dumps({"previous_db": None, "error": "locked"}))
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TRIGGER no_tv BEFORE UPDATE OF token_version ON users "
                    "BEGIN SELECT RAISE(ABORT, 'read-only users'); END")
        con.commit()
    finally:
        con.close()
    proc = boot()
    assert proc.returncode != 0
    assert "refusing to serve" in proc.stderr
    assert marker.exists()


# ---------------------------------------------------------------------------
# A pre-restore database that is damaged or gone still gets everyone signed
# out
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def test_a_restore_over_a_damaged_database_signs_out_without_reading_it(tmp_path, runner):
    """Restoring over a damaged live database — the usual reason to restore —
    must not make the sign-out fail reading the parked copy and leave a retry
    marker that keeps every later start from serving until someone deletes it
    by hand. Every counter moves past a floor no value of the
    unreadable database can have reached, and nothing is left pending."""
    env = Env(tmp_path / "e")
    (env.data / "wsi.db").write_bytes(b"SQLite format 3\x00" + b"\xde\xad" * 4096)
    _add_auth(env.stage / "wsi.db", {1: 50, 3: 4}, generation=5)
    before = _now_ms()
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    tvs, gen = _auth_state(env.data / "wsi.db")
    assert set(tvs) == {1, 3} and min(tvs.values()) > before and gen > before
    assert not _revocation_marker(env.data).exists()
    assert "session_revocation_error" not in env.result()


def test_a_parked_database_deleted_before_the_reconciliation_is_not_read_as_none(tmp_path, runner):
    """A committed swap whose bookkeeping a crash cut short, with the parked
    database deleted by hand before the next start: read as "nothing was
    parked", it would sign out against the restored values alone, so a token
    the deleted database had issued at the restored value + 1 would stay
    valid. The journal's park step says a database was parked, so it
    is signed out past the floor."""
    from wsi_app import restore_apply

    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    _add_auth(env.stage / "wsi.db", {1: 50}, generation=5)
    steps = restore_apply.plan_journal(
        stage_id=STAGE_ID, scope=env.scope, stage_dir=str(env.stage),
        data_dir=str(env.data), db_path=str(env.data / "wsi.db"), app_version=None,
    )["steps"]
    assert runner.apply(env, kill_at=1 + 2 * len(steps) + 1).killed
    _make_unrecoverable(env)  # deletes the parked wsi.db
    before = _now_ms()
    assert _assert_reconciled(env, runner, runner.boot(env)) == "new"
    tvs, gen = _auth_state(env.data / "wsi.db")
    assert tvs[1] > before and gen > before
    assert not _revocation_marker(env.data).exists()


def test_the_floor_reaches_a_database_without_an_auth_settings_row(tmp_path):
    from wsi_app import restore_apply

    db = tmp_path / "wsi.db"
    _add_auth(db, {1: 50}, generation=None)
    before = _now_ms()
    restore_apply.invalidate_sessions(str(db), str(tmp_path / "gone.db"))
    assert _auth_state(db)[0][1] > before
    assert json.loads(_session_floor(tmp_path).read_text())["session_generation"] > before
    # With nothing parked (None) there is nothing to sign out past.
    db2 = tmp_path / "b" / "wsi.db"
    db2.parent.mkdir()
    _add_auth(db2, {1: 50}, generation=5)
    restore_apply.invalidate_sessions(str(db2), None)
    assert _auth_state(db2) == ({1: 51}, 6)


def test_an_io_error_reading_the_parked_database_is_not_no_sessions(tmp_path, monkeypatch):
    """``disk I/O error`` is an OperationalError, like "no such table": read
    leniently, a parked database answering it would read as one without any
    sessions, i.e. fail open."""
    from wsi_app import restore_apply

    db, prev = tmp_path / "wsi.db", tmp_path / "prev.db"
    _add_auth(db, {1: 50}, generation=5)
    _add_auth(prev, {1: 52}, generation=9)
    real_connect = sqlite3.connect

    class _Failing:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a):
            if "FROM users" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self.inner.execute(sql, *a)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(sqlite3, "connect", lambda p, *a, **k: (
        _Failing(real_connect(p, *a, **k)) if str(p) == str(prev) else real_connect(p, *a, **k)))
    before = _now_ms()
    restore_apply.invalidate_sessions(str(db), str(prev))
    tvs, gen = _auth_state(db)
    assert tvs[1] > before and gen > before


_TOKEN_SCRIPT = (
    "import sys\n"
    "from fastapi.testclient import TestClient\n"
    "from wsi_app.main import app\n"
    "with TestClient(app) as c:\n"
    "    tok = sys.argv[1]\n"
    "    if tok == 'mint':\n"
    "        from wsi_app.auth import create_token\n"
    "        from wsi_app.db import SessionLocal\n"
    "        from wsi_app.models import User\n"
    "        db = SessionLocal()\n"
    "        tok = create_token(db.query(User).first())\n"
    "        db.close()\n"
    "        print('TOKEN=' + tok)\n"
    "    r = c.get('/api/admin/me', headers={'Authorization': 'Bearer ' + tok})\n"
    "    print('STATUS=%d' % r.status_code)\n"
)


@pytest.mark.parametrize("previous", ["gone", "damaged"])
def test_the_boot_signs_out_past_a_parked_database_it_cannot_read(tmp_path, previous):
    """End to end through the real lifespan: a retry marker naming a parked
    database that was deleted (an operator tidying ``.pre-restore-*``) or is
    damaged does not keep the service from starting — and a token issued
    before is refused afterwards."""
    db = tmp_path / "wsi.db"
    marker = _revocation_marker(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": f"sqlite:///{db}",
        "DATA_DIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "RESTORE_REQUEST_PATH": str(tmp_path / "run" / "restore.request"),
        "JWT_SECRET": "x" * 40,
        "ADMIN_BOOTSTRAP_PASSWORD": "Sup3r-Secret-Passw0rd!",
        "WSI_SETTINGS_REFRESH_SECONDS": "0",
    }

    def boot(arg: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", _TOKEN_SCRIPT, arg], env=env, cwd=APP_DIR,
                              capture_output=True, text=True, timeout=120)

    proc = boot("mint")
    assert proc.returncode == 0, proc.stderr
    token = re.search(r"TOKEN=(\S+)", proc.stdout).group(1)
    assert "STATUS=200" in proc.stdout
    parked = tmp_path / ".pre-restore-x" / "wsi.db"
    if previous == "damaged":
        parked.parent.mkdir()
        parked.write_bytes(b"\x00garbage" * 1024)
    marker.write_text(json.dumps({"previous_db": str(parked), "error": "file is not a database",
                                  "stage_id": "stage-0123456789ab"}))
    proc = boot(token)
    assert proc.returncode == 0, proc.stderr
    assert "STATUS=401" in proc.stdout, proc.stdout
    assert not marker.exists()


# ---------------------------------------------------------------------------
# One restore's pending sign-out is never dropped by another
# ---------------------------------------------------------------------------

def test_inprocess_apply_refuses_while_a_sign_out_is_pending(tmp_path, dev_settings):
    """Dev keeps serving after a restore whose sign-out failed twice (the
    swap's and the post-apply hook's retry). A second swap would park the
    database the marker belongs to and drop or overwrite the marker; it is
    refused until a restart has run the retry."""
    from wsi_app import backup as _backup

    env = Env(tmp_path / "e")
    dev_settings(env)
    marker = _revocation_marker(env.data)
    marker.write_text(json.dumps({"stage_id": "stage-aaaaaaaaaaaa", "previous_db": None, "error": "x"}))
    with pytest.raises(RuntimeError, match="sign-out is still pending.*restart the app"):
        _backup.inprocess_apply(STAGE_ID, "full", _DummyEngine())
    assert env.state() == "old" and env.stage.is_dir() and marker.exists()
    assert not env.journal.exists()


def test_a_successful_sign_out_removes_only_its_own_marker(tmp_path):
    from wsi_app import restore_apply

    env = Env(tmp_path / "e")
    _add_auth(env.data / "wsi.db", {1: 50}, generation=5)
    marker = _revocation_marker(env.data)
    journal = {"db_path": str(env.data / "wsi.db"), "steps": [], "stage_id": STAGE_ID}
    other = {"stage_id": "stage-aaaaaaaaaaaa", "previous_db": None, "error": "x"}
    marker.write_text(json.dumps(other))
    assert restore_apply.invalidate_sessions_after(journal) is None
    assert json.loads(marker.read_text()) == other  # restore A's, still pending
    marker.write_text(json.dumps({**other, "stage_id": STAGE_ID}))
    assert restore_apply.invalidate_sessions_after(journal) is None
    assert not marker.exists()


# ---------------------------------------------------------------------------
# A connection opened mid-swap waits for it
# ---------------------------------------------------------------------------

def test_a_connection_opened_mid_swap_waits_and_sees_the_restored_db(tmp_path, monkeypatch, dev_settings):
    """A request thread connecting while the live DB path is missing must not
    create an empty wsi.db + -wal there: the promote would refuse, the rollback
    meet "both exist", and the dev swap jam behind its journal. Such a
    connection waits at db.db_swap_gate and opens the restored DB."""
    import threading
    import time

    from sqlalchemy import create_engine, text

    from wsi_app import backup as _backup
    from wsi_app import db as _db
    from wsi_app import restore_apply as ra

    env = Env(tmp_path / "e")
    dev_settings(env)
    eng = create_engine(f"sqlite:///{env.data / 'wsi.db'}")
    _db.install_db_swap_wait(eng)
    seen: dict = {}

    def query() -> None:
        with eng.connect() as conn:
            seen["width"] = conn.execute(text("SELECT width FROM slides WHERE id = 1")).scalar()

    parked = []
    real = ra.move_durable

    def move(src, dst):
        real(src, dst)
        if src == str(env.data / "wsi.db") and not parked:
            # The live DB was just parked: the path is missing now.
            t = threading.Thread(target=query)
            t.start()
            time.sleep(0.3)
            assert t.is_alive(), "the connection did not wait for the swap"
            assert not (env.data / "wsi.db").exists()
            parked.append(t)

    monkeypatch.setattr(ra, "move_durable", move)
    try:
        _backup.inprocess_apply(STAGE_ID, env.scope, eng)
        (t,) = parked
        t.join(10)
        assert not t.is_alive()
        assert seen == {"width": 200}  # the restored database
        assert not env.journal.exists()
    finally:
        eng.dispose()
    assert env.state() == "new"


def test_the_swap_gate_wait_is_bounded(monkeypatch):
    from wsi_app import db as _db

    monkeypatch.setattr(_db, "DB_SWAP_WAIT_SECONDS", 0.05)
    with _db.db_swap_gate():
        with _db.engine.connect():
            pass  # proceeds once the bounded wait ran out


def test_an_incomplete_stage_is_refused_by_both_swaps(tmp_path, dev_settings):
    """A stage directory whose staging job died mid-extract never passed the
    archive's checks; without the marker stage_restore writes last, neither
    swap may take it."""
    from wsi_app import backup as _backup

    env = Env(tmp_path / "e")
    dev_settings(env)
    (env.stage / _backup.STAGE_COMPLETE_MARKER).unlink()
    req = tmp_path / "run" / "restore.request"
    with _settings(restore_request_path=str(req)):
        with pytest.raises(RuntimeError, match="not completely staged"):
            _backup.request_apply(STAGE_ID, "full", "someone")
        assert not req.exists()
    with pytest.raises(RuntimeError, match="not completely staged"):
        _backup.inprocess_apply(STAGE_ID, "full", _DummyEngine())
    assert env.state() == "old" and env.stage.is_dir()


def test_the_stage_marker_is_never_promoted(tmp_path, runner):
    env = Env(tmp_path / "e")
    assert _assert_reconciled(env, runner, runner.apply(env)) == "new"
    assert not list(env.data.rglob(".staged-ok"))


def test_a_request_from_another_release_is_not_applied(tmp_path, monkeypatch):
    """update.sh deploying a release while a request waited for its restart:
    the new release's boot would apply a stage the old one checked. It is
    reported and dropped, nothing moved, the stage left for a re-stage."""
    env = Env(tmp_path / "other-release")
    monkeypatch.setattr(restore_apply, "_running_app_version", lambda: "9.9.9")
    req = json.loads(env.req.read_text(encoding="utf-8"))
    req["app_version"] = "0.0.1"
    env.req.write_text(json.dumps(req), encoding="utf-8")
    assert restore_apply.main([str(env.req), str(env.res), str(env.journal)]) == 0
    res = env.result()
    assert res["ok"] is False and res["stage"] == "version_changed", res
    assert "0.0.1" in res["error"] and "9.9.9" in res["error"], res
    assert env.state() == "old"
    assert not env.req.exists() and not env.journal.exists()
    assert (env.stage / "wsi.db").exists()


def test_the_running_release_is_read_from_its_version_stamp(tmp_path, monkeypatch):
    app = tmp_path / "repo" / "services" / "app" / "wsi_app"
    app.mkdir(parents=True)
    monkeypatch.setattr(restore_apply, "__file__", str(app / "restore_apply.py"))
    assert restore_apply._running_app_version() is None
    (tmp_path / "repo" / "VERSION").write_text("v1.4.2\n", encoding="utf-8")
    assert restore_apply._running_app_version() == "1.4.2"
