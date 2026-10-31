"""Hardening regression tests across several subsystems.

Covers filesystem paths, schema reconciliation, the cross-worker slide
mutation lock, job slots, config parsing, and API behaviour. Each test pins
one defect so a later refactor can't silently reintroduce it. Grouped by the
surface they guard rather than by file.
"""
from __future__ import annotations

import dataclasses
import os

import pytest
from sqlalchemy import delete, text

from wsi_app.config import settings


# ---------------------------------------------------------------------------
# Filesystem paths derived from mutable DB rows
# ---------------------------------------------------------------------------

def test_tour_asset_dir_rejects_traversal_tokens():
    """A restored/hand-edited DB must not aim the asset rmtree outside the tree."""
    from wsi_app.admin_helpers import tour_asset_dir

    base = os.path.realpath(os.path.join(settings.data_dir, "assets", "tours"))
    # Sanity: a well-formed token still resolves, and stays under the base.
    ok = os.path.realpath(tour_asset_dir("abcDEF123_-"))
    assert ok.startswith(base + os.sep)

    for bad in ("../../..", "..", ".", "", "a/b", "/etc", "x" * 33, None, 7):
        with pytest.raises(ValueError):
            tour_asset_dir(bad)  # type: ignore[arg-type]


def test_thumb_filename_refuses_non_basename_stored_value():
    """thumb_filename feeds paths that get *written* through (rebuild, replace)."""
    from wsi_app.admin_helpers import thumb_filename

    # A legitimate stored name is reused verbatim so replace/rebuild stay stable.
    assert thumb_filename(4, "4-deadbeef.jpg") == "4-deadbeef.jpg"

    for bad in ("../wsi.db", "../../etc/passwd", "/abs/path.jpg", "..", ".", ".hidden"):
        out = thumb_filename(4, bad)
        assert out != bad
        assert out.startswith("4-") and out.endswith(".jpg")
        assert os.path.basename(out) == out


def test_valid_public_id_matches_minted_tokens():
    from wsi_app.admin_helpers import mint_public_id, valid_public_id

    for _ in range(20):
        assert valid_public_id(mint_public_id())
    assert not valid_public_id("../x")
    assert not valid_public_id("")
    assert not valid_public_id("a" * 33)


# ---------------------------------------------------------------------------
# Schema reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_indexes_recreates_a_dropped_unique_index():
    """ALTER TABLE ADD COLUMN cannot carry UNIQUE, so a DB upgraded in place
    lacks these.

    Simulates the upgraded-in-place shape by dropping the index the ORM
    declares, then asserts the reconciler puts it back and that it actually
    enforces uniqueness again (the upload/replace race backstop).
    """
    from wsi_app.db import engine
    from wsi_app.main import _reconcile_indexes

    idx = "ix_slides_wsi_filename_norm"
    with engine.begin() as conn:
        before = {
            r[1] for r in conn.exec_driver_sql("PRAGMA index_list(slides)").fetchall()
        }
        assert idx in before, "fresh schema should carry the unique index"

    # The DB is session-scoped and shared with every later test file, several of
    # which rely on this exact index for their duplicate-filename 409. Restore it
    # unconditionally so a failure here surfaces as one clear failure instead of
    # cascading into unrelated ones.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP INDEX "{idx}"')
            assert idx not in {
                r[1]
                for r in conn.exec_driver_sql("PRAGMA index_list(slides)").fetchall()
            }

        _reconcile_indexes()

        with engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA index_list(slides)").fetchall()
            recreated = {r[1]: r[2] for r in rows}
            assert idx in recreated
            assert recreated[idx] == 1, "must be recreated as UNIQUE, not a plain index"
    finally:
        _reconcile_indexes()


def test_reconcile_indexes_is_idempotent():
    from wsi_app.main import _reconcile_indexes

    _reconcile_indexes()
    _reconcile_indexes()  # second pass must not raise on already-present indexes


def test_reconcile_indexes_survives_duplicate_rows(caplog):
    """A UNIQUE index that can't be built must log and continue, not block boot."""
    from wsi_app.db import engine
    from wsi_app.main import _reconcile_indexes

    from wsi_app.db import SessionLocal
    from wsi_app.models import Slide

    idx = "ix_slides_wsi_filename_norm"
    # One try/finally covering the drop, the duplicate rows AND the assertions:
    # the DB is session-scoped, so a failure anywhere in between must not leave
    # later test files without the index (or with stray duplicate rows).
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{idx}"')
        # Insert through the ORM so every NOT NULL column picks up its model
        # default; the index is dropped first so the duplicate norm is accepted.
        db = SessionLocal()
        try:
            for tag in ("d1", "d2"):
                db.add(Slide(
                    catalog_id=tag, title=f"dup {tag}",
                    wsi_filename=f"{tag}.svs", wsi_filename_norm="dup.svs",
                    thumb_filename=f"{tag}.jpg", width=1, height=1, is_published=False,
                ))
            db.commit()
        finally:
            db.close()

        _reconcile_indexes()  # must not raise
        with engine.begin() as conn:
            present = {
                r[1]
                for r in conn.exec_driver_sql("PRAGMA index_list(slides)").fetchall()
            }
        assert idx not in present, "cannot exist while duplicates remain"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM slides WHERE wsi_filename_norm = 'dup.svs'"))
        _reconcile_indexes()


