"""The download token (``…/download?t=<jwt>``) stays out of uvicorn's access log.

uvicorn writes every request target to its access log, which goes to the
journal by default; a signed-link download carries an hour-long staff token
in its query string. ``wsi_app.main`` attaches a filter to ``uvicorn.access``
that masks it, as nginx's wsi_combined log format does one hop out.
"""
from __future__ import annotations

import logging

import pytest

import wsi_app.main as _main

# uvicorn's own access-log call: (client, method, path?query, version, status).
_ACCESS_MSG = '%s - "%s %s HTTP/%s" %d'


def _logged(path: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, _ACCESS_MSG,
        ("127.0.0.1:5000", "GET", path, "1.1", 200), None,
    )
    assert logging.getLogger("uvicorn.access").filter(record)
    return record.getMessage()


@pytest.mark.parametrize("path, expected", [
    ("/api/admin/backup/wsi-backup-1.tar.zst/download?t=eyJhbGciOi.x.y",
     "/api/admin/backup/wsi-backup-1.tar.zst/download?t=[redacted]"),
    ("/api/admin/slides/7/download?t=abc",
     "/api/admin/slides/7/download?t=[redacted]"),
    ("/api/admin/slides/export/exp-1/download?x=1&t=abc&y=2",
     "/api/admin/slides/export/exp-1/download?x=1&t=[redacted]&y=2"),
    # A percent-encoded key: Starlette decodes ``%74`` to ``t``, so the token
    # authenticates, and uvicorn logs the raw query string.
    ("/api/admin/slides/7/download?%74=abc",
     "/api/admin/slides/7/download?%74=[redacted]"),
    ("/api/admin/slides/7/download?x=1&%74=abc&t=def",
     "/api/admin/slides/7/download?x=1&%74=[redacted]&t=[redacted]"),
])
def test_download_token_is_masked(path, expected):
    assert _logged(path) == f'127.0.0.1:5000 - "GET {expected} HTTP/1.1" 200'


@pytest.mark.parametrize("path", [
    # Not a download route: left as uvicorn wrote it.
    "/api/slides?t=1",
    "/favicon.ico?t=1700000000",
    # A download route without a token, or with a parameter merely ending in t.
    "/api/admin/slides/7/download",
    "/api/admin/slides/7/download?format=x&at=1",
    # Keys the app never reads as ``t``: query keys are case-sensitive, and a
    # doubly-encoded one decodes only to ``%74``.
    "/api/admin/slides/7/download?T=1",
    "/api/admin/slides/7/download?%54=1",
    "/api/admin/slides/7/download?%2574=1",
])
def test_other_requests_are_logged_verbatim(path):
    assert _logged(path) == f'127.0.0.1:5000 - "GET {path} HTTP/1.1" 200'


def test_filter_is_attached_once():
    _main._redact_download_tokens_in_access_log()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, _main._DownloadTokenRedactFilter) for f in access.filters) == 1
