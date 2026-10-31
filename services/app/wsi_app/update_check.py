"""Lightweight async check for newer upstream releases.

The admin dashboard reads `get_update_status()` to decide whether to mark
the App version row with an "update available" indicator. The actual
network call to GitHub runs in a background thread; the request handler
only ever reads the in-memory cache, so the dashboard never blocks on
this lookup.

Configuration (all optional, all read once at import time):

* ``WSI_UPDATE_CHECK=0`` (or ``false`` / ``no`` / ``off``)
      Disable the check entirely (e.g. air-gapped servers).
* ``WSI_UPDATE_CHECK_REPO=<owner>/<name>``
      Override the upstream GitHub repository polled for releases. The
      default targets the canonical tessera repo; override when forking
      or when the repo is renamed on the GitHub side.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .app_meta import parse_version
from .config import env_bool
from .version import APP_VERSION, is_authoritative, resolve_version

# GitHub repository polled for the latest tagged release. The default
# points at the canonical upstream; operators of forks (or anyone who
# rehosts the project elsewhere) can redirect the check via the env var
# without editing code.
_GITHUB_REPO = os.environ.get("WSI_UPDATE_CHECK_REPO", "yosukey/tessera-wsi")
_GITHUB_LATEST = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_GITHUB_TAGS   = f"https://api.github.com/repos/{_GITHUB_REPO}/tags"
_TTL_SECONDS = 12 * 3600
# Backoff between consecutive *failed* attempts. A transient network
# outage shouldn't pin the dashboard to a stale answer for the full TTL,
# but the GitHub API also must not be hammered on every dashboard load.
_RETRY_AFTER_FAIL_SECONDS = 600
_FETCH_TIMEOUT = 4
# Cap the response body read from GitHub. A release/tags JSON is a few KB;
# 1 MiB is ample headroom. A misconfigured WSI_UPDATE_CHECK_REPO pointing at a
# large or streaming endpoint could otherwise balloon the worker's memory on an
# unbounded ``resp.read()``. Reading at most this many bytes truncates such a
# body; the truncated JSON then fails to parse and _fetch_latest falls through
# to its ``return None`` (the cached value is preserved), so the cap degrades
# gracefully rather than crashing.
_MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB
_ENABLED = env_bool("WSI_UPDATE_CHECK", True)

_lock = threading.Lock()
_state: dict = {
    "checked_at": 0.0,       # last *successful* fetch
    "last_attempt_at": 0.0,  # last fetch attempt (success or failure)
    "latest": None,
    "in_flight": False,
}


def _fetch_latest() -> Optional[str]:
    """Try the Releases endpoint first; fall back to the Tags list on 404.

    Repos that publish annotated tags but no GitHub Releases return 404 from
    /releases/latest.  Falling back to /tags lets those repos still surface
    an update notification.
    """
    ua = f"wsi-update-check/{APP_VERSION}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": ua}
    try:
        req = urllib.request.Request(_GITHUB_LATEST, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read(_MAX_RESPONSE_BYTES))
            tag = data.get("tag_name")
            if isinstance(tag, str) and tag:
                return tag
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return None
        # Reached when /releases/latest returned 404 (repo has no published
        # release yet) OR returned 200 with a missing/empty tag_name. The
        # latter is rare on github.com but observed on self-hosted mirrors;
        # treat both the same way and try the tags list.
        req2 = urllib.request.Request(_GITHUB_TAGS, headers=headers)
        with urllib.request.urlopen(req2, timeout=_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read(_MAX_RESPONSE_BYTES))
        # /tags returns by tag-creation time (newest first), not by version.
        # A hotfix tag cut later on an older minor (e.g. v0.9.10 created
        # after v0.10.0) would otherwise be picked as "latest" and trigger
        # false update banners. Scan the page and pick the highest parseable
        # vX.Y.Z[-...] tag instead.
        if isinstance(data, list) and data:
            best_tag: Optional[str] = None
            best_key: Optional[tuple] = None
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not name:
                    continue
                key = parse_version(name)
                if key is None:
                    continue
                if best_key is None or key > best_key:
                    best_key = key
                    best_tag = name
            return best_tag
        return None
    except Exception:
        return None


def _refresh_async() -> None:
    with _lock:
        if _state["in_flight"]:
            return
        _state["in_flight"] = True

    def _run() -> None:
        latest = None
        try:
            latest = _fetch_latest()
        finally:
            with _lock:
                now = time.time()
                _state["last_attempt_at"] = now
                # Only overwrite the cached value on success. Otherwise a
                # transient network blip would wipe a previously-good
                # `latest` and the "update available" indicator would
                # vanish until the next successful fetch.
                if latest is not None:
                    _state["latest"] = latest
                    _state["checked_at"] = now
                _state["in_flight"] = False

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        with _lock:
            _state["in_flight"] = False


def _iso(ts: float) -> Optional[str]:
    if ts <= 0:
        return None
    return (
        datetime.datetime.fromtimestamp(ts)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def get_update_status() -> dict:
    """Return the cached upstream-version status. Never blocks on network."""
    # `current` is a live read so a deploy (which rewrites VERSION) clears
    # the "update available" indicator without waiting for the 12h
    # upstream-cache TTL or a uvicorn restart.
    current, current_source = resolve_version()
    if not _ENABLED:
        return {
            "enabled": False,
            "current": current,
            "current_source": current_source,
            "latest": None,
            "update_available": False,
            "comparable": False,
            "checked_at": None,
        }

    with _lock:
        checked = _state["checked_at"]
        last_attempt = _state["last_attempt_at"]
        latest = _state["latest"]

    now = time.time()
    # Refresh when the last *successful* fetch is stale, but back off
    # between consecutive failed attempts so a network outage doesn't
    # hammer the GitHub API on every dashboard request.
    if (now - checked > _TTL_SECONDS
            and now - last_attempt > _RETRY_AFTER_FAIL_SECONDS):
        _refresh_async()

    # A version from the frontend/package.json fallback does not follow the
    # release tags, so it may sit far behind the code actually running;
    # comparing it would announce an "update" to a release this build is
    # already past. Treat it like an unparseable version.
    cur = parse_version(current) if is_authoritative(current_source) else None
    lat = parse_version(latest) if latest else None
    # `comparable` exposes "it can't be decided whether the running version
    # is behind upstream" (no trustworthy, parseable version) — otherwise the
    # UI would silently show "up to date" for such builds even though no
    # comparison actually ran.
    comparable = bool(cur and lat)
    return {
        "enabled": True,
        "current": current,
        # Where `current` came from (version.SOURCE_*), so an incomparable
        # result can be told apart: a fallback version vs a non-version string.
        "current_source": current_source,
        "latest": latest,
        "update_available": bool(comparable and lat > cur),
        "comparable": comparable,
        "checked_at": _iso(checked),
        # `last_attempt_at` separates "fetch hasn't run yet" from "fetch
        # ran but failed" — `checked_at` alone can't, because it's only
        # written on success. The badge uses this to stop showing
        # "Checking…" indefinitely after a failed GitHub fetch.
        "last_attempt_at": _iso(last_attempt),
    }
