"""Tests for wsi_app.admin_helpers shared helpers.

get_or_create_singleton backs the singleton settings rows (ViewerSettings,
NetworkSettings, …). The concurrent-init path is the interesting one: two
sessions can both observe an empty table and race to insert id=1.
"""
from __future__ import annotations

from sqlalchemy import delete

from wsi_app.admin_helpers import get_or_create_singleton
from wsi_app.db import SessionLocal
from wsi_app.models import ViewerSettings


def test_get_or_create_singleton_handles_concurrent_insert():
    # Simulate the race: this session sees no row, but a *different* committed
    # session creates id=1 before this session's flush. The helper must recover by
    # re-reading the winner's row rather than propagating IntegrityError.
    cleaner = SessionLocal()
    try:
        cleaner.execute(delete(ViewerSettings))
        cleaner.commit()
    finally:
        cleaner.close()

    db = SessionLocal()
    other = SessionLocal()
    try:
        assert db.get(ViewerSettings, 1) is None
        # Winner commits id=1 out from under it, then this flush of the same id
        # would hit the PRIMARY KEY — the helper's SAVEPOINT + re-read recovers.
        other.add(ViewerSettings(id=1))
        other.commit()
        obj = get_or_create_singleton(db, ViewerSettings)
        assert obj is not None
        assert obj.id == 1
    finally:
        db.close()
        other.close()


# ---------------------------------------------------------------------------
# stream_upload_to_file: in-kernel copy of Starlette's spool file
# ---------------------------------------------------------------------------


def _spooled_upload(data: bytes):
    import tempfile

    from fastapi import UploadFile

    # What Starlette's multipart parser hands the endpoint: a spool file,
    # rolled to disk past 1 MiB, left at offset 0.
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    spool.write(data)
    spool.seek(0)
    return UploadFile(file=spool, filename="x.svs")


def test_upload_is_copied_in_kernel_from_the_spool(tmp_path, monkeypatch):
    import os

    import pytest

    from wsi_app import admin_helpers

    if not hasattr(os, "copy_file_range"):
        pytest.skip("no copy_file_range on this platform")
    data = os.urandom(3 * 1024 * 1024 + 17)
    calls = []
    real = os.copy_file_range
    monkeypatch.setattr(os, "copy_file_range", lambda *a: calls.append(a) or real(*a))
    upload = _spooled_upload(data)
    # Python-level reads would mean the second copy went through userspace.
    monkeypatch.setattr(upload.file, "read", lambda *a: pytest.fail("chunked copy used"))
    dst = tmp_path / "out.svs"
    assert admin_helpers.stream_upload_to_file(upload, str(dst)) == len(data)
    assert dst.read_bytes() == data and calls


def test_oversized_spool_is_refused_before_copying(tmp_path, monkeypatch):
    import os

    import pytest
    from fastapi import HTTPException

    from wsi_app import admin_helpers
    from wsi_app.config import settings

    import dataclasses

    monkeypatch.setattr(admin_helpers, "settings", dataclasses.replace(settings, wsi_max_upload_bytes=1024))
    monkeypatch.setattr(os, "copy_file_range", lambda *a: pytest.fail("copied"), raising=False)
    dst = tmp_path / "big.svs"
    with pytest.raises(HTTPException) as excinfo:
        admin_helpers.stream_upload_to_file(_spooled_upload(b"x" * 4096), str(dst))
    assert excinfo.value.status_code == 413
    assert not dst.exists()


def test_upload_copy_falls_back_when_the_kernel_refuses(tmp_path, monkeypatch):
    import errno
    import io
    import os

    from fastapi import UploadFile

    from wsi_app import admin_helpers

    data = os.urandom(2 * 1024 * 1024 + 5)
    state = {"n": 0}
    real = getattr(os, "copy_file_range", None)

    def flaky(src, dst, count, offset_src, offset_dst):
        # Copy part of it, then fail the way a cross-filesystem spool does.
        state["n"] += 1
        if state["n"] == 1 and real is not None:
            return real(src, dst, min(count, 4096), offset_src, offset_dst)
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "copy_file_range", flaky, raising=False)
    dst = tmp_path / "fallback.svs"
    assert admin_helpers.stream_upload_to_file(_spooled_upload(data), str(dst)) == len(data)
    assert dst.read_bytes() == data
    # A stream with no file descriptor goes straight to the chunked copy.
    plain = tmp_path / "plain.svs"
    upload = UploadFile(file=io.BytesIO(b"abc"), filename="p.svs")
    assert admin_helpers.stream_upload_to_file(upload, str(plain)) == 3
    assert plain.read_bytes() == b"abc"


def test_small_in_memory_upload_skips_the_fast_path(tmp_path, monkeypatch):
    """A spool still held in memory must not be rolled to TMPDIR by the fast
    path: if that write fails (TMPDIR full) SpooledTemporaryFile has already
    swapped the bytes for a truncated file, which the fallback would then
    copy as if complete. The chunked copy reads the in-memory bytes instead."""
    import errno
    import os
    import tempfile

    from wsi_app import admin_helpers

    real_tempfile = tempfile.TemporaryFile

    class _FullDisk:
        # SpooledTemporaryFile.rollover() installs the new file BEFORE copying
        # the buffered bytes into it; the copy then hits a full TMPDIR.
        def __init__(self, *a, **k):
            self._f = real_tempfile(*a, **k)

        def write(self, _b):
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name):
            return getattr(self._f, name)

    monkeypatch.setattr(tempfile, "TemporaryFile", _FullDisk)
    data = os.urandom(300_000)
    upload = _spooled_upload(data)
    assert upload.file._rolled is False
    dst = tmp_path / "small.svs"
    assert admin_helpers.stream_upload_to_file(upload, str(dst)) == len(data)
    assert dst.read_bytes() == data
