"""Admin multipart uploads are authenticated before their body is read.

FastAPI parses a File()/Form() body before it resolves dependencies, and
Starlette spools each file part to a temp file while doing so. Without a
pre-body check, ``require_admin`` on /api/admin/restore would only run after
an anonymous client's whole upload had already been written to disk, so
parallel unauthenticated uploads could fill it. These tests pin that the
refusal happens with the body untouched, and that legitimate uploads still work.
"""
from __future__ import annotations

import tempfile

import pytest

from wsi_app.auth import create_token

from .test_backup import _await_job

_BODY = b"\0" * (2 * 1024 * 1024)  # larger than the 1 MiB in-memory spool


@pytest.fixture
def spool_counter(monkeypatch):
    """Count temp files Starlette's multipart parser creates."""
    created: list[int] = []
    real = tempfile.SpooledTemporaryFile

    def counting(*a, **kw):
        created.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", counting)
    import starlette.formparsers as fp

    if hasattr(fp, "SpooledTemporaryFile"):
        monkeypatch.setattr(fp, "SpooledTemporaryFile", counting)
    return created


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/restore",
        "/api/admin/slides/import",
        "/api/admin/slides/upload",
        "/api/admin/slides/1/wsi",
        "/api/admin/tours/1/assets/upload",
        # "$" in the router's patterns (and nginx's) also matches before a
        # trailing newline, so these reach the same endpoints.
        "/api/admin/restore%0A",
        "/api/admin/slides/import%0A",
        "/api/admin/slides/upload%0A",
        "/api/admin/slides/1/wsi%0A",
    ],
)
def test_anonymous_upload_is_refused_before_spooling(client, spool_counter, path):
    r = client.post(path, files={"file": ("x.bin", _BODY, "application/octet-stream")})
    assert r.status_code == 401
    assert r.json()["detail"] == "Missing token"
    assert spool_counter == [], "body was spooled before authentication"


def test_invalid_token_is_refused_before_spooling(client, spool_counter):
    r = client.post(
        "/api/admin/restore",
        files={"file": ("x.bin", _BODY, "application/octet-stream")},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid token"
    assert spool_counter == []


def test_underprivileged_role_is_refused_before_spooling(
    client, spool_counter, instructor_user, manager_user
):
    instr = {"Authorization": f"Bearer {create_token(instructor_user)}"}
    mgr = {"Authorization": f"Bearer {create_token(manager_user)}"}
    files = {"file": ("x.bin", _BODY, "application/octet-stream")}
    for path, headers in (
        ("/api/admin/restore", mgr),          # admin only
        ("/api/admin/slides/import", mgr),    # admin only
        ("/api/admin/slides/upload", instr),  # manager or above
        ("/api/admin/slides/7/wsi", instr),   # manager or above
    ):
        r = client.post(path, files=files, headers=headers)
        assert r.status_code == 403, (path, r.status_code, r.text)
        assert r.json()["detail"] == "Forbidden"
    assert spool_counter == []


def test_authorised_upload_still_reaches_the_endpoint(client, spool_counter, admin_headers):
    """An admin's multipart request passes straight through (the endpoint then
    applies its own validation, here rejecting a non-archive)."""
    r = client.post(
        "/api/admin/restore",
        files={"file": ("x.bin", _BODY, "application/octet-stream")},
        headers=admin_headers,
    )
    assert r.status_code not in (401, 403), r.text
    assert spool_counter, "an authorised upload should be parsed"
    # The endpoint stages the file on a background job, which fails on the
    # non-archive only once its thread gets to it. Until then the job holds
    # the single job slot, and a later test's restore / import upload is
    # answered 409 (only on a runner slow enough, so it would look flaky).
    _await_job(client, admin_headers, r.json()["job_id"])


def test_anonymous_urlencoded_body_is_refused_before_parsing(client, monkeypatch):
    """A urlencoded body is buffered in memory by the form parser (up to 1000
    fields of 1 MiB). On the big-body locations that is a pre-auth memory
    sink unless it too is refused before parsing."""
    import starlette.formparsers as fp

    parsed: list[int] = []
    real_parse = fp.FormParser.parse

    async def counting_parse(self):
        parsed.append(1)
        return await real_parse(self)

    monkeypatch.setattr(fp.FormParser, "parse", counting_parse)
    r = client.post(
        "/api/admin/slides/upload",
        content=b"a=" + b"x" * (512 * 1024),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 401
    assert parsed == [], "urlencoded body was parsed before authentication"


@pytest.fixture
def set_cap():
    """Settings is a frozen dataclass: override a field, restore afterwards."""
    from wsi_app.config import settings

    saved: dict = {}

    def _set(name: str, value: int) -> None:
        saved.setdefault(name, getattr(settings, name))
        object.__setattr__(settings, name, value)

    yield _set
    for name, value in saved.items():
        object.__setattr__(settings, name, value)


def _asgi_post(path: str, headers: list, body_chunks: list):
    """Drive the ASGI app directly so the test can see how much of the request
    body was pulled. Returns (status, response_json, receive_calls)."""
    import asyncio
    import json

    from wsi_app.main import app

    calls = {"n": 0}
    chunks = list(body_chunks)
    sent: dict = {"status": None, "body": b""}

    async def receive():
        calls["n"] += 1
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "path": path, "raw_path": path.encode(),
        "root_path": "", "scheme": "http", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    return sent["status"], json.loads(sent["body"] or b"{}"), calls["n"]


@pytest.mark.parametrize(
    "path,cap_attr",
    [
        ("/api/admin/restore", "backup_max_upload_bytes"),
        ("/api/admin/slides/import", "backup_max_upload_bytes"),
        ("/api/admin/slides/upload", "wsi_max_upload_bytes"),
        ("/api/admin/slides/7/wsi", "wsi_max_upload_bytes"),
    ],
)
def test_oversized_content_length_is_refused_without_reading_the_body(
    admin_user, path, cap_attr, set_cap
):
    """An authorised upload whose declared size is over the route's cap gets
    413 before Starlette spools a byte of it."""
    set_cap(cap_attr, 5 * 1024 * 1024)
    token = create_token(admin_user)
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", str(50 * 1024 * 1024 * 1024).encode()),
    ]
    status, body, receives = _asgi_post(path, headers, [b"--xyz\r\n"])
    assert status == 413, body
    assert receives == 0, "the request body was read before the size check"


def test_content_length_within_the_cap_is_not_refused_early(admin_user, set_cap):
    set_cap("backup_max_upload_bytes", 5 * 1024 * 1024)
    token = create_token(admin_user)
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", b"1000"),
    ]
    status, _body, receives = _asgi_post("/api/admin/restore", headers, [b"--xyz--\r\n"])
    assert status != 413 and receives > 0