def test_column_add_warns_about_constraints_it_cannot_create(caplog):
    """ADD COLUMN can't carry a UniqueConstraint or FK; the reconciler must say
    so rather than leave the column silently unconstrained. A unique *index*
    (restored by _reconcile_indexes) is not reported."""
    import logging
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, UniqueConstraint
    from wsi_app.main import _warn_unapplied_column_constraints

    md = MetaData()
    Table("parent", md, Column("id", Integer, primary_key=True))
    t = Table(
        "child", md,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parent.id", ondelete="CASCADE")),
        Column("code", Integer, unique=True),
        Column("indexed", Integer, unique=True, index=True),
        Column("a", Integer),
        UniqueConstraint("a", "code"),
    )
    with caplog.at_level(logging.WARNING, logger="wsi_app"):
        _warn_unapplied_column_constraints(t, t.c.parent_id)
        _warn_unapplied_column_constraints(t, t.c.code)
        _warn_unapplied_column_constraints(t, t.c.indexed)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("child.parent_id" in m and "parent.id ON DELETE CASCADE" in m for m in msgs)
    assert any("child.code" in m and "UNIQUE(code)" in m and "UNIQUE(a, code)" in m for m in msgs)
    assert not any("child.indexed" in m for m in msgs)


# ---------------------------------------------------------------------------
# Cross-worker slide mutation lock
# ---------------------------------------------------------------------------

def test_slide_mutation_lock_is_reentrant_across_sequential_uses():
    from wsi_app.admin_helpers import slide_mutation_lock

    for _ in range(3):
        with slide_mutation_lock(123):
            pass


def test_slide_mutation_lock_takes_a_real_file_lock():
    """The lock must be a host-level flock, not just an in-process mutex.

    Production runs ``uvicorn --workers N``, so an in-process mutex alone would
    not exclude a mutation handled by another worker process. ``flock`` is
    scoped to the open file description, so a second ``open()`` — even in this
    process — is subject to the same exclusion a sibling worker would face.
    """
    import fcntl

    from wsi_app.admin_helpers import slide_mutation_lock

    lock_path = os.path.join(settings.data_dir, "locks", "slide-4242.lock")

    def held_by_someone_else() -> bool:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    with slide_mutation_lock(4242):
        assert os.path.isfile(lock_path), "lock file should exist while held"
        assert held_by_someone_else(), "another opener must be excluded"
    assert not held_by_someone_else(), "lock must be released on context exit"


def test_slide_mutation_lock_degrades_when_lock_dir_unavailable(monkeypatch, caplog):
    """An unwritable data_dir must not fail the mutation (fail-open by design)."""
    from wsi_app import admin_helpers

    def boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(admin_helpers.os, "makedirs", boom)
    with admin_helpers.slide_mutation_lock(999):
        pass  # must not raise


# ---------------------------------------------------------------------------
# Job slot
# ---------------------------------------------------------------------------

def test_slot_guard_serialises_claim_and_act():
    from wsi_app import jobs as _jobs

    with _jobs.store.slot_guard():
        # Nothing to assert beyond "it is a working context manager the cancel
        # endpoints can wrap their check+rmtree in"; the exclusion itself is the
        # same flock try_claim_running takes.
        assert _jobs.store.has_running_kind("slide_import") in (True, False)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def test_env_int_names_the_offending_variable():
    from wsi_app.config import env_int

    os.environ["WSI_TEST_BOGUS_INT"] = "2O"
    try:
        with pytest.raises(ValueError) as exc:
            env_int("WSI_TEST_BOGUS_INT", 20)
        msg = str(exc.value)
        assert "WSI_TEST_BOGUS_INT" in msg
        assert "'2O'" in msg
    finally:
        del os.environ["WSI_TEST_BOGUS_INT"]


