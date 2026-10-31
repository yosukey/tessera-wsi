"""Slide upload / replace refuse up front when the data volume cannot hold them,
and map a write failure (ENOSPC or otherwise) to a localisable error.

Export, import and backup check free space too; without the check the upload
paths would spool and copy gigabytes before finding out the disk was full, and
upload and replace both answer ``uploadFailed`` rather than a bare 500.
"""
from __future__ import annotations

import errno
import io
from collections import namedtuple

import pytest

from wsi_app import admin_helpers, disk_space
from wsi_app import main as app_main
from wsi_app.auth import create_token
from wsi_app.models import Slide
from wsi_app.routers import admin_slides

from .test_upload_preauth import _asgi_post

_Usage = namedtuple("_Usage", "total used free")
_NO_SPACE = "adminDash.apiErr.uploadNoSpace"


def _disk_free(monkeypatch, free: int) -> None:
    monkeypatch.setattr(
        disk_space.shutil, "disk_usage", lambda p: _Usage(free * 4, free * 3, free)
    )


def _files(size: int = 512):
    return {"file": ("scan.svs", io.BytesIO(b"\x00" * size), "application/octet-stream")}


@pytest.mark.parametrize("path", ["/api/admin/slides/upload", "/api/admin/slides/7/wsi"])
def test_declared_size_beyond_free_space_is_refused_before_the_body_is_read(
    admin_user, monkeypatch, path
):
    _disk_free(monkeypatch, 1024 * 1024 * 1024)  # 1 GiB free
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {create_token(admin_user)}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", str(768 * 1024 * 1024).encode()),  # needs 2x = 1.5 GiB
    ]
    status, body, receives = _asgi_post(path, headers, [b"--xyz\r\n"])
    assert status == 507, body
    assert body["detail"]["code"] == _NO_SPACE
    assert receives == 0


def test_declared_size_that_fits_with_headroom_is_let_through(admin_user, monkeypatch):
    _disk_free(monkeypatch, 1024 * 1024 * 1024)
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {create_token(admin_user)}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", str(256 * 1024 * 1024).encode()),
    ]
    status, _body, receives = _asgi_post("/api/admin/slides/upload", headers, [b"--xyz--\r\n"])
    assert status is not None and status != 507 and receives > 0


def test_upload_endpoint_checks_space_for_a_body_without_content_length(
    client, admin_headers, db_session, monkeypatch
):
    """Chunked bodies carry no Content-Length for the middleware; once spooled
    the endpoint knows the size and still refuses before reserving a slide."""
    monkeypatch.setattr(app_main, "_upload_no_space", lambda *a: False)
    _disk_free(monkeypatch, 100)
    before = db_session.query(Slide).count()
    r = client.post("/api/admin/slides/upload", headers=admin_headers, files=_files(512))
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == _NO_SPACE
    db_session.expire_all()
    assert db_session.query(Slide).count() == before


def test_replace_endpoint_checks_space(client, admin_headers, slide, monkeypatch):
    monkeypatch.setattr(app_main, "_upload_no_space", lambda *a: False)
    _disk_free(monkeypatch, 100)
    r = client.post(f"/api/admin/slides/{slide.id}/wsi", headers=admin_headers, files=_files(512))
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == _NO_SPACE


@pytest.mark.parametrize("err,status,code", [
    (OSError(errno.ENOSPC, "No space left on device"), 507, _NO_SPACE),
    (OSError(errno.EIO, "I/O error"), 500, "adminDash.apiErr.uploadFailed"),
])
def test_upload_and_replace_map_write_errors_the_same_way(
    client, admin_headers, db_session, slide, monkeypatch, err, status, code
):
    def boom(*a, **kw):
        raise err

    monkeypatch.setattr(admin_slides, "stream_upload_to_file", boom)
    before = db_session.query(Slide).count()

    r = client.post("/api/admin/slides/upload", headers=admin_headers, files=_files())
    assert r.status_code == status, r.text
    assert r.json()["detail"]["code"] == code
    db_session.expire_all()
    assert db_session.query(Slide).count() == before  # no ghost placeholder row

    r = client.post(f"/api/admin/slides/{slide.id}/wsi", headers=admin_headers, files=_files())
    assert r.status_code == status, r.text
    assert r.json()["detail"]["code"] == code