def _admin_upload_headers(user, content_length: int) -> list:
    return [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {create_token(user)}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
        (b"content-length", str(content_length).encode()),
    ]


@pytest.fixture
def job_slot_taken():
    """Another backup / restore / transfer job holds the single job slot."""
    from wsi_app import jobs as _jobs

    job = _jobs.store.try_claim_running("backup")
    assert job is not None
    _jobs.store.update(job.id, state="running")
    yield
    _jobs.store.update(job.id, state="succeeded")
    _jobs.store.prune_terminal(max_age_seconds=0)


@pytest.fixture
def restore_apply_pending(tmp_path):
    """A restore-apply's request file awaits the restart that consumes it."""
    from wsi_app.config import settings

    request = tmp_path / "restore.request"
    request.write_text("{}")
    saved = settings.restore_request_path
    object.__setattr__(settings, "restore_request_path", str(request))
    yield
    object.__setattr__(settings, "restore_request_path", saved)


@pytest.mark.parametrize("state", ["job_slot_taken", "restore_apply_pending"])
@pytest.mark.parametrize("path", ["/api/admin/restore", "/api/admin/slides/import"])
def test_busy_restore_or_import_is_refused_before_the_body_is_read(
    request, client, admin_user, admin_headers, monkeypatch, path, state
):
    """The endpoints answer 409 while the job slot is taken, but only after
    FastAPI has parsed (spooled) the whole bundle, which can be hundreds of
    GB. The pre-auth gives the same answer with the body untouched."""
    from wsi_app import main as app_main

    request.getfixturevalue(state)
    status, body, receives = _asgi_post(
        path, _admin_upload_headers(admin_user, 10 * 1024 * 1024), [b"--xyz\r\n"]
    )
    assert status == 409, body
    assert receives == 0, "the request body was read before the busy check"

    # Exactly what the endpoint itself answers once it has the body.
    monkeypatch.setattr(app_main, "_upload_busy_detail", lambda route: None)
    r = client.post(
        path,
        files={"file": ("bundle.tar.zst", _BODY, "application/octet-stream")},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == body["detail"]


def test_busy_check_follows_authorisation(manager_user, job_slot_taken):
    """An under-privileged caller still learns nothing but 403."""
    status, body, receives = _asgi_post(
        "/api/admin/restore", _admin_upload_headers(manager_user, 1000), [b"--xyz--\r\n"]
    )
    assert status == 403 and body["detail"] == "Forbidden" and receives == 0


def test_slide_upload_does_not_wait_for_the_job_slot(admin_user, job_slot_taken):
    """Slide uploads never claim the job slot, so a running backup must not
    refuse them."""
    status, _body, receives = _asgi_post(
        "/api/admin/slides/upload", _admin_upload_headers(admin_user, 1000), [b"--xyz--\r\n"]
    )
    assert status != 409 and receives > 0


def test_upload_size_and_space_checks_run_off_the_event_loop(admin_user, monkeypatch):
    """realpath / stat / statvfs of the data volumes block; on a stalled
    (network) mount they must not park the worker's event loop."""
    import threading

    from wsi_app import admin_helpers

    seen: list = []

    def recording(size, factor=2):
        seen.append(threading.current_thread())
        return False

    monkeypatch.setattr(admin_helpers, "upload_space_short", recording)
    status, _body, receives = _asgi_post(
        "/api/admin/slides/upload", _admin_upload_headers(admin_user, 1000), [b"--xyz--\r\n"]
    )
    assert receives > 0
    # _asgi_post runs the event loop on this (the main) thread.
    assert seen and all(t is not threading.main_thread() for t in seen), seen


# ---------------------------------------------------------------------------
# A body with no Content-Length is checked as it streams in
# ---------------------------------------------------------------------------
def _chunked_headers(user) -> list:
    """An authorised multipart request that declares no length (chunked)."""
    return [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {create_token(user)}".encode()),
        (b"content-type", b"multipart/form-data; boundary=xyz"),
    ]


def _multipart_chunks(n: int, size: int) -> list:
    head = (b"--xyz\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n")
    return [head] + [b"\0" * size for _ in range(n)] + [b"\r\n--xyz--\r\n"]


@pytest.mark.parametrize(
    "path,cap_attr",
    [
        ("/api/admin/restore", "backup_max_upload_bytes"),
        ("/api/admin/slides/import", "backup_max_upload_bytes"),
        ("/api/admin/slides/upload", "wsi_max_upload_bytes"),
        ("/api/admin/slides/7/wsi", "wsi_max_upload_bytes"),
    ],
)
def test_a_chunked_body_over_the_cap_is_cut_off_as_it_arrives(admin_user, path, cap_attr, set_cap, monkeypatch):
    """Without Content-Length the size cap never saw the request, and the whole
    body was spooled before the endpoint refused it."""
    import wsi_app.main as main

    set_cap(cap_attr, 10_000)
    monkeypatch.setattr(main, "_UPLOAD_CAP_SLACK", 0)
    chunks = _multipart_chunks(200, 1000)  # 200 KB against a 10 KB cap
    status, body, receives = _asgi_post(path, _chunked_headers(admin_user), chunks)
    assert status == 413, body
    assert body["detail"] == "Upload too large"
    assert receives < len(chunks) // 2, "the body was read to the end before it was refused"


def test_a_chunked_body_within_the_cap_is_not_refused(admin_user, set_cap, monkeypatch):
    import wsi_app.main as main

    set_cap("wsi_max_upload_bytes", 10_000_000)
    monkeypatch.setattr(main, "_UPLOAD_CAP_SLACK", 0)
    chunks = _multipart_chunks(5, 1000)
    status, _body, receives = _asgi_post("/api/admin/slides/upload", _chunked_headers(admin_user), chunks)
    assert status != 413 and receives >= len(chunks)


def test_a_chunked_body_is_cut_off_once_the_volume_cannot_hold_it(admin_user, set_cap, monkeypatch):
    import wsi_app.main as main

    set_cap("wsi_max_upload_bytes", 10_000_000)
    monkeypatch.setattr(main, "_UPLOAD_SPACE_RECHECK_BYTES", 5_000)
    seen: list[int] = []

    def no_room(route, nbytes):
        seen.append(nbytes)
        return True

    monkeypatch.setattr(main, "_upload_stream_no_space", no_room)
    chunks = _multipart_chunks(100, 1000)
    status, body, receives = _asgi_post("/api/admin/slides/upload", _chunked_headers(admin_user), chunks)
    assert status == 507, body
    assert body["detail"]["code"] == "adminDash.apiErr.uploadNoSpace"
    assert seen and seen[0] >= 5_000, "the space was re-measured against the bytes received so far"
    assert receives < len(chunks) // 2


def test_a_declared_body_is_not_space_checked_again_while_streaming(admin_user, set_cap, monkeypatch):
    """With a Content-Length the pre-body check already measured the space for
    the whole body; re-measuring per 64 MiB would only repeat it. (The slide
    upload route stands in for the archive ones: it claims no job slot, and a
    file of an unsupported name is refused by the endpoint without side effects.)"""
    import wsi_app.main as main

    set_cap("wsi_max_upload_bytes", 10_000_000)
    monkeypatch.setattr(main, "_UPLOAD_SPACE_RECHECK_BYTES", 1_000)
    monkeypatch.setattr(main, "_upload_no_space", lambda route, nbytes: False)
    calls: list[int] = []
    real = main._upload_no_space

    def counting(route, nbytes):
        calls.append(nbytes)
        return real(route, nbytes)

    chunks = _multipart_chunks(20, 1000)
    headers = _admin_upload_headers(admin_user, sum(len(c) for c in chunks))
    monkeypatch.setattr(main, "_upload_no_space", counting)
    _asgi_post("/api/admin/slides/upload", headers, chunks)
    assert len(calls) == 1, "only the pre-body check measured the space"


@pytest.mark.parametrize(
    "path",
    ["/api/admin/restore", "/api/admin/slides/import", "/api/admin/slides/upload", "/api/admin/slides/7/wsi"],
)
@pytest.mark.parametrize("suffix", ["", "\n"])
def test_the_upload_gate_covers_every_path_the_router_sends_to_an_upload_endpoint(path, suffix):
    """The gate that refuses a hopeless body before it is read must see every
    request the router would hand to those endpoints, trailing newline included."""
    from starlette.routing import Match

    from wsi_app.main import _upload_route, app

    scope = {"type": "http", "method": "POST", "path": path + suffix, "root_path": ""}
    if not any(r.matches(scope)[0] == Match.FULL for r in app.router.routes):
        pytest.skip("the router no longer accepts this path")
    assert _upload_route(path + suffix) is not None


# ---------------------------------------------------------------------------
# The streamed space check asks for what is still to be written, not the whole
# upload again (the bytes already spooled are already off the free space)
# ---------------------------------------------------------------------------
@pytest.fixture
def one_volume(monkeypatch, tmp_path):
    """One volume whose free space the test sets, with no reserve."""
    import wsi_app.admin_helpers as ah
    import wsi_app.disk_space as ds

    state = {"free": 0}
    monkeypatch.setattr(ah, "upload_space_volumes", lambda: (str(tmp_path), str(tmp_path), True))
    monkeypatch.setattr(ds, "free_bytes", lambda path: state["free"])
    monkeypatch.setattr(ds, "reserve_for", lambda nbytes: 0)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    return state


def test_a_slide_that_fits_is_not_refused_just_because_it_was_sent_chunked(monkeypatch, one_volume):
    import wsi_app.main as main

    monkeypatch.setattr(main, "_UPLOAD_SPACE_RECHECK_BYTES", 1_000)
    route = main._upload_route("/api/admin/slides/upload")
    # 300 KB free before a 120 KB upload: it fits twice over (spool + staged
    # copy), and the declared-length check says so...
    one_volume["free"] = 300_000
    assert main._upload_no_space(route, 120_000) is False
    # ...so once all 120 KB are spooled (180 KB left), the chunked recheck must
    # not ask for two more copies of it.
    one_volume["free"] = 180_000
    assert main._upload_stream_no_space(route, 120_000) is False
    # What is genuinely still to be written is the staged copy plus what is next.
    one_volume["free"] = 120_000
    assert main._upload_stream_no_space(route, 120_000) is True


def test_an_archive_that_fits_is_not_refused_just_because_it_was_sent_chunked(monkeypatch, one_volume):
    import wsi_app.main as main

    monkeypatch.setattr(main, "_UPLOAD_SPACE_RECHECK_BYTES", 1_000)
    route = main._upload_route("/api/admin/restore")
    one_volume["free"] = 300_000
    assert main._upload_no_space(route, 200_000) is False
    # 200 KB spooled, 100 KB left: the body is in, only the next stretch counts.
    one_volume["free"] = 100_000
    assert main._upload_stream_no_space(route, 200_000) is False
    one_volume["free"] = 500
    assert main._upload_stream_no_space(route, 200_000) is True