def test_role_permissions_read_without_a_row_is_least_privilege_and_writes_nothing(
    client, admin_headers, db_session
):
    """A fresh install has no RolePermissions row until an admin first changes
    a flag. Every read (GET /api/admin/me, GET /api/admin/role-permissions)
    must report the all-off defaults without creating the row: a read that
    inserts would race other workers' first loads on the primary key."""
    from wsi_app.models import RolePermissions

    db_session.execute(delete(RolePermissions))
    db_session.commit()
    all_off = {
        "manager_can_view_analytics": False,
        "manager_can_view_server": False,
        "instructor_can_view_analytics": False,
        "instructor_can_view_server": False,
    }

    r = client.get("/api/admin/me", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["role_permissions"] == all_off
    r = client.get("/api/admin/role-permissions", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == all_off
    db_session.expire_all()
    assert db_session.get(RolePermissions, 1) is None


def test_forwarded_proto_takes_the_first_hop():
    """A proxy chain that appends rather than overwrites must not read as http.

    A call site comparing the whole header to "https" would, behind an extra
    load balancer where the value arrives as "https, http", read the request
    as plaintext — dropping Secure from the session cookie — and build the
    OAuth redirect_uri as "https, http://host/...", which Google rejects.
    """
    from wsi_app.network import forwarded_proto

    assert forwarded_proto({"x-forwarded-proto": "https, http"}) == "https"
    assert forwarded_proto({"x-forwarded-proto": " HTTPS , http "}) == "https"
    assert forwarded_proto({"x-forwarded-proto": "http"}) == "http"
    # Absent header: fall back to the ASGI scheme, or to "" when none is given
    # (breakglass_exempt relies on "" not matching "http").
    assert forwarded_proto({}, "https") == "https"
    assert forwarded_proto({}) == ""
    assert forwarded_proto({"x-forwarded-proto": ""}) == ""


def test_env_int_treats_empty_as_unset():
    from wsi_app.config import env_int

    os.environ["WSI_TEST_EMPTY_INT"] = ""
    try:
        assert env_int("WSI_TEST_EMPTY_INT", 7) == 7
    finally:
        del os.environ["WSI_TEST_EMPTY_INT"]



@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("YES", True), (" on ", True),
     ("0", False), ("false", False), ("No", False), ("off", False)],
)
def test_env_bool_shared_vocabulary(monkeypatch, raw, expected):
    """Every flag shares one vocabulary, so ``WSI_UPDATE_CHECK=false`` really
    disables the check and ``WSI_ERROR_LOG_ENABLED=true`` keeps capture on."""
    from wsi_app.config import env_bool

    monkeypatch.setenv("WSI_TEST_BOOL", raw)
    assert env_bool("WSI_TEST_BOOL", not expected) is expected


def test_env_bool_unset_blank_and_bogus(monkeypatch, caplog):
    from wsi_app.config import env_bool

    monkeypatch.delenv("WSI_TEST_BOOL", raising=False)
    assert env_bool("WSI_TEST_BOOL", True) is True
    monkeypatch.setenv("WSI_TEST_BOOL", "  ")
    assert env_bool("WSI_TEST_BOOL", False) is False
    # An unrecognised value warns and keeps the default rather than failing
    # the boot of an install whose wsi.env holds it.
    monkeypatch.setenv("WSI_TEST_BOOL", "enabled")
    with caplog.at_level("WARNING", logger="wsi_app.config"):
        assert env_bool("WSI_TEST_BOOL", False) is False
    assert "WSI_TEST_BOOL" in caplog.text and "'enabled'" in caplog.text


# ---------------------------------------------------------------------------
# API behaviour
# ---------------------------------------------------------------------------

def test_bulk_unpublish_rotates_thumbnail_token(
    client, admin_headers, db_session, slide, attach_wsi
):
    """Bulk unpublish must invalidate shared thumbnail URLs like the single path.

    /thumbs is an ungated static mount, so the random token in the filename is
    the only thing gating a URL handed out while the slide was public.
    """
    from wsi_app.admin_helpers import thumb_filename

    attach_wsi(slide)
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    original = thumb_filename(slide.id)
    with open(os.path.join(thumbs_dir, original), "wb") as fh:
        fh.write(b"jpeg")
    slide.thumb_filename = original
    slide.is_published = True
    db_session.commit()

    r = client.post(
        "/api/admin/slides/bulk/publish",
        headers=admin_headers,
        json={"slide_ids": [slide.id], "publish": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is True

    db_session.refresh(slide)
    assert slide.thumb_filename != original, "token must be rotated"
    assert os.path.isfile(os.path.join(thumbs_dir, slide.thumb_filename))
    assert not os.path.exists(os.path.join(thumbs_dir, original)), (
        "the previously-shared URL must stop resolving"
    )


def test_patch_empty_form_value_is_treated_as_absent(
    client, admin_headers, db_session, slide, attach_wsi
):
    """Pin the coercion the metadata PATCH's semantics rest on.

    An empty form field arrives as None, not "", so ``catalog_id=`` reads as
    "leave it alone" rather than "clear it"; blanking takes the explicit
    clear_catalog_id / clear_title flags (see test_slide_metadata_patch.py).
    If a framework upgrade starts delivering "" instead, this test fails and
    every save that leaves a field empty starts clearing it.
    """
    attach_wsi(slide)
    slide.is_published = True
    db_session.commit()

    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        headers=admin_headers,
        data={"catalog_id": "", "title": slide.title},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(type(slide), slide.id).catalog_id == "TEST-001", (
        "an empty value must not clear the field"
    )


def test_patch_clears_the_mpp_override_with_the_null_sentinel(
    client, admin_headers, db_session, slide
):
    """Dropping a manual MPP override has to be reachable from the dashboard.

    The override falls back to the file-derived value when it is 0.0, but an
    empty form field reads as "absent" (see the test above), so clearing needs
    the same "null" sentinel the tri-state overlay flags use. Without it the
    endpoint answers 200 while silently keeping the old override, and the
    viewer's scale bar stays wrong.
    """
    set_it = client.patch(
        f"/api/admin/slides/{slide.id}",
        headers=admin_headers,
        data={"catalog_id": "C1", "title": "T1", "mpp_x": "0.5", "mpp_y": "0.5"},
    )
    assert set_it.status_code == 200, set_it.text
    db_session.expire_all()
    assert db_session.get(type(slide), slide.id).mpp_x == 0.5

    cleared = client.patch(
        f"/api/admin/slides/{slide.id}",
        headers=admin_headers,
        data={"catalog_id": "C1", "title": "T1", "mpp_x": "null", "mpp_y": "null"},
    )
    assert cleared.status_code == 200, cleared.text
    db_session.expire_all()
    fresh = db_session.get(type(slide), slide.id)
    assert (fresh.mpp_x, fresh.mpp_y) == (0.0, 0.0), "the override must be gone"


def test_tour_asset_upload_enforces_per_tour_file_quota(
    client, admin_headers, db_session, monkeypatch
):
    """The per-file cap is not a bound on total usage; the count cap is."""
    import io

    from PIL import Image

    from wsi_app.models import Slide, Tour

    s = Slide(title="Q", catalog_id="Q-001", wsi_filename="q.svs")
    db_session.add(s)
    db_session.commit()
    t = Tour(title="QT", slide_id=s.id)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    # `settings` is a frozen dataclass; swap the module-level reference the
    # quota helper reads instead of mutating the instance.
    from wsi_app.routers import admin_tours

    monkeypatch.setattr(
        admin_tours, "settings",
        dataclasses.replace(settings, tour_asset_max_files=2),
    )

    def _png() -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), (1, 2, 3)).save(buf, "PNG")
        return buf.getvalue()

    def upload():
        return client.post(
            f"/api/admin/tours/{t.id}/assets/upload",
            headers=admin_headers,
            files={"file": ("a.png", io.BytesIO(_png()), "image/png")},
            data={"duplicate_policy": "rename"},
        )

    assert upload().status_code == 200
    assert upload().status_code == 200
    third = upload()
    assert third.status_code == 409, third.text
    assert "maximum" in third.json()["detail"]


