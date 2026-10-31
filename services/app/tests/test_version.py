"""Tests for the app-version resolution order (wsi_app.version).

VERSION file -> ``git describe --tags`` -> frontend/package.json -> "unknown",
the same order frontend/vite.config.ts applies at build time. Only the first
two are authoritative: a release is a git tag and package.json is not bumped
with it, so a tarball deploy could report a stale package.json version that
looks older than its own DB.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from wsi_app import version


def _tree(tmp_path, *, version_file=None, package=None):
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    if version_file is not None:
        (root / "VERSION").write_text(version_file, encoding="utf-8")
    if package is not None:
        (root / "frontend" / "package.json").write_text(
            json.dumps({"name": "x", "version": package}), encoding="utf-8"
        )
    return root


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root),
             "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    )


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_version_file_wins_and_loses_its_leading_v(tmp_path):
    root = _tree(tmp_path, version_file="v0.9.240-3-gabc1234\n", package="0.1.0")
    got = version.resolve_version(root)
    assert got == ("0.9.240-3-gabc1234", version.SOURCE_VERSION_FILE)
    assert version.is_authoritative(got.source)


def test_blank_version_file_falls_through(tmp_path):
    root = _tree(tmp_path, version_file="  \n", package="0.9.240")
    assert version.resolve_version(root) == ("0.9.240", version.SOURCE_PACKAGE_JSON)


@needs_git
def test_git_describe_before_package_json(tmp_path):
    root = _tree(tmp_path, package="0.1.0")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    _git(root, "tag", "v0.9.240")
    assert version.resolve_version(root) == ("0.9.240", version.SOURCE_GIT)
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "two")
    got = version.resolve_version(root)
    assert got.source == version.SOURCE_GIT
    assert got.version.startswith("0.9.240-1-g")
    assert version.is_authoritative(got.source)


@needs_git
def test_git_without_a_reachable_tag_falls_back_to_package_json(tmp_path):
    """No ``--always``: a bare commit hash is not a version, so an untagged
    clone (a shallow CI checkout) reports package.json, marked untrusted."""
    root = _tree(tmp_path, package="0.9.240")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    got = version.resolve_version(root)
    assert got == ("0.9.240", version.SOURCE_PACKAGE_JSON)
    assert not version.is_authoritative(got.source)


def test_nothing_readable_is_unknown(tmp_path):
    root = _tree(tmp_path)
    (root / "frontend" / "package.json").write_text("{not json", encoding="utf-8")
    got = version.resolve_version(root)
    assert got == ("unknown", version.SOURCE_UNKNOWN)
    assert not version.is_authoritative(got.source)

