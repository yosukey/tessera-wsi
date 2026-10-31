"""The shared free-space rule (wsi_app.disk_space) and the call sites that use it.

Every large write — backup, export, restore / import staging and extraction,
reset staging, slide uploads, restore / import upload spools, the copy of an
imported slide's WSI — asks disk_space the same question: will N bytes fit at
this path and still leave the install ``reserve_for(N)``? The second half of
this module pins that each path really goes through it, by swapping the policy
and watching every path's decision follow.
"""
from __future__ import annotations

import dataclasses
import os
import tarfile
from collections import namedtuple

import pytest

from wsi_app import admin_helpers, disk_space
from wsi_app import backup as _backup
from wsi_app import main as app_main
from wsi_app import slide_transfer as _st
from wsi_app.config import settings
from wsi_app.routers import admin_tours

_Usage = namedtuple("_Usage", "total used free")
_MIN = disk_space.RESERVE_MIN_BYTES


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def test_reserve_is_a_tenth_with_a_256_mib_floor():
    assert _MIN == 256 * 1024 * 1024
    assert disk_space.RESERVE_FRACTION == 0.1
    assert disk_space.reserve_for(0) == _MIN
    assert disk_space.reserve_for(-5) == _MIN  # never below the floor
    assert disk_space.reserve_for(1) == _MIN
    # The crossover: below 10x the floor the floor wins, above it the 10 %.
    assert disk_space.reserve_for(10 * _MIN - 10) == _MIN
    assert disk_space.reserve_for(10 * _MIN) == _MIN
    assert disk_space.reserve_for(10 * _MIN + 100) == _MIN + 10
    assert disk_space.reserve_for(1000 * _MIN) == 100 * _MIN


def test_shortfall_boundary_is_inclusive():
    n = 1000
    fits = n + disk_space.reserve_for(n)
    assert disk_space.shortfall_given(n, fits) is None
    short = disk_space.shortfall_given(n, fits - 1, "/vol")
    assert short == disk_space.SpaceShortfall(
        path="/vol", needed=n, reserve=disk_space.reserve_for(n), free=fits - 1,
    )
    assert short.required == fits


def test_unknown_free_space_never_refuses():
    assert disk_space.shortfall_given(10**18, None) is None


def test_reserve_override_replaces_the_policy():
    assert disk_space.shortfall_given(100, 150, reserve=50) is None
    short = disk_space.shortfall_given(100, 149, reserve=50)
    assert short is not None and short.reserve == 50
    # 0 means "the bytes only".
    assert disk_space.shortfall_given(100, 100, reserve=0) is None


def test_a_zero_byte_write_still_needs_the_reserve():
    """Callers that mean "unknown size" by 0 skip the check themselves; the
    helper answers the question it is asked."""
    assert disk_space.shortfall_given(0, _MIN) is None
    assert disk_space.shortfall_given(0, _MIN - 1) is not None


def test_message_and_params_carry_every_figure():
    short = disk_space.SpaceShortfall(path="/data/backups", needed=123, reserve=456, free=78)
    msg = short.message("backup volume")
    assert msg.startswith("insufficient free space on backup volume:")
    for figure in ("123", "456", "78", "/data/backups"):
        assert figure in msg
    assert "under" not in disk_space.SpaceShortfall("", 1, 2, 0).message()
    assert short.as_params() == {"needed_bytes": 123, "free_bytes": 78, "reserve_bytes": 456}


def test_free_bytes_is_none_when_unknowable(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: _Usage(0, 0, 0))
    assert disk_space.free_bytes(str(tmp_path)) is None  # FUSE / pseudo fs
    assert disk_space.check(str(tmp_path), 10**18) is None

    def boom(p):
        raise OSError("stale NFS handle")

    monkeypatch.setattr(disk_space.shutil, "disk_usage", boom)
    assert disk_space.free_bytes(str(tmp_path)) is None
    assert disk_space.check(str(tmp_path), 10**18) is None

    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda p: _Usage(100, 40, 60))
    assert disk_space.free_bytes(str(tmp_path)) == 60