def test_app_logout_rejects_cross_site_post(client):
    """Logout CSRF: a cross-site POST must not be able to clear a session.

    Logout needs no ambient credential to work — the browser simply honours
    the deletion Set-Cookie on the response — so SameSite does not cover it and
    an attacker page could sign any visitor out at will.
    """
    blocked = client.post(
        "/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert blocked.status_code == 403, blocked.text

    blocked2 = client.post(
        "/api/auth/logout", headers={"Origin": "https://evil.example.org"}
    )
    assert blocked2.status_code == 403, blocked2.text

    # The dashboard's own same-origin fetch still logs out.
    ok = client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200, ok.text


def test_app_login_rejects_cross_site_post(client, db_session):
    """Login CSRF: a cross-site POST must not be able to seat a session."""
    from wsi_app import app_auth as _app_auth
    from wsi_app.models import AuthSettings, AuthUser
    from wsi_app.auth import hash_password

    st = db_session.get(AuthSettings, 1)
    if st is None:
        st = AuthSettings(id=1)
        db_session.add(st)
    st.mode = "basic"
    db_session.add(AuthUser(username="viewer1", password_hash=hash_password("pw-correct-1")))
    db_session.commit()
    _app_auth.refresh(db_session)
    try:
        creds = {"username": "viewer1", "password": "pw-correct-1"}

        blocked = client.post(
            "/api/auth/login", data=creds,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert blocked.status_code == 403, blocked.text
        assert _app_auth.SESSION_COOKIE not in blocked.cookies

        blocked2 = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "https://evil.example.org"},
        )
        assert blocked2.status_code == 403, blocked2.text

        # Same-origin and non-browser (no fetch-metadata, no Origin) still work.
        ok = client.post(
            "/api/auth/login", data=creds,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert ok.status_code == 200, ok.text

        ok2 = client.post("/api/auth/login", data=creds)
        assert ok2.status_code == 200, ok2.text
    finally:
        st.mode = "none"
        # Drop the row too: the test DB is session-scoped, so leaving app-auth
        # users behind makes this test observable from any later one.
        db_session.execute(
            delete(AuthUser).where(AuthUser.username.in_(("viewer1", "viewer-port")))
        )
        db_session.commit()
        _app_auth.refresh(db_session)


def test_replace_wsi_notices_a_delete_committed_while_streaming(
    client, admin_headers, db_session, slide, attach_wsi, monkeypatch
):
    """The under-lock re-fetch must see another session's committed delete.

    Splitting the replace into an unlocked staging phase and a locked
    swap-and-commit phase only helps if phase 2 genuinely re-reads the row.
    Phase 1 already loaded it, so without dropping the session's read
    transaction the identity map (and SQLite's snapshot) would serve the stale
    object and the handler would happily swap files into place for a slide that
    no longer exists.
    """
    import io

    from wsi_app.db import SessionLocal
    from wsi_app.models import Slide
    from wsi_app.routers import admin_slides

    attach_wsi(slide)
    slide_id = slide.id
    real_stream = admin_slides.stream_upload_to_file

    def stream_then_delete(upload, dest):
        real_stream(upload, dest)
        # Simulate a concurrent worker deleting the row mid-upload.
        other = SessionLocal()
        try:
            row = other.get(Slide, slide_id)
            if row is not None:
                other.delete(row)
                other.commit()
        finally:
            other.close()

    fake_props = {
        "width": 10, "height": 10, "mpp_x": 0.5, "mpp_y": 0.5,
        "vendor": "aperio", "acquired_at": "",
    }

    def fake_thumbnail(path, out_path, max_size=(512, 512)):
        from PIL import Image

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        Image.new("RGB", (1, 1), (0, 0, 0)).save(out_path, "JPEG")

    monkeypatch.setattr(admin_slides, "stream_upload_to_file", stream_then_delete)
    monkeypatch.setattr(admin_slides, "read_slide_properties", lambda p: fake_props)
    monkeypatch.setattr(admin_slides, "make_thumbnail", fake_thumbnail)

    r = client.post(
        f"/api/admin/slides/{slide_id}/wsi",
        headers=admin_headers,
        files={"file": ("fresh.svs", io.BytesIO(b"\x00" * 64), "application/octet-stream")},
    )
    assert r.status_code == 404, r.text

    # The staged bytes must not be left behind, and no WSI may have been
    # swapped into place under the deleted slide's name.
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    leftovers = [
        f for f in os.listdir(wsi_dir)
        if f.startswith(f"{slide_id}_fresh") or ".upload-" in f
    ]
    assert leftovers == [], f"staged/committed files left behind: {leftovers}"


def test_slide_dwell_ended_at_ratchets_forward(client, admin_headers, slide):
    """The ended_at bump must actually match rows, not silently update zero.

    dwell_ms and ended_at are raised by separate conditional UPDATEs so
    concurrent beacons can't lose an update. The ended_at statement binds a
    naive UTC value to match how SQLite stores the column — if that ever stops
    lining up, the WHERE matches nothing and ended_at silently freezes at the
    first report while dwell_ms keeps climbing.
    """
    import datetime as dt

    from wsi_app.db import SessionLocal
    from wsi_app.models import SlideDwellEvent

    sid = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    now = dt.datetime.now(dt.timezone.utc)
    started = (now - dt.timedelta(minutes=10)).isoformat()

    def beacon(dwell_ms: int, ended_offset_min: int):
        return client.post(
            "/api/analytics/slide-dwell",
            json={
                "slide_public_id": slide.public_id,
                "session_id": sid,
                "started_at": started,
                "ended_at": (now - dt.timedelta(minutes=ended_offset_min)).isoformat(),
                "dwell_ms": dwell_ms,
            },
            headers=admin_headers,
        )

    # Every ended_at stays within the beacon's receipt skew of now
    # (routers.analytics._CLIENT_CLOCK_SKEW, 5 min): these beacons all arrive
    # now, and one stamped further back would be re-anchored to its receipt.
    assert beacon(60_000, 4).status_code == 204
    db = SessionLocal()
    try:
        first = db.query(SlideDwellEvent).filter(
            SlideDwellEvent.session_id == sid
        ).one().ended_at
    finally:
        db.close()

    # A later ended_at with a *smaller* dwell: ended_at must advance, dwell must
    # not regress. This separates the two ratchets.
    assert beacon(10_000, 2).status_code == 204
    db = SessionLocal()
    try:
        row = db.query(SlideDwellEvent).filter(
            SlideDwellEvent.session_id == sid
        ).one()
        assert row.dwell_ms == 60_000, "a smaller report must not lower dwell"
        assert row.ended_at > first, "a later report must advance ended_at"
        advanced = row.ended_at
    finally:
        db.close()

    # An earlier ended_at must not walk it back.
    assert beacon(90_000, 4).status_code == 204
    db = SessionLocal()
    try:
        row = db.query(SlideDwellEvent).filter(
            SlideDwellEvent.session_id == sid
        ).one()
        assert row.ended_at == advanced, "an earlier report must not rewind ended_at"
        assert row.dwell_ms == 90_000, "a larger report must still raise dwell"
    finally:
        db.close()


def test_login_origin_fallback_ignores_port(client, db_session):
    """The Origin fallback must not reject a same-host login on a non-default port.

    nginx forwards ``Host: $host``, which carries no port, while the browser's
    Origin includes one whenever the site is served on a non-default port.
    Comparing full authorities would 403 every login on such an install — for
    browsers old enough to lack Sec-Fetch-Site, which is exactly when the
    fallback is the only check running.
    """
    from wsi_app import app_auth as _app_auth
    from wsi_app.auth import hash_password
    from wsi_app.models import AuthSettings, AuthUser

    st = db_session.get(AuthSettings, 1)
    if st is None:
        st = AuthSettings(id=1)
        db_session.add(st)
    st.mode = "basic"
    db_session.add(
        AuthUser(username="viewer-port", password_hash=hash_password("pw-correct-2"))
    )
    db_session.commit()
    _app_auth.refresh(db_session)
    try:
        creds = {"username": "viewer-port", "password": "pw-correct-2"}

        # No Sec-Fetch-Site (legacy browser); Origin carries a port the proxied
        # Host header does not.
        ok = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "http://testserver:8443", "Host": "testserver"},
        )
        assert ok.status_code == 200, ok.text

        # A genuinely different hostname is still refused.
        bad = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "http://evil.example.org:8443", "Host": "testserver"},
        )
        assert bad.status_code == 403, bad.text

        # IPv6 literal with a port on both sides.
        ok6 = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "http://[::1]:8443", "Host": "[::1]"},
        )
        assert ok6.status_code == 200, ok6.text

        # A fully-qualified name with its trailing dot: nginx's $host drops
        # the dot, the browser's Origin keeps it — the same site either way.
        dot = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "http://testserver.:8443", "Host": "testserver"},
        )
        assert dot.status_code == 200, dot.text
        # ... but only one dot, and only that: another name is still refused.
        bad_dot = client.post(
            "/api/auth/login", data=creds,
            headers={"Origin": "http://evil.example.org.", "Host": "testserver"},
        )
        assert bad_dot.status_code == 403, bad_dot.text
    finally:
        st.mode = "none"
        # Drop the row too: the test DB is session-scoped, so leaving app-auth
        # users behind makes this test observable from any later one.
        db_session.execute(
            delete(AuthUser).where(AuthUser.username.in_(("viewer1", "viewer-port")))
        )
        db_session.commit()
        _app_auth.refresh(db_session)


