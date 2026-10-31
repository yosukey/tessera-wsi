"""infra/lib/envfile_set.py: the atomic rewrite behind set-workers.sh,
set-cache-size.sh and sync-from.sh's secret carry-over."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[3] / "infra" / "lib" / "envfile_set.py"
_spec = importlib.util.spec_from_file_location("envfile_set_under_test", _PATH)
envfile_set = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = envfile_set
_spec.loader.exec_module(envfile_set)


def test_the_new_content_replaces_the_file_with_its_mode(tmp_path):
    env = tmp_path / "wsi.env"
    env.write_text("A=1\n")
    env.chmod(0o640)
    envfile_set.write_atomic(str(env), ["A=2\n"])
    assert env.read_text() == "A=2\n"
    assert (env.stat().st_mode & 0o777) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wsi.env"]


def test_a_failed_rename_leaves_the_file_and_no_temp_file_behind(tmp_path, monkeypatch):
    env = tmp_path / "wsi.env"
    env.write_text("A=1\n")

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(envfile_set.os, "replace", boom)
    with pytest.raises(OSError):
        envfile_set.write_atomic(str(env), ["A=2\n"])
    assert env.read_text() == "A=1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wsi.env"]


def test_a_symlinked_env_file_stays_a_link_and_its_target_is_rewritten(tmp_path):
    real = tmp_path / "managed" / "wsi.env"
    real.parent.mkdir()
    real.write_text("A=1\n")
    link = tmp_path / "wsi.env"
    link.symlink_to(real)
    envfile_set.write_atomic(str(link), ["A=2\n"])
    assert link.is_symlink() and real.read_text() == "A=2\n"
    assert sorted(p.name for p in real.parent.iterdir()) == ["wsi.env"]


@pytest.mark.skipif(os.geteuid() != 0, reason="giving a file to another account needs root")
def test_the_owner_and_group_survive_the_rewrite(tmp_path):
    env = tmp_path / "wsi.env"
    env.write_text("A=1\n")
    os.chown(env, 65534, 65533)
    envfile_set.write_atomic(str(env), ["A=2\n"])
    st = env.stat()
    assert (st.st_uid, st.st_gid) == (65534, 65533)


def test_without_the_right_to_chown_the_rewrite_still_succeeds(tmp_path, monkeypatch):
    env = tmp_path / "wsi.env"
    env.write_text("A=1\n")

    def refused(*a, **kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(envfile_set.os, "chown", refused)
    envfile_set.write_atomic(str(env), ["A=2\n"])
    assert env.read_text() == "A=2\n"
