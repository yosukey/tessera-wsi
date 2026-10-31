"""Leftovers of an upload / WSI replace that did not finish.

* A failed finalising commit must still drop the placeholder row (the session
  is rolled back before the cleanup touches it).
* The boot sweep (reap_interrupted_slide_swaps) settles the staged
  ``.upload-`` copies and the ``.rollback-`` parks a killed replace left.
"""
import io
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, exc, select

from wsi_app.admin_helpers import (
    reap_interrupted_slide_swaps,
    reap_orphan_slide_reservations,
    utcnow,
)
from wsi_app.config import settings
from wsi_app.db import engine
from wsi_app.models import Slide

from .test_upload_duplicate_filename import _stub_openslide, _upload


def _wsi_dir() -> str:
    d = os.path.join(settings.data_dir, "wsi")
    os.makedirs(d, exist_ok=True)
    return d


def _thumbs_dir() -> str:
    d = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _age(path: str, minutes: int) -> None:
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


@pytest.fixture
def make_slide(db_session):
    made = []

    def _make(name: str, data: bytes, thumb: str = "") -> Slide:
        s = Slide(title=name)
        db_session.add(s)
        db_session.commit()
        stored = f"{s.id}_{name}"
        _write(os.path.join(_wsi_dir(), stored), data)
        s.wsi_filename = stored
        s.wsi_filename_norm = name.casefold()
        s.file_size_bytes = len(data)
        s.thumb_filename = thumb or f"{s.id}-feedface.jpg"
        db_session.commit()
        _write(os.path.join(_thumbs_dir(), s.thumb_filename), b"old-thumb")
        made.append(s.id)
        return s

    yield _make
    db_session.rollback()
    db_session.execute(delete(Slide).where(Slide.id.in_(made)))
    db_session.commit()
    # Slide ids restart after the delete: leave nothing for the next test.
    for d in (_wsi_dir(), _thumbs_dir()):
        for fname in os.listdir(d):
            if any(fname.startswith((f"{i}_", f"{i}-", f"{i}.")) for i in made):
                os.remove(os.path.join(d, fname))


