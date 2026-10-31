"""Tile URLs are versioned by the slide's WSI content generation.

``Slide.wsi_generation`` starts at 1 and every WSI replace moves it to
``max(previous + 1, now in ms)`` — strictly up, and past any value an earlier,
restored-away replace may have issued. The
API reports it as ``tile_version``; the viewer opens
``/dzi/{public_id}.dzi?v={tile_version}`` and OpenSeadragon carries the query
onto every tile URL. nginx keys its 30-day shared tile cache on ``$uri?v=``, so
a replace moves viewers onto URLs no old tile was ever stored under instead of
mixing cached old tiles with fresh renders of the new file.

The /dzi/ routes keep each cache key single-generation: the current ``v`` is
served (and may be stored), an absent ``v`` (a script) is served but
marked ``X-Accel-Expires: 0``, and any other ``v`` is an uncacheable 404.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import update

from wsi_app import dzi as dzi_module
from wsi_app import slide_resolve_cache
from wsi_app.config import settings
from wsi_app.models import Slide


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeDz:
    def get_dzi(self, fmt: str) -> str:
        return f'<Image Format="{fmt}"/>'

    def get_tile(self, level, coord):
        from PIL import Image

        return Image.new("RGB", (4, 4))


def _entry(_slide_id):
    entry = dzi_module._DZEntry(osr=None, dz=_FakeDz(), etag='"etag"', size=0, path="")
    entry.refs = 1
    return entry


@contextmanager
def _fake_pyramid():
    """Stub the OpenSlide handle so the routes' gating runs without a real WSI."""
    with patch.object(dzi_module, "_get_dz", side_effect=_entry):
        yield


def _slide(db_session, *, published=True, generation=None, wsi_filename="gen.svs") -> Slide:
    s = Slide(title="Gen", catalog_id="GEN-1", wsi_filename=wsi_filename, is_published=published)
    if generation is not None:
        s.wsi_generation = generation
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _paths(public_id: str) -> list[str]:
    return [f"/dzi/{public_id}.dzi", f"/dzi/{public_id}_files/3/1_2.jpeg"]


def _no_store(r) -> bool:
    return r.headers.get("x-accel-expires") == "0"


@contextmanager
def _stub_openslide_for_replace():
    """Patch the OpenSlide-backed helpers so any bytes upload / replace cleanly
    (mirrors tests/test_upload_duplicate_filename.py)."""
    os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "thumbs"), exist_ok=True)
    from wsi_app import admin_helpers
    from wsi_app.routers import admin_slides

    props = {"width": 100, "height": 100, "mpp_x": 0.25, "mpp_y": 0.25,
             "vendor": "aperio", "acquired_at": "2024-01-15"}

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image
        Image.new("RGB", (1, 1)).save(out_path, "JPEG")

    with patch.object(admin_slides, "read_slide_properties", return_value=props), \
         patch.object(admin_slides, "make_thumbnail", side_effect=fake_thumbnail), \
         patch.object(admin_helpers, "read_associated_images", return_value={}):
        yield


