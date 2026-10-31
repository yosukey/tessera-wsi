"""Tests for the link-only ("unlisted") visibility state on slides and tours.

An unlisted item is *published* but hidden from the public browse surfaces
(index listing, search, facets, tour listings) while remaining reachable by
its direct URL — so it can be shared by link without appearing in discovery.

Since public URLs are keyed by an unguessable ``public_id`` token (never
the sequential integer PK), "reachable by its direct URL" means *by its token*:
listing is hidden, direct access **by public_id** works, and direct access
**by numeric PK returns 404** — so an unlisted item cannot be found by
walking sequential ids.

Covers:
* New slides/tours default to unlisted=False.
* Public list / facets hide an unlisted (but published) slide; its direct
  detail endpoint still serves it by public_id, and 404s by numeric PK.
* Public /api/tours and /api/slides/{public_id}/tours hide unlisted tours, and
  hide every tour of an unlisted parent slide; the direct
  /api/tours/{public_id} still serves an unlisted tour (and 404s by PK).
* The admin unlisted toggle endpoints flip state (in any publish state) and
  the admin list echoes the flag.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app.models import Slide, SlideAttribute, Tour


@pytest.fixture(autouse=True)
def _clean_slides(db_session):
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()
    yield
    db_session.execute(delete(Tour))
    db_session.execute(delete(SlideAttribute))
    db_session.execute(delete(Slide))
    db_session.commit()


@pytest.fixture
def published_slide(db_session) -> Slide:
    s = Slide(title="Visible", catalog_id="V-001", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture
def unlisted_slide(db_session) -> Slide:
    """Published + unlisted: reachable by link, hidden from browse."""
    s = Slide(title="Hidden", catalog_id="H-001", is_published=True, unlisted=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_new_slide_and_tour_default_to_listed(db_session):
    s = Slide(title="Fresh", catalog_id="F-001")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.unlisted is False

    t = Tour(slide_id=s.id, title="T")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    assert t.unlisted is False


# ---------------------------------------------------------------------------
# Slide listing vs direct access
# ---------------------------------------------------------------------------
def test_public_list_hides_unlisted_slide(client, published_slide, unlisted_slide):
    r = client.get("/api/slides")
    assert r.status_code == 200
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert published_slide.id in ids
    assert unlisted_slide.id not in ids
    # `total` reflects the browse-visible count, not the published count.
    assert body["total"] == 1


def test_public_search_does_not_surface_unlisted_slide(client, unlisted_slide):
    # Searching by the slide's own catalog_id must not reveal it.
    r = client.get("/api/slides", params={"q": "H-001"})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_public_detail_still_serves_unlisted_slide(client, unlisted_slide):
    # The whole point of link-only: the direct URL (keyed by the unguessable
    # public_id) keeps working.
    r = client.get(f"/api/slides/{unlisted_slide.public_id}")
    assert r.status_code == 200
    assert r.json()["id"] == unlisted_slide.id
    assert r.json()["public_id"] == unlisted_slide.public_id


def test_public_detail_by_numeric_id_is_404(client, unlisted_slide, published_slide):
    # Enumeration is closed: the numeric PK is never a valid public identifier,
    # so walking 1,2,3… returns 404 even for a published (or unlisted) slide.
    r = client.get(f"/api/slides/{unlisted_slide.id}")
    assert r.status_code == 404
    r = client.get(f"/api/slides/{published_slide.id}")
    assert r.status_code == 404


def test_facets_exclude_unlisted_slide_attributes(client, db_session, unlisted_slide):
    # An unlisted slide's metadata must not leak into the public filter facets
    # (otherwise the slide could be inferred from the browse filters).
    db_session.add(SlideAttribute(slide_id=unlisted_slide.id, key="stain", value="HE-secret"))
    db_session.commit()
    r = client.get("/api/facets")
    assert r.status_code == 200
    values = {item["value"] for group in r.json()["groups"].values() for item in group}
    assert "HE-secret" not in values


# ---------------------------------------------------------------------------
# Tour listing vs direct access
# ---------------------------------------------------------------------------
def test_unlisted_tour_hidden_from_listings_but_reachable_directly(
    client, db_session, published_slide
):
    tour = Tour(
        slide_id=published_slide.id, title="Secret tour",
        is_published=True, unlisted=True,
    )
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    # Global tour list: hidden.
    r = client.get("/api/tours")
    assert tour.id not in {t["id"] for t in r.json()}

    # The parent slide's own tour list: hidden too.
    r = client.get(f"/api/slides/{published_slide.public_id}/tours")
    assert r.json() == []

    # Direct link (by the tour's public_id): still works.
    r = client.get(f"/api/tours/{tour.public_id}")
    assert r.status_code == 200
    assert r.json()["tour"]["id"] == tour.id
    assert r.json()["tour"]["public_id"] == tour.public_id
    assert r.json()["tour"]["slide_public_id"] == published_slide.public_id

    # By numeric PK: 404 (enumeration closed).
    r = client.get(f"/api/tours/{tour.id}")
    assert r.status_code == 404


def test_listed_tour_of_unlisted_slide_hidden_from_global_list(
    client, db_session, unlisted_slide
):
    # A listed tour under an *unlisted* slide must not appear in the global
    # tour browse — surfacing it would re-expose the hidden slide.
    tour = Tour(
        slide_id=unlisted_slide.id, title="Listed but orphaned",
        is_published=True, unlisted=False,
    )
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    r = client.get("/api/tours")
    assert tour.id not in {t["id"] for t in r.json()}

    # But it stays reachable by its direct URL (the slide is still published).
    r = client.get(f"/api/tours/{tour.public_id}")
    assert r.status_code == 200


def test_slide_tour_list_shows_listed_tour_even_when_slide_unlisted(
    client, db_session, unlisted_slide
):
    # You reached the slide via its link, so its own tour list shows listed
    # tours; only the tour's own unlisted flag hides it here.
    listed = Tour(slide_id=unlisted_slide.id, title="Shown", is_published=True)
    hidden = Tour(
        slide_id=unlisted_slide.id, title="Hidden",
        is_published=True, unlisted=True,
    )
    db_session.add_all([listed, hidden])
    db_session.commit()
    db_session.refresh(listed)
    db_session.refresh(hidden)

    r = client.get(f"/api/slides/{unlisted_slide.public_id}/tours")
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()}
    assert listed.id in ids
    assert hidden.id not in ids


# ---------------------------------------------------------------------------
# Admin toggle endpoints
# ---------------------------------------------------------------------------
def test_admin_set_slide_unlisted_toggles_state(
    client, admin_headers, db_session, slide
):
    r = client.post(
        f"/api/admin/slides/{slide.id}/unlisted",
        data={"unlisted": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unlisted": True}
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).unlisted is True

    r = client.post(
        f"/api/admin/slides/{slide.id}/unlisted",
        data={"unlisted": "0"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).unlisted is False


def test_admin_set_slide_unlisted_settable_while_draft(
    client, admin_headers, db_session, slide
):
    # Pre-marking a draft as link-only (before publishing) must be allowed so
    # the slide never flashes onto the top page between the two toggles. The
    # `slide` fixture is a draft.
    assert slide.is_published is False
    r = client.post(
        f"/api/admin/slides/{slide.id}/unlisted",
        data={"unlisted": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).unlisted is True


def test_admin_set_tour_unlisted_toggles_state(
    client, admin_headers, db_session, published_slide
):
    tour = Tour(slide_id=published_slide.id, title="T", is_published=True)
    db_session.add(tour)
    db_session.commit()
    db_session.refresh(tour)

    r = client.post(
        f"/api/admin/tours/{tour.id}/unlisted",
        data={"unlisted": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    # The tour's new revision rides along (see Tour.revision).
    assert r.json() == {"ok": True, "unlisted": True, "revision": 1}
    db_session.expire_all()
    assert db_session.get(Tour, tour.id).unlisted is True


def test_admin_slide_list_echoes_unlisted_flag(
    client, admin_headers, published_slide, unlisted_slide
):
    r = client.get("/api/admin/slides", headers=admin_headers)
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()}
    assert items[published_slide.id]["unlisted"] is False
    assert items[unlisted_slide.id]["unlisted"] is True


def test_admin_tour_list_echoes_unlisted_flag(
    client, admin_headers, db_session, published_slide
):
    listed = Tour(slide_id=published_slide.id, title="L", is_published=True)
    hidden = Tour(
        slide_id=published_slide.id, title="H",
        is_published=True, unlisted=True,
    )
    db_session.add_all([listed, hidden])
    db_session.commit()
    db_session.refresh(listed)
    db_session.refresh(hidden)

    r = client.get("/api/admin/tours", headers=admin_headers)
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()}
    assert items[listed.id]["unlisted"] is False
    assert items[hidden.id]["unlisted"] is True


def test_set_unlisted_404s_for_unknown_slide_and_tour(client, admin_headers):
    r = client.post(
        "/api/admin/slides/999999/unlisted",
        data={"unlisted": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 404
    r = client.post(
        "/api/admin/tours/999999/unlisted",
        data={"unlisted": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 404


@pytest.mark.parametrize("role", ["admin", "manager", "instructor"])
def test_set_unlisted_allowed_for_every_staff_role(
    client, staff_client, db_session, role
):
    # Mirrors the publish toggle: any staff role may flip link-only.
    s = Slide(title="Toggle", catalog_id=f"U-{role}")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    headers = staff_client(role)
    r = client.post(
        f"/api/admin/slides/{s.id}/unlisted",
        data={"unlisted": "1"},
        headers=headers,
    )
    assert r.status_code == 200, f"role={role} got {r.status_code}: {r.text}"