def test_filesystem_without_block_accounting_is_not_treated_as_full(monkeypatch):
    """A volume that reports total=0 (FUSE, /proc) is "unknown": refusing every
    upload on it would be worse than letting the write's own ENOSPC decide."""
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: _Usage(0, 0, 0))
    assert admin_helpers.upload_space_short(10 * 1024 * 1024) is False


def test_free_space_is_measured_where_the_wsi_directory_really_lives(tmp_path, monkeypatch):
    """data_dir/wsi may be a symlink to another volume (documented setup)."""
    data = tmp_path / "data"
    big = tmp_path / "bigdisk"
    data.mkdir()
    big.mkdir()
    (data / "wsi").symlink_to(big)
    seen = []

    def fake(path):
        seen.append(path)
        return _Usage(10**12, 0, 10**12)

    monkeypatch.setattr(disk_space.shutil, "disk_usage", fake)
    original = admin_helpers.settings.data_dir
    object.__setattr__(admin_helpers.settings, "data_dir", str(data))  # frozen dataclass
    try:
        assert admin_helpers.upload_space_short(1024) is False
    finally:
        object.__setattr__(admin_helpers.settings, "data_dir", original)
    assert seen == [str(big)]


def test_spool_volume_is_checked_when_wsi_lives_on_another_filesystem(tmp_path, monkeypatch):
    """With wsi/ symlinked to a big volume, the body is still spooled to TMPDIR on
    the (small) data volume: that volume must hold the body once, or the spool
    fails inside the multipart parser as an opaque 400."""
    import tempfile

    data = tmp_path / "data"
    big = tmp_path / "bigdisk"
    data.mkdir()
    big.mkdir()
    (data / "wsi").symlink_to(big)
    spool = tempfile.gettempdir()
    free = {str(big): 10**12, spool: 1024 * 1024 * 1024}  # 1 TB vs 1 GiB free

    monkeypatch.setattr(admin_helpers, "_same_filesystem", lambda a, b: False)
    monkeypatch.setattr(
        disk_space.shutil, "disk_usage",
        lambda p: _Usage(10**13, 0, free[str(p)]),
    )
    original = admin_helpers.settings.data_dir
    object.__setattr__(admin_helpers.settings, "data_dir", str(data))
    try:
        assert admin_helpers.upload_space_short(5 * 1024 ** 3) is True    # 5 GiB > 1 GiB spool
        assert admin_helpers.upload_space_short(512 * 1024 ** 2) is False  # fits both, 1x each
    finally:
        object.__setattr__(admin_helpers.settings, "data_dir", original)


@pytest.fixture
def split_volumes(tmp_path, monkeypatch):
    """data_dir/wsi symlinked to a volume of its own. The returned function
    sets each volume's free space and hands back the dict the fake
    disk_usage reads, so a test can spend spool space as Starlette would."""
    import tempfile

    data = tmp_path / "data"
    big = tmp_path / "bigdisk"
    data.mkdir()
    big.mkdir()
    (data / "wsi").symlink_to(big)
    free: dict = {}
    monkeypatch.setattr(admin_helpers, "_same_filesystem", lambda a, b: False)
    monkeypatch.setattr(
        disk_space.shutil, "disk_usage",
        lambda p: _Usage(10**13, 0, free[str(p)]),
    )
    original = admin_helpers.settings.data_dir
    object.__setattr__(admin_helpers.settings, "data_dir", str(data))  # frozen dataclass

    def _set(*, spool_free: int, wsi_free: int) -> dict:
        free.update({tempfile.gettempdir(): spool_free, str(big): wsi_free})
        return free

    yield _set
    object.__setattr__(admin_helpers.settings, "data_dir", original)


_GiB = 1024 ** 3


def test_already_spooled_upload_only_needs_room_for_the_staged_copy(split_volumes):
    """The endpoint's check runs after Starlette has spooled the body
    (factor=1): with wsi/ on its own volume only the copy to wsi/ is left, so
    the spool volume must not be asked for the body a second time, or an
    upload with size <= spool free < 2 x size would be refused 507 although
    it had already been received."""
    import tempfile

    free = split_volumes(spool_free=7 * _GiB, wsi_free=1000 * _GiB)
    assert admin_helpers.upload_space_short(5 * _GiB) is False  # pre-auth, factor=2
    free[tempfile.gettempdir()] -= 5 * _GiB  # the body is spooled: 2 GiB left there
    assert admin_helpers.upload_space_short(5 * _GiB, factor=1) is False