def test_reaper_skips_a_slide_whose_mutation_lock_is_held(db_session):
    """A live upload must survive the boot-time orphan reaper.

    The reaper deletes placeholder rows (empty wsi_filename) and their
    ``{id}_*`` files once past the age cutoff. That cutoff is not proof the
    upload is dead — the body cap defaults to 8 GiB, so a real upload can still
    be streaming — and its staging file is ``wsi/{id}_{name}.upload-<token>``,
    which the id-prefix match would delete along with the row. Holding the
    slide's mutation lock (as the upload does) must make the reaper skip it.
    """
    import datetime as dt

    from wsi_app.admin_helpers import (
        reap_orphan_slide_reservations,
        slide_mutation_lock,
    )
    from wsi_app.models import Slide

    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)

    # A placeholder row old enough to be reaped, mid-upload.
    row = Slide(title="", catalog_id="", wsi_filename="", thumb_filename="x.jpg",
                width=0, height=0)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    row.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    db_session.commit()
    slide_id = row.id

    staging = os.path.join(wsi_dir, f"{slide_id}_big.svs.upload-deadbeef")
    with open(staging, "wb") as fh:
        fh.write(b"partial")

    # Lock held (upload in flight on another worker) -> must be skipped.
    with slide_mutation_lock(slide_id):
        reap_orphan_slide_reservations(db_session, older_than_minutes=30)
    db_session.expire_all()
    assert db_session.get(Slide, slide_id) is not None, "live upload's row was reaped"
    assert os.path.exists(staging), "live upload's staged bytes were deleted"

    # Lock free (upload really died) -> reaped.
    reap_orphan_slide_reservations(db_session, older_than_minutes=30)
    db_session.expire_all()
    assert db_session.get(Slide, slide_id) is None, "dead reservation should be reaped"
    assert not os.path.exists(staging), "dead reservation's file should be removed"


