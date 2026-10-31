"""Tests for the SPA / built-asset handler (``main._spa_handler``).

Production serves the frontend build from ``services/app/static``, a symlink
install.sh atomically re-points at each new ``static.rel.<n>`` build dir (and
then deletes the old one). The handler is built against a temp tree here,
laid out the same way, since the test checkout has no build.
"""
from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from wsi_app import main

IMMUTABLE = "public, max-age=31536000, immutable"


def _build(root, name, chunk):
    d = root / name
    (d / "static-assets").mkdir(parents=True)
    (d / "index.html").write_text(f"<!doctype html><title>{name}</title>")
    (d / "static-assets" / chunk).write_text(f"/* {name} */")
    (d / "favicon.svg").write_text("<svg/>")
    return d


@pytest.fixture
def site(tmp_path):
    _build(tmp_path, "static.rel.1", "index-aaaa1111.js")
    link = tmp_path / "static"
    os.symlink("static.rel.1", link)
    app = Starlette()
    app.router.default = main._spa_handler(str(link))
    return tmp_path, link, TestClient(app)


def test_index_shell_is_revalidated(site):
    _, _, client = site
    for path in ("/", "/index.html", "/viewer/abc123", "/admin/slides"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "static.rel.1" in r.text
        assert r.headers["cache-control"] == "no-cache", path


def test_hashed_assets_are_immutable(site):
    _, _, client = site
    r = client.get("/static-assets/index-aaaa1111.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == IMMUTABLE
    # A non-hashed public file keeps the default (validator-based) caching.
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert "cache-control" not in r.headers


def test_missing_hashed_asset_is_404_not_the_shell(site):
    """A page loaded before a deploy asks for a chunk the new build no longer
    has. index.html with a 200 there is HTML parsed as JavaScript: a blank
    page instead of the failed import the SPA recovers from by reloading."""
    _, _, client = site
    for path in ("/static-assets/index-gone0000.js", "/static-assets/x/../y.css", "/static-assets"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "<title>" not in r.text


def test_new_build_is_served_after_the_symlink_flip(site):
    """install.sh publishes by renaming a new symlink over ``static`` and
    reaping the old build dir. A served root resolved once at import would
    send every file of the new build to a 404 until the next restart."""
    root, link, client = site
    _build(root, "static.rel.2", "index-bbbb2222.js")
    tmp_link = root / "static.lnk"
    os.symlink("static.rel.2", tmp_link)
    os.replace(tmp_link, link)
    shutil.rmtree(root / "static.rel.1")

    r = client.get("/static-assets/index-bbbb2222.js")
    assert r.status_code == 200
    assert "static.rel.2" in r.text
    assert r.headers["cache-control"] == IMMUTABLE
    r = client.get("/")
    assert "static.rel.2" in r.text
    assert client.get("/static-assets/index-aaaa1111.js").status_code == 404


def test_files_outside_the_build_are_not_served(site):
    root, _, client = site
    (root / "secret.txt").write_text("nope")
    r = client.get("/..%2Fsecret.txt")
    assert "nope" not in r.text
    assert client.get("/%00foo").status_code == 200  # the shell, not a 500


def test_api_prefixes_still_404(site):
    _, _, client = site
    for path in ("/api/nope", "/dzi/x.dzi", "/thumbs/x", "/assets/x"):
        assert client.get(path).status_code == 404, path
