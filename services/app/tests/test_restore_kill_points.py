"""Kill points of the restore-apply swap past its last rename.

test_restore_apply_journal.py kills the swap right after every rename /
replace — the moves, the journal cursor writes, the commit marker and the
result file. What runs after those is bookkeeping, and a SIGKILL or power
loss can land in it just as well:

* the sqlite commit inside ``invalidate_sessions`` (the sign-out that moves
  every staff token / app cookie counter past both sides of the swap) —
  before it lands, and after;
* the removal of the request file (boot path only);
* the removal of the journal;
* the stage's rmtree after it.

Whatever the point, the next start must leave the restored tree with every
session signed out, the journal and the request gone, and the result saying
the restore succeeded — and the leftovers of a half-removed stage must not
be applicable again.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import pytest

from .test_restore_apply_journal import (
    APP_DIR,
    STAGE_ID,
    Cli,
    Env,
    InProcess,
    _add_auth,
    _auth_state,
    _Killed,
    dev_settings,  # noqa: F401 - the fixture, used by name
)

# ``python -m wsi_app.restore_apply`` (as start-wsi-app.sh runs it), dying
# with os._exit — the SIGKILL shape: no except / finally / atexit — at:
#   KILL_COMMIT=before|after   the first ``with conn:`` commit that changed
#                              rows (invalidate_sessions'), before it lands
#                              or right after;
#   KILL_AFTER_REMOVE=<path>   right after os.remove(<path>) landed;
#   KILL_IN_RMTREE=1           at the first unlink after the journal was
#                              removed (inside close_journal's stage rmtree).
_KILL_DRIVER = r'''
import os, runpy, sqlite3, sys
commit = os.environ.get("KILL_COMMIT") or None
remove_at = os.environ.get("KILL_AFTER_REMOVE") or None
in_rmtree = os.environ.get("KILL_IN_RMTREE") == "1"
journal = sys.argv[3]
journal_gone = [False]
real_remove, real_unlink = os.remove, os.unlink

def _after_unlink(path, kw):
    if "dir_fd" not in kw and os.fspath(path) == journal:
        journal_gone[0] = True
        if not in_rmtree and remove_at == journal:
            os._exit(137)
        return
    if remove_at is not None and "dir_fd" not in kw and os.fspath(path) == remove_at:
        os._exit(137)
    if in_rmtree and journal_gone[0]:
        os._exit(137)

def remove(path, *a, **kw):
    real_remove(path, *a, **kw)
    _after_unlink(path, kw)

def unlink(path, *a, **kw):
    real_unlink(path, *a, **kw)
    _after_unlink(path, kw)

os.remove, os.unlink = remove, unlink

if commit:
    class _Conn(sqlite3.Connection):
        def __exit__(self, exc_type, exc, tb):
            if exc_type is None and self.in_transaction and commit == "before":
                os._exit(137)
            changed = exc_type is None and self.in_transaction
            out = super().__exit__(exc_type, exc, tb)
            if changed and commit == "after":
                os._exit(137)
            return out

    real_connect = sqlite3.connect

    def connect(*a, **kw):
        kw.setdefault("factory", _Conn)
        return real_connect(*a, **kw)

    sqlite3.connect = connect

runpy.run_module("wsi_app.restore_apply", run_name="__main__", alter_sys=True)
'''


def _cli_killed(env: Env, **kill: str) -> None:
    driver = env.root / "kill_driver.py"
    driver.write_text(_KILL_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(driver), str(env.req), str(env.res), str(env.journal)],
        cwd=APP_DIR,
        env=dict(os.environ, PYTHONPATH=str(APP_DIR), **kill),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 137, (proc.returncode, proc.stderr)


@contextmanager
def _inprocess_kill(*, commit: str = "", remove_at: str = "", in_rmtree: bool = False,
                    journal: str = ""):
    """The in-process twin of _KILL_DRIVER: raises _Killed (a BaseException
    no handler may act on) at the same points; from then on every remove /
    unlink / rename raises it too, as a dead process touches nothing."""
    dead = [False]
    journal_gone = [False]
    real = {n: getattr(os, n) for n in ("remove", "unlink", "rename", "replace", "rmdir")}

    def die():
        dead[0] = True
        raise _Killed()

    def wrap(name):
        def inner(*a, **kw):
            if dead[0]:
                raise _Killed()
            real[name](*a, **kw)
            if name not in ("remove", "unlink") or "dir_fd" in kw:
                if name in ("remove", "unlink") and in_rmtree and journal_gone[0]:
                    die()
                return
            path = os.fspath(a[0])
            if path == journal:
                journal_gone[0] = True
                if remove_at == journal and not in_rmtree:
                    die()
                return
            if remove_at and path == remove_at:
                die()
            if in_rmtree and journal_gone[0]:
                die()
        return inner

    class _Conn(sqlite3.Connection):
        def __exit__(self, exc_type, exc, tb):
            if dead[0]:
                raise _Killed()
            changed = exc_type is None and self.in_transaction
            if changed and commit == "before":
                die()
            out = super().__exit__(exc_type, exc, tb)
            if changed and commit == "after":
                die()
            return out

    real_connect = sqlite3.connect

    def connect(*a, **kw):
        kw.setdefault("factory", _Conn)
        return real_connect(*a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        for name in real:
            mp.setattr(os, name, wrap(name))
        if commit:
            mp.setattr(sqlite3, "connect", connect)
        yield


def _inprocess_killed(runner: InProcess, env: Env, **kill) -> None:
    from wsi_app import backup

    runner.use(env)
    with _inprocess_kill(journal=str(env.journal), **kill):
        with pytest.raises(_Killed):
            backup.inprocess_apply(STAGE_ID, env.scope, _DummyEngine())


class _DummyEngine:
    def dispose(self) -> None:
        pass


def _seed_sessions(env: Env) -> None:
    # Live: user 1 revoked twice since the backup; archive: its old value.
    _add_auth(env.data / "wsi.db", {1: 52}, generation=9)
    _add_auth(env.stage / "wsi.db", {1: 50}, generation=5)


def _assert_restored_and_signed_out(env: Env, *, boot_path: bool, result_expected: bool = True) -> None:
    assert env.state() == "new", env.state()
    assert not env.journal.exists(), "journal left behind: the app would refuse to start"
    if boot_path:  # the dev in-process apply never reads Env's request file
        assert not env.req.exists(), "request left behind: it would be re-applied"
    # Past both sides of the swap (52 live, 50 archived; generation 9 / 5).
    # A sign-out that landed before the kill is run again by the next start,
    # which moves the counters on once more (max(restored, previous) + 1):
    # harmless, they only ever move forward.
    tvs, gen = _auth_state(env.data / "wsi.db")
    assert tvs[1] in (53, 54) and gen in (10, 11), (tvs, gen)
    # The killed commit left no hot journal beside the live DB.
    assert not (env.data / "wsi.db-journal").exists()
    if result_expected:
        res = env.result()
        assert res["ok"] is True and res["stage_id"] == STAGE_ID, res
        assert "session_revocation_error" not in res, res
    # A half-removed stage may be left for the 24 h sweep, but it can never
    # be applied again: its database was promoted out of it.
    assert not (env.stage / "wsi.db").exists()


_POINTS = ["commit-before", "commit-after", "request-removed", "journal-removed", "stage-rmtree"]


@pytest.mark.parametrize("point", _POINTS)
@pytest.mark.parametrize("entry", ["cli", "inprocess"])
def test_a_kill_after_the_last_rename_still_boots_into_the_restored_tree(
    tmp_path, dev_settings, entry, point,
):
    env = Env(tmp_path / "e")
    _seed_sessions(env)
    if entry == "cli":
        runner = Cli()
        kill = {
            "commit-before": {"KILL_COMMIT": "before"},
            "commit-after": {"KILL_COMMIT": "after"},
            "request-removed": {"KILL_AFTER_REMOVE": str(env.req)},
            "journal-removed": {"KILL_AFTER_REMOVE": str(env.journal)},
            "stage-rmtree": {"KILL_IN_RMTREE": "1"},
        }[point]
        _cli_killed(env, **kill)
    else:
        if point == "request-removed":
            pytest.skip("the dev in-process apply has no request file")
        runner = InProcess(dev_settings)
        kill = {
            "commit-before": {"commit": "before"},
            "commit-after": {"commit": "after"},
            "journal-removed": {"remove_at": str(env.journal)},
            "stage-rmtree": {"in_rmtree": True},
        }[point]
        _inprocess_killed(runner, env, **kill)

    committed_journal = env.journal.exists()
    if point.startswith("commit") or point == "request-removed":
        # Killed with the journal still committed: the next start finishes
        # the bookkeeping instead of rolling the restore back.
        assert committed_journal
        assert json.loads(env.journal.read_text())["phase"] == "committed"
    else:
        assert not committed_journal

    run = runner.boot(env)
    assert not run.killed
    if runner.name == "cli":
        assert run.exit_code == 0, run.stderr
    else:
        assert not run.error, run.error
        assert run.outcome == ("completed" if committed_journal else None)
    # The dev reconciliation (recover_interrupted_apply) writes no result
    # file of its own, so an in-process apply killed before writing it
    # leaves none; the boot path always reports.
    result_expected = runner.name == "cli" or not point.startswith("commit")
    assert env.res.exists() is result_expected
    _assert_restored_and_signed_out(
        env, boot_path=runner.name == "cli", result_expected=result_expected,
    )
    # Which sign-outs landed pins down where the kill fell: once (the killed
    # run's, or — killed before its commit — the boot's), or twice (the
    # killed run's, then the boot's re-run of a still-committed journal).
    twice = point in ("commit-after", "request-removed")
    assert _auth_state(env.data / "wsi.db") == (({1: 54}, 11) if twice else ({1: 53}, 10))
    if committed_journal and runner.name == "cli":
        assert env.result().get("recovered"), env.result()
    # Booting again changes nothing: no journal, no request, same counters.
    before = _auth_state(env.data / "wsi.db")
    again = runner.boot(env)
    assert not again.killed and _auth_state(env.data / "wsi.db") == before