def test_tour_asset_overwrite_allowed_at_byte_quota(
    client, admin_headers, db_session, monkeypatch
):
    """Replacing an existing asset must stay possible at the byte cap.

    The replacement swaps a file rather than adding one, so charging the
    outgoing file against the incoming upload would strand an author at the cap:
    the only way to fix a wrong image would be to delete it first, breaking
    every markdown reference until the re-upload lands.
    """
    import io

    from PIL import Image

    from wsi_app.models import Slide, Tour
    from wsi_app.routers import admin_tours

    s = Slide(title="B", catalog_id="B-001", wsi_filename="b.svs")
    db_session.add(s)
    db_session.commit()
    t = Tour(title="BT", slide_id=s.id)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    def png() -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (9, 9, 9)).save(buf, "PNG")
        return buf.getvalue()

    def upload(policy: str, name: str = "same.png"):
        return client.post(
            f"/api/admin/tours/{t.id}/assets/upload",
            headers=admin_headers,
            files={"file": (name, io.BytesIO(png()), "image/png")},
            data={"duplicate_policy": policy},
        )

    # Seed one asset, then pin the byte cap below current usage.
    assert upload("overwrite").status_code == 200
    monkeypatch.setattr(
        admin_tours, "settings",
        dataclasses.replace(settings, tour_asset_max_total_bytes=1, tour_asset_max_files=1),
    )

    # Overwriting the existing name is still allowed...
    again = upload("overwrite")
    assert again.status_code == 200, again.text
    # ...while adding a new file is refused by both caps.
    added = upload("rename")
    assert added.status_code == 409, added.text
    other = upload("overwrite", name="other.png")
    assert other.status_code == 409, other.text


