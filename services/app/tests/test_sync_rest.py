"""REST endpoint tests for the live-sync router."""
from __future__ import annotations

import dataclasses

import pytest

from wsi_app.models import Slide
from wsi_app.routers.sync import (
    _MAX_DB_ID,
    _normalize_cursor_frame,
    _normalize_view_frame,
)


def test_normalize_view_frame_rejects_out_of_range_slide_id():
    base = {"type": "view", "bounds": [0, 0, 1, 1], "rotation_deg": 0}
    # >= 2**63 would raise OverflowError at SQLite bind time downstream.
    assert _normalize_view_frame({**base, "slide_id": 2**63}) is None
    assert _normalize_view_frame({**base, "slide_id": _MAX_DB_ID + 1}) is None
    assert _normalize_view_frame({**base, "slide_id": 0}) is None
    assert _normalize_view_frame({**base, "slide_id": -1}) is None
    # A valid id still passes.
    ok = _normalize_view_frame({**base, "slide_id": 7})
    assert ok is not None and ok["slide_id"] == 7


def test_normalize_frames_drop_ints_too_large_for_float():
    """A 400-digit JSON integer is a Python int float() cannot represent; the
    validators must drop the frame, not raise OverflowError into the WS loop."""
    huge = 10**400
    assert _normalize_cursor_frame({"x": huge, "y": 0}) is None
    view = {"slide_id": 7, "bounds": [0, 0, 1, 1]}
    assert _normalize_view_frame({**view, "bounds": [huge, 0, 1, 1]}) is None
    assert _normalize_view_frame({**view, "rotation_deg": huge}) is None
    # An optional ``t`` that overflows is simply omitted.
    ok = _normalize_cursor_frame({"x": 1, "y": 2, "t": huge})
    assert ok is not None and "t" not in ok


def _set_public_base_url(monkeypatch, value):
    """Point the sync router at a Settings copy with ``public_base_url`` set.

    ``Settings`` is a frozen dataclass, so the field can't be mutated in place;
    swap the module-level reference the router reads for a ``replace`` copy.
    """
    import wsi_app.routers.sync as sync_mod

    patched = dataclasses.replace(sync_mod.settings, public_base_url=value)
    monkeypatch.setattr(sync_mod, "settings", patched)


@pytest.fixture(autouse=True)
def _publish_slide(slide, db_session):
    """Publish the shared ``slide`` fixture for every test in this module.

    Live-sync session creation refuses unpublished slides (403), so the
    success-path tests below need a published slide. The shared conftest
    fixture defaults to a draft (is_published=False) and is relied on by other
    suites, so flip it here rather than changing the shared fixture.
    """
    slide.is_published = True
    db_session.commit()


def test_create_session_requires_admin(client, slide):
    r = client.post("/api/admin/sync/sessions", json={"slide_id": slide.id})
    assert r.status_code == 401


def test_create_session_unknown_slide(client, admin_headers):
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": 9999},
        headers=admin_headers,
    )
    assert r.status_code == 404
    # Coded so the presenter's launcher can show it in the viewer's language.
    assert r.json()["detail"]["code"] == "sync.apiErr.slideNotFound"


def test_create_session_rejects_unpublished_slide(client, admin_headers, db_session):
    # A draft/private slide must not be sync-able: followers couldn't fetch its
    # tiles, so creation is refused with 403 (and no discovery event fires).
    draft = Slide(title="Draft Slide", catalog_id="DRAFT-001", is_published=False)
    db_session.add(draft)
    db_session.commit()
    db_session.refresh(draft)
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": draft.id},
        headers=admin_headers,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "sync.apiErr.slideUnpublished"


def test_create_session_returns_id_and_follower_url(client, admin_headers, slide):
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slide_id"] == slide.id
    assert body["slide_public_id"] == slide.public_id
    assert isinstance(body["session_id"], str) and len(body["session_id"]) >= 20
    # The share URL is keyed by the slide's public_id (never the numeric PK).
    assert body["follower_url"].endswith(
        f"/slides/{slide.public_id}?sync={body['session_id']}"
    )


def test_create_session_over_room_cap_returns_503(
    client, admin_headers, slide, monkeypatch
):
    """When ``_rooms`` is already at MAX_ROOMS, create returns 503 (capacity
    exhausted) rather than growing the room table unbounded — the REST mapping
    of RoomManager.RoomLimitError."""
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "MAX_ROOMS", 1)
    # Pre-fill the live manager to the cap with a room owned by someone else, so
    # superseding the caller's sessions frees nothing and create hits the ceiling.
    sr.manager._rooms["foreign"] = sr.Room(
        session_id="foreign", slide_id=1, owner_user_id=999
    )
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "sync.apiErr.tooManySessions"


