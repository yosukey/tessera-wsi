"""Google OAuth login: post-login redirect target and ID-token claim checks."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from wsi_app import app_auth as _app_auth
from wsi_app.app_auth import safe_next_path


@pytest.fixture
def google_mode(monkeypatch):
    def _set(domains=("example.com",), emails=()):
        monkeypatch.setattr(
            _app_auth,
            "_snapshot",
            _app_auth._AuthSnapshot(
                mode="google_oauth",
                google=_app_auth.MappingProxyType({
                    "client_id": "cid",
                    "client_secret": "secret",
                    "allowed_domains": tuple(domains),
                    "allowed_emails": frozenset(emails),
                }),
            ),
        )

    _set()
    return _set


def _claims(**over):
    base = {"email": "a@example.com", "email_verified": True, "hd": "example.com"}
    base.update(over)
    return base


def _login(client, monkeypatch, *, next_path=None, claims=None):
    """Drive start -> callback and return the callback response."""
    monkeypatch.setattr(
        _app_auth, "google_exchange_code", lambda code, uri: {"id_token": "t"}
    )
    monkeypatch.setattr(
        _app_auth, "verify_google_id_token", lambda tok: claims or _claims()
    )
    params = {} if next_path is None else {"next": next_path}
    start = client.get("/api/auth/oauth/start", params=params, follow_redirects=False)
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return client.get(
        "/api/auth/oauth/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "slides/x",
        "//evil.com",
        "//evil.com/slides/x",
        "/\\evil.com",
        "/slides\\x",
        "https://evil.com/",
        "javascript:alert(1)",
        "/ok\r\nSet-Cookie: x=1",
        "/login",
        "/login?next=/slides/x",
        "/" + "a" * 3000,
    ],
)
def test_safe_next_path_rejects(raw):
    assert safe_next_path(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["/", "/slides/abc?sync=Xy_9-z", "/tours/t1#step-2", "/loginx", "/a%2F%2Fb"],
)
def test_safe_next_path_accepts(raw):
    assert safe_next_path(raw) == raw


def test_oauth_callback_honours_valid_next(client, monkeypatch, google_mode):
    target = "/slides/abc?sync=tok&x=1;y"
    r = _login(client, monkeypatch, next_path=target)
    assert r.status_code == 302
    assert r.headers["location"] == target


@pytest.mark.parametrize(
    "evil", ["//evil.com", "/\\evil.com", "https://evil.com/x", "evil.com", "/login"]
)
def test_oauth_callback_ignores_unsafe_next(client, monkeypatch, google_mode, evil):
    r = _login(client, monkeypatch, next_path=evil)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_oauth_callback_revalidates_tampered_cookie(client, monkeypatch, google_mode):
    """The cookie is client-controlled: the callback must not trust it."""
    import base64

    monkeypatch.setattr(
        _app_auth, "google_exchange_code", lambda code, uri: {"id_token": "t"}
    )
    monkeypatch.setattr(_app_auth, "verify_google_id_token", lambda tok: _claims())
    start = client.get("/api/auth/oauth/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    client.cookies.set(
        _app_auth.OAUTH_NEXT_COOKIE,
        base64.urlsafe_b64encode(b"//evil.com").decode(),
        path="/api/auth",
    )
    r = client.get(
        "/api/auth/oauth/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
    # Garbage that is not base64 at all (set after start, which would otherwise
    # clear a pre-existing cookie).
    start = client.get("/api/auth/oauth/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    client.cookies.set(_app_auth.OAUTH_NEXT_COOKIE, "!!!not-base64", path="/api/auth")
    r = client.get(
        "/api/auth/oauth/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_stale_next_cookie_does_not_leak_into_the_next_signin(client, monkeypatch, google_mode):
    """A start that carried a destination leaves a 10-minute cookie. A later
    start with no destination (same browser) must go home, not to the old one."""
    target = "/slides/secret-room?sync=tok"
    first = _login(client, monkeypatch, next_path=target)
    assert first.headers["location"] == target
    # Simulate a callback that never ran for the first start: the cookie lingers.
    start = client.get("/api/auth/oauth/start", params={"next": target}, follow_redirects=False)
    assert start.status_code == 302
    r = _login(client, monkeypatch)  # no next
    assert r.headers["location"] == "/"


def test_oauth_callback_without_next_goes_home(client, monkeypatch, google_mode):
    assert _login(client, monkeypatch).headers["location"] == "/"


def test_non_workspace_account_with_allowed_email_domain_is_refused(
    client, monkeypatch, google_mode
):
    """No ``hd`` claim means a personal account that merely uses an address at
    the allowed domain."""
    r = _login(client, monkeypatch, claims=_claims(hd=None))
    assert r.status_code == 403
    claims = _claims()
    del claims["hd"]
    assert _login(client, monkeypatch, claims=claims).status_code == 403
    assert "wsi_session" not in client.cookies


def test_hd_claim_must_be_an_allowed_domain(client, monkeypatch, google_mode):
    assert _login(client, monkeypatch, claims=_claims(hd="other.org")).status_code == 403
    assert _login(client, monkeypatch, claims=_claims(hd=["example.com"])).status_code == 403
    ok = _login(client, monkeypatch, claims=_claims(hd="Example.COM"))
    assert ok.status_code == 302


@pytest.mark.parametrize("flag", ["false", "true", 1, None, "yes"])
def test_email_verified_must_be_boolean_true(client, monkeypatch, google_mode, flag):
    r = _login(client, monkeypatch, claims=_claims(email_verified=flag))
    assert r.status_code == 403


def test_email_domain_must_still_be_allowed(client, monkeypatch, google_mode):
    r = _login(client, monkeypatch, claims=_claims(email="a@other.org", hd="example.com"))
    assert r.status_code == 403


def test_google_email_allowed_unit():
    cfg = {"allowed_domains": ("example.com",)}
    f = _app_auth.google_email_allowed
    assert f("a@example.com", cfg=cfg, hd="example.com")
    assert not f("a@example.com", cfg=cfg)
    assert not f("a@example.com", cfg=cfg, hd="other.org")
    assert not f("a@other.org", cfg=cfg, hd="example.com")
    assert not f("a@example.com", cfg={"allowed_domains": ()}, hd="example.com")


def test_individually_allowed_email_signs_in_without_workspace_hd(
    client, monkeypatch, google_mode
):
    """A listed address is admitted on its own: no ``hd``, domain unlisted."""
    google_mode(domains=("example.com",), emails=("friend@gmail.com",))
    claims = _claims(email="Friend@Gmail.com")
    del claims["hd"]
    r = _login(client, monkeypatch, claims=claims)
    assert r.status_code == 302
    # Another address at the same personal-mail domain stays refused.
    claims = _claims(email="other@gmail.com")
    del claims["hd"]
    assert _login(client, monkeypatch, claims=claims).status_code == 403


def test_individually_allowed_email_still_needs_verified_email(
    client, monkeypatch, google_mode
):
    google_mode(domains=(), emails=("friend@gmail.com",))
    r = _login(
        client, monkeypatch,
        claims=_claims(email="friend@gmail.com", email_verified=False, hd=None),
    )
    assert r.status_code == 403


def test_google_email_allowed_individual_addresses_unit():
    f = _app_auth.google_email_allowed
    cfg = {"allowed_domains": (), "allowed_emails": frozenset({"a@gmail.com"})}
    assert f("a@gmail.com", cfg=cfg)
    assert f("A@GMAIL.com", cfg=cfg)
    assert not f("b@gmail.com", cfg=cfg)
    assert not f("a@gmail.com.evil.org", cfg=cfg)
    # Domain rule still applies alongside the address list.
    cfg = {"allowed_domains": ("example.com",), "allowed_emails": frozenset({"a@gmail.com"})}
    assert f("x@example.com", cfg=cfg, hd="example.com")
    assert not f("x@example.com", cfg=cfg)
    assert not f("a@gmail.com", cfg={"allowed_domains": (), "allowed_emails": frozenset()})
    # Listing an address at a listed Workspace domain does not lift its hd
    # requirement (that would only admit a personal account at the address).
    cfg = {"allowed_domains": ("example.com",), "allowed_emails": frozenset({"x@example.com"})}
    assert not f("x@example.com", cfg=cfg)
    assert f("x@example.com", cfg=cfg, hd="example.com")
