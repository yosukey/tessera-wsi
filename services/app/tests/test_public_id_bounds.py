"""Public slide/tour endpoints must bound their input rather than 500.

Ids: public read surfaces are keyed by the unguessable ``public_id`` token
(an opaque string), never the numeric PK. A numeric or otherwise-bogus path
segment is simply a token that matches nothing → 404 (never a 5xx from a
SQLite integer-bind overflow), and an over-length segment is rejected by the
path's ``max_length`` bound → 422.

Search: the public /api/slides ``q`` term and facet list are length-bounded so
an attacker can't push an arbitrarily large query through the unauthenticated
search box (422 on overlong ``q``; facet list capped internally, not 422'd).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

# A long, all-numeric path segment: would overflow an int-bound
# path param, but is just a non-matching public_id token.
_BOGUS_NUMERIC = str(2**63)
# Past the public_id path max_length (64).
_OVERLONG = "a" * 65


def test_get_slide_numeric_id_is_404_not_500(client: TestClient):
    r = client.get(f"/api/slides/{_BOGUS_NUMERIC}")
    assert r.status_code == 404, r.text


def test_get_tour_numeric_id_is_404_not_500(client: TestClient):
    r = client.get(f"/api/tours/{_BOGUS_NUMERIC}")
    assert r.status_code == 404, r.text


def test_list_tours_numeric_slide_id_is_404_not_500(client: TestClient):
    r = client.get(f"/api/slides/{_BOGUS_NUMERIC}/tours")
    assert r.status_code == 404, r.text


def test_get_slide_overlong_public_id_is_422(client: TestClient):
    r = client.get(f"/api/slides/{_OVERLONG}")
    assert r.status_code == 422, r.text


def test_list_slides_search_oversized_numeric_q_ok(client: TestClient):
    # A 20-digit numeric search term passes str.isdigit() and int() but would
    # overflow the SQLite bind. It must degrade to a normal (empty) search.
    r = client.get("/api/slides", params={"q": "9" * 20})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_list_slides_search_non_decimal_digit_q_ok(client: TestClient):
    # "²".isdigit() is True but int("²") raises ValueError.
    r = client.get("/api/slides", params={"q": "²"})
    assert r.status_code == 200, r.text


def test_list_slides_search_matches_real_id(client: TestClient, db_session):
    from wsi_app.models import Slide

    s = Slide(title="Findable", catalog_id="F-001", is_published=True)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)

    r = client.get("/api/slides", params={"q": str(s.id)})
    assert r.status_code == 200, r.text
    ids = [it["id"] for it in r.json()["items"]]
    assert s.id in ids


def test_list_slides_rejects_overlong_q(client: TestClient):
    r = client.get("/api/slides", params={"q": "x" * 201})
    assert r.status_code == 422


def test_list_slides_accepts_bounded_q_and_many_facets(client: TestClient):
    # A 200-char term and a large facet list are accepted (capped internally)
    # rather than 422'd, so the search box stays forgiving.
    r = client.get(
        "/api/slides",
        params={"q": "x" * 200, "facet": [f"stain:v{i}" for i in range(300)]},
    )
    assert r.status_code == 200, r.text