def test_create_session_honours_x_forwarded_proto(client, admin_headers, slide):
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers={
            **admin_headers,
            "X-Forwarded-Proto": "https",
            "Host": "wsi.example",
        },
    )
    assert r.status_code == 200
    assert r.json()["follower_url"].startswith("https://wsi.example/slides/")


def test_create_session_ignores_client_x_forwarded_host(client, admin_headers, slide):
    # nginx passes a client-supplied X-Forwarded-Host through untouched, so the
    # share link must be built from Host (which nginx pins to $host) only —
    # otherwise a caller could point every follower's link/QR at their own host.
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers={
            **admin_headers,
            "Host": "wsi.example",
            "X-Forwarded-Host": "attacker.example",
        },
    )
    assert r.status_code == 200
    url = r.json()["follower_url"]
    assert url.startswith("http://wsi.example/slides/")
    assert "attacker.example" not in url


def test_create_session_uses_public_base_url_when_set(
    client, admin_headers, slide, monkeypatch
):
    # When WSI_PUBLIC_BASE_URL is configured, the follower URL is pinned to that
    # canonical entry point and ignores the presenter's request host — so a
    # server reachable under multiple names/addresses still hands out a link
    # every participant can reach.
    _set_public_base_url(monkeypatch, "https://canonical.example")
    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers={
            **admin_headers,
            "X-Forwarded-Proto": "http",
            "Host": "presenter-only-host.lan",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["follower_url"] == (
        f"https://canonical.example/slides/{slide.public_id}?sync={body['session_id']}"
    )


def test_peek_returns_slide_metadata(client, admin_headers, slide):
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]

    r = client.get(f"/api/sync/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["slide_id"] == slide.id
    assert body["slide_title"] == "Test Slide"
    assert body["slide_catalog_id"] == "TEST-001"
    # require_name surfaces the server's WSI_SYNC_REQUIRE_NAME so the
    # follower's Join dialog can suppress the "Join anonymously" button.
    # Without it, an anonymous follower would be hard-rejected by the WS
    # hello with a 4400 close and (since 4400 isn't a stop-code in sync.ts)
    # spin forever in the reconnect loop.
    assert body["require_name"] is False  # default — env var unset in tests


def test_peek_unknown_session(client):
    r = client.get("/api/sync/sessions/does-not-exist")
    assert r.status_code == 404


def test_unpublishing_mid_session_hides_it_from_the_public_peek(
    client, admin_headers, slide, db_session
):
    """The creation-time publish gate runs once; unpublishing must still bite.

    ``GET /api/sync/sessions/{sid}`` is public once the app-access mode is
    ``none``. Nothing ends a live room when staff pull its slide from view, so
    without a re-check the now-private slide's title and catalog_id — and the
    token needed to join it — stay served for the rest of the session's life.
    """
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    assert client.get(f"/api/sync/sessions/{sid}").status_code == 200

    slide.is_published = False
    db_session.commit()

    gone = client.get(f"/api/sync/sessions/{sid}")
    assert gone.status_code == 404, gone.text

    # The presenter keeps seeing their own session so they can stop it.
    owned = client.get("/api/admin/sync/sessions", headers=admin_headers)
    assert owned.status_code == 200, owned.text
    assert sid in {s["session_id"] for s in owned.json()["sessions"]}


def test_there_is_no_public_session_list(client, admin_headers, slide):
    """Nothing consumes a public list of every live session (the index page's
    join banner rides the discovery WebSocket), and such a list would hand anyone who
    could reach it the title and catalog id of a link-only slide being
    presented, so it does not exist."""
    _create_session(client, admin_headers, slide.id)
    assert client.get("/api/sync/sessions").status_code == 404


def _create_session(client, headers, slide_id):
    return client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide_id},
        headers=headers,
    ).json()["session_id"]


def test_create_supersedes_callers_previous_session(client, admin_headers, slide):
    """One live session per presenter: starting a second session ends the
    caller's first one, so the index page never shows two banners for a single
    presenter who left an orphan running."""
    first = _create_session(client, admin_headers, slide.id)
    second = _create_session(client, admin_headers, slide.id)
    assert first != second
    assert client.get(f"/api/sync/sessions/{second}").status_code == 200
    assert client.get(f"/api/sync/sessions/{first}").status_code == 404  # superseded


def test_create_does_not_supersede_other_presenters(
    client, admin_headers, other_admin_headers, slide
):
    other = _create_session(client, other_admin_headers, slide.id)
    mine = _create_session(client, admin_headers, slide.id)
    # Both survive — the per-presenter cap is scoped to the caller.
    assert client.get(f"/api/sync/sessions/{other}").status_code == 200
    assert client.get(f"/api/sync/sessions/{mine}").status_code == 200