def test_try_lock_releases_exactly_once_when_flock_is_held():
    """The cross-process-contention path must not double-release the thread lock.

    Two distinct paths report "not acquired": another thread in this process
    holds the mutex, and another *process* holds the flock. The second one is the
    one the reaper actually hits in production, and releasing the thread lock
    both inside that branch and in the enclosing finally would raise "release
    unlocked lock", or worse, free a lock a concurrent thread was holding.
    """
    import fcntl

    from wsi_app.admin_helpers import (
        _slide_lock_path,
        _slide_thread_lock,
        slide_mutation_lock,
        try_slide_mutation_lock,
    )

    slide_id = 8484
    os.makedirs(os.path.join(settings.data_dir, "locks"), exist_ok=True)
    # Hold the flock on a separate fd — flock is scoped to the open file
    # description, so this is indistinguishable from a sibling worker holding it,
    # while leaving this process's thread lock free.
    fd = os.open(_slide_lock_path(slide_id), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with try_slide_mutation_lock(slide_id) as acquired:
            assert acquired is False, "must report the flock as unavailable"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # The thread lock must be free again — a double release would have raised
    # above, and a missed release would deadlock the blocking acquire below.
    assert _slide_thread_lock(slide_id).acquire(blocking=False), (
        "thread lock left held after the not-acquired path"
    )
    _slide_thread_lock(slide_id).release()

    # And the full blocking lock still works on the same slide afterwards.
    with slide_mutation_lock(slide_id):
        pass


def test_try_lock_releases_the_thread_lock_when_the_body_raises():
    from wsi_app.admin_helpers import _slide_thread_lock, try_slide_mutation_lock

    slide_id = 8585
    with pytest.raises(RuntimeError, match="boom"):
        with try_slide_mutation_lock(slide_id) as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    assert _slide_thread_lock(slide_id).acquire(blocking=False), (
        "thread lock leaked when the caller's body raised"
    )
    _slide_thread_lock(slide_id).release()


def test_unpublish_restores_the_thumbnail_when_the_commit_fails(
    client, admin_headers, db_session, slide, attach_wsi, monkeypatch
):
    """A failed commit must not leave the row pointing at a renamed-away file.

    Unpublishing rotates the thumbnail's capability token by renaming the file
    on disk *before* committing. If the commit then fails (SQLite busy timeout
    under concurrent write load), the rolled-back row still carries the OLD name
    while only the NEW name exists — the thumbnail 404s in the gallery and the
    admin list until someone runs rebuild-thumbnails.
    """
    from wsi_app.admin_helpers import thumb_filename

    attach_wsi(slide)
    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    original = thumb_filename(slide.id)
    with open(os.path.join(thumbs_dir, original), "wb") as fh:
        fh.write(b"jpeg-bytes")
    slide.thumb_filename = original
    slide.is_published = True
    db_session.commit()

    # Fail the commit that follows the rename, from inside the endpoint's own
    # session (a different Session instance than the fixture's).
    import sqlalchemy.orm

    real_commit = sqlalchemy.orm.Session.commit
    calls = {"n": 0}

    def flaky_commit(self, *a, **k):
        calls["n"] += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", flaky_commit)
    try:
        # The endpoint re-raises after undoing the rename (a commit failure must
        # not be reported as success), and TestClient re-raises server
        # exceptions, so the failure surfaces here rather than as a 500 body.
        with pytest.raises(RuntimeError, match="database is locked"):
            client.post(
                f"/api/admin/slides/{slide.id}/publish",
                headers=admin_headers,
                data={"publish": "0"},
            )
    finally:
        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", real_commit)

    assert calls["n"] >= 1, "the commit under test never ran"
    # The row was rolled back to the old name, so that name must exist on disk.
    db_session.rollback()
    db_session.expire_all()
    row = db_session.get(type(slide), slide.id)
    assert row.thumb_filename == original
    assert os.path.isfile(os.path.join(thumbs_dir, original)), (
        "rename was not undone; the row now points at a missing file"
    )


def test_bulk_unpublish_restores_thumbnails_when_commits_fail(
    client, admin_headers, db_session, attach_wsi, monkeypatch
):
    """Same rollback as the single path, across the bulk loop's `continue` branch.

    Exercised with two slides so a per-iteration scoping mistake in the
    rotated-from/rotated-to bookkeeping (they are declared inside the
    state-changed branch) would show up as one slide's rename being undone with
    the other's name.
    """
    from wsi_app.admin_helpers import thumb_filename
    from wsi_app.models import Slide

    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)

    originals = {}
    ids = []
    for tag in ("bulkA", "bulkB"):
        s = Slide(title=tag, catalog_id=f"{tag}-1")
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        attach_wsi(s)
        name = thumb_filename(s.id)
        with open(os.path.join(thumbs_dir, name), "wb") as fh:
            fh.write(tag.encode())
        s.thumb_filename = name
        s.is_published = True
        db_session.commit()
        originals[s.id] = name
        ids.append(s.id)

    import sqlalchemy.orm

    real_commit = sqlalchemy.orm.Session.commit

    def flaky_commit(self, *a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", flaky_commit)
    try:
        r = client.post(
            "/api/admin/slides/bulk/publish",
            headers=admin_headers,
            json={"slide_ids": ids, "publish": False},
        )
    finally:
        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", real_commit)

    # Bulk is best-effort: each slide reports its own failure rather than raising.
    assert r.status_code == 200, r.text
    results = {row["slide_id"]: row for row in r.json()["results"]}
    for sid in ids:
        assert results[sid]["error"] == "commit_failed", results[sid]
        # Each slide's own original name must be back — and with its own bytes,
        # which is what catches a cross-iteration mix-up.
        path = os.path.join(thumbs_dir, originals[sid])
        assert os.path.isfile(path), f"slide {sid} thumbnail not restored"

    db_session.rollback()
    db_session.expire_all()
    for sid in ids:
        row = db_session.get(Slide, sid)
        assert row.thumb_filename == originals[sid]
        assert row.is_published is True, "the rolled-back slide must still be public"


def test_tour_asset_byte_quota_bounds_additive_uploads(
    client, admin_headers, db_session, monkeypatch
):
    """The byte quota must bound what an additive upload can add.

    A mere pre-check against *current* usage would let every upload
    overshoot ``tour_asset_max_total_bytes`` by up to one full per-file cap (and
    two concurrent uploads to one tour could each pass a check the other
    invalidated). The upload streams against the tour's remaining headroom, so
    the configured total is an actual ceiling.

    Replacements stay exempt — see
    test_tour_asset_overwrite_allowed_at_byte_quota for why.
    """
    import io

    from PIL import Image

    from wsi_app.models import Slide, Tour
    from wsi_app.routers import admin_tours

    s = Slide(title="H", catalog_id="H-001", wsi_filename="h.svs")
    db_session.add(s)
    db_session.commit()
    t = Tour(title="HT", slide_id=s.id)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    def png(size: int) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (size, size), (7, 7, 7)).save(buf, "PNG")
        return buf.getvalue()

    first = png(2)
    # Headroom for the first file and a little more, but far less than a
    # second file of the same size — the window a mere pre-check would wave past.
    monkeypatch.setattr(
        admin_tours, "settings",
        dataclasses.replace(
            settings,
            tour_asset_max_total_bytes=len(first) + 8,
            tour_asset_max_files=0,
        ),
    )

    def upload(payload: bytes, name: str):
        return client.post(
            f"/api/admin/tours/{t.id}/assets/upload",
            headers=admin_headers,
            files={"file": (name, io.BytesIO(payload), "image/png")},
            data={"duplicate_policy": "rename"},
        )

    assert upload(first, "a.png").status_code == 200
    over = upload(first, "b.png")
    assert over.status_code == 409, over.text

    # The rejected upload left nothing behind on disk, so a retry loop cannot
    # accumulate partials past the cap either.
    base = os.path.join(
        settings.data_dir, "assets", "tours", str(t.public_id)
    )
    on_disk = [n for n in os.listdir(base) if not n.startswith(".")]
    assert len(on_disk) == 1

    total = sum(os.path.getsize(os.path.join(base, n)) for n in on_disk)
    assert total <= len(first) + 8


