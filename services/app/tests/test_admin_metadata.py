"""Tests for the admin metadata-options endpoints.

Covers:
- Cascade delete on slide_attributes (so the dashboard's "remove from N
  slides" warning isn't a lie).
- The 409 duplicate-add response shape the frontend depends on, including
  the race recovery when two requests slip past the SELECT.
- The orphan keys/values UNION in the list endpoint.
- The published/draft split in the usage endpoints.
- The slides_affected count used by the dashboard's TOCTOU guard.
- DELETE-as-query-params (the form-body variant is non-idiomatic and
  vulnerable to proxy body-stripping).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from wsi_app.models import MetadataOption, Slide, SlideAttribute


def test_list_options_unions_metadata_and_orphan_attributes(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="union_tags", value="rare"))
    db_session.add(MetadataOption(key="union_diag", value="benign"))
    _make_slide(db_session, attrs=[("union_tags", "common"), ("union_notes", "free-form")])
    db_session.commit()

    r = client.get("/api/admin/metadata/options", headers=admin_headers)
    assert r.status_code == 200, r.text
    options = r.json()["options"]
    # Registered + orphan values merged under their keys, deduped & sorted.
    assert options["union_tags"] == ["common", "rare"]
    assert options["union_diag"] == ["benign"]
    # Key that exists only via slide_attributes still surfaces.
    assert options["union_notes"] == ["free-form"]
    # Base filter keys still come back even with no entries.
    for base in ["stain", "organ", "lab"]:
        assert base in options


def test_option_usage_splits_drafts_and_published(client, admin_headers, db_session):
    _make_slide(db_session, attrs=[("opt_usage_key", "x")], published=True)
    _make_slide(db_session, attrs=[("opt_usage_key", "x")], published=False)
    db_session.commit()

    r = client.get(
        "/api/admin/metadata/options/usage",
        params={"key": "opt_usage_key", "value": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slide_count"] == 2
    assert body["published_slide_count"] == 1


def test_key_usage_splits_drafts_and_published(client, admin_headers, db_session):
    _make_slide(db_session, attrs=[("key_usage_key", "a")], published=True)
    _make_slide(db_session, attrs=[("key_usage_key", "b")], published=False)
    _make_slide(db_session, attrs=[("key_usage_key", "c")], published=False)
    db_session.commit()

    r = client.get(
        "/api/admin/metadata/keys/usage",
        params={"key": "key_usage_key"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slide_count"] == 3
    assert body["published_slide_count"] == 1


def test_delete_option_cascades_to_slide_attributes(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="del_opt_key", value="rare"))
    s_id = _make_slide(db_session, attrs=[("del_opt_key", "rare"), ("del_opt_key", "keep")])
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "del_opt_key", "value": "rare"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["options_removed"] == 1
    assert body["slide_attrs_removed"] == 1
    # Distinct slides affected matches rowcount here (UNIQUE constraint).
    assert body["slides_affected"] == 1

    db_session.expire_all()
    rows = (
        db_session.query(SlideAttribute)
        .filter(SlideAttribute.slide_id == s_id)
        .order_by(SlideAttribute.value.asc())
        .all()
    )
    assert [r.value for r in rows] == ["keep"]
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "del_opt_key", MetadataOption.value == "rare")
        .count()
        == 0
    )


def test_delete_option_when_only_attributes_exist_still_cleans_slides(
    client, admin_headers, db_session
):
    """An orphan attribute (no MetadataOption row) is still cleared."""
    s_id = _make_slide(db_session, attrs=[("orphan_only_key", "HE_orphan")])
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "orphan_only_key", "value": "HE_orphan"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["options_removed"] == 0
    assert body["slide_attrs_removed"] == 1
    assert body["slides_affected"] == 1

    db_session.expire_all()
    assert (
        db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id).count()
        == 0
    )


def test_delete_option_matching_nothing_releases_write_lock_before_audit(
    client, admin_headers, db_session
):
    """A no-op delete must not hold the SQLite write lock into audit().

    The 0-row DELETE still opens a write transaction; left uncommitted, the
    audit row (written on another connection) would wait out busy_timeout
    (~5 s) and then be lost.
    """
    import time

    from sqlalchemy import select

    from wsi_app.models import AuditLog

    t0 = time.monotonic()
    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "noop_del_key", "value": "nothing-here"},
        headers=admin_headers,
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert r.json()["options_removed"] == 0
    assert r.json()["slide_attrs_removed"] == 0
    assert elapsed < 2.0, f"delete stalled {elapsed:.1f}s on the SQLite write lock"

    db_session.expire_all()
    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "metadata.option.delete",
            AuditLog.target == "noop_del_key=nothing-here",
        )
    ).scalars().all()
    assert len(rows) == 1


def test_delete_key_cascades_across_all_slides(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="del_key_cascade", value="malignant"))
    db_session.add(MetadataOption(key="del_key_cascade", value="benign"))
    a = _make_slide(db_session, attrs=[("del_key_cascade", "malignant"), ("del_key_cascade_keep", "x")])
    b = _make_slide(db_session, attrs=[("del_key_cascade", "benign")])
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/keys",
        params={"key": "del_key_cascade"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["options_removed"] == 2
    assert body["slide_attrs_removed"] == 2
    assert body["slides_affected"] == 2

    db_session.expire_all()
    keys_a = {row.key for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == a)}
    keys_b = {row.key for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == b)}
    assert keys_a == {"del_key_cascade_keep"}
    assert keys_b == set()
    assert db_session.query(MetadataOption).filter(MetadataOption.key == "del_key_cascade").count() == 0


def test_delete_key_slides_affected_distinct_from_rowcount(client, admin_headers, db_session):
    """A single slide carrying multiple values under one key counts once."""
    _make_slide(db_session, attrs=[("del_key_multi", "a"), ("del_key_multi", "b"), ("del_key_multi", "c")])
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/keys",
        params={"key": "del_key_multi"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slide_attrs_removed"] == 3
    assert body["slides_affected"] == 1


def test_delete_base_key_is_rejected(client, admin_headers, db_session):
    s_id = _make_slide(db_session, attrs=[("stain", "HE_base_test")])
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/keys",
        params={"key": "stain"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    # The base-key guard must run before any cascade — the slide row should
    # be untouched.
    db_session.expire_all()
    assert (
        db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id).count()
        == 1
    )


def test_add_option_duplicate_returns_409_with_duplicate_flag(client, admin_headers):
    r1 = client.post(
        "/api/admin/metadata/options",
        data={"key": "dup_test_key", "value": "dup"},
        headers=admin_headers,
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        "/api/admin/metadata/options",
        data={"key": "dup_test_key", "value": "dup"},
        headers=admin_headers,
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body.get("duplicate") is True
    assert body.get("ok") is False
    # Same row reused — no second insert.
    assert isinstance(body.get("id"), int)


def test_add_option_recovers_to_409_when_commit_races(client, admin_headers, db_session):
    """Two concurrent admins can both slip past the SELECT-then-INSERT
    window; the unique constraint then fires on commit. The endpoint must
    rollback, re-query, and return the same 409 shape — not bubble a 500."""
    from sqlalchemy.orm import Session
    from wsi_app.db import SessionLocal

    original_commit = Session.commit
    state = {"raced": False}

    def commit_with_race(self: Session, *args, **kwargs):
        if not state["raced"]:
            state["raced"] = True
            # Simulate a parallel admin committing the same (key, value)
            # row first, *between* the route's SELECT and its commit.
            other = SessionLocal()
            try:
                other.add(MetadataOption(key="race_key", value="race_val"))
                original_commit(other)
            finally:
                other.close()
            # Now the route's pending INSERT collides with uq_metadata_option.
            return original_commit(self, *args, **kwargs)
        return original_commit(self, *args, **kwargs)

    with patch.object(Session, "commit", commit_with_race):
        r = client.post(
            "/api/admin/metadata/options",
            data={"key": "race_key", "value": "race_val"},
            headers=admin_headers,
        )

    assert r.status_code == 409, r.text
    body = r.json()
    assert body.get("duplicate") is True
    assert body.get("ok") is False
    assert isinstance(body.get("id"), int)
    # The IntegrityError path was actually exercised.
    assert state["raced"] is True
    # Only one row in the DB — the racing insert, not a duplicate.
    rows = (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "race_key", MetadataOption.value == "race_val")
        .all()
    )
    assert len(rows) == 1


def _make_slide(db_session, attrs, published: bool = False):
    s = Slide(title="t", catalog_id="c", is_published=published)
    db_session.add(s)
    db_session.flush()
    for k, v in attrs:
        db_session.add(SlideAttribute(slide_id=s.id, key=k, value=v))
    return s.id


# ---------------------------------------------------------------------------
# RBAC role matrix: metadata endpoints are gated by ``require_manager_or_above``
# — except the option list, which every staff role reads (instructors pick
# tour metadata from it in the tour editor).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 200),
])
def test_list_options_role_matrix(client, staff_client, role, expected):
    headers = staff_client(role)
    r = client.get("/api/admin/metadata/options", headers=headers)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_post_option_role_matrix(client, staff_client, role, expected):
    headers = staff_client(role)
    r = client.post(
        "/api/admin/metadata/options",
        data={"key": "rbac_key", "value": "v"},
        headers=headers,
    )
    # Admin/manager 200 (or 409 on duplicate across parametrise runs); the
    # 403 boundary is what this pins for instructors.
    if expected == 403:
        assert r.status_code == 403
    else:
        assert r.status_code in (200, 409), r.text


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_metadata_keys_usage_role_matrix(client, staff_client, role, expected):
    headers = staff_client(role)
    r = client.get(
        "/api/admin/metadata/keys/usage",
        params={"key": "any_key"},
        headers=headers,
    )
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_metadata_counts_role_matrix(client, staff_client, role, expected):
    headers = staff_client(role)
    r = client.get("/api/admin/metadata/counts", headers=headers)
    assert r.status_code == expected, r.text


# ---------------------------------------------------------------------------
# Error-code contract: validation failures return a language-independent
# {code, params} detail (translated client-side) instead of hardcoded
# single-language text, so the dashboard renders errors in the active locale.
# ---------------------------------------------------------------------------
def test_add_option_blank_key_returns_error_code(client, admin_headers):
    r = client.post(
        "/api/admin/metadata/options",
        data={"key": "  ", "value": "x"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "adminDash.apiErr.metaKeyRequired"
    # No params for this code; the message is a plain string client-side.
    assert "params" not in detail


def test_add_option_long_value_returns_code_with_params(client, admin_headers):
    r = client.post(
        "/api/admin/metadata/options",
        data={"key": "stain", "value": "x" * 1000},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "adminDash.apiErr.metaValMaxLen"
    # Interpolation param the frontend needs to render the limit.
    assert detail["params"]["max"] == 255


def test_delete_base_category_returns_error_code(client, admin_headers):
    r = client.delete(
        "/api/admin/metadata/keys",
        params={"key": "stain"},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.baseCategoryImmutable"


# ---------------------------------------------------------------------------
# Rename (PATCH): a value is renamed in place so the slides that carry it keep
# their assignment — the whole point over delete+re-add. Both the
# MetadataOption master row and the slide_attributes rows must move together.
# ---------------------------------------------------------------------------
def test_rename_value_moves_option_and_slide_attrs(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="ren_key", value="old"))
    s_id = _make_slide(db_session, attrs=[("ren_key", "old"), ("ren_key", "keep")])
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_key", "old_value": "old", "new_value": "new"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["renamed"] is True
    assert body["slides_affected"] == 1
    assert body["slide_attrs_updated"] == 1

    db_session.expire_all()
    # Master list moved old -> new.
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "ren_key", MetadataOption.value == "old")
        .count()
        == 0
    )
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "ren_key", MetadataOption.value == "new")
        .count()
        == 1
    )
    # The slide's assignment followed the rename; the unrelated value stayed.
    vals = sorted(
        row.value
        for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id)
    )
    assert vals == ["keep", "new"]


def test_rename_value_noop_when_same(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="ren_same", value="v"))
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_same", "old_value": "v", "new_value": "v"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is False
    assert body["slides_affected"] == 0

    db_session.expire_all()
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "ren_same", MetadataOption.value == "v")
        .count()
        == 1
    )


def test_rename_value_orphan_only_registers_target(client, admin_headers, db_session):
    """A value that exists only on slides (never registered) is renamable and
    the target becomes a registered option."""
    s_id = _make_slide(db_session, attrs=[("ren_orphan", "old_orphan")])
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_orphan", "old_value": "old_orphan", "new_value": "new_orphan"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is True
    assert body["slide_attrs_updated"] == 1

    db_session.expire_all()
    # Slide assignment moved.
    vals = [
        row.value
        for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id)
    ]
    assert vals == ["new_orphan"]
    # The value is a registered option under its new name.
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "ren_orphan", MetadataOption.value == "new_orphan")
        .count()
        == 1
    )


def test_rename_value_reject_when_target_registered(client, admin_headers, db_session):
    db_session.add(MetadataOption(key="ren_dup", value="old"))
    db_session.add(MetadataOption(key="ren_dup", value="target"))
    s_id = _make_slide(db_session, attrs=[("ren_dup", "old")])
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_dup", "old_value": "old", "new_value": "target"},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["target_exists"] is True

    # Nothing changed: both options remain and the slide still carries old.
    db_session.expire_all()
    assert (
        db_session.query(MetadataOption).filter(MetadataOption.key == "ren_dup").count() == 2
    )
    vals = [
        row.value
        for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id)
    ]
    assert vals == ["old"]


def test_rename_value_reject_when_target_on_slides_only(client, admin_headers, db_session):
    """The target need not be registered — an orphan attribute carrying the new
    name on any slide is also a collision (the dashboard cloud shows it)."""
    a = _make_slide(db_session, attrs=[("ren_soft", "old")])
    b = _make_slide(db_session, attrs=[("ren_soft", "target")])
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_soft", "old_value": "old", "new_value": "target"},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["target_exists"] is True

    db_session.expire_all()
    assert [row.value for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == a)] == ["old"]
    assert [row.value for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == b)] == ["target"]


def test_rename_value_under_base_key_allowed(client, admin_headers, db_session):
    """Renaming a value under a base key (e.g. stain) is fine — only KEY
    mutation is base-immutable, not the values within it."""
    db_session.add(MetadataOption(key="stain", value="HandE"))
    s_id = _make_slide(db_session, attrs=[("stain", "HandE")])
    db_session.commit()

    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "stain", "old_value": "HandE", "new_value": "H&E"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] is True

    db_session.expire_all()
    assert [row.value for row in db_session.query(SlideAttribute).filter(SlideAttribute.slide_id == s_id)] == ["H&E"]


def test_rename_value_blank_new_value_returns_error_code(client, admin_headers):
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_val_key", "old_value": "old", "new_value": "  "},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.metaValRequired"


def test_rename_value_long_new_value_returns_code_with_params(client, admin_headers):
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ren_val_key", "old_value": "old", "new_value": "x" * 1000},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "adminDash.apiErr.metaValMaxLen"
    assert detail["params"]["max"] == 255


def test_rename_value_no_match_returns_404(client, admin_headers, db_session):
    """Renaming an old_value that exists nowhere (no registered option, no
    slide attribute) must 404 rather than silently registering a brand-new
    (key, new_value) option — a rename must carry an existing value forward."""
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "nomatch_key", "old_value": "ghost", "new_value": "target"},
        headers=admin_headers,
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["not_found"] is True
    # Nothing was written for either the old or the new value.
    db_session.expire_all()
    assert (
        db_session.query(MetadataOption)
        .filter(MetadataOption.key == "nomatch_key")
        .count()
        == 0
    )


def test_rename_value_validates_key(client, admin_headers, db_session):
    """The rename path applies the same key validation as add, so a key
    with a control character is a 422 rather than registering the malformed
    key via the orphan branch."""
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "bad\x1bkey", "old_value": "old", "new_value": "new"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.metaKeyCtrlChar"
    db_session.expire_all()
    assert db_session.query(MetadataOption).filter(MetadataOption.key == "bad\x1bkey").count() == 0


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("manager", 200),
    ("instructor", 403),
])
def test_rename_option_role_matrix(client, staff_client, role, expected):
    headers = staff_client(role)
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "rbac_ren_key", "old_value": "old", "new_value": "new"},
        headers=headers,
    )
    if expected == 403:
        assert r.status_code == 403
    else:
        # old_value "old" matches nothing (no registered option, no slide
        # attribute), so the rename 404s rather than registering a brand-new
        # (key, new) option (see test_rename_value_no_match_returns_404).
        # A 404 still proves the request cleared the auth gate.
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# A delete is conditional on the slide set
# the user confirmed, not merely warned about afterwards
# ---------------------------------------------------------------------------
def test_option_delete_refuses_when_the_confirmed_slide_set_changed(client, admin_headers, db_session):
    a = _make_slide(db_session, attrs=[("fp_key", "v")])
    b = _make_slide(db_session, attrs=[])
    db_session.add(MetadataOption(key="fp_key", value="v"))
    db_session.commit()
    usage = client.get(
        "/api/admin/metadata/options/usage", params={"key": "fp_key", "value": "v"},
        headers=admin_headers,
    ).json()
    assert usage["slide_count"] == 1

    # Same count, different slide: a count comparison could not see this.
    db_session.query(SlideAttribute).filter_by(slide_id=a, key="fp_key").delete()
    db_session.add(SlideAttribute(slide_id=b, key="fp_key", value="v"))
    db_session.commit()

    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "fp_key", "value": "v", "expected_fingerprint": usage["slides_fingerprint"]},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json() == {"ok": False, "changed": True, "slide_count": 1, "tour_count": 0}
    db_session.expire_all()
    assert db_session.query(SlideAttribute).filter_by(slide_id=b, key="fp_key").count() == 1
    assert db_session.query(MetadataOption).filter_by(key="fp_key").count() == 1

    usage = client.get(
        "/api/admin/metadata/options/usage", params={"key": "fp_key", "value": "v"},
        headers=admin_headers,
    ).json()
    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "fp_key", "value": "v", "expected_fingerprint": usage["slides_fingerprint"]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["slides_affected"] == 1


def test_key_delete_refuses_when_the_confirmed_slide_set_changed(client, admin_headers, db_session):
    _make_slide(db_session, attrs=[("fp_cat", "x"), ("fp_cat", "y")])
    db_session.add(MetadataOption(key="fp_cat", value="x"))
    db_session.commit()
    usage = client.get(
        "/api/admin/metadata/keys/usage", params={"key": "fp_cat"}, headers=admin_headers,
    ).json()
    _make_slide(db_session, attrs=[("fp_cat", "z")])
    db_session.commit()
    r = client.delete(
        "/api/admin/metadata/keys",
        params={"key": "fp_cat", "expected_fingerprint": usage["slides_fingerprint"]},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["slide_count"] == 2
    db_session.expire_all()
    assert db_session.query(SlideAttribute).filter_by(key="fp_cat").count() == 3
    # Without a fingerprint (scripts, non-SPA callers) the delete still runs.
    r = client.delete("/api/admin/metadata/keys", params={"key": "fp_cat"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slides_affected"] == 2