def test_list_owned_sessions_requires_admin(client):
    assert client.get("/api/admin/sync/sessions").status_code == 401


def test_list_owned_sessions_returns_callers_session(client, admin_headers, slide):
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    r = client.get("/api/admin/sync/sessions", headers=admin_headers)
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert {s["session_id"] for s in sessions} == {sid}
    assert sessions[0]["slide_id"] == slide.id
    # No presenter socket yet (see test_sync_ws for an attached one).
    assert sessions[0]["presenter_connected"] is False


def test_list_owned_sessions_includes_follower_url(client, admin_headers, slide):
    # The owned-sessions list backs the presenter's "resume" affordance, so it
    # carries a ready-to-use share URL built server-side (same canonicalisation
    # as create) rather than leaving the client to rebuild it from its origin.
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    r = client.get("/api/admin/sync/sessions", headers=admin_headers)
    assert r.status_code == 200
    entry = r.json()["sessions"][0]
    assert entry["follower_url"].endswith(f"/slides/{slide.public_id}?sync={sid}")


def test_list_owned_sessions_follower_url_honours_public_base_url(
    client, admin_headers, slide, monkeypatch
):
    _set_public_base_url(monkeypatch, "https://canonical.example")
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    r = client.get("/api/admin/sync/sessions", headers=admin_headers)
    assert r.status_code == 200
    entry = r.json()["sessions"][0]
    assert entry["follower_url"] == (
        f"https://canonical.example/slides/{slide.public_id}?sync={sid}"
    )


def test_public_peek_omits_owner_fields(client, admin_headers, slide):
    # follower_url and presenter_connected are owner-scoped (resume
    # affordance). The public peek must not surface them.
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    peek = client.get(f"/api/sync/sessions/{sid}")
    assert peek.status_code == 200
    assert peek.json().get("follower_url") is None
    assert peek.json().get("presenter_connected") is None


def test_list_owned_sessions_excludes_other_presenters(
    client, admin_headers, other_admin_headers, slide
):
    # A session created by one admin must not appear in another admin's
    # owned-sessions list — it backs a per-presenter "resume" affordance.
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    r = client.get("/api/admin/sync/sessions", headers=other_admin_headers)
    assert r.status_code == 200
    sids = {s["session_id"] for s in r.json()["sessions"]}
    assert sid not in sids


def test_delete_session_any_staff(
    client, admin_headers, other_admin_headers, slide
):
    """Any authenticated staff user may end a sync session (e.g. cleaning up
    after a colleague's abandoned presentation). The deletion request from
    a different staff user must succeed."""
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]

    # A different admin (non-owner) can delete the session.
    ok = client.delete(
        f"/api/admin/sync/sessions/{sid}",
        headers=other_admin_headers,
    )
    assert ok.status_code == 200

    # After delete, peek returns 404.
    assert client.get(f"/api/sync/sessions/{sid}").status_code == 404


def test_delete_session_owner_can_also_delete(
    client, admin_headers, slide
):
    """The owner can also delete — 'any staff' is a superset of owner-only."""
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    ok = client.delete(
        f"/api/admin/sync/sessions/{sid}",
        headers=admin_headers,
    )
    assert ok.status_code == 200
    assert client.get(f"/api/sync/sessions/{sid}").status_code == 404


def test_delete_session_by_different_role_staff(
    client, admin_headers, instructor_headers, slide
):
    """A staff user of one role (instructor) creating and another (admin)
    ending: the any-staff policy holds across roles, not just admin-admin
    pairs."""
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=instructor_headers,
    ).json()["session_id"]
    ok = client.delete(
        f"/api/admin/sync/sessions/{sid}",
        headers=admin_headers,
    )
    assert ok.status_code == 200


def test_unmount_end_is_ignored_once_another_client_attached(
    client, admin_headers, slide
):
    """The presenter page's "end because I'm leaving" DELETE carries its
    client id; once another tab/device has attached, it must not end the
    session that one is driving — only the last attacher's DELETE does."""
    import wsi_app.sync_room as sr

    sid = _create_session(client, admin_headers, slide.id)
    sr.manager.get(sid).presenter_client_id = "p-tablet"  # attached last
    r = client.delete(
        f"/api/admin/sync/sessions/{sid}?client_id=p-laptop", headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "superseded": True}
    assert sr.manager.get(sid) is not None

    r = client.delete(
        f"/api/admin/sync/sessions/{sid}?client_id=p-tablet", headers=admin_headers,
    )
    assert r.json() == {"ok": True}
    assert sr.manager.get(sid) is None


