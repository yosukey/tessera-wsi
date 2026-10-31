"""Duplicate filename guard for WSI upload and replace.

Files are stored on disk as ``{slide_id}_{safe_name}``; the original name
(``safe_name``) must be unique across slides, matched case-insensitively.
"""
import io
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest


@contextmanager
def _stub_openslide():
    """Patch the OpenSlide-backed helpers so any byte blob "uploads" cleanly."""
    from wsi_app.config import settings
    os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "thumbs"), exist_ok=True)
    from wsi_app import admin_helpers
    from wsi_app.routers import admin_slides

    fake_props = {"width": 100, "height": 100, "mpp_x": 0.25, "mpp_y": 0.25,
                  "vendor": "aperio", "acquired_at": "2024-01-15"}

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image
        Image.new("RGB", (1, 1), (0, 0, 0)).save(out_path, "JPEG")

    # read_associated_images lives in admin_helpers (shared by upload and
    # the slide-transfer import finalize path).
    with patch.object(admin_slides, "read_slide_properties", return_value=fake_props), \
         patch.object(admin_slides, "make_thumbnail", side_effect=fake_thumbnail), \
         patch.object(admin_helpers, "read_associated_images", return_value={}):
        yield


def _upload(client, admin_headers, name):
    return client.post(
        "/api/admin/slides/upload",
        headers=admin_headers,
        files={"file": (name, io.BytesIO(b"\x00" * 512), "application/octet-stream")},
    )


def _replace(client, admin_headers, slide_id, name):
    return client.post(
        f"/api/admin/slides/{slide_id}/wsi",
        headers=admin_headers,
        files={"file": (name, io.BytesIO(b"\x00" * 512), "application/octet-stream")},
    )


def test_duplicate_name_upload_is_rejected(client, admin_headers):
    with _stub_openslide():
        first = _upload(client, admin_headers, "dup.svs")
        assert first.status_code == 200, first.text
        second = _upload(client, admin_headers, "dup.svs")
    assert second.status_code == 409, second.text


def test_duplicate_name_is_case_insensitive(client, admin_headers):
    with _stub_openslide():
        first = _upload(client, admin_headers, "sample.svs")
        assert first.status_code == 200, first.text
        clash = _upload(client, admin_headers, "Sample.SVS")
    assert clash.status_code == 409, clash.text


def test_distinct_names_both_succeed(client, admin_headers):
    with _stub_openslide():
        a = _upload(client, admin_headers, "alpha.svs")
        b = _upload(client, admin_headers, "beta.svs")
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text


def test_replace_rejects_name_of_other_slide(client, admin_headers):
    with _stub_openslide():
        a = _upload(client, admin_headers, "one.svs")
        b = _upload(client, admin_headers, "two.svs")
        assert a.status_code == 200 and b.status_code == 200
        # Replacing slide B's WSI with a file named like slide A's must 409.
        clash = _replace(client, admin_headers, b.json()["slide_id"], "one.svs")
    assert clash.status_code == 409, clash.text


def test_replace_same_slide_same_name_succeeds(client, admin_headers):
    """Re-uploading a slide's own file (e.g. after a metadata restore) is allowed."""
    with _stub_openslide():
        up = _upload(client, admin_headers, "restore.svs")
        assert up.status_code == 200, up.text
        slide_id = up.json()["slide_id"]
        again = _replace(client, admin_headers, slide_id, "restore.svs")
    assert again.status_code == 200, again.text