def test_check_measures_the_given_path(monkeypatch):
    seen = []

    def usage(p):
        seen.append(p)
        return _Usage(10**13, 0, 10)

    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    short = disk_space.check("/somewhere", 5)
    assert seen == ["/somewhere"]
    assert short is not None and short.path == "/somewhere" and short.free == 10


# ---------------------------------------------------------------------------
# Every call site uses the shared policy
# ---------------------------------------------------------------------------
#
# The volume reports 10 TB free, far more than any of these tests writes. With
# the reserve swapped for 0 each path must let its write through; with a
# reserve larger than the volume each must refuse. A path with a reserve rule
# of its own would not follow the swap.

_HUGE = 10**15


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setattr(
        disk_space.shutil, "disk_usage", lambda p: _Usage(10**14, 9 * 10**13, 10**13),
    )

    def _set(reserve: int) -> None:
        monkeypatch.setattr(disk_space, "reserve_for", lambda nbytes: reserve)

    return _set


def _plain_tar(path, members: dict) -> None:
    import io

    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_backup_preflight_uses_the_shared_policy(policy):
    policy(0)
    _backup._preflight_backup_free_space(_backup.SCOPE_METADATA)
    policy(_HUGE)
    with pytest.raises(RuntimeError, match="insufficient free space on backup volume"):
        _backup._preflight_backup_free_space(_backup.SCOPE_METADATA)


def test_extraction_uses_the_shared_policy(policy, tmp_path):
    archive = str(tmp_path / "a.tar")
    _plain_tar(archive, {"a.bin": b"a" * 100})
    policy(0)
    with tarfile.open(archive) as tar:
        _backup._safe_extract(tar, str(tmp_path / "ok"), free_bytes=100)
    policy(_HUGE)
    with tarfile.open(archive) as tar:
        with pytest.raises(RuntimeError, match="insufficient free space on staging volume"):
            _backup._safe_extract(tar, str(tmp_path / "no"), free_bytes=10**13)


def test_restore_staging_precheck_uses_the_shared_policy(policy, tmp_path):
    """The pre-check asks for the reserve the extraction keeps, not just the
    archive's size."""
    archive = tmp_path / "garbage.tar.gz"
    archive.write_bytes(b"not an archive" * 100)
    policy(_HUGE)
    with pytest.raises(RuntimeError, match="insufficient free space on staging volume"):
        _backup.stage_restore(str(archive))
    policy(0)
    with pytest.raises(RuntimeError) as ei:
        _backup.stage_restore(str(archive))
    assert "insufficient free space" not in str(ei.value)  # got past it: corrupt


def test_reset_staging_uses_the_shared_policy(policy):
    """Reset staging snapshots the whole live DB onto the staging volume, so
    it is checked against the policy too."""
    stage_id = _backup.new_stage_id()
    stage_dir = os.path.join(settings.backup_dir, ".staging", stage_id)
    policy(_HUGE)
    with pytest.raises(RuntimeError, match="insufficient free space on staging volume"):
        _backup.create_reset_stage(stage_id)
    assert not os.path.exists(stage_dir)  # nothing left behind
    policy(0)
    _backup._preflight_reset_free_space(stage_dir)


def test_slide_upload_uses_the_shared_policy(policy):
    """A slide upload needs the reserve as well, not only 2x its size."""
    policy(0)
    assert admin_helpers.upload_space_short(1000) is False
    assert admin_helpers.upload_space_short(1000, factor=1) is False
    policy(_HUGE)
    assert admin_helpers.upload_space_short(1000) is True
    assert admin_helpers.upload_space_short(1000, factor=1) is True


def test_slide_upload_on_split_volumes_uses_the_shared_policy(policy, monkeypatch):
    monkeypatch.setattr(admin_helpers, "_same_filesystem", lambda a, b: False)
    policy(0)
    assert admin_helpers.upload_space_short(1000) is False
    policy(_HUGE)
    assert admin_helpers.upload_space_short(1000) is True