def test_failed_final_commit_drops_the_placeholder(client, admin_headers, db_session):
    """An OperationalError at the finalising commit (e.g. "database is
    locked") leaves the session needing a rollback; the cleanup must still
    delete the reserved row rather than strand an empty-filename slide."""
    from sqlalchemy import func

    before = db_session.execute(select(func.max(Slide.id))).scalar() or 0
    fired = {"n": 0}

    def _fail_finalise(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE SLIDES") and "wsi_filename" in statement:
            fired["n"] += 1
            raise exc.OperationalError(statement, params, Exception("database is locked"))

    event.listen(engine, "before_cursor_execute", _fail_finalise)
    try:
        with _stub_openslide():
            r = _upload(client, admin_headers, "commit-fails.svs")
    finally:
        event.remove(engine, "before_cursor_execute", _fail_finalise)
    assert fired["n"] >= 1
    assert r.status_code == 500, r.text
    db_session.expire_all()
    ghosts = db_session.execute(
        select(Slide.id).where(
            Slide.id > before,
            (Slide.wsi_filename.is_(None)) | (Slide.wsi_filename == ""),
        )
    ).all()
    assert ghosts == []
    assert not [f for f in os.listdir(_wsi_dir()) if f.endswith("_commit-fails.svs")]


def test_same_name_replace_killed_before_commit_is_rolled_back(db_session, make_slide):
    s = make_slide("same.svs", b"old-bytes")
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    thumb = os.path.join(_thumbs_dir(), s.thumb_filename)
    # The swap ran (old parked, new in place) but the commit never landed.
    os.replace(wsi, wsi + ".rollback-000000000001")
    _write(wsi, b"new-bytes-longer")
    os.replace(thumb, thumb + ".rollback-000000000001")
    _write(thumb, b"new-thumb")

    stats = reap_interrupted_slide_swaps(db_session)

    assert stats["parks_restored"] == 1
    with open(wsi, "rb") as fh:
        assert fh.read() == b"old-bytes"
    with open(thumb, "rb") as fh:
        assert fh.read() == b"old-thumb"
    assert not os.path.exists(wsi + ".rollback-000000000001")
    assert not os.path.exists(thumb + ".rollback-000000000001")


def test_different_name_replace_killed_before_commit_is_rolled_back(db_session, make_slide):
    s = make_slide("before.svs", b"old-bytes")
    old = os.path.join(_wsi_dir(), s.wsi_filename)
    new = os.path.join(_wsi_dir(), f"{s.id}_after.svs")
    # The park is named after the NEW destination.
    os.replace(old, new + ".rollback-000000000002")
    _write(new, b"new-bytes")

    stats = reap_interrupted_slide_swaps(db_session)

    assert stats["parks_restored"] == 1
    with open(old, "rb") as fh:
        assert fh.read() == b"old-bytes"
    assert not os.path.exists(new)
    assert not os.path.exists(new + ".rollback-000000000002")


def test_park_left_after_a_landed_commit_is_removed(db_session, make_slide):
    s = make_slide("landed.svs", b"new-bytes")
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    _write(wsi + ".rollback-000000000003", b"the-old-bytes")

    stats = reap_interrupted_slide_swaps(db_session)

    assert stats["parks_removed"] == 1
    assert not os.path.exists(wsi + ".rollback-000000000003")
    with open(wsi, "rb") as fh:
        assert fh.read() == b"new-bytes"


def test_ambiguous_park_is_left_alone(db_session, make_slide):
    s = make_slide("unclear.svs", b"12345")
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    _write(wsi, b"1234567")
    _write(wsi + ".rollback-000000000004", b"123456789")

    stats = reap_interrupted_slide_swaps(db_session)

    assert stats["parks_kept"] == 1
    assert os.path.exists(wsi + ".rollback-000000000004")


def test_staged_copies_are_removed_once_old(db_session, make_slide):
    s = make_slide("staged.svs", b"bytes")
    old_stage = os.path.join(_wsi_dir(), f"{s.id}_x.upload-aaaaaaaaaaaa.svs")
    fresh_stage = os.path.join(_wsi_dir(), f"{s.id}_y.upload-bbbbbbbbbbbb.svs")
    old_thumb_stage = os.path.join(_thumbs_dir(), f"{s.id}.upload-aaaaaaaaaaaa")
    for p in (old_stage, fresh_stage, old_thumb_stage):
        _write(p, b"x")
    _age(old_stage, 120)
    _age(old_thumb_stage, 120)

    stats = reap_interrupted_slide_swaps(db_session)

    assert stats["staged_removed"] == 2
    assert not os.path.exists(old_stage)
    assert not os.path.exists(old_thumb_stage)
    assert os.path.exists(fresh_stage)
    os.remove(fresh_stage)


def test_reservation_reaper_removes_the_unnamed_thumbnail(db_session):
    s = Slide(wsi_filename="", created_at=utcnow() - timedelta(minutes=90))
    db_session.add(s)
    db_session.commit()
    sid = s.id
    leaked = os.path.join(_thumbs_dir(), f"{sid}-0123456789abcdef.jpg")
    _write(leaked, b"thumb")

    assert reap_orphan_slide_reservations(db_session) >= 1
    assert not os.path.exists(leaked)


T = "0123456789ab"


@pytest.mark.parametrize("name", ["case.rollback-1.svs", "scan.upload-a.svs",
                                  "x.rollback-0123456789ab.svs", "y.upload-0123456789ab.svs"])
def test_a_slide_file_that_merely_looks_like_a_leftover_is_kept(db_session, make_slide, name):
    """Upload names may contain ".rollback-" / ".upload-"; the sweep matches
    only the exact generated shapes and never a name a row references."""
    s = make_slide(name, b"live-bytes")
    path = os.path.join(_wsi_dir(), s.wsi_filename)
    _age(path, 600)

    stats = reap_interrupted_slide_swaps(db_session)

    assert os.path.exists(path), stats
    assert stats == {"staged_removed": 0, "parks_restored": 0, "parks_removed": 0, "parks_kept": 0}


def test_a_rollback_moves_the_slide_to_a_fresh_tile_generation(db_session, make_slide):
    s = make_slide("gen.svs", b"old-bytes")
    before = s.wsi_generation
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    os.replace(wsi, wsi + f".rollback-{T}")
    _write(wsi, b"new-bytes-longer")

    assert reap_interrupted_slide_swaps(db_session)["parks_restored"] == 1
    db_session.expire_all()
    assert db_session.get(Slide, s.id).wsi_generation > (before or 0)


def test_killed_between_thumbnail_swap_and_wsi_park_restores_the_old_thumbnail(db_session, make_slide):
    """Thumbnail already swapped, WSI not yet parked: the staged WSI of the
    same token is still there, so the old thumbnail goes back."""
    s = make_slide("t.svs", b"old-bytes")
    thumb = os.path.join(_thumbs_dir(), s.thumb_filename)
    os.replace(thumb, thumb + f".rollback-{T}")
    _write(thumb, b"new-thumb")
    staged = os.path.join(_wsi_dir(), f"{s.id}_t.upload-{T}.svs")
    _write(staged, b"new-bytes")
    _age(staged, 120)

    stats = reap_interrupted_slide_swaps(db_session)

    with open(thumb, "rb") as fh:
        assert fh.read() == b"old-thumb"
    assert not os.path.exists(staged) and stats["staged_removed"] == 1
    with open(os.path.join(_wsi_dir(), s.wsi_filename), "rb") as fh:
        assert fh.read() == b"old-bytes"


def test_thumbnail_park_is_kept_with_a_kept_wsi_park(db_session, make_slide):
    s = make_slide("k.svs", b"12345")
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    thumb = os.path.join(_thumbs_dir(), s.thumb_filename)
    _write(wsi, b"1234567")
    _write(wsi + f".rollback-{T}", b"123456789")
    _write(thumb + f".rollback-{T}", b"old-thumb")

    assert reap_interrupted_slide_swaps(db_session)["parks_kept"] == 1
    assert os.path.exists(thumb + f".rollback-{T}")


@pytest.mark.parametrize("identical", [True, False])
def test_same_size_park_is_dropped_only_when_identical(db_session, make_slide, identical):
    s = make_slide("same-size.svs", b"AAAAAAAA")
    wsi = os.path.join(_wsi_dir(), s.wsi_filename)
    _write(wsi + f".rollback-{T}", b"AAAAAAAA" if identical else b"BBBBBBBB")

    stats = reap_interrupted_slide_swaps(db_session)

    if identical:
        assert stats["parks_removed"] == 1 and not os.path.exists(wsi + f".rollback-{T}")
    else:
        assert stats["parks_kept"] == 1 and os.path.exists(wsi + f".rollback-{T}")
