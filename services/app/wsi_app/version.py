import json
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

# Where a resolved version came from (see :func:`resolve_version`).
SOURCE_VERSION_FILE = "VERSION"
SOURCE_GIT = "git describe"
SOURCE_PACKAGE_JSON = "package.json"
SOURCE_UNKNOWN = "unknown"

# Sources that name the commit actually running. A release is just a git tag;
# ``frontend/package.json`` is not bumped with it, so it can lag the code
# arbitrarily — far enough to make the app look older than a database it
# stamped itself.
_AUTHORITATIVE_SOURCES = frozenset({SOURCE_VERSION_FILE, SOURCE_GIT})

# Repo layout: <repo>/services/app/wsi_app/version.py -> repo root via
# parents[3]; install.sh rsyncs the repo to /opt/wsi, so the same relative
# paths hold in production.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_GIT_TIMEOUT_SECONDS = 5


class ResolvedVersion(NamedTuple):
    version: str
    source: str


def _strip_v(tag: str) -> str:
    # AdminServerSection.tsx and the About dialog prepend "v" themselves, so a
    # stored "v0.2.0" would render as "vv0.2.0".
    return tag[1:] if tag.startswith("v") else tag


def _read_version_file(repo_root: Path) -> Optional[str]:
    try:
        stamped = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _strip_v(stamped) or None


def _git_describe(repo_root: Path) -> Optional[str]:
    if not (repo_root / ".git").exists():
        return None
    try:
        described = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # No git binary, no reachable tag, a repo git refuses to read
        # (safe.directory), or a hung call: none of these is a version.
        return None
    return _strip_v(described) or None


def _read_package_json(repo_root: Path) -> Optional[str]:
    try:
        pkg = json.loads(
            (repo_root / "frontend" / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    version = pkg.get("version") if isinstance(pkg, dict) else None
    if not isinstance(version, str):
        return None
    return version.strip() or None


def resolve_version(repo_root: Optional[Path] = None) -> ResolvedVersion:
    """Return the running app version and the source it was read from.

    Resolution order (``frontend/vite.config.ts`` applies the same one at
    build time, so the About dialog and the admin Server Info page agree):

    1. ``<repo>/VERSION`` — written by ``infra/install.sh`` at deploy time
       with ``git describe --tags --always`` against the source repo. This is
       the authoritative answer in production: the deploy checks out a
       release tag and rsyncs to ``/opt/wsi`` without ``.git``, and the stamp
       survives the rsync.
    2. ``git describe --tags`` — a development checkout where ``install.sh``
       has not run but ``.git`` and a reachable tag exist.
    3. ``<repo>/frontend/package.json`` — a tree with neither (e.g. a release
       tarball unpacked by hand). Not bumped by releases, so it is a
       *fallback*: :func:`is_authoritative` is False for it and the
       app_version downgrade guards and the update check do not trust it.
    4. ``"unknown"`` — none of the above readable.

    Reads from disk on every call so admin endpoints show the deployed version
    after an update without a backend restart.
    """
    root = _REPO_ROOT if repo_root is None else repo_root
    stamped = _read_version_file(root)
    if stamped:
        return ResolvedVersion(stamped, SOURCE_VERSION_FILE)
    described = _git_describe(root)
    if described:
        return ResolvedVersion(described, SOURCE_GIT)
    pkg = _read_package_json(root)
    if pkg:
        return ResolvedVersion(pkg, SOURCE_PACKAGE_JSON)
    return ResolvedVersion("unknown", SOURCE_UNKNOWN)


def read_version() -> str:
    """The running app version (live read; see :func:`resolve_version`)."""
    return resolve_version().version


def is_authoritative(source: str) -> bool:
    """Whether a version from ``source`` identifies the code actually running."""
    return source in _AUTHORITATIVE_SOURCES


# Snapshot for code paths that need one identity for the lifetime of the
# process (migrations, backup metadata, the boot-time downgrade guard): those
# must not see the value flip mid-write.
APP_VERSION, APP_VERSION_SOURCE = resolve_version()