def test_asset_family_detection_splits_mp4_audio_from_video():
    """ftyp files are classified by major brand, not lumped in as video.

    Reporting every ISO-BMFF file as "video" would make the content-type
    cross-check in _validate_asset_content 415 a legitimate .m4a
    (Content-Type audio/mp4, brand "M4A "). Inert while the default
    TOUR_ASSET_ALLOWED_MIME_PREFIXES is "image/", but wrong for the
    audio-enabled deployments config.py explicitly invites.
    """
    from wsi_app.routers.admin_tours import _detect_asset_family

    def ftyp(brand: bytes) -> bytes:
        return b"\x00\x00\x00\x20ftyp" + brand + b"\x00\x00\x02\x00"

    assert _detect_asset_family(ftyp(b"M4A ")) == "audio"
    assert _detect_asset_family(ftyp(b"M4B ")) == "audio"
    # Video and generic container brands stay video.
    assert _detect_asset_family(ftyp(b"M4V ")) == "video"
    assert _detect_asset_family(ftyp(b"isom")) == "video"
    assert _detect_asset_family(ftyp(b"mp42")) == "video"
    # The RIFF split (WEBP image vs WAVE audio) is unaffected.
    assert _detect_asset_family(b"RIFF\x00\x00\x00\x00WEBP") == "image"
    assert _detect_asset_family(b"RIFF\x00\x00\x00\x00WAVE") == "audio"


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_api_docs_not_served_by_default(client, path):
    """The generated schema lists every admin route; it is opt-in
    (WSI_API_DOCS=1), so a stock deployment must not serve it."""
    from wsi_app.config import settings

    assert settings.api_docs_enabled is False
    r = client.get(path)
    assert "swagger" not in r.text.lower() and "redoc" not in r.text.lower()
    assert '"openapi"' not in r.text


def test_short_jwt_secret_logs_a_security_warning_at_startup(monkeypatch, caplog):
    """A JWT_SECRET under 32 bytes is below HS256's key-size floor. Booting is
    not refused (an operator-supplied secret must not stop the service),
    but startup says so loudly — except in the insecure-defaults dev mode."""
    from wsi_app import auth as _auth
    from wsi_app import main as _main

    def warned() -> bool:
        caplog.clear()
        with caplog.at_level("WARNING", logger="wsi_app.startup"):
            _main._warn_short_jwt_secret()
        return any("JWT_SECRET is shorter than 32 bytes" in m for m in caplog.messages)

    monkeypatch.setattr(_auth, "_allow_insecure", False)
    monkeypatch.setattr(_auth, "JWT_SECRET", "deadbeef")
    assert warned()
    monkeypatch.setattr(_auth, "JWT_SECRET", "x" * 32)
    assert not warned()
    monkeypatch.setattr(_auth, "JWT_SECRET", "deadbeef")
    monkeypatch.setattr(_auth, "_allow_insecure", True)
    assert not warned()
