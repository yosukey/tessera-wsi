"""End-to-end checks for the GET /dzi/{slide_id}.szi route.

The route exists so the szi-tile-source frontend library can read tiles via
HTTP Range. The Range / 206 contract is verified, along with the extension
gate (a non-szi slide must not leak through this path) and the 304 ETag round-trip.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wsi_app.config import settings
from wsi_app.models import Slide


def _make_szi(path: Path, payload: bytes = b"hello world") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "image.dzi",
            (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<Image TileSize="256" Overlap="0" Format="jpeg">'
                b'<Size Width="32" Height="32"/></Image>'
            ),
        )
        zf.writestr("image_files/0/0_0.jpeg", payload)


@pytest.fixture
def szi_slide(db_session) -> Slide:
    fname = "test-szi.szi"
    wsi_dir = Path(settings.data_dir) / "wsi"
    _make_szi(wsi_dir / fname)
    s = Slide(
        title="SZI test",
        catalog_id="SZI-001",
        wsi_filename=fname,
        width=32,
        height=32,
        mpp_x=0.0,
        mpp_y=0.0,
        vendor="szi",
        is_published=True,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    yield s
    try:
        os.remove(wsi_dir / fname)
    except OSError:
        pass


def test_szi_endpoint_serves_full_archive(client: TestClient, szi_slide: Slide) -> None:
    r = client.get(f"/dzi/{szi_slide.public_id}.szi")
    assert r.status_code == 200
    assert r.headers.get("accept-ranges") == "bytes"
    assert r.headers.get("etag", "").startswith(f'"{szi_slide.id}-')
    # Response body should be a valid ZIP.
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "image.dzi" in zf.namelist()


def test_szi_endpoint_honours_range(client: TestClient, szi_slide: Slide) -> None:
    r = client.get(f"/dzi/{szi_slide.public_id}.szi", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.headers.get("content-range", "").startswith("bytes 0-3/")
    assert len(r.content) == 4


def test_szi_endpoint_emits_304(client: TestClient, szi_slide: Slide) -> None:
    first = client.get(f"/dzi/{szi_slide.public_id}.szi")
    etag = first.headers["etag"]
    second = client.get(f"/dzi/{szi_slide.public_id}.szi", headers={"If-None-Match": etag})
    assert second.status_code == 304


def test_szi_endpoint_does_not_short_circuit_range_with_if_none_match(
    client: TestClient, szi_slide: Slide
) -> None:
    # szi-tile-source reads the archive piecewise via Range; if the browser
    # has cached the ETag and sends both Range + If-None-Match, returning a
    # bare 304 (empty body) hands back nothing for the requested partial.
    # Verify the route degrades to a normal 206 instead.
    first = client.get(f"/dzi/{szi_slide.public_id}.szi")
    etag = first.headers["etag"]
    second = client.get(
        f"/dzi/{szi_slide.public_id}.szi",
        headers={"If-None-Match": etag, "Range": "bytes=0-3"},
    )
    assert second.status_code == 206
    assert len(second.content) == 4


def test_szi_endpoint_advertises_no_cache(client: TestClient, szi_slide: Slide) -> None:
    # Match the rest of the /dzi/ family: rely on ETag revalidation rather
    # than browser heuristic freshness so a replaced archive is never served
    # stale from the client cache.
    r = client.get(f"/dzi/{szi_slide.public_id}.szi")
    assert r.headers.get("cache-control") == "no-cache"


def test_szi_endpoint_rejects_non_szi_slide(client: TestClient, db_session) -> None:
    # A slide whose filename is .svs must not be served via this endpoint;
    # the existing /dzi/{id}.dzi pipeline owns that case.
    s = Slide(
        title="DZI",
        catalog_id="DZI-001",
        wsi_filename="something.svs",
        is_published=True,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    r = client.get(f"/dzi/{s.public_id}.szi")
    assert r.status_code == 404


def test_szi_endpoint_404_for_missing_slide(client: TestClient) -> None:
    r = client.get("/dzi/999999.szi")
    assert r.status_code == 404


def test_szi_endpoint_hides_unpublished_from_anonymous(
    client: TestClient, szi_slide: Slide, db_session
) -> None:
    # Mirror the /api/slides/{id} contract: a draft must look identical to a
    # missing one, so an anonymous probe with a guessed id can't pull the
    # archive bytes.
    szi_slide.is_published = False
    db_session.commit()
    r = client.get(f"/dzi/{szi_slide.public_id}.szi")
    assert r.status_code == 404


def test_szi_endpoint_serves_unpublished_to_admin(
    client: TestClient, szi_slide: Slide, admin_headers, db_session
) -> None:
    # Admin previews ride through with the admin Bearer token (the frontend
    # passes it via szi-tile-source's fetchOptions.headers).
    szi_slide.is_published = False
    db_session.commit()
    r = client.get(f"/dzi/{szi_slide.public_id}.szi", headers=admin_headers)
    assert r.status_code == 200


def _swap_in_new_archive(slide: Slide, payload: bytes, *, bump_generation: bool) -> None:
    """What a WSI replace does to a slide, minus the upload: move it off its
    generation (committed first) and swap a new archive onto its path."""
    from sqlalchemy import update

    from wsi_app.db import SessionLocal

    if bump_generation:
        db = SessionLocal()
        try:
            db.execute(
                update(Slide).where(Slide.id == slide.id)
                .values(wsi_generation=Slide.wsi_generation + 1)
            )
            db.commit()
        finally:
            db.close()
    path = Path(settings.data_dir) / "wsi" / slide.wsi_filename
    staged = path.with_name(path.name + ".new")
    _make_szi(staged, payload)
    os.replace(staged, path)


def test_szi_replace_before_open_refuses_the_old_generation(
    client: TestClient, szi_slide: Slide, monkeypatch
) -> None:
    """A versioned range read must not get the new archive's bytes when a
    replace lands between the generation check and the file open."""
    from wsi_app import dzi

    gen = szi_slide.wsi_generation
    real_slide_path = dzi._slide_path

    def slide_path_then_replace(slide):
        p = real_slide_path(slide)
        _swap_in_new_archive(szi_slide, b"new bytes " * 50, bump_generation=True)
        return p

    monkeypatch.setattr(dzi, "_slide_path", slide_path_then_replace)
    r = client.get(f"/dzi/{szi_slide.public_id}.szi?v={gen}", headers={"Range": "bytes=0-3"})
    assert r.status_code == 404


def test_szi_replace_after_open_keeps_serving_the_opened_archive(
    client: TestClient, szi_slide: Slide, monkeypatch
) -> None:
    """A swap after the handler opened the file must not reach the response:
    the whole body (and its length) is the archive the checks were made on."""
    from wsi_app import dzi

    path = Path(settings.data_dir) / "wsi" / szi_slide.wsi_filename
    original = path.read_bytes()
    real_etag = dzi._file_etag

    def etag_then_replace(slide_id, st):
        _swap_in_new_archive(szi_slide, b"new bytes " * 50, bump_generation=False)
        return real_etag(slide_id, st)

    monkeypatch.setattr(dzi, "_file_etag", etag_then_replace)
    r = client.get(f"/dzi/{szi_slide.public_id}.szi?v={szi_slide.wsi_generation}")
    assert r.status_code == 200
    assert r.content == original
    assert path.read_bytes() != original


def _record_size(db_session, slide: Slide) -> int:
    path = Path(settings.data_dir) / "wsi" / slide.wsi_filename
    slide.file_size_bytes = path.stat().st_size
    db_session.commit()
    return slide.file_size_bytes


def test_szi_interim_generation_serves_the_archive_the_row_describes(
    client: TestClient, szi_slide: Slide, db_session
) -> None:
    """While a replace holds its interim generation, the row still describes
    the old archive: that one is served, and the new one swapped onto the path
    is not, so range reads under that generation never mix the two."""
    _record_size(db_session, szi_slide)
    original = (Path(settings.data_dir) / "wsi" / szi_slide.wsi_filename).read_bytes()

    # The replace has moved to its interim generation; the swap is still ahead.
    db_session.expire_all()
    row = db_session.get(Slide, szi_slide.id)
    row.wsi_generation += 1
    db_session.commit()
    interim = row.wsi_generation
    r = client.get(f"/dzi/{szi_slide.public_id}.szi?v={interim}")
    assert r.status_code == 200 and r.content == original

    _swap_in_new_archive(szi_slide, b"new bytes " * 50, bump_generation=False)
    r = client.get(f"/dzi/{szi_slide.public_id}.szi?v={interim}", headers={"Range": "bytes=0-3"})
    assert r.status_code == 404
    assert r.headers.get("x-accel-expires") == "0"


def test_szi_serves_once_the_row_describes_the_new_archive(
    client: TestClient, szi_slide: Slide, db_session
) -> None:
    _record_size(db_session, szi_slide)
    _swap_in_new_archive(szi_slide, b"new bytes " * 50, bump_generation=True)
    new_size = _record_size(db_session, szi_slide)
    db_session.expire_all()
    gen = db_session.get(Slide, szi_slide.id).wsi_generation
    r = client.get(f"/dzi/{szi_slide.public_id}.szi?v={gen}")
    assert r.status_code == 200 and len(r.content) == new_size


def test_open_file_response_reads_its_own_fd_and_closes_it(tmp_path: Path) -> None:
    """Even on a server offering http.response.pathsend, the body goes out
    through the fd the handler opened (the /dev/fd path would be re-opened by
    the server after that fd is closed), and the fd is closed afterwards."""
    import anyio

    from wsi_app import dzi

    p = tmp_path / "a.szi"
    p.write_bytes(b"archive bytes")
    fd = os.open(p, os.O_RDONLY)
    response = dzi._OpenFileResponse(fd, os.fstat(fd), media_type="application/zip")
    sent: list = []

    async def send(message) -> None:
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": "http", "method": "GET", "headers": [], "extensions": {"http.response.pathsend": {}}}
    anyio.run(response, scope, receive, send)
    assert not any(m["type"] == "http.response.pathsend" for m in sent)
    assert b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body") == b"archive bytes"
    with pytest.raises(OSError):
        os.fstat(fd)
