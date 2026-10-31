"""Tests for the /api/edge-authz probe that backs the nginx ``auth_request``
gate on the edge-cached read surfaces (/dzi/ tiles, /thumbs/ thumbnails,
/assets/ tour attachments).

The probe exists so those responses can be SHARED across authenticated users:
their nginx cache keys do not fold in the per-user session cookie, so a
cache HIT could otherwise bypass the app-access gate. nginx runs the probe as
an ``auth_request`` subrequest and consults ONLY its status code — 2xx
authorizes the fetch, the middleware's 401 denies it.

These tests lock in that the probe's verdict tracks the app-access gate
exactly, including the critical invariant that it is NOT under the
``/api/auth/`` bypass prefix (which would make it authorize every request and
silently open the shared cache to unauthenticated clients).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from wsi_app import app_auth as _app_auth
from wsi_app.auth import hash_password
from wsi_app.models import AuthSettings, AuthUser, Slide


def _set_mode(db_session, mode: str) -> None:
    """Force AuthSettings to a mode and refresh the in-memory snapshot the
    middleware reads (mirrors the dashboard write path)."""
    row = db_session.get(AuthSettings, 1)
    if row is None:
        row = AuthSettings(id=1)
        db_session.add(row)
    row.mode = mode
    db_session.commit()
    _app_auth.refresh(db_session)


def test_authz_path_is_gated_not_bypassed():
    """The probe MUST be gated and MUST NOT inherit the /api/auth/ bypass —
    otherwise it would authorize every request and the shared cache would be
    served to unauthenticated clients. The surfaces it guards must be gated
    identically so the probe's verdict matches a direct request's."""
    assert _app_auth.is_bypass_path("/api/edge-authz") is False
    assert _app_auth.is_gated_path("/api/edge-authz") is True
    for guarded in ("/dzi/x.dzi", "/thumbs/t.jpg", "/assets/tours/1/a.png"):
        assert _app_auth.is_gated_path(guarded) is True


def test_authz_allows_when_gate_disabled(client):
    """mode=none (default): the probe authorizes everyone, matching the guarded
    surfaces being public in that mode."""
    r = client.get("/api/edge-authz")
    assert r.status_code == 204


def test_authz_denies_unauthenticated_under_gate(client, db_session):
    """Under a gate (basic/ldap/google_oauth) an unauthenticated request is
    denied, so nginx would 401 the object instead of serving the shared cache."""
    _set_mode(db_session, "basic")
    try:
        r = client.get("/api/edge-authz")
        assert r.status_code == 401
    finally:
        _set_mode(db_session, "none")


def test_authz_allows_valid_session_cookie(client, db_session):
    """A user with a valid app-access session cookie is authorized, so their
    tile / thumbnail / asset requests are served from the shared cache."""
    db_session.execute(delete(AuthUser))
    db_session.add(AuthUser(username="alice", password_hash=hash_password("secret-pass")))
    db_session.commit()
    _set_mode(db_session, "basic")
    try:
        login = client.post(
            "/api/auth/login",
            data={"username": "alice", "password": "secret-pass"},
        )
        assert login.status_code == 200
        assert _app_auth.SESSION_COOKIE in login.cookies
        # The cookie is now in the TestClient jar; the probe must authorize it.
        r = client.get("/api/edge-authz")
        assert r.status_code == 204
    finally:
        client.cookies.clear()
        _set_mode(db_session, "none")


def test_authz_allows_admin_bearer_under_gate(client, db_session, admin_headers):
    """An admin's Bearer token bypasses the app-access gate (draft preview),
    so the probe authorizes it even under a gate. nginx bypasses the authz
    cache for Authorization-bearing requests, so this verdict is always live."""
    _set_mode(db_session, "basic")
    try:
        r = client.get("/api/edge-authz", headers=admin_headers)
        assert r.status_code == 204
    finally:
        _set_mode(db_session, "none")


@pytest.mark.parametrize("headers_fixture", ["manager_headers", "instructor_headers"])
def test_authz_allows_staff_bearer_under_gate(
    request, client, db_session, headers_fixture
):
    """Any staff bearer (manager / instructor), not just admin, bypasses the
    app-access gate. The middleware admits every staff role because the
    token-keyed draft-preview bypass (staff_token_valid) is only reachable
    once the request clears the gate — an admin-only check here would 401 the
    managers/instructors that bypass was built for before their request ever
    reached the route."""
    headers = request.getfixturevalue(headers_fixture)
    _set_mode(db_session, "basic")
    try:
        r = client.get("/api/edge-authz", headers=headers)
        assert r.status_code == 204
    finally:
        _set_mode(db_session, "none")


# ---------------------------------------------------------------------------
# Per-slide verdicts. nginx passes the original URI as X-Original-URI; for a
# /dzi/ URI the probe applies the /dzi/ routes' publish gate, because a cache
# HIT never reaches those routes — without it a tile cached while a slide was
# public would stay servable for the tile cache's 30 days after unpublish/delete.
# ---------------------------------------------------------------------------

def _make_slide(db_session, *, published: bool) -> Slide:
    s = Slide(title="Edge", catalog_id="EDGE-1", wsi_filename="edge.svs", is_published=published)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _dzi_uris(public_id: str) -> list[str]:
    """Every /dzi/ URI shape a viewer sends for one slide (raw, as nginx's
    $request_uri forwards it)."""
    return [
        f"/dzi/{public_id}.dzi?v=1",
        f"/dzi/{public_id}_files/12/3_4.jpeg?v=1",
        f"/dzi/{public_id}_files/0/0_0.jpeg",
        f"/dzi/{public_id}.szi?v=2",
    ]


def _probe(client, uri: str, headers: dict | None = None):
    return client.get("/api/edge-authz", headers={"X-Original-URI": uri, **(headers or {})})


def test_authz_allows_published_slide(client, db_session):
    s = _make_slide(db_session, published=True)
    for uri in _dzi_uris(s.public_id):
        assert _probe(client, uri).status_code == 204, uri


def test_authz_denies_draft_slide_anonymous(client, db_session):
    """403 — not 404, which auth_request would turn into a 500 — so nginx
    refuses the cached tile."""
    s = _make_slide(db_session, published=False)
    for uri in _dzi_uris(s.public_id):
        assert _probe(client, uri).status_code == 403, uri


@pytest.mark.parametrize("headers_fixture", ["admin_headers", "manager_headers", "instructor_headers"])
def test_authz_allows_draft_slide_for_staff_bearer(request, client, db_session, headers_fixture):
    """The draft preview: the viewer sends the staff JWT on every tile request
    (OSD ajaxHeaders / szi-tile-source fetchOptions), nginx forwards it to the
    probe, and the probe admits it like the /dzi/ routes do."""
    headers = request.getfixturevalue(headers_fixture)
    s = _make_slide(db_session, published=False)
    for uri in _dzi_uris(s.public_id):
        assert _probe(client, uri, headers).status_code == 204, uri


def test_authz_draft_needs_a_valid_staff_bearer(client, db_session):
    """Only a bearer the middleware verified counts: a forged one is admitted
    by mode=none like any other request, but must not unlock a draft."""
    s = _make_slide(db_session, published=False)
    r = _probe(client, f"/dzi/{s.public_id}.dzi?v=1", {"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 403


def test_authz_denies_unknown_or_deleted_slide(client, db_session, admin_headers, attach_wsi):
    assert _probe(client, "/dzi/NoSuchToken0_files/0/0_0.jpeg?v=1").status_code == 403
    s = _make_slide(db_session, published=True)
    attach_wsi(s)
    assert _probe(client, f"/dzi/{s.public_id}.dzi?v=1").status_code == 204
    r = client.delete(f"/api/admin/slides/{s.id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    for uri in _dzi_uris(s.public_id):
        assert _probe(client, uri).status_code == 403, uri


def test_authz_reflects_unpublish_at_once(client, db_session, admin_headers, attach_wsi):
    """An unpublish flushes the resolve cache the probe reads, so this worker's
    next verdict is a denial (nginx re-asks within its 60 s verdict TTL)."""
    s = _make_slide(db_session, published=True)
    attach_wsi(s)
    uri = f"/dzi/{s.public_id}_files/1/0_0.jpeg?v=1"
    assert _probe(client, uri).status_code == 204
    r = client.post(f"/api/admin/slides/{s.id}/publish", data={"publish": 0}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert _probe(client, uri).status_code == 403


def test_authz_non_dzi_uris_keep_the_app_access_verdict(client, db_session):
    """/thumbs/ and /assets/ are not judged per object (thumbnail names rotate
    on unpublish; tour attachments must stay reachable by the editor's
    markdown preview of a draft tour, which carries no bearer)."""
    _make_slide(db_session, published=False)
    for uri in ("/thumbs/1_abc.jpg?v=1700000000", "/assets/tours/TourTok/pic.png", "/"):
        assert _probe(client, uri).status_code == 204, uri
    # No header at all (a hand-edited nginx config that omits it, or a direct
    # call): the app-access verdict alone.
    assert client.get("/api/edge-authz").status_code == 204


def test_authz_denies_unparseable_dzi_uri(client, db_session):
    """Anything under /dzi/ that is not a tile / descriptor / archive URI of a
    well-formed token is refused. nginx keys such URIs apart ("dzi:?"), so a
    denial here also means they can never ride a cached allow."""
    s = _make_slide(db_session, published=True)
    for uri in (
        f"/dzi/{s.public_id}.xml",
        f"/dzi/{s.public_id}",
        "/dzi/",
        f"/dzi/%{s.public_id}_files/0/0_0.jpeg",
        f"/dzi/{'a' * 65}_files/0/0_0.jpeg",
        f"/dzi/{s.public_id}.dzi.bak",
    ):
        assert _probe(client, uri).status_code == 403, uri


def test_authz_slide_check_is_served_from_the_resolve_cache(client, db_session, monkeypatch):
    """The probe runs before every tile nginx serves (verdicts are cached per
    slide only for cookie-only requests), so a known slide must cost no DB
    query: the second verdict comes from slide_resolve_cache."""
    from wsi_app import dzi as dzi_module

    s = _make_slide(db_session, published=True)
    uri = f"/dzi/{s.public_id}_files/1/0_0.jpeg?v=1"
    assert _probe(client, uri).status_code == 204  # primes the cache
    opened = []
    real = dzi_module.SessionLocal
    monkeypatch.setattr(dzi_module, "SessionLocal", lambda: opened.append(1) or real())
    for _ in range(3):
        assert _probe(client, uri).status_code == 204
    assert opened == []


def test_authz_under_gate_still_needs_app_access(client, db_session):
    """The per-slide check is in addition to, never instead of, the app-access
    gate: an unauthenticated request for a published slide is still 401."""
    s = _make_slide(db_session, published=True)
    _set_mode(db_session, "basic")
    try:
        assert _probe(client, f"/dzi/{s.public_id}.dzi?v=1").status_code == 401
    finally:
        _set_mode(db_session, "none")