def test_recheck_before_commit_rejects_and_cleans_up(client, admin_headers, db_session):
    """A second upload of a name that slips past the entry-time uniqueness
    check (simulating the TOCTOU window during a long stream) must still be
    caught by the pre-commit re-check — and that rejection must clean up the
    placeholder row + on-disk file rather than leaving an orphan."""
    from wsi_app.config import settings
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides

    name = "recheckorphan.svs"
    with _stub_openslide():
        first = _upload(client, admin_headers, name)
        assert first.status_code == 200, first.text

        # Make the *entry* uniqueness check a no-op on its first invocation so
        # the racing upload reaches the finalise re-check, which runs the real
        # logic against the now-committed first slide.
        real = admin_slides._assert_unique_wsi_filename
        state = {"n": 0}

        def gate(db, safe_name, *, exclude_slide_id=None):
            state["n"] += 1
            if state["n"] == 1:
                return  # simulate the entry check losing the race
            return real(db, safe_name, exclude_slide_id=exclude_slide_id)

        with patch.object(admin_slides, "_assert_unique_wsi_filename", side_effect=gate):
            second = _upload(client, admin_headers, name)
        assert second.status_code == 409, second.text

    db_session.expire_all()
    matches = [
        s for s in db_session.query(Slide).all()
        if (s.wsi_filename or "").endswith(f"_{name}")
    ]
    assert len(matches) == 1, f"orphan slide row left behind: {matches}"

    wsi_dir = os.path.join(settings.data_dir, "wsi")
    on_disk = [f for f in os.listdir(wsi_dir) if f.endswith(f"_{name}")]
    assert len(on_disk) == 1, f"orphan file left behind: {on_disk}"


def test_assert_unique_resolves_via_indexed_norm_column(db_session):
    """The uniqueness check is a single indexed lookup on ``wsi_filename_norm``
    (the casefolded original basename carrying the UNIQUE index), not an O(n)
    Python scan that re-derives the basename from every row. Verify: a
    populated norm collides case-insensitively; ``exclude_slide_id`` skips the
    row being replaced; a NULL-norm placeholder (no file yet) never matches."""
    from fastapi import HTTPException
    from wsi_app.models import Slide
    from wsi_app.routers.admin_slides import _assert_unique_wsi_filename

    norm = "idxnorm_regr.svs"
    owner = Slide(wsi_filename=f"7_{norm}", wsi_filename_norm=norm)
    placeholder = Slide(wsi_filename="", wsi_filename_norm=None)
    db_session.add_all([owner, placeholder])
    # flush (not commit) makes the rows visible to same-session queries while
    # the final rollback keeps the shared test DB clean.
    db_session.flush()
    try:
        with pytest.raises(HTTPException) as ei:
            _assert_unique_wsi_filename(db_session, norm)
        assert ei.value.status_code == 409

        # Case-insensitive: the query target is casefolded to match the column.
        with pytest.raises(HTTPException):
            _assert_unique_wsi_filename(db_session, norm.upper())

        # Replacing the owning slide with its own name is allowed.
        _assert_unique_wsi_filename(db_session, norm, exclude_slide_id=owner.id)

        # A distinct name doesn't collide; the NULL-norm placeholder is ignored.
        _assert_unique_wsi_filename(db_session, "idxnorm_unrelated.svs")
    finally:
        db_session.rollback()


def test_upload_survives_many_slide_id_collisions(client, admin_headers, db_session):
    """Parallel uploads all read max(id)+1 and race to insert it; the loser of a
    collision retries. Five immediate retries are not enough under real
    concurrency (500 "Failed to reserve slide ID"), so the loop allows more."""
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides

    taken = Slide(title="taken", catalog_id="COLLIDE-1")
    db_session.add(taken)
    db_session.commit()
    db_session.refresh(taken)
    real = admin_slides.reserve_next_slide_id
    calls = {"n": 0}

    def colliding(db):
        calls["n"] += 1
        if calls["n"] <= 12:
            return taken.id  # a racing worker already committed this id
        return real(db)

    with _stub_openslide(), patch.object(admin_slides, "reserve_next_slide_id", colliding), \
         patch.object(admin_slides.time, "sleep", lambda s: None):
        r = _upload(client, admin_headers, "collide.svs")
    assert r.status_code == 200, r.text
    assert calls["n"] == 13
