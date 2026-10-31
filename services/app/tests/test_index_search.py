"""Server-side unified search for the public index page.

Search results assembled client-side from two capped slide lists plus the
full /api/tours dump can't see past the cap: a slide matching only through
a tour title, or a tour whose parent slide has to be checked against an
active filter, would go missing once the catalog outgrew ``max_index_items``.
So the search runs in SQL:

* ``GET /api/slides?q=`` also matches slides through their published, listed
  tours' titles, reports those tours per slide (``matched_tours``), and caps /
  counts the combined match set; ``match_tours=false`` restricts ``q`` to the
  slide's own fields (the "Slides" view).
* ``GET /api/index/tours`` is the "Tours" view: tours matching ``q`` on their
  own title or their parent slide's fields, narrowed by the same filters on
  the tours' own metadata, in slide-list order (by tour title for a title sort), capped with a total, each carrying its parent's card
  data.
* ``GET /api/tours`` (no parameters) keeps its own shape for other callers.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app import app_auth as _app_auth
from wsi_app.models import AuthSettings, Slide, SlideAttribute, Tour, TourAttribute


@pytest.fixture(autouse=True)
def _clean_catalog(db_session):
    def _wipe():
        db_session.execute(delete(TourAttribute))
        db_session.execute(delete(Tour))
        db_session.execute(delete(SlideAttribute))
        db_session.execute(delete(Slide))
        db_session.commit()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def make_slide(db_session):
    def _make(catalog_id: str, title: str = "", *, published: bool = True,
              unlisted: bool = False, attrs: dict | None = None) -> Slide:
        s = Slide(catalog_id=catalog_id, title=title or f"Slide {catalog_id}",
                  is_published=published, unlisted=unlisted)
        db_session.add(s)
        db_session.flush()
        for key, values in (attrs or {}).items():
            for v in values:
                db_session.add(SlideAttribute(slide_id=s.id, key=key, value=v))
        db_session.commit()
        db_session.refresh(s)
        return s
    return _make


@pytest.fixture
def make_tour(db_session):
    def _make(slide: Slide, title: str, *, published: bool = True, unlisted: bool = False,
              attrs: dict | None = None) -> Tour:
        t = Tour(slide_id=slide.id, title=title, is_published=published, unlisted=unlisted)
        db_session.add(t)
        db_session.flush()
        for key, values in (attrs or {}).items():
            for v in values:
                db_session.add(TourAttribute(tour_id=t.id, key=key, value=v))
        db_session.commit()
        db_session.refresh(t)
        return t
    return _make


def _slides(client, **params):
    r = client.get("/api/slides", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _tour_index(client, **params):
    r = client.get("/api/index/tours", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# /api/slides: tour-title matches
# ---------------------------------------------------------------------------
def test_tour_title_only_match_past_the_cap_is_returned_and_counted(client, make_slide, make_tour):
    # Twelve slides; the last one by catalog id matches "mitosis" only through
    # its tour. With a client-side merge, a per_page=5 cap on the
    # unfiltered list would cut that slide off and no card could be built
    # for it.
    slides = [make_slide(f"S-{i:02d}") for i in range(12)]
    target = slides[-1]
    tour = make_tour(target, "Mitosis walkthrough")
    make_tour(target, "Overview")  # same slide, title doesn't match

    body = _slides(client, q="mitosis", sort="catalog_id", per_page=5)
    assert body["total"] == 1
    assert [it["id"] for it in body["items"]] == [target.id]
    assert body["items"][0]["matched_tours"] == [
        {"id": tour.id, "public_id": tour.public_id, "title": "Mitosis walkthrough"}
    ]


def test_cap_and_total_cover_the_combined_match_set(client, make_slide, make_tour):
    # Two slides match on their own title, one only through a tour title, and
    # it sorts last: the cap applies to the union in server order, and total
    # counts all three so the truncation banner reports the real hit count.
    a = make_slide("A-1", "Mitosis atlas")
    b = make_slide("B-1", "Mitotic figures in mitosis")
    make_slide("C-1", "Unrelated")
    d = make_slide("D-1", "Liver")
    d_tour = make_tour(d, "Spot the MITOSIS")

    capped = _slides(client, q="mitosis", sort="catalog_id", per_page=2)
    assert capped["total"] == 3
    assert [it["id"] for it in capped["items"]] == [a.id, b.id]

    full = _slides(client, q="mitosis", sort="catalog_id", per_page=50)
    assert full["total"] == 3
    by_id = {it["id"]: it for it in full["items"]}
    assert list(by_id) == [a.id, b.id, d.id]
    # Slides matching on their own fields carry no matched tours.
    assert by_id[a.id]["matched_tours"] == []
    assert by_id[b.id]["matched_tours"] == []
    assert [m["public_id"] for m in by_id[d.id]["matched_tours"]] == [d_tour.public_id]

    # page 2 continues the same ordering.
    page2 = _slides(client, q="mitosis", sort="catalog_id", per_page=2, page=2)
    assert [it["id"] for it in page2["items"]] == [d.id]


def test_matched_tours_also_listed_when_the_slide_matches_itself(client, make_slide, make_tour):
    # matched_tours explains a tour match whatever else matched, so the card
    # can still pull the matching tour to the front of its inline list.
    s = make_slide("M-1", "Mitosis atlas")
    make_tour(s, "Overview")
    t2 = make_tour(s, "Mitosis close-up")
    t3 = make_tour(s, "More mitosis")
    item = _slides(client, q="mitosis")["items"][0]
    assert [m["id"] for m in item["matched_tours"]] == [t2.id, t3.id]


def test_draft_or_unlisted_tours_never_cause_a_match(client, make_slide, make_tour):
    draft_parent = make_slide("X-1")
    make_tour(draft_parent, "Mitosis draft", published=False)
    unlisted_parent = make_slide("X-2")
    make_tour(unlisted_parent, "Mitosis link-only", unlisted=True)
    # A listed tour on a link-only or draft slide must not surface the slide.
    hidden_slide = make_slide("X-3", unlisted=True)
    make_tour(hidden_slide, "Mitosis on hidden slide")
    draft_slide = make_slide("X-4", published=False)
    make_tour(draft_slide, "Mitosis on draft slide")

    body = _slides(client, q="mitosis")
    assert body["items"] == []
    assert body["total"] == 0

    # And on a slide that does match, only the listed tour is reported.
    mixed = make_slide("X-5")
    listed = make_tour(mixed, "Mitosis public")
    make_tour(mixed, "Mitosis draft", published=False)
    make_tour(mixed, "Mitosis link-only", unlisted=True)
    body = _slides(client, q="mitosis")
    assert [it["id"] for it in body["items"]] == [mixed.id]
    assert [m["id"] for m in body["items"][0]["matched_tours"]] == [listed.id]


def test_match_tours_false_searches_slide_fields_only(client, make_slide, make_tour):
    own = make_slide("O-1", "Mitosis atlas")
    via_tour = make_slide("T-1", "Liver")
    make_tour(via_tour, "Mitosis tour")

    for off in ("false", "0"):  # the index page sends 0
        body = _slides(client, q="mitosis", match_tours=off)
        assert [it["id"] for it in body["items"]] == [own.id]
        assert body["total"] == 1
        assert body["items"][0]["matched_tours"] == []

    # Default: tours count.
    assert {it["id"] for it in _slides(client, q="mitosis")["items"]} == {own.id, via_tour.id}


def test_no_q_lists_everything_without_matched_tours(client, make_slide, make_tour):
    s = make_slide("N-1")
    make_tour(s, "Mitosis")
    body = _slides(client)
    assert body["total"] == 1
    assert body["items"][0]["matched_tours"] == []


def test_filters_apply_to_tour_title_matches(client, make_slide, make_tour):
    he = make_slide("F-1", attrs={"stain": ["HE"], "grade": ["II"]})
    make_tour(he, "Mitosis HE")
    pas = make_slide("F-2", attrs={"stain": ["PAS"], "grade": ["II"]})
    make_tour(pas, "Mitosis PAS")

    assert [it["id"] for it in _slides(client, q="mitosis", stain="HE")["items"]] == [he.id]
    body = _slides(client, q="mitosis", facet="grade:II", sort="catalog_id")
    assert [it["id"] for it in body["items"]] == [he.id, pas.id]
    assert _slides(client, q="mitosis", facet="grade:III")["total"] == 0


def _count_queries(fn) -> int:
    """Statements the engine runs while ``fn`` executes."""
    from sqlalchemy import event
    from wsi_app.db import engine

    seen: list[str] = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)
    return len(seen)


def test_matched_tours_and_tour_cards_cost_no_query_per_row(client, make_slide, make_tour):
    # The index page is the busiest public endpoint: matched tours (and the
    # tour index's parent card data) must come from a fixed number of queries,
    # not one per returned slide or tour.
    def seed(n: int, prefix: str):
        for i in range(n):
            s = make_slide(f"{prefix}-{i:03d}")
            make_tour(s, f"Mitosis {prefix} {i}")
            make_tour(s, f"Other {prefix} {i}")

    seed(3, "A")
    small_slides = _count_queries(lambda: _slides(client, q="mitosis", per_page=500))
    small_tours = _count_queries(lambda: _tour_index(client, q="mitosis", per_page=500))
    seed(40, "B")
    body = _slides(client, q="mitosis", per_page=500)
    assert body["total"] == 43
    assert all(len(it["matched_tours"]) == 1 for it in body["items"])
    assert _count_queries(lambda: _slides(client, q="mitosis", per_page=500)) == small_slides
    assert _count_queries(lambda: _tour_index(client, q="mitosis", per_page=500)) == small_tours


# ---------------------------------------------------------------------------
# /api/index/tours
# ---------------------------------------------------------------------------
def test_tour_index_visibility_matches_api_tours(client, make_slide, make_tour):
    s = make_slide("V-1", "Visible slide")
    listed = make_tour(s, "Listed")
    make_tour(s, "Draft", published=False)
    make_tour(s, "Link-only", unlisted=True)
    make_tour(make_slide("V-2", unlisted=True), "On unlisted slide")
    make_tour(make_slide("V-3", published=False), "On draft slide")

    body = _tour_index(client)
    assert body["total"] == 1
    assert [it["id"] for it in body["items"]] == [listed.id]
    item = body["items"][0]
    assert item["public_id"] == listed.public_id
    assert item["slide_public_id"] == s.public_id
    assert item["slide_id"] == s.id
    assert item["is_published"] is True
    # Parent card data travels with the tour, so the view needs no slide list.
    assert item["slide"] == {
        "id": s.id, "public_id": s.public_id, "catalog_id": "V-1",
        "title": "Visible slide", "thumb_url": "",
        "image_adjust_brightness": None, "image_adjust_contrast": None,
        "image_adjust_saturation": None, "attributes": {},
    }
    assert item["attributes"] == {}
    # Same set as the /api/tours listing.
    assert {t["id"] for t in client.get("/api/tours").json()} == {listed.id}


def test_tour_index_q_matches_title_or_parent_slide_fields(client, make_slide, make_tour):
    by_title_slide = make_slide("Q-1", "Liver")
    mitosis = make_tour(by_title_slide, "Mitosis")
    make_tour(by_title_slide, "Overview")  # sibling: must NOT match via "Mitosis"
    by_catalog = make_slide("MITO-7", "Kidney")
    c1 = make_tour(by_catalog, "Glomeruli")
    c2 = make_tour(by_catalog, "Tubules")
    by_attr = make_slide("Q-3", "Lung", attrs={"tags": ["mitotic-index"]})
    a1 = make_tour(by_attr, "Alveoli")
    make_tour(make_slide("Q-4", "Skin"), "Epidermis")

    body = _tour_index(client, q="mito", sort="catalog_id")
    assert body["total"] == 4
    # Grouped by parent in slide order (catalog_id: MITO-7, Q-1, Q-3), then
    # creation order within a slide.
    assert [it["id"] for it in body["items"]] == [c1.id, c2.id, mitosis.id, a1.id]

    # The numeric-id leg of the slide match works here too.
    by_id = _tour_index(client, q=str(by_title_slide.id))
    assert mitosis.id in [it["id"] for it in by_id["items"]]


def test_tour_index_honours_filters_for_tours_anywhere_in_the_catalog(client, make_slide, make_tour):
    # The only HE tour sits on a slide that sorts after many other HE slides,
    # i.e. past any slide-list cap a client-side check would depend on.
    # The filters match the tours' own metadata, so each tour carries its
    # values itself.
    for i in range(8):
        make_slide(f"H-{i:02d}", attrs={"stain": ["HE"], "organ": ["Liver"]})
    late = make_slide("H-99", attrs={"stain": ["HE"], "organ": ["Kidney"]})
    he_tour = make_tour(late, "Late HE tour", attrs={"stain": ["HE"], "organ": ["Kidney"]})
    pas = make_slide("P-01", attrs={"stain": ["PAS"], "organ": ["Kidney"]})
    make_tour(pas, "PAS tour", attrs={"stain": ["PAS"], "organ": ["Kidney"]})

    assert [it["id"] for it in _tour_index(client, stain="HE")["items"]] == [he_tour.id]
    # facet tokens and base keys AND across keys, OR within one.
    assert [it["id"] for it in _tour_index(client, facet="organ:Kidney", stain="HE")["items"]] == [he_tour.id]
    assert _tour_index(client, stain=["HE", "PAS"])["total"] == 2
    # q and filters combine: a title match on a filtered-out slide stays out.
    assert _tour_index(client, q="PAS tour", stain="HE")["total"] == 0


def test_tour_index_cap_total_and_paging(client, make_slide, make_tour):
    s1 = make_slide("C-1")
    s2 = make_slide("C-2")
    tours = [make_tour(s2, "b1"), make_tour(s1, "a1"), make_tour(s2, "b2"),
             make_tour(s1, "a2"), make_tour(s1, "a3")]
    expected = [tours[1].id, tours[3].id, tours[4].id, tours[0].id, tours[2].id]

    first = _tour_index(client, sort="catalog_id", per_page=2)
    assert first["total"] == 5
    assert first["per_page"] == 2
    assert [it["id"] for it in first["items"]] == expected[:2]
    rest = _tour_index(client, sort="catalog_id", per_page=2, page=2)
    assert [it["id"] for it in rest["items"]] == expected[2:4]


def test_tour_index_follows_the_slide_sort(client, make_slide, make_tour):
    zebra = make_slide("A-1", "Zebra")
    alpha = make_slide("Z-1", "Alpha")
    tz = make_tour(zebra, "t")
    ta = make_tour(alpha, "t")

    def ids(**p):
        return [it["id"] for it in _tour_index(client, **p)["items"]]

    assert ids(sort="catalog_id") == [tz.id, ta.id]
    assert ids(sort="catalog_id", order="desc") == [ta.id, tz.id]
    # Same parent order as the slide list for the same parameters.
    slide_order = [it["id"] for it in _slides(client, sort="catalog_id")["items"]]
    assert slide_order == [zebra.id, alpha.id]


def test_tour_index_title_sort_uses_the_tour_title(client, make_slide, make_tour):
    # The tours' titles sort the opposite way to their slides': a title sort
    # follows the tour's own title, which is what each row shows first.
    first = make_slide("S-01", "Slide A")
    second = make_slide("S-02", "Slide B")
    t2 = make_tour(first, "Step 2")
    t1 = make_tour(second, "Step 1")
    t1b = make_tour(first, "Step 1")

    def ids(**p):
        return [it["id"] for it in _tour_index(client, **p)["items"]]

    # Equal titles fall back to the slide order (Slide A before Slide B).
    assert ids(sort="title") == [t1b.id, t1.id, t2.id]
    assert ids(sort="title", order="desc") == [t2.id, t1.id, t1b.id]


def test_tour_index_input_bounds(client):
    assert client.get("/api/index/tours", params={"q": "x" * 201}).status_code == 422
    assert client.get("/api/index/tours", params={"per_page": 5001}).status_code == 422
    r = client.get("/api/index/tours", params={"q": "9" * 20, "facet": [f"stain:v{i}" for i in range(300)]})
    assert r.status_code == 200, r.text


def test_tour_index_is_behind_app_access_like_api_slides(client, db_session, make_slide, make_tour):
    make_tour(make_slide("G-1"), "Gated")
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.mode = "basic"
    db_session.commit()
    _app_auth.refresh(db_session)
    try:
        assert client.get("/api/slides").status_code == 401
        assert client.get("/api/index/tours").status_code == 401
    finally:
        row.mode = "none"
        db_session.commit()
        _app_auth.refresh(db_session)
    assert client.get("/api/index/tours").status_code == 200


# ---------------------------------------------------------------------------
# /api/tours keeps its own shape
# ---------------------------------------------------------------------------
def test_api_tours_keeps_its_own_shape(client, make_slide, make_tour):
    s2 = make_slide("L-2")
    s1 = make_slide("L-1")
    t1 = make_tour(s2, "second slide's tour")
    t2 = make_tour(s1, "first slide's tour")
    r = client.get("/api/tours")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # Ordered by (slide_id, id), not by the slide sort, and no card data.
    assert [t["id"] for t in body] == [t1.id, t2.id]
    assert set(body[0]) == {
        "id", "public_id", "slide_public_id", "slide_id", "title",
        "description", "is_published", "unlisted", "revision",
    }
    # The edit counter is admin-only; the public listing leaves it null.
    assert body[0]["revision"] is None
    assert body[0]["slide_public_id"] == s2.public_id