@pytest.mark.parametrize("factor,spool_free,wsi_free,short", [
    # Before the body is read (factor=2): the spool must take it once, wsi/ once.
    (2, 4, 1000, True),
    (2, 6, 1000, False),
    (2, 1000, 4, True),
    # Once it is spooled (factor=1): only the copy to wsi/ is left.
    (1, 0, 1000, False),
    (1, 1000, 4, True),
    (1, 1000, 6, False),
])
def test_split_volume_space_rule(split_volumes, factor, spool_free, wsi_free, short):
    """A 5 GiB upload against the free space (GiB) of each volume."""
    split_volumes(spool_free=spool_free * _GiB, wsi_free=wsi_free * _GiB)
    assert admin_helpers.upload_space_short(5 * _GiB, factor=factor) is short


# A restore / slide-import archive is spooled to TMPDIR — the data volume under
# systemd — before anything else runs. Those routes must check the space too
# (the spool is not on the backup volume), or a body larger than the data
# volume's free space would fill it before being refused.
_ARCHIVE_ROUTES = ["/api/admin/restore", "/api/admin/slides/import"]


def _archive_headers(admin_user, size: int) -> list:
    return [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {create_token(admin_user)}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", str(size).encode()),
    ]


@pytest.mark.parametrize("path", _ARCHIVE_ROUTES)
def test_archive_upload_beyond_the_spool_space_is_refused_before_the_body_is_read(
    admin_user, monkeypatch, path
):
    _disk_free(monkeypatch, 1024 * 1024 * 1024)  # 1 GiB free
    # 900 MiB fits, but not with the 256 MiB reserve left free.
    status, body, receives = _asgi_post(path, _archive_headers(admin_user, 900 * 1024 * 1024), [b"--xyz\r\n"])
    assert status == 507, body
    assert body["detail"]["code"] == _NO_SPACE
    assert receives == 0


@pytest.mark.parametrize("path", _ARCHIVE_ROUTES)
def test_archive_upload_that_fits_the_spool_is_let_through(admin_user, monkeypatch, path):
    _disk_free(monkeypatch, 1024 * 1024 * 1024)
    status, _body, receives = _asgi_post(path, _archive_headers(admin_user, 512 * 1024 * 1024), [b"--xyz--\r\n"])
    assert status is not None and status != 507 and receives > 0


def test_archive_space_rule_measures_the_spool_directory(monkeypatch):
    import tempfile

    seen = []

    def usage(p):
        seen.append(str(p))
        return _Usage(10**13, 0, 10**12)

    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    assert admin_helpers.archive_upload_space_short(5 * 1024 ** 3) is False
    assert seen == [tempfile.gettempdir()]
    assert admin_helpers.archive_upload_space_short(0) is False


# GET /api/admin/storage reports what the bulk-upload screen needs to predict
# upload_space_short's verdict for each file of a batch (a margin of the
# screen's own, such as 1 GiB on the data-dir figure, would match no server
# rule). These pin that the reported volumes and policy are the ones
# the check uses, by re-deriving its verdict from the response alone — the
# same arithmetic frontend/src/lib/admin/uploadSpace.ts does.


def _predicted_short(space: dict, size: int) -> bool:
    """upload_space_short(size) as the client computes it from ``space``."""

    def reserve(n: int) -> int:
        return max(int(max(0, n) * space["reserve_fraction"]), space["reserve_min_bytes"])

    def short(n: int, free) -> bool:
        return free is not None and n + reserve(n) > free

    copies = space["copies"]
    if space["same_volume"]:
        return short(copies * size, space["target_free_bytes"])
    return short(size, space["target_free_bytes"]) or (
        copies > 1 and short((copies - 1) * size, space["spool_free_bytes"])
    )