def test_unmount_end_of_a_room_nobody_attached_to_ends_it(
    client, admin_headers, slide
):
    """A presenter that leaves before its first hello landed still ends the
    room it created, instead of leaving it up for the grace window."""
    import wsi_app.sync_room as sr

    sid = _create_session(client, admin_headers, slide.id)
    r = client.delete(
        f"/api/admin/sync/sessions/{sid}?client_id=p-laptop", headers=admin_headers,
    )
    assert r.json() == {"ok": True}
    assert sr.manager.get(sid) is None


def test_delete_unknown_session(client, admin_headers):
    r = client.delete("/api/admin/sync/sessions/missing", headers=admin_headers)
    assert r.status_code == 404


def test_delete_requires_admin(client, admin_headers, slide):
    sid = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    ).json()["session_id"]
    r = client.delete(f"/api/admin/sync/sessions/{sid}")
    assert r.status_code == 401


# ----- GET /api/admin/sync/sessions/all (staff management list) ----------

def test_list_all_sessions_requires_admin(client):
    assert client.get("/api/admin/sync/sessions/all").status_code == 401


def test_list_all_sessions_reports_session_fields(client, admin_headers, slide):
    sid = _create_session(client, admin_headers, slide.id)
    r = client.get("/api/admin/sync/sessions/all", headers=admin_headers)
    assert r.status_code == 200
    rows = {s["session_id"]: s for s in r.json()["sessions"]}
    assert sid in rows
    row = rows[sid]
    assert row["slide_id"] == slide.id
    # No presenter WebSocket has attached (REST create only), so the room is
    # orphaned-in-grace from the start: flagged not-connected with a countdown.
    assert row["presenter_connected"] is False
    assert row["grace_remaining"] is not None
    assert row["follower_count"] == 0
    assert row["created_at"] > 0
    assert row["owner_username"]  # owner resolved from the JWT subject


def test_list_all_sessions_sees_other_presenters_sessions(
    client, admin_headers, other_admin_headers, slide
):
    """Any staff user can see (and therefore end) every session, not just
    their own — mirrors the DELETE policy a manager relies on to clear an
    orphan an instructor abandoned."""
    others = _create_session(client, other_admin_headers, slide.id)
    rows = client.get(
        "/api/admin/sync/sessions/all", headers=admin_headers
    ).json()["sessions"]
    assert others in {s["session_id"] for s in rows}


def test_list_all_sessions_drops_ended_session(client, admin_headers, slide):
    sid = _create_session(client, admin_headers, slide.id)
    client.delete(f"/api/admin/sync/sessions/{sid}", headers=admin_headers)
    rows = client.get(
        "/api/admin/sync/sessions/all", headers=admin_headers
    ).json()["sessions"]
    assert sid not in {s["session_id"] for s in rows}


def test_create_session_over_cap_does_not_destroy_existing_session(
    client, admin_headers, admin_user, slide, monkeypatch
):
    """A create that is rejected for capacity must not tear down what it failed
    to replace.

    "One live session per presenter" as two steps — end the caller's sessions,
    then create — would let a create that hit the room ceiling return 503
    *after* the presenter's running session had already been ended, dropping
    every follower attached to it. The registry swap is atomic: on
    RoomLimitError the existing session survives.
    """
    import wsi_app.sync_room as sr

    monkeypatch.setattr(sr, "MAX_ROOMS", 1)
    # A foreign room already fills the cap on its own, so freeing the caller's
    # room cannot make space — the create is genuinely doomed.
    sr.manager._rooms["foreign"] = sr.Room(
        session_id="foreign", slide_id=1, owner_user_id=999
    )
    sr.manager._rooms["mine"] = sr.Room(
        session_id="mine", slide_id=slide.id, owner_user_id=admin_user.id
    )

    r = client.post(
        "/api/admin/sync/sessions",
        json={"slide_id": slide.id},
        headers=admin_headers,
    )
    assert r.status_code == 503, r.text
    assert "mine" in sr.manager._rooms, "rejected create destroyed the live session"
    assert "foreign" in sr.manager._rooms


def test_create_session_supersedes_caller_previous_session(
    client, admin_headers, admin_user, slide
):
    """The happy path still enforces one live session per presenter."""
    import wsi_app.sync_room as sr

    first = client.post(
        "/api/admin/sync/sessions", json={"slide_id": slide.id}, headers=admin_headers,
    )
    assert first.status_code == 200
    first_sid = first.json()["session_id"]

    second = client.post(
        "/api/admin/sync/sessions", json={"slide_id": slide.id}, headers=admin_headers,
    )
    assert second.status_code == 200
    second_sid = second.json()["session_id"]

    assert second_sid != first_sid
    assert first_sid not in sr.manager._rooms
    assert second_sid in sr.manager._rooms
    owned = [r for r in sr.manager._rooms.values() if r.owner_user_id == admin_user.id]
    assert len(owned) == 1
