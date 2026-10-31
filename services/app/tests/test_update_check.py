"""Tests for the upstream release check (wsi_app.update_check).

It orders versions with ``app_meta.parse_version`` (the downgrade guards' own
parser), so git-describe post-tag builds (``X.Y.Z-N-g<sha>``) rank above their
tag — N commits past a tag is NEWER than the bare tag, otherwise the dashboard
shows a false "update available" for the release the build is already ahead
of. A version from the frontend/package.json fallback is never compared.
"""
from __future__ import annotations

import json

import pytest

from wsi_app import update_check, version


class _FakeResp:
    """Minimal stand-in for the http.client.HTTPResponse context manager.

    ``read(amt)`` honours the byte cap the same way the real response does, so
    the test can prove _fetch_latest passes a bound to resp.read().
    """

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]


def test_fetch_latest_reads_small_body(monkeypatch):
    body = b'{"tag_name": "v1.2.3"}'
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(body),
    )
    assert update_check._fetch_latest() == "v1.2.3"


def test_fetch_latest_caps_oversized_body(monkeypatch):
    # A body whose tag_name only becomes readable *past* the 1 MiB cap must be
    # truncated: without the cap json.loads would read the whole (huge) payload
    # and return the tag; with it, the truncated JSON fails to parse and
    # _fetch_latest returns None (graceful degradation, no unbounded read).
    padding = b"x" * (2 * update_check._MAX_RESPONSE_BYTES)
    body = b'{"padding": "' + padding + b'", "tag_name": "v9.9.9"}'
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(body),
    )
    assert update_check._fetch_latest() is None


def test_fetch_latest_picks_highest_tag_not_newest(monkeypatch):
    """/tags lists by creation time; a later hotfix on an older minor must not
    win, and non-version tags are ignored."""
    import urllib.error

    tags = [{"name": "v0.9.10"}, {"name": "nightly"}, {"name": "v0.10.0-rc1"},
            {"name": "v0.10.0"}, {"name": "v0.9.240-3-gabc1234"}]

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/releases/latest"):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return _FakeResp(json.dumps(tags).encode())

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest() == "v0.10.0"


def _status(monkeypatch, current, source, latest):
    monkeypatch.setattr(update_check, "_ENABLED", True)
    monkeypatch.setattr(
        update_check, "resolve_version",
        lambda: version.ResolvedVersion(current, source),
    )
    monkeypatch.setattr(update_check, "_refresh_async", lambda: None)
    monkeypatch.setitem(update_check._state, "latest", latest)
    monkeypatch.setitem(update_check._state, "checked_at", 1.0)
    monkeypatch.setitem(update_check._state, "last_attempt_at", 1.0)
    return update_check.get_update_status()


@pytest.mark.parametrize("source", [version.SOURCE_VERSION_FILE, version.SOURCE_GIT])
def test_update_available_only_when_upstream_is_newer(monkeypatch, source):
    st = _status(monkeypatch, "0.9.239", source, "v0.9.240")
    assert st["comparable"] and st["update_available"]
    assert st["current_source"] == source
    # A build 3 commits past the latest tag is ahead of it, not behind.
    st = _status(monkeypatch, "0.9.240-3-gabc1234", source, "v0.9.240")
    assert st["comparable"] and not st["update_available"]
    st = _status(monkeypatch, "0.9.240-rc1-3-gabc1234", source, "v0.9.240-rc1")
    assert not st["update_available"]
    st = _status(monkeypatch, "0.9.240-rc1-3-gabc1234", source, "v0.9.240")
    assert st["update_available"]


def test_package_json_fallback_is_never_compared(monkeypatch):
    """A release archive without VERSION reports frontend/package.json, which
    may lag the code; it must not produce an "update available" banner (it
    would, forever, with the never-bumped 0.1.0)."""
    st = _status(monkeypatch, "0.1.0", version.SOURCE_PACKAGE_JSON, "v0.9.240")
    assert st["update_available"] is False
    assert st["comparable"] is False
    assert st["current_source"] == version.SOURCE_PACKAGE_JSON


def test_unparseable_current_is_not_comparable(monkeypatch):
    st = _status(monkeypatch, "unknown", version.SOURCE_UNKNOWN, "v0.9.240")
    assert not st["comparable"] and not st["update_available"]