def _upload(client, headers, name):
    r = client.post(
        "/api/admin/slides/upload", headers=headers,
        files={"file": (name, io.BytesIO(b"\x00" * 256), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()["slide_id"]


def _replace(client, headers, slide_id, name):
    return client.post(
        f"/api/admin/slides/{slide_id}/wsi", headers=headers,
        files={"file": (name, io.BytesIO(b"\x01" * 256), "application/octet-stream")},
    )


def _uniq(stem: str) -> str:
    return f"{stem}-{os.urandom(3).hex()}.svs"


# ---------------------------------------------------------------------------
# The generation: default, bump on replace, exposure
# ---------------------------------------------------------------------------

def test_new_slide_starts_at_generation_one(db_session):
    s = _slide(db_session)
    assert s.wsi_generation == 1


def test_replace_bumps_generation_and_api_reports_it(client, db_session, admin_headers):
    name = _uniq("replace")
    with _stub_openslide_for_replace():
        slide_id = _upload(client, admin_headers, name)
        assert db_session.get(Slide, slide_id).wsi_generation == 1
        before = time.time_ns() // 1_000_000
        # Same file name: every tile PATH is unchanged, only the generation moves.
        assert _replace(client, admin_headers, slide_id, name).status_code == 200
        db_session.expire_all()
        first = db_session.get(Slide, slide_id).wsi_generation
        assert first >= before
        assert _replace(client, admin_headers, slide_id, _uniq("renamed")).status_code == 200
    db_session.expire_all()
    s = db_session.get(Slide, slide_id)
    assert s.wsi_generation > first

    detail = client.get(f"/api/admin/slides/{slide_id}", headers=admin_headers).json()
    assert detail["tile_version"] == s.wsi_generation
    listed = {x["id"]: x for x in client.get("/api/admin/slides", headers=admin_headers).json()}
    assert listed[slide_id]["tile_version"] == s.wsi_generation
    # Drafts are visible on the token-keyed public detail to staff only.
    public = client.get(f"/api/slides/{s.public_id}", headers=admin_headers).json()
    assert public["tile_version"] == s.wsi_generation


def test_replace_after_a_restore_never_reissues_a_generation(client, db_session, admin_headers):
    """A backup restore puts the generation back to the backup's value. A
    replace after that must not land on a number a later, discarded replace
    already used — nginx may still hold that replace's tiles under it."""
    name = _uniq("restored")
    with _stub_openslide_for_replace():
        slide_id = _upload(client, admin_headers, name)
        assert _replace(client, admin_headers, slide_id, name).status_code == 200
        db_session.expire_all()
        discarded = db_session.get(Slide, slide_id).wsi_generation
        # What a restore of a backup taken before that replace leaves behind.
        db_session.get(Slide, slide_id).wsi_generation = 1
        db_session.commit()
        assert _replace(client, admin_headers, slide_id, name).status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, slide_id).wsi_generation > discarded


def test_replace_keeps_a_counter_already_past_the_clock_moving_up(client, db_session, admin_headers):
    """``previous + 1`` still wins when the stored generation is ahead of the
    clock (a clock stepped back), so a replace never reuses the current URL."""
    name = _uniq("ahead")
    ahead = time.time_ns() // 1_000_000 + 10_000_000
    with _stub_openslide_for_replace():
        slide_id = _upload(client, admin_headers, name)
        db_session.get(Slide, slide_id).wsi_generation = ahead
        db_session.commit()
        assert _replace(client, admin_headers, slide_id, name).status_code == 200
    db_session.expire_all()
    # Retired once before the swap, moved again by the commit.
    assert db_session.get(Slide, slide_id).wsi_generation == ahead + 2


def test_public_detail_reports_tile_version_to_anonymous(client, db_session):
    s = _slide(db_session, published=True, generation=5)
    r = client.get(f"/api/slides/{s.public_id}")
    assert r.status_code == 200
    assert r.json()["tile_version"] == 5


def test_rejected_replace_keeps_generation(client, db_session, admin_headers):
    """A replace refused before the commit (here: the name belongs to another
    slide) leaves the generation alone, so the old tile URLs stay current."""
    taken = _uniq("taken")
    with _stub_openslide_for_replace():
        _upload(client, admin_headers, taken)
        slide_id = _upload(client, admin_headers, _uniq("mine"))
        assert _replace(client, admin_headers, slide_id, taken).status_code == 409
    db_session.expire_all()
    assert db_session.get(Slide, slide_id).wsi_generation == 1


def test_replace_makes_new_generation_current_at_once(client, db_session, admin_headers):
    """The resolve cache holds each slide's generation; replace flushes it, so
    the very next request on this worker already treats the new v as current
    and the old one as stale."""
    name = _uniq("swap")
    with _stub_openslide_for_replace():
        slide_id = _upload(client, admin_headers, name)
        db_session.expire_all()
        s = db_session.get(Slide, slide_id)
        s.is_published = True
        db_session.commit()
        slide_resolve_cache.invalidate_slide_meta()
        with _fake_pyramid():
            # Prime the cache with generation 1.
            assert client.get(f"/dzi/{s.public_id}.dzi?v=1").status_code == 200
            assert _replace(client, admin_headers, slide_id, name).status_code == 200
            db_session.expire_all()
            new_v = db_session.get(Slide, slide_id).wsi_generation
            assert client.get(f"/dzi/{s.public_id}.dzi?v={new_v}").status_code == 200
            r = client.get(f"/dzi/{s.public_id}.dzi?v=1")
    assert r.status_code == 404
    assert _no_store(r)


# ---------------------------------------------------------------------------
# The ?v= contract on the /dzi/ routes
# ---------------------------------------------------------------------------

def test_current_generation_is_served_and_cacheable(client, db_session):
    s = _slide(db_session, generation=4)
    with _fake_pyramid():
        for path in _paths(s.public_id):
            r = client.get(f"{path}?v=4")
            assert r.status_code == 200, path
            assert not _no_store(r), path


def test_stale_generation_is_an_uncacheable_404(client, db_session):
    s = _slide(db_session, generation=4)
    with _fake_pyramid():
        for path in _paths(s.public_id):
            for v in ("3", "1", "5", "0"):
                r = client.get(f"{path}?v={v}")
                assert r.status_code == 404, (path, v)
                assert _no_store(r), (path, v)


def test_absent_generation_is_served_but_not_cacheable(client, db_session):
    """A script or other non-SPA caller without ``v`` still works; nginx just
    must not store what it got (the bare key could hold any generation)."""
    s = _slide(db_session, generation=4)
    with _fake_pyramid():
        for path in _paths(s.public_id):
            for suffix in ("", "?v=", "?r=junk"):
                r = client.get(f"{path}{suffix}")
                assert r.status_code == 200, (path, suffix)
                assert _no_store(r), (path, suffix)


def test_a_tile_geometry_change_moves_every_slide_to_new_tile_urls(db_session, monkeypatch):
    """A changed DZI_TILE_SIZE / DZI_OVERLAP re-cuts every pyramid under the
    same URLs, while nginx keeps tiles 30 days and browsers a day: the next
    boot gives every slide a new generation, so viewers ask for new URLs."""
    from wsi_app import app_meta
    from wsi_app.db import engine
    from wsi_app.models import AppMeta

    key = dzi_module.KEY_TILE_GEOMETRY
    s = _slide(db_session, generation=4)
    db_session.query(AppMeta).filter_by(key=key).delete()
    db_session.commit()
    try:
        # The first boot that knows about it only records the geometry ...
        assert dzi_module.reconcile_tile_geometry(engine) is False
        assert dzi_module.reconcile_tile_geometry(engine) is False
        db_session.expire_all()
        assert db_session.get(Slide, s.id).wsi_generation == 4
        # ... a different one moves every slide past every number used so far.
        monkeypatch.setattr(dzi_module, "_TILE_GEOMETRY", "t510o2")
        assert dzi_module.reconcile_tile_geometry(engine) is True
        db_session.expire_all()
        assert db_session.get(Slide, s.id).wsi_generation > 4
        assert app_meta.get(db_session, key) == "t510o2"
        assert dzi_module.reconcile_tile_geometry(engine) is False
    finally:
        db_session.query(AppMeta).filter_by(key=key).delete()
        db_session.commit()


def test_the_app_leaves_the_tiles_browser_policy_to_nginx(client, db_session):
    """nginx gives a current-generation tile its day-long browser caching
    ($wsi_dzi_tile_cache_control; infra/tests/test_tile_cache_control_nginx.sh).
    The app keeps saying no-cache, tiles included: nginx stores the app's
    header with every cached tile, so a policy sent from here would survive a
    rollback of the nginx config for as long as those tiles stay cached."""
    s = _slide(db_session, generation=4)
    tile, desc = f"/dzi/{s.public_id}_files/3/1_2.jpeg", f"/dzi/{s.public_id}.dzi"
    with _fake_pyramid():
        r = client.get(f"{tile}?v=4")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        again = client.get(f"{tile}?v=4", headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"
        assert client.get(f"{desc}?v=4").headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("query", [
    "v=04",        # non-canonical: a distinct nginx key for the same generation
    "v=%34",       # nginx's $arg_v is the raw "%34"
    "v=4.0",
    "v=+4",
    "v=-4",
    "v=abc",
    "v=4&v=4",     # duplicates: nginx keys the first, Starlette reads the last
    "v=4&v=3",
    "v=3&v=4",
    "%76=4",       # Starlette decodes the name to v; nginx's $arg_v doesn't see it
    "v",           # bare name next to a real one
])
def test_ambiguous_or_malformed_generation_is_refused(client, db_session, query):
    """Only a spelling nginx's $arg_v and the app read identically may be
    served: anything else could park one generation's bytes under another
    generation's cache key for 30 days."""
    s = _slide(db_session, generation=4)
    if query == "v":
        query = "v&v=4"
    with _fake_pyramid():
        for path in _paths(s.public_id):
            r = client.get(f"{path}?{query}")
            assert r.status_code == 404, (path, query)
            assert _no_store(r), (path, query)


def test_generation_arg_matches_nginx_spelling_rules(client, db_session):
    """nginx matches $arg_v case-insensitively and ignores other args — the
    cache key then is ``?v=4`` for both, so serving them is consistent."""
    s = _slide(db_session, generation=4)
    with _fake_pyramid():
        for path in _paths(s.public_id):
            for query in ("V=4", "r=x&v=4", "v=4&r=x"):
                r = client.get(f"{path}?{query}")
                assert r.status_code == 200, (path, query)
                assert not _no_store(r), (path, query)


def test_draft_with_stale_generation_still_looks_missing(client, db_session):
    """The publish gate runs before the generation check, so for a caller who
    may not see the draft every answer matches a missing slide's (no
    don't-store marker that would reveal the token exists)."""
    s = _slide(db_session, published=False, generation=4)
    with _fake_pyramid():
        for path in _paths(s.public_id):
            for suffix in ("?v=4", "?v=3", ""):
                r = client.get(f"{path}{suffix}")
                assert r.status_code == 404, (path, suffix)
                assert not _no_store(r), (path, suffix)
        missing = client.get("/dzi/NoSuchSlide1.dzi?v=3")
    assert missing.status_code == 404 and not _no_store(missing)


def test_draft_preview_checks_generation_too(client, db_session, admin_headers):
    s = _slide(db_session, published=False, generation=4)
    with _fake_pyramid():
        assert client.get(f"/dzi/{s.public_id}.dzi?v=4", headers=admin_headers).status_code == 200
        r = client.get(f"/dzi/{s.public_id}.dzi?v=3", headers=admin_headers)
    assert r.status_code == 404 and _no_store(r)


def test_newer_generation_than_cached_rereads_the_row(client, db_session, monkeypatch):
    """Another worker committed a replace this worker has not heard about yet
    (its sentinel check is throttled): a viewer already on the new v must be
    served, not 404'd, and the old v must turn stale from then on."""
    monkeypatch.setattr(
        slide_resolve_cache, "settings",
        SimpleNamespace(
            data_dir=settings.data_dir,
            slide_meta_cache_ttl=300,
            slide_meta_gen_check_interval=3600,  # never re-check during the test
            slide_meta_cache_max=4096,
        ),
    )
    slide_resolve_cache._reset_for_tests()
    s = _slide(db_session, generation=1)
    with _fake_pyramid():
        assert client.get(f"/dzi/{s.public_id}.dzi?v=1").status_code == 200  # cached: gen 1
        # The other worker's replace: the row moves, no local invalidation.
        db_session.execute(update(Slide).where(Slide.id == s.id).values(wsi_generation=2))
        db_session.commit()
        assert client.get(f"/dzi/{s.public_id}_files/3/1_2.jpeg?v=2").status_code == 200
        assert client.get(f"/dzi/{s.public_id}.dzi?v=2").status_code == 200
        assert client.get(f"/dzi/{s.public_id}.dzi?v=1").status_code == 404


def test_szi_archive_checks_generation(client, db_session):
    """szi-tile-source reads one archive in many Range requests; a stale v is a
    clean 404 instead of a replaced archive's bytes under the old directory."""
    name = f"gen-{os.urandom(3).hex()}.szi"
    path = os.path.join(settings.data_dir, "wsi", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("image.dzi", b'<Image TileSize="256" Overlap="0" Format="jpeg"><Size Width="8" Height="8"/></Image>')
    try:
        s = _slide(db_session, generation=6, wsi_filename=name)
        url = f"/dzi/{s.public_id}.szi"
        assert client.get(f"{url}?v=6", headers={"Range": "bytes=0-3"}).status_code == 206
        assert client.get(url).status_code == 200
        r = client.get(f"{url}?v=5", headers={"Range": "bytes=0-3"})
        assert r.status_code == 404 and _no_store(r)
        assert client.get(f"{url}?v=6&v=6").status_code == 404
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# Slide import: a kept bundle public_id starts in an unused generation
# ---------------------------------------------------------------------------

def _poll(client, job_id, headers, tries=300):
    rj = {}
    for _ in range(tries):
        rj = client.get(f"/api/admin/slides/transfer/jobs/{job_id}", headers=headers).json()
        if rj.get("state") in ("succeeded", "failed"):
            return rj
        time.sleep(0.02)
    return rj


def _export_bundle(client, headers, slide_id) -> tuple[bytes, str]:
    r = client.post("/api/admin/slides/export", json={"slide_ids": [slide_id]}, headers=headers)
    assert r.status_code == 200, r.text
    rj = _poll(client, r.json()["job_id"], headers)
    assert rj["state"] == "succeeded", rj
    res = rj["result"]
    u = client.post(f"/api/admin/slides/export/{res['export_id']}/download_url", headers=headers)
    assert u.status_code == 200, u.text
    dl = client.get(u.json()["url"])
    assert dl.status_code == 200
    return dl.content, res["archive_format"]


def _import_bundle(client, headers, content: bytes, archive_format: str) -> dict:
    st = client.post(
        "/api/admin/slides/import", headers=headers,
        files={"file": (f"bundle.{archive_format}", io.BytesIO(content), "application/octet-stream")},
    )
    assert st.status_code == 202, st.text
    staged = _poll(client, st.json()["job_id"], headers)
    assert staged["state"] == "succeeded", staged
    ra = client.post("/api/admin/slides/import/apply",
                     json={"stage_id": staged["result"]["stage_id"], "resolutions": {}}, headers=headers)
    assert ra.status_code == 200, ra.text
    rj = _poll(client, ra.json()["job_id"], headers)
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["imported_count"] == 1, rj
    return rj["result"]["imported"][0]


@pytest.fixture
def patched_import(monkeypatch):
    from wsi_app import slide_transfer as st

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image
        Image.new("RGB", (1, 1)).save(out_path, "JPEG")

    monkeypatch.setattr(st, "read_slide_properties", lambda p: {
        "width": 10, "height": 10, "mpp_x": 0.5, "mpp_y": 0.5, "vendor": "t", "acquired_at": "",
    })
    monkeypatch.setattr(st, "make_thumbnail", fake_thumbnail)
    monkeypatch.setattr(st, "attach_associated_images", lambda slide, path: None)


def _exportable_slide(db_session, attach_wsi, generation: int) -> Slide:
    s = Slide(title="Exp", catalog_id="EXP-1", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    base = f"exp-{os.urandom(3).hex()}.svs"
    attach_wsi(s, name=f"{s.id}_{base}")
    s.wsi_filename_norm = base.casefold()
    s.wsi_generation = generation
    db_session.commit()
    db_session.refresh(s)
    return s


def test_import_keeping_public_id_starts_in_an_unused_generation(
    client, db_session, admin_headers, attach_wsi, patched_import,
):
    """Export, delete, re-import: the bundle's token is free again and kept, but
    nginx may still hold the deleted incarnation's tiles under its low
    generations (here v=1..3) — a restart at 1 would serve them for the
    bundle's different bytes."""
    src = _exportable_slide(db_session, attach_wsi, generation=3)
    token = src.public_id
    content, fmt = _export_bundle(client, admin_headers, src.id)
    assert client.delete(f"/api/admin/slides/{src.id}", headers=admin_headers).status_code == 200

    before_ms = time.time_ns() // 1_000_000
    imported = _import_bundle(client, admin_headers, content, fmt)
    db_session.expire_all()
    new = db_session.get(Slide, imported["slide_id"])
    assert new.public_id == token
    assert new.wsi_generation >= before_ms  # far past any earlier generation
    detail = client.get(f"/api/admin/slides/{new.id}", headers=admin_headers).json()
    assert detail["tile_version"] == new.wsi_generation
    with _fake_pyramid():
        assert client.get(f"/dzi/{token}.dzi?v={new.wsi_generation}").status_code == 200
        for old in (1, 2, 3):
            assert client.get(f"/dzi/{token}.dzi?v={old}").status_code == 404


def test_import_with_reminted_public_id_starts_at_one(
    client, db_session, admin_headers, attach_wsi, patched_import,
):
    """The source still exists, so the import gets a brand-new token with no
    cache history: the plain default generation is safe."""
    src = _exportable_slide(db_session, attach_wsi, generation=3)
    content, fmt = _export_bundle(client, admin_headers, src.id)
    imported = _import_bundle(client, admin_headers, content, fmt)
    db_session.expire_all()
    new = db_session.get(Slide, imported["slide_id"])
    assert new.public_id != src.public_id
    assert new.wsi_generation == 1


# ---------------------------------------------------------------------------
# The swap window: bytes move before the row commits
# ---------------------------------------------------------------------------
class _FirstByteOsr:
    """Fake OpenSlide handle that remembers the file's first byte, so a tile's
    colour says which file served it."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.first = f.read(1)[0]

    def close(self):
        pass


class _FirstByteDZG:
    def __init__(self, osr, **kw):
        self.osr = osr

    def get_dzi(self, fmt):
        return f'<Image Format="{fmt}"/>'

    def get_tile(self, level, coord):
        from PIL import Image

        return Image.new("RGB", (4, 4), (self.osr.first * 200, 0, 0))


def _replace_probing_the_window(client, db_session, admin_headers, *, fail_commit):
    """Replace a published slide's WSI (same name) while a tile request with
    the pre-replace ``v`` lands between the on-disk swap and the commit."""
    from unittest.mock import patch

    from sqlalchemy import update as _update
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as _Session

    from fastapi.testclient import TestClient

    from wsi_app.main import app
    from wsi_app.routers import admin_slides

    name = _uniq("window")
    seen: dict = {}
    real_commit = _Session.commit

    def maybe_failing_commit(self):
        if seen.pop("fail_next_commit", False):
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        return real_commit(self)

    with _stub_openslide_for_replace(), \
            patch.object(dzi_module, "open_wsi", side_effect=_FirstByteOsr), \
            patch.object(dzi_module, "BoundedDeepZoomGenerator", _FirstByteDZG):
        slide_id = _upload(client, admin_headers, name)  # bytes start with \x00
        db_session.execute(_update(Slide).where(Slide.id == slide_id).values(is_published=True))
        db_session.commit()
        s = db_session.get(Slide, slide_id)
        old_v = s.wsi_generation
        url = f"/dzi/{s.public_id}_files/0/0_0.jpeg?v={old_v}"
        assert client.get(url).status_code == 200
        real_attach = admin_slides._attach_associated_images

        def probe(slide, wsi_path):
            r = TestClient(app).get(url)
            seen["status"] = r.status_code
            seen["no_store"] = _no_store(r)
            seen["fail_next_commit"] = fail_commit
            return real_attach(slide, wsi_path)

        with patch.object(admin_slides, "_attach_associated_images", side_effect=probe), \
                patch.object(_Session, "commit", maybe_failing_commit):
            r = client.post(
                f"/api/admin/slides/{slide_id}/wsi", headers=admin_headers,
                files={"file": (name, io.BytesIO(b"\x01" * 256), "application/octet-stream")},
            )
    db_session.expire_all()
    return r, old_v, db_session.get(Slide, slide_id).wsi_generation, seen


def test_the_old_generation_is_retired_before_the_bytes_move(client, db_session, admin_headers):
    """In the swap window the new bytes are on disk but the row is not
    committed yet. The pre-replace v must already be refused (uncacheably),
    or nginx could store the new file's tiles under the old generation."""
    r, old_v, new_v, seen = _replace_probing_the_window(
        client, db_session, admin_headers, fail_commit=False
    )
    assert r.status_code == 200, r.text
    assert new_v > old_v
    assert seen["status"] == 404 and seen["no_store"]


def test_a_failed_commit_does_not_make_the_old_generation_current_again(
    client, db_session, admin_headers
):
    """Rolled back to the old bytes, the slide must still be on a generation
    no swap-window response was ever served under."""
    r, old_v, new_v, seen = _replace_probing_the_window(
        client, db_session, admin_headers, fail_commit=True
    )
    assert r.status_code == 500
    assert seen["status"] == 404
    assert new_v > old_v


def test_a_handle_on_another_generations_bytes_is_refused(client, db_session):
    """The resolve cache can still call an old v current on another worker
    for up to its check interval while a replace swaps the file. A handle
    opened on the new bytes records the generation current after it opened —
    never the old one — so the old v is refused (uncacheably) there too."""
    s = _slide(db_session, generation=7)

    def newer_bytes(_slide_id):
        entry = dzi_module._DZEntry(
            osr=None, dz=_FakeDz(), etag='"e"', size=0, path="", generation=8,
        )
        entry.refs = 1
        return entry

    with patch.object(dzi_module, "_get_dz", side_effect=newer_bytes):
        r = client.get(f"/dzi/{s.public_id}_files/0/0_0.jpeg?v=7")
    assert r.status_code == 404
    assert _no_store(r)


def test_a_handle_older_than_the_requested_generation_is_reopened(client, db_session):
    """A replace that rolled back restores the old file (same mtime, so its
    handle looks fresh) under a newer generation; the handle must be reopened
    to learn it rather than refuse the now-current v forever."""
    s = _slide(db_session, generation=9)
    handles = iter([8, 9])
    cleared = []

    def reopen(_slide_id):
        entry = dzi_module._DZEntry(
            osr=None, dz=_FakeDz(), etag='"e"', size=0, path="", generation=next(handles),
        )
        entry.refs = 1
        return entry

    with patch.object(dzi_module, "_get_dz", side_effect=reopen), \
            patch.object(dzi_module, "clear_dz_cache", side_effect=cleared.append):
        r = client.get(f"/dzi/{s.public_id}_files/0/0_0.jpeg?v=9")
    assert r.status_code == 200
    assert cleared == [s.id]


# ---------------------------------------------------------------------------
# A replace landing while a worker opens the slide
# ---------------------------------------------------------------------------
class _ClosingOsr:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _real_slide(db_session, *, generation, shade):
    """A published slide backed by a real (tiny, pyramid-less) tiled TIFF
    whose every pixel is ``shade``, so a tile says which file served it."""
    name = f"{os.urandom(4).hex()}_race.tif"
    s = _slide(db_session, generation=generation, wsi_filename=name)
    path = os.path.join(settings.data_dir, "wsi", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_shade(path, shade)
    return s, path


def _write_shade(path, shade):
    import numpy as np
    import tifffile

    tmp = f"{path}.tmp-{os.urandom(3).hex()}"
    tifffile.imwrite(
        tmp, np.full((64, 64, 3), shade, np.uint8), tile=(16, 16), photometric="rgb", compression="zlib",
    )
    os.replace(tmp, path)


def _shade(entry):
    tile = entry.dz.get_tile(entry.dz.level_count - 1, (0, 0))
    try:
        return tile.getpixel((0, 0))[0]
    finally:
        tile.close()


def test_a_replace_during_the_open_never_tags_the_old_file_with_the_new_generation(db_session):
    """_open_dz reads the generation after opening the file. An open that
    stalls across a whole replace (swap + commit) must not record the NEW
    generation for its handle on the OLD file, or the current ?v= would serve
    — and nginx store for 30 days — the old bytes."""
    s, path = _real_slide(db_session, generation=5, shade=10)
    real_open = dzi_module.open_wsi
    stalled = []

    def open_across_a_replace(p):
        osr = real_open(p)
        if not stalled:
            stalled.append(osr)
            # The whole replace lands while this open is in flight.
            _write_shade(path, 200)
            db_session.execute(update(Slide).where(Slide.id == s.id).values(wsi_generation=7))
            db_session.commit()
        return osr

    with patch.object(dzi_module, "open_wsi", side_effect=open_across_a_replace):
        entry = dzi_module._open_dz(s.id)
    try:
        assert entry.generation == 7
        assert _shade(entry) == 200, "generation 7 must be served generation 7's bytes"
        assert entry.etag == dzi_module._dz_etag(s.id, os.stat(path))
    finally:
        entry.osr.close()


def test_a_replace_rolled_back_during_the_open_is_not_missed(db_session):
    """A replace whose commit fails puts the old file back by rename — the
    same inode, mtime and size — so a stat before and after the open can't
    see that the open landed on the new file in between. Every swap is
    bracketed by a generation bump, so a changed generation catches it: the
    handle on the swapped-in file must not be tagged with the rollback's
    generation, whose ?v= nginx would then store the wrong bytes under."""
    name = f"{os.urandom(4).hex()}_rollback.svs"
    s = _slide(db_session, generation=5, wsi_filename=name)
    path = os.path.join(settings.data_dir, "wsi", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 64)
    parked = f"{path}.rollback-test"
    swapped = []

    def open_during_a_failed_replace(p):
        if swapped:
            return _FirstByteOsr(p)
        swapped.append(True)
        # The replace retires the generation, parks the old file and swaps
        # the new one in; the open lands on it.
        db_session.execute(update(Slide).where(Slide.id == s.id).values(wsi_generation=6))
        db_session.commit()
        os.rename(path, parked)
        with open(path, "wb") as f:
            f.write(b"\x01" * 64)
        osr = _FirstByteOsr(p)
        # Its commit fails: the old file is renamed back and the
        # generation moved past the interim one.
        os.replace(parked, path)
        db_session.execute(update(Slide).where(Slide.id == s.id).values(wsi_generation=7))
        db_session.commit()
        return osr

    with patch.object(dzi_module, "open_wsi", side_effect=open_during_a_failed_replace), \
            patch.object(dzi_module, "BoundedDeepZoomGenerator", _FirstByteDZG):
        entry = dzi_module._open_dz(s.id)
    assert entry.generation == 7
    assert entry.osr.first == 0, "generation 7 must be served the restored file's bytes"
    assert len(swapped) == 1


def test_an_entry_cached_by_a_racing_opener_is_checked_before_it_is_shared(db_session):
    """Two cold requests open the slide at once and the slower one finds the
    faster one's entry already cached. That entry is handed out only if its
    file is still the one on disk: one opened before a replace swapped the
    file must not serve the requests that opened the new one."""
    s, path = _real_slide(db_session, generation=3, shade=200)
    dzi_module.clear_dz_cache(s.id)
    stale_osr = _ClosingOsr()
    stale = dzi_module._DZEntry(
        osr=stale_osr, dz=_FakeDz(), etag='"old-file"', size=1, path=path, generation=3,
    )
    real_open_dz = dzi_module._open_dz

    def open_while_another_thread_caches_the_old_file(slide_id):
        opened = real_open_dz(slide_id)
        with dzi_module._dz_lock:
            dzi_module._dz_cache[slide_id] = stale
        return opened

    with patch.object(dzi_module, "_open_dz", side_effect=open_while_another_thread_caches_the_old_file):
        entry = dzi_module._get_dz(s.id)
    try:
        assert entry is not stale
        assert _shade(entry) == 200
        with dzi_module._dz_lock:
            assert dzi_module._dz_cache.get(s.id) is entry
        assert stale.closing and stale_osr.closed == 1
    finally:
        dzi_module._release(entry)
        dzi_module.clear_dz_cache(s.id)