def _upload_space(client, headers) -> dict:
    r = client.get("/api/admin/storage", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # The top-level data-dir fields stay too.
    assert {"total_bytes", "free_bytes", "used_bytes"} <= set(body)
    return body["upload_space"]


def test_storage_reports_the_slide_upload_cap(client, admin_headers):
    """The dashboard checks and names WSI_MAX_UPLOAD_BYTES as reported here,
    so a raised or lowered cap reaches it without a frontend rebuild."""
    from wsi_app.config import settings

    original = settings.wsi_max_upload_bytes
    object.__setattr__(settings, "wsi_max_upload_bytes", 16 * 1024 ** 3)
    try:
        r = client.get("/api/admin/storage", headers=admin_headers)
    finally:
        object.__setattr__(settings, "wsi_max_upload_bytes", original)
    assert r.status_code == 200, r.text
    assert r.json()["max_upload_bytes"] == 16 * 1024 ** 3


def test_storage_reports_the_shared_reserve_policy(client, admin_headers):
    """The numbers the client simulates with are disk_space's and
    upload_space_short's own, not copies that could drift."""
    import inspect

    space = _upload_space(client, admin_headers)
    assert space["reserve_fraction"] == disk_space.RESERVE_FRACTION
    assert space["reserve_min_bytes"] == disk_space.RESERVE_MIN_BYTES
    default_factor = inspect.signature(admin_helpers.upload_space_short).parameters["factor"].default
    assert space["copies"] == admin_helpers.SLIDE_UPLOAD_COPIES == default_factor


def test_storage_same_volume_figures_predict_the_upload_check(client, admin_headers, monkeypatch):
    monkeypatch.setattr(admin_helpers, "_same_filesystem", lambda a, b: True)
    size = 5 * _GiB
    # 2 x 5 GiB plus the reserve (10 % of 10 GiB = 1 GiB) is an exact fit.
    exact = 2 * size + disk_space.reserve_for(2 * size)
    assert exact == 11 * _GiB
    for free, short in ((exact, False), (exact - 1, True)):
        _disk_free(monkeypatch, free)
        space = _upload_space(client, admin_headers)
        assert space["same_volume"] is True
        assert space["target_free_bytes"] == space["spool_free_bytes"] == free
        assert _predicted_short(space, size) is short
        assert admin_helpers.upload_space_short(size) is short


def test_storage_split_volume_figures_predict_the_upload_check(client, admin_headers, split_volumes):
    """wsi/ symlinked to its own volume: the endpoint reports that volume and
    the spool's separately, measured where upload_space_short measures."""
    size = 5 * _GiB
    once = size + disk_space.reserve_for(size)  # each volume takes the body once
    cases = [
        (once, once, False),         # exact fit on both
        (once - 1, 1000 * _GiB, True),   # spool one byte short
        (1000 * _GiB, once - 1, True),   # wsi/ one byte short
        (once, 1000 * _GiB, False),  # a full 2x on the spool is not needed
    ]
    for spool_free, wsi_free, short in cases:
        split_volumes(spool_free=spool_free, wsi_free=wsi_free)
        space = _upload_space(client, admin_headers)
        assert space["same_volume"] is False
        assert space["target_free_bytes"] == wsi_free
        assert space["spool_free_bytes"] == spool_free
        assert _predicted_short(space, size) is short
        assert admin_helpers.upload_space_short(size) is short


def test_storage_reports_unknown_free_space_as_null(client, admin_headers, monkeypatch):
    """Unreadable or capacity-less volumes are "unknown" — the check passes on
    them, so the client must not block either."""

    def unreadable(p):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(disk_space.shutil, "disk_usage", unreadable)
    space = _upload_space(client, admin_headers)
    assert space["target_free_bytes"] is None
    assert space["spool_free_bytes"] is None
    assert _predicted_short(space, 100 * _GiB) is False
    assert admin_helpers.upload_space_short(100 * _GiB) is False

    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: _Usage(0, 0, 0))
    space = _upload_space(client, admin_headers)
    assert space["target_free_bytes"] is None and space["spool_free_bytes"] is None


def test_body_spool_running_out_of_disk_is_a_507_not_a_400(client, admin_headers, monkeypatch):
    """Starlette spools the multipart body before the endpoint runs and
    FastAPI reports any failure there as a 400 "error parsing the body"; a
    full disk while spooling must read as the localised no-space 507."""
    import errno as _errno
    import io as _io

    from starlette.datastructures import UploadFile as _StarletteUploadFile

    async def _enospc(self, data):
        raise OSError(_errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_StarletteUploadFile, "write", _enospc)
    r = client.post(
        "/api/admin/slides/upload",
        headers=admin_headers,
        files={"file": ("full.svs", _io.BytesIO(b"\x00" * 2048), "application/octet-stream")},
    )
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.uploadNoSpace"


def test_other_body_parse_errors_stay_400(client, admin_headers):
    r = client.post(
        "/api/admin/slides/upload",
        headers={**admin_headers, "Content-Type": "multipart/form-data; boundary=x"},
        content=b"--x\r\nnot a valid part",
    )
    assert r.status_code in (400, 422), r.text
