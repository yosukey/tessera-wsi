"""A tour's own metadata (tour_attributes).

Covers the admin tour API (save / read / revision / validation, and the
registered-values rule for instructors) and the metadata-option endpoints,
which treat tour attributes like slide attributes: listed as orphans,
counted, renamed and deleted with the option.
"""
from __future__ import annotations

import pytest

from wsi_app.models import MetadataOption, Slide, SlideAttribute, Tour, TourAttribute

NOT_REGISTERED = "adminTour.apiErr.attrValueNotRegistered"
TOUR_CONFLICT = "adminTour.apiErr.tourConflict"


def _make_tour(client, headers, slide_id: int) -> int:
    r = client.post(f"/api/admin/slides/{slide_id}/tours", data={"title": "T"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _detail_tour(client, headers, tour_id) -> dict:
    r = client.get(f"/api/admin/tours/{tour_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["tour"]


def _patch(client, headers, tour_id, **body):
    return client.patch(f"/api/admin/tours/{tour_id}", json=body, headers=headers)


def _tour_attrs(db_session, tour_id) -> set:
    db_session.expire_all()
    return {
        (a.key, a.value)
        for a in db_session.query(TourAttribute).filter(TourAttribute.tour_id == tour_id)
    }


def _tour_revision(db_session, tour_id) -> int:
    db_session.expire_all()
    return db_session.get(Tour, tour_id).revision


# ---------------------------------------------------------------------------
# tour API
# ---------------------------------------------------------------------------
def test_patch_saves_attributes_and_reads_them_back(client, admin_headers, slide):
    tid = _make_tour(client, admin_headers, slide.id)
    assert _detail_tour(client, admin_headers, tid)["attributes"] == {}

    r = _patch(client, admin_headers, tid, attributes={"level": [" basic ", "basic", ""], "year": ["2"]})
    assert r.status_code == 200, r.text
    # Stripped, de-duplicated, sorted; the response carries the saved copy.
    assert r.json()["tour"]["attributes"] == {"level": ["basic"], "year": ["2"]}
    assert _detail_tour(client, admin_headers, tid)["attributes"] == {"level": ["basic"], "year": ["2"]}

    listed = client.get(f"/api/admin/slides/{slide.id}/tours", headers=admin_headers).json()
    assert next(t for t in listed if t["id"] == tid)["attributes"] == {"level": ["basic"], "year": ["2"]}
    everything = client.get("/api/admin/tours", headers=admin_headers).json()
    assert next(t for t in everything if t["id"] == tid)["attributes"] == {"level": ["basic"], "year": ["2"]}


def test_attributes_replace_as_a_whole_and_omitting_them_leaves_them(client, admin_headers, slide, db_session):
    tid = _make_tour(client, admin_headers, slide.id)
    assert _patch(client, admin_headers, tid, attributes={"level": ["basic"]}).status_code == 200
    # A title-only save doesn't touch them.
    assert _patch(client, admin_headers, tid, title="Renamed").status_code == 200
    assert _tour_attrs(db_session, tid) == {("level", "basic")}
    # A new map replaces the old one, keys included; {} clears.
    assert _patch(client, admin_headers, tid, attributes={"year": ["1"]}).status_code == 200
    assert _tour_attrs(db_session, tid) == {("year", "1")}
    assert _patch(client, admin_headers, tid, attributes={}).status_code == 200
    assert _tour_attrs(db_session, tid) == set()


def test_attribute_save_is_covered_by_the_tour_revision(client, admin_headers, slide):
    tid = _make_tour(client, admin_headers, slide.id)
    rev = _detail_tour(client, admin_headers, tid)["revision"]
    r = _patch(client, admin_headers, tid, attributes={"level": ["basic"]}, expected_revision=rev)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == rev + 1
    # A save built from the old revision is refused with the current copy.
    stale = _patch(client, admin_headers, tid, attributes={"level": ["advanced"]}, expected_revision=rev)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == TOUR_CONFLICT
    assert detail["current"]["tour"]["attributes"] == {"level": ["basic"]}


@pytest.mark.parametrize("attributes", [
    {"bad:key": ["x"]},
    {"k": ["x" * 256]},
    {"k": ["a\x01b"]},
])
def test_invalid_attributes_are_refused_without_consuming_a_revision(client, admin_headers, slide, attributes):
    tid = _make_tour(client, admin_headers, slide.id)
    rev = _detail_tour(client, admin_headers, tid)["revision"]
    r = _patch(client, admin_headers, tid, attributes=attributes)
    assert r.status_code == 422, r.text
    assert _detail_tour(client, admin_headers, tid)["revision"] == rev


def test_a_non_list_value_is_refused(client, admin_headers, slide):
    tid = _make_tour(client, admin_headers, slide.id)
    assert _patch(client, admin_headers, tid, attributes={"k": "HE"}).status_code == 422


def test_instructor_may_only_pick_values_the_dashboard_offers(client, instructor_headers, slide, db_session):
    db_session.add(MetadataOption(key="ta_level", value="basic"))
    db_session.commit()
    tid = _make_tour(client, instructor_headers, slide.id)

    ok = _patch(client, instructor_headers, tid, attributes={"ta_level": ["basic"]})
    assert ok.status_code == 200, ok.text

    rev = _tour_revision(db_session, tid)
    r = _patch(client, instructor_headers, tid, attributes={"ta_level": ["basic", "made-up"]})
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": NOT_REGISTERED, "params": {"key": "ta_level", "value": "made-up"}}
    assert _tour_attrs(db_session, tid) == {("ta_level", "basic")}
    # Checked after the revision bump (under the write lock), and rolled back.
    assert _tour_revision(db_session, tid) == rev

    # A value in use on a slide but never registered is in the option list
    # the picker shows (as an orphan), so it is accepted too.
    db_session.add(SlideAttribute(slide_id=slide.id, key="ta_level", value="in-use"))
    db_session.commit()
    ok = _patch(client, instructor_headers, tid, attributes={"ta_level": ["in-use"]})
    assert ok.status_code == 200, ok.text


def test_the_vocabulary_check_runs_inside_the_write_lock(client, instructor_headers, slide, db_session, monkeypatch):
    """After the revision bump, so a rename / delete can't commit between the
    check and the write: the check sees the bumped revision in its session."""
    from sqlalchemy import select
    from wsi_app.routers import admin_tours

    db_session.add(MetadataOption(key="ta_lock", value="v"))
    db_session.commit()
    tid = _make_tour(client, instructor_headers, slide.id)
    rev = _tour_revision(db_session, tid)
    seen = []
    real = admin_tours._check_tour_vocabulary

    def spy(db, cleaned, user):
        seen.append(db.execute(select(Tour.revision).where(Tour.id == tid)).scalar_one())
        return real(db, cleaned, user)

    monkeypatch.setattr(admin_tours, "_check_tour_vocabulary", spy)
    assert _patch(client, instructor_headers, tid, attributes={"ta_lock": ["v"]}).status_code == 200
    assert seen == [rev + 1]


def test_instructor_may_keep_or_clear_a_value_the_tour_already_carries(
    client, admin_headers, instructor_headers, slide, db_session,
):
    tid = _make_tour(client, admin_headers, slide.id)
    # A manager-or-above may store an unregistered value (as on slides).
    assert _patch(client, admin_headers, tid, attributes={"ta_orphan": ["free"]}).status_code == 200
    # The instructor's save that keeps it passes; so does one that drops it.
    assert _patch(client, instructor_headers, tid, attributes={"ta_orphan": ["free"]}).status_code == 200
    assert _patch(client, instructor_headers, tid, attributes={}).status_code == 200
    assert _tour_attrs(db_session, tid) == set()


def test_deleting_the_tour_or_its_slide_drops_its_attributes(client, admin_headers, db_session):
    s = Slide(title="S", catalog_id="TA-DEL")
    db_session.add(s)
    db_session.commit()
    t1 = _make_tour(client, admin_headers, s.id)
    t2 = _make_tour(client, admin_headers, s.id)
    for tid in (t1, t2):
        assert _patch(client, admin_headers, tid, attributes={"k": ["v"]}).status_code == 200
    assert client.delete(f"/api/admin/tours/{t1}", headers=admin_headers).status_code == 200
    assert _tour_attrs(db_session, t1) == set()
    assert client.delete(f"/api/admin/slides/{s.id}", headers=admin_headers).status_code == 200
    assert _tour_attrs(db_session, t2) == set()


# ---------------------------------------------------------------------------
# metadata options
# ---------------------------------------------------------------------------
def _tour_with(db_session, slide_id: int, pairs, *, published=False) -> int:
    t = Tour(slide_id=slide_id, title="T", is_published=published)
    db_session.add(t)
    db_session.flush()
    for k, v in pairs:
        db_session.add(TourAttribute(tour_id=t.id, key=k, value=v))
    db_session.commit()
    return t.id


def test_option_list_includes_values_found_only_on_tours(client, admin_headers, slide, db_session):
    _tour_with(db_session, slide.id, [("ta_list_key", "only-on-a-tour")])
    options = client.get("/api/admin/metadata/options", headers=admin_headers).json()["options"]
    assert options["ta_list_key"] == ["only-on-a-tour"]


def test_option_usage_and_counts_cover_tours(client, admin_headers, db_session):
    # "Published" means served publicly: the tour and its slide.
    live = Slide(title="Live", catalog_id="TA-LIVE", is_published=True)
    draft = Slide(title="Draft", catalog_id="TA-DRAFT")
    db_session.add_all([live, draft])
    db_session.commit()
    _tour_with(db_session, live.id, [("ta_use", "x")], published=True)
    _tour_with(db_session, live.id, [("ta_use", "x")])
    _tour_with(db_session, draft.id, [("ta_use", "x")], published=True)
    usage = client.get(
        "/api/admin/metadata/options/usage", params={"key": "ta_use", "value": "x"}, headers=admin_headers,
    ).json()
    assert (usage["slide_count"], usage["tour_count"], usage["published_tour_count"]) == (0, 3, 1)
    key_usage = client.get(
        "/api/admin/metadata/keys/usage", params={"key": "ta_use"}, headers=admin_headers,
    ).json()
    assert (key_usage["tour_count"], key_usage["published_tour_count"]) == (3, 1)
    counts = client.get("/api/admin/metadata/counts", headers=admin_headers).json()
    assert counts["tour_values"]["ta_use"]["x"] == {"total": 3, "published": 1}
    assert counts["tour_keys"]["ta_use"] == {"total": 3, "published": 1}
    assert "ta_use" not in counts["values"]


def test_option_delete_strips_tours_and_bumps_their_revision(client, admin_headers, slide, db_session):
    db_session.add(MetadataOption(key="ta_del", value="v"))
    db_session.commit()
    tid = _tour_with(db_session, slide.id, [("ta_del", "v"), ("ta_del", "keep")])
    rev = _tour_revision(db_session, tid)
    usage = client.get(
        "/api/admin/metadata/options/usage", params={"key": "ta_del", "value": "v"}, headers=admin_headers,
    ).json()
    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "ta_del", "value": "v", "expected_fingerprint": usage["slides_fingerprint"]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["tours_affected"], r.json()["tour_attrs_removed"]) == (1, 1)
    assert _tour_attrs(db_session, tid) == {("ta_del", "keep")}
    assert _tour_revision(db_session, tid) == rev + 1


def test_option_delete_refuses_when_the_confirmed_tour_set_changed(client, admin_headers, slide, db_session):
    _tour_with(db_session, slide.id, [("ta_fp", "v")])
    usage = client.get(
        "/api/admin/metadata/options/usage", params={"key": "ta_fp", "value": "v"}, headers=admin_headers,
    ).json()
    # Another tour picks the value up after the confirmation dialog showed.
    late = _tour_with(db_session, slide.id, [("ta_fp", "v")])
    r = client.delete(
        "/api/admin/metadata/options",
        params={"key": "ta_fp", "value": "v", "expected_fingerprint": usage["slides_fingerprint"]},
        headers=admin_headers,
    )
    assert r.status_code == 409
    assert r.json() == {"ok": False, "changed": True, "slide_count": 0, "tour_count": 2}
    assert _tour_attrs(db_session, late) == {("ta_fp", "v")}


def test_the_fingerprint_is_unchanged_for_slide_only_values(client, admin_headers, db_session):
    """Values no tour carries keep the slide-only fingerprint."""
    from wsi_app.routers.admin_metadata import _slide_set_fingerprint, _usage_fingerprint

    assert _usage_fingerprint([3, 1, 2]) == _slide_set_fingerprint([1, 2, 3])
    assert _usage_fingerprint([1], [5]) != _usage_fingerprint([1])


def test_key_delete_strips_tours(client, admin_headers, slide, db_session):
    db_session.add(MetadataOption(key="ta_key_del", value="a"))
    db_session.commit()
    tid = _tour_with(db_session, slide.id, [("ta_key_del", "a"), ("ta_other", "b")])
    r = client.delete("/api/admin/metadata/keys", params={"key": "ta_key_del"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["tours_affected"] == 1
    assert _tour_attrs(db_session, tid) == {("ta_other", "b")}


def test_rename_carries_tours_and_bumps_their_revision(client, admin_headers, slide, db_session):
    db_session.add(MetadataOption(key="ta_ren", value="old"))
    db_session.commit()
    tid = _tour_with(db_session, slide.id, [("ta_ren", "old")])
    rev = _tour_revision(db_session, tid)
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ta_ren", "old_value": "old", "new_value": "new"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["tours_affected"], r.json()["tour_attrs_updated"]) == (1, 1)
    assert _tour_attrs(db_session, tid) == {("ta_ren", "new")}
    assert _tour_revision(db_session, tid) == rev + 1


def test_rename_of_a_value_only_tours_carry_registers_it(client, admin_headers, slide, db_session):
    tid = _tour_with(db_session, slide.id, [("ta_ren_orphan", "old")])
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ta_ren_orphan", "old_value": "old", "new_value": "new"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _tour_attrs(db_session, tid) == {("ta_ren_orphan", "new")}
    db_session.expire_all()
    assert db_session.query(MetadataOption).filter_by(key="ta_ren_orphan", value="new").count() == 1


def test_rename_onto_a_value_a_tour_carries_is_refused(client, admin_headers, slide, db_session):
    db_session.add(MetadataOption(key="ta_ren_dup", value="a"))
    db_session.add(SlideAttribute(slide_id=slide.id, key="ta_ren_dup", value="a"))
    db_session.commit()
    _tour_with(db_session, slide.id, [("ta_ren_dup", "b")])
    r = client.patch(
        "/api/admin/metadata/options",
        data={"key": "ta_ren_dup", "old_value": "a", "new_value": "b"},
        headers=admin_headers,
    )
    assert r.status_code == 409
    assert r.json()["target_exists"] is True