def test_archive_upload_spool_uses_the_shared_policy(policy):
    policy(0)
    assert admin_helpers.archive_upload_space_short(1000) is False
    policy(_HUGE)
    assert admin_helpers.archive_upload_space_short(1000) is True


@pytest.mark.parametrize("route", app_main._UPLOAD_ROUTES, ids=lambda r: r.space_check)
def test_upload_preauth_uses_the_shared_policy(policy, route):
    policy(0)
    assert app_main._upload_no_space(route, 1000) is False
    policy(_HUGE)
    assert app_main._upload_no_space(route, 1000) is True


def test_export_preflight_uses_the_shared_policy(policy, monkeypatch, slide, attach_wsi):
    """The export asks for the shared reserve, not just 1.1x its WSI bytes."""
    attach_wsi(slide)

    class _PastPreflight(Exception):
        pass

    def stop(*a, **kw):
        raise _PastPreflight

    # The first thing export_slides does after the free-space preflight:
    # stop there, so the passing case writes no archive.
    monkeypatch.setattr(_st._backup, "_utc_iso_z", stop)
    policy(0)
    with pytest.raises(_PastPreflight):
        _st.export_slides([slide.id])
    policy(_HUGE)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.export_slides([slide.id])
    assert ei.value.code == "exportNoSpace"


def test_import_staging_precheck_uses_the_shared_policy(policy, tmp_path):
    archive = tmp_path / "garbage.tar.gz"
    archive.write_bytes(b"not an archive" * 100)
    policy(_HUGE)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.stage_import(str(archive))
    assert ei.value.code == "importNoSpace"
    policy(0)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st.stage_import(str(archive))
    assert ei.value.code == "importCorrupt"


def test_import_plan_and_wsi_copy_use_the_shared_policy(policy, monkeypatch, db_session, tmp_path):
    from .test_slide_transfer import _slide_doc, _stage_bundle

    src = tmp_path / "src.svs"
    src.write_bytes(b"x" * 1000)
    policy(0)
    _st._check_wsi_copy_space(str(src), str(tmp_path), "pid")
    policy(_HUGE)
    with pytest.raises(_st.SlideTransferError) as ei:
        _st._check_wsi_copy_space(str(src), str(tmp_path), "pid")
    assert ei.value.code == "importNoSpace"

    # The plan's data_space reports the same verdict, with the figures.
    monkeypatch.setattr(_st, "_same_filesystem", lambda a, b: False)
    t = os.urandom(3).hex()
    stage_id = _stage_bundle([_slide_doc(f"ds{t}", f"ds-{t}.svs", [])])
    try:
        _dir, manifest, doc = _st._read_stage(stage_id)
        policy(0)
        assert _st.plan_import(stage_id, manifest, doc, db_session)["data_space"] is None
        policy(_HUGE)
        space = _st.plan_import(stage_id, manifest, doc, db_session)["data_space"]
        assert space == {
            "needed_bytes": len(b"WSIDATA"), "free_bytes": 10**13, "reserve_bytes": _HUGE,
        }
    finally:
        _st.cleanup_import_stage(stage_id)


def test_tour_asset_floor_is_the_one_configured_reserve(policy, monkeypatch, tmp_path):
    """Tour assets keep their operator-set floor (TOUR_ASSET_MIN_FREE_BYTES)
    instead of the shared policy, but measure through disk_space."""
    monkeypatch.setattr(
        admin_tours, "settings",
        dataclasses.replace(settings, tour_asset_min_free_bytes=10**13 - 100),
    )
    policy(_HUGE)  # not consulted
    admin_tours._enforce_tour_asset_free_space(str(tmp_path), 100)
    with pytest.raises(Exception) as ei:
        admin_tours._enforce_tour_asset_free_space(str(tmp_path), 101)
    assert ei.value.status_code == 507
    assert ei.value.detail["code"] == "adminDash.apiErr.uploadNoSpace"
    # Unknown capacity passes, as everywhere.
    monkeypatch.setattr(disk_space, "free_bytes", lambda p: None)
    admin_tours._enforce_tour_asset_free_space(str(tmp_path), 10**18)
