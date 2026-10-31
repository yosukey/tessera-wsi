"""The public index with tours' own metadata (tour_attributes).

A tour's effective metadata is its slide's plus its own. On the index page:

* the "Tours" view (``/api/index/tours``) filters tours on their own
  metadata alone, a value their slide passes down not counting;
* the "All" view (``/api/slides``) lists a slide that matches by itself or
  through one of its published, listed tours, and names those tours first
  (``matched_tours``);
* the "Slides" view (``match_tours=false``) keeps to the slides' own metadata;
* ``/api/facets`` counts each value for each of the three views.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app.models import Slide, SlideAttribute, Tour, TourAttribute


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
    def _make(catalog_id: str, *, title: str = "", attrs: dict | None = None,
              published: bool = True, unlisted: bool = False) -> Slide:
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
    def _make(slide: Slide, title: str, *, attrs: dict | None = None,
              published: bool = True, unlisted: bool = False) -> Tour:
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


def _get(client, path, **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _tour_titles(client, **params):
    return [t["title"] for t in _get(client, "/api/index/tours", **params)["items"]]


def _all_view(client, **params):
    """{catalog_id: [matched tour titles]} of the "All" view."""
    items = _get(client, "/api/slides", **params)["items"]
    return {s["catalog_id"]: [m["title"] for m in s["matched_tours"]] for s in items}


# ---------------------------------------------------------------------------
# "Tours" view
# ---------------------------------------------------------------------------
def test_tours_view_filters_on_the_tours_own_metadata_only(client, make_slide, make_tour):
    he = make_slide("A", attrs={"stain": ["HE"]})
    make_tour(he, "basic tour", attrs={"level": ["basic"]})
    make_tour(he, "plain tour")
    make_tour(he, "he tour", attrs={"stain": ["HE"]})
    pas = make_slide("B", attrs={"stain": ["PAS"]})
    make_tour(pas, "pas basic", attrs={"level": ["basic"]})

    # The tour's own value matches…
    assert _tour_titles(client, facet="level:basic") == ["basic tour", "pas basic"]
    # …its slide's doesn't: only the tour that carries HE itself is listed.
    assert _tour_titles(client, stain="HE") == ["he tour"]
    # A key the tour lacks, though its slide has it, filters it out.
    assert _tour_titles(client, stain="HE", facet="level:basic") == []
    # The search still reaches the slide's fields.
    assert _tour_titles(client, q="Slide A") == ["basic tour", "plain tour", "he tour"]


def test_tours_view_searches_a_tours_own_values(client, make_slide, make_tour):
    s = make_slide("A")
    make_tour(s, "first", attrs={"level": ["advanced"]})
    make_tour(s, "second")
    assert _tour_titles(client, q="advan") == ["first"]


# ---------------------------------------------------------------------------
# "All" and "Slides" views
# ---------------------------------------------------------------------------
def test_all_view_lists_a_slide_through_a_tours_own_value(client, make_slide, make_tour):
    a = make_slide("A", attrs={"stain": ["HE"]})
    make_tour(a, "plain")
    make_tour(a, "basic tour", attrs={"level": ["basic"]})
    b = make_slide("B", attrs={"level": ["basic"]})
    make_tour(b, "b tour")
    make_slide("C")

    # A gets in through its tour, which is named; B carries the value itself,
    # so none of its tours is "the reason".
    assert _all_view(client, facet="level:basic") == {"A": ["basic tour"], "B": []}
    assert _get(client, "/api/slides", facet="level:basic")["total"] == 2
    # The "Slides" view keeps to the slides' own metadata.
    assert _all_view(client, facet="level:basic", match_tours="false") == {"B": []}


def test_all_view_needs_one_tour_to_pass_every_key(client, make_slide, make_tour):
    s = make_slide("A")
    make_tour(s, "t1", attrs={"level": ["basic"]})
    make_tour(s, "t2", attrs={"year": ["2"]})
    assert _all_view(client, facet=["level:basic", "year:2"]) == {}
    make_tour(s, "t3", attrs={"level": ["basic"], "year": ["2"]})
    assert _all_view(client, facet=["level:basic", "year:2"]) == {"A": ["t3"]}


def test_all_view_combines_search_and_filters_per_tour(client, make_slide, make_tour):
    # The slide's title hits q and a tour passes the filter: listed.
    hit = make_slide("A", title="liver lobule")
    make_tour(hit, "with level", attrs={"level": ["basic"]})
    # q hits one tour and the filter another: not listed.
    split = make_slide("B")
    make_tour(split, "liver tour")
    make_tour(split, "other", attrs={"level": ["basic"]})
    assert _all_view(client, q="liver", facet="level:basic") == {"A": ["with level"]}


def test_a_search_alone_still_names_only_the_tours_it_hit(client, make_slide, make_tour):
    s = make_slide("A", title="liver")
    make_tour(s, "liver tour")
    make_tour(s, "other")
    assert _all_view(client, q="liver") == {"A": ["liver tour"]}


@pytest.mark.parametrize("hidden", [{"published": False}, {"unlisted": True}])
def test_a_draft_or_link_only_tour_pulls_nothing_in(client, make_slide, make_tour, hidden):
    s = make_slide("A")
    make_tour(s, "hidden", attrs={"level": ["basic"]}, **hidden)
    assert _all_view(client, facet="level:basic") == {}
    assert _tour_titles(client, facet="level:basic") == []
    groups = _get(client, "/api/facets")["groups"]
    assert "basic" not in [i["value"] for i in groups.get("level", [])]


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------
def test_facets_count_each_value_for_each_view(client, make_slide, make_tour):
    a = make_slide("A", attrs={"stain": ["HE"]})
    make_tour(a, "a1", attrs={"stain": ["PAS"], "level": ["basic"]})
    # Carries its slide's own value again: counted once, not twice.
    make_tour(a, "a2", attrs={"stain": ["HE"]})
    # A draft tour on a listed slide counts nowhere, inherited values included.
    make_tour(a, "a3", attrs={"level": ["basic"]}, published=False)
    b = make_slide("B", attrs={"stain": ["PAS"]})
    make_tour(b, "b1")
    # Unlisted slides surface nothing, their tours included.
    hidden = make_slide("C", attrs={"stain": ["PAS"]}, unlisted=True)
    make_tour(hidden, "c1", attrs={"level": ["basic"]})

    groups = _get(client, "/api/facets")["groups"]

    def item(key, value):
        return next(i for i in groups[key] if i["value"] == value)

    # The "Tours" count is the tours carrying the value themselves: a2 for HE
    # (a1 inherits it from slide A and isn't counted), a1 for PAS (b1 inherits
    # it from slide B).
    assert item("stain", "HE") == {"value": "HE", "count": 1, "all_count": 1, "tour_count": 1}
    # PAS: slide B; slides A (via a1) and B; tour a1.
    assert item("stain", "PAS") == {"value": "PAS", "count": 1, "all_count": 2, "tour_count": 1}
    # A value only a tour carries is listed, with no slide count of its own.
    assert item("level", "basic") == {"value": "basic", "count": 0, "all_count": 1, "tour_count": 1}


# ---------------------------------------------------------------------------
# what the public screens get to show
# ---------------------------------------------------------------------------
def test_tour_index_items_carry_both_attribute_sets(client, make_slide, make_tour):
    s = make_slide("A", attrs={"stain": ["HE"]})
    make_tour(s, "t", attrs={"level": ["basic"]})
    item = _get(client, "/api/index/tours")["items"][0]
    assert item["attributes"] == {"level": ["basic"]}
    assert item["slide"]["attributes"] == {"stain": ["HE"]}


def test_public_tour_detail_carries_its_own_attributes(client, make_slide, make_tour):
    s = make_slide("A", attrs={"stain": ["HE"]})
    t = make_tour(s, "t", attrs={"level": ["basic", "advanced"]})
    body = _get(client, f"/api/tours/{t.public_id}")
    assert body["tour"]["attributes"] == {"level": ["advanced", "basic"]}
    # The /api/tours listing keeps its shape.
    assert "attributes" not in _get(client, "/api/tours")[0]
