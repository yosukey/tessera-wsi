"""Public app-access authentication routes (login flows for end users).

These routes implement the /api/auth/* surface used by the React app for
session-based login (Basic, LDAP, Google OAuth). Distinct from the admin
JWT login defined in admin_auth.py.
"""
from __future__ import annotations

import base64
import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import app_auth as _app_auth
from .. import cf_access as _cf_access
from .. import dzi as _dzi
from .. import login_throttle
from ..admin_helpers import audit
from ..config import settings
from ..db import get_db
from ..models import AuthUser
from ..network import forwarded_proto, get_client_ip
from .admin_auth import _masked_unknown_username

# Namespace prefix so app-login failures share ONE per-IP bucket across the
# form endpoint and the middleware's HTTP Basic branch, kept separate from the
# admin-login and re-auth buckets so the three limits are independent.
APP_LOGIN_THROTTLE_PREFIX = "app:"


def app_login_throttle_key(client_ip: str) -> str:
    # An IPv6 client is keyed on its /64 (see login_throttle.ip_bucket), so
    # rotating addresses inside it buys no fresh budget.
    return f"{APP_LOGIN_THROTTLE_PREFIX}{login_throttle.ip_bucket(client_ip)}"

router = APIRouter()


def _oauth_redirect_uri(request: Request) -> str:
    # Reconstruct the callback from the host this request actually arrived on.
    proto = forwarded_proto(request.headers, request.url.scheme) or "http"
    host = request.headers.get("host") or (
        request.url.netloc or "localhost"
    )
    derived = f"{proto}://{host}/api/auth/oauth/callback"
    # Prefer the operator-pinned public base URL so the redirect_uri sent to
    # Google does not depend on whatever Host header an upstream proxy happens
    # to forward — but ONLY when this request is actually on the pinned host.
    #
    # The OAuth state cookie is host-scoped: /oauth/start sets it on the host
    # the browser is currently on, and /oauth/callback can only read it back on
    # that same host. Pinning the redirect_uri unconditionally forces the
    # callback onto the public host even for a login that STARTED on another
    # host — e.g. the LAN-direct host the probe redirects same-VLAN clients to
    # (see docs/operations.md -> "Redirect URIs when the LAN-direct host is
    # also in use"). The state cookie set on that other host is then absent at
    # the public-host callback and the login fails with "OAuth state mismatch".
    #
    # So apply the pin only when the request Host matches the pinned host; on
    # any other host, derive the callback from the current host so both legs
    # run together. Google's Authorized-redirect-URI allow-list bounds the
    # derived value exactly as it does the WSI_PUBLIC_BASE_URL-unset default,
    # so this does not widen the attack surface: an operator serving more than
    # one hostname simply registers each callback there.
    if settings.public_base_url:
        pinned_host = urlsplit(settings.public_base_url).netloc.lower()
        if pinned_host and host.lower() == pinned_host:
            return f"{settings.public_base_url}/api/auth/oauth/callback"
    return derived


def _is_secure_request(request: Request) -> bool:
    return forwarded_proto(request.headers, request.url.scheme) == "https"


def _secrets_compare(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _reject_cross_site_post(request: Request) -> None:
    """Refuse a session-mutating POST that a *different* site initiated (CSRF).

    Guards both ends of the session lifecycle, because both are driven purely
    by the response's ``Set-Cookie`` and so need no ambient credential to
    abuse:

    * **Login.** An attacker auto-submits a cross-origin form POST carrying
      their OWN credentials; the victim's browser stores the resulting
      ``wsi_session`` cookie and the victim then browses under the attacker's
      identity. No privilege is gained (app access is all-or-nothing and
      view-only), but every page view the victim makes is then attributed to
      the attacker's subject in the analytics tables.
    * **Logout.** The same trick against ``/api/auth/logout`` makes the browser
      honour the deletion ``Set-Cookie``, signing the victim out at will.

    ``SameSite=Lax`` on the cookie does not help in either case — the *request*
    carries no cookie, so nothing is blocked on the way in.

    Enforced from the fetch-metadata headers rather than a double-submit token
    so no client-side plumbing is needed:

    * ``Sec-Fetch-Site`` is set by the browser and unforgeable from script;
      ``same-origin`` / ``none`` (a direct navigation) are accepted.
    * Browsers that predate fetch metadata still send ``Origin`` on a
      cross-origin POST, so fall back to comparing it against the request Host.

    The fallback compares HOSTNAMES ONLY, deliberately ignoring the port: nginx
    forwards ``Host: $host``, which carries no port, while the browser's
    ``Origin`` includes one whenever the site is served on a non-default port.
    Comparing the full authority would then reject every login on such an
    install. The port is not what distinguishes an attacker's site from the
    app's own, so dropping it costs nothing that matters here.

    A request carrying neither header is allowed through: that is a non-browser
    client (curl, a scripted integration), which is not a CSRF vector because
    there is no ambient cookie jar to abuse.
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        if site in ("same-origin", "none"):
            return
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    origin = (request.headers.get("origin") or "").strip()
    if not origin or origin.lower() == "null":
        return
    # Parse the Host header through urlsplit too, so an IPv6 literal
    # ("[::1]:8000") and a port suffix are stripped the same way on both sides.
    # One trailing dot is dropped as well: nginx strips it from $host while the
    # browser's Origin keeps it, so "wsi.example." would otherwise refuse every
    # login from a browser without fetch metadata (as in the sync WS check).
    host_name = (urlsplit(f"//{request.headers.get('host') or ''}").hostname or "").lower()
    origin_name = (urlsplit(origin).hostname or "").lower()
    host_name = host_name[:-1] if host_name.endswith(".") else host_name
    origin_name = origin_name[:-1] if origin_name.endswith(".") else origin_name
    if host_name and origin_name and origin_name != host_name:
        raise HTTPException(status_code=403, detail="cross-site request rejected")


@router.get("/api/auth/state")
def auth_state(request: Request):
    """Tell the frontend which login flow to render.

    ``authenticated`` says whether the middleware would admit this visitor
    (what the public pages' gate needs); ``app_session`` whether that rests
    on an app-access session cookie — the one thing ``/api/auth/logout``
    ends, so the only case in which "Sign out" means anything."""
    mode = _app_auth.current_mode()
    # On the break-glass raw-IP and direct-LAN-IP paths the middleware admits
    # the request without app auth, so report an un-gated state: otherwise
    # useAuthGate would bounce the visitor to /login for a flow (e.g. Google
    # OAuth) that cannot complete on a private-IP/http origin, stranding them
    # on a login screen.
    client_host = request.client.host if request.client else "127.0.0.1"
    if (
        _app_auth.breakglass_exempt(request.headers, client_host)
        or _app_auth.lan_direct_exempt(request.headers, client_host)
    ):
        return {"mode": "none", "authenticated": True, "app_session": False}
    out = {"mode": mode, "authenticated": False, "app_session": False}
    cookie = request.cookies.get(_app_auth.SESSION_COOKIE)
    if cookie and _app_auth.decode_session_token(cookie):
        out["authenticated"] = True
        out["app_session"] = True
    elif mode != "none":
        # This path bypasses the middleware, so it must recognise the other
        # credentials the middleware admits. A staff bearer (the SPA sends the
        # dashboard's token when it holds one): staff are never asked for a
        # second, app-access login, so without this a manager opening a draft
        # preview or the presenter view from the dashboard is bounced to
        # /login. And a verified Cloudflare Access JWT (Path A): without it an
        # external user CF Access already let in, with no LDAP / Google
        # account here, is bounced likewise. Neither is an app session
        # (app_session stays false): logging out ends neither, so a "Sign
        # out" offered on them would only bounce off /login back to the page.
        # Both checks are synchronous (a users-table read; cf_jwt_status may
        # fetch the JWKS), which is fine here: this is a sync handler, so they
        # run in the threadpool, not on the event loop.
        auth_header = request.headers.get("authorization", "")
        if _app_auth.staff_token_valid(auth_header):
            out["authenticated"] = True
        else:
            cf_status, _claims = _cf_access.cf_jwt_status(request.headers, client_host)
            if cf_status == "valid":
                out["authenticated"] = True
    # Operator-configured overrides for the login screen (empty -> the
    # frontend uses its built-in per-mode defaults). Surfaced unauthenticated
    # so the login page can render them before any session exists.
    out["login_message"] = _app_auth.login_message()
    out["login_button_label"] = _app_auth.login_button_label()
    return out


@router.get("/api/edge-authz", include_in_schema=False)
def edge_authz(request: Request) -> Response:
    """Authorization probe for the nginx ``auth_request`` gate on the
    edge-cached read surfaces: ``/dzi/`` tiles, ``/thumbs/`` thumbnails, and
    ``/assets/`` tour attachments.

    Reaching this handler means the app-access middleware already admitted the
    request (a valid session cookie / admin bearer / Cloudflare-Access JWT / a
    configured exemption, or ``mode == "none"``). nginx consults only the
    status code of the auth subrequest: 2xx authorizes the fetch, 401/403 deny
    it (auth_request turns any other status into a 500), and the middleware's
    own 401 for an unauthenticated gated request is the app-access denial.

    Per-slide check. nginx passes the original request URI as
    ``X-Original-URI``. For a ``/dzi/{public_id}...`` URI the probe also
    applies the /dzi/ routes' publish gate — the slide must exist and be
    published, or the request must carry a valid staff bearer (the draft
    preview, which the middleware records as ``request.state.staff_bearer``
    when it admits one, so no second users-table read is paid here) — read from
    the in-memory slide_resolve_cache, never a per-request DB query for a known
    slide. Without it, a cached tile of an unpublished / deleted slide stayed
    servable at its URL for the cache's 30-day TTL, since a cache HIT never
    reaches the routes' own gate. nginx folds the slide token into the cached
    verdict's key (``$wsi_authz_object``), so an allow for one slide is never
    reused for another, and the verdict's 60 s TTL is the revocation latency of
    an unpublish / delete at the edge. A denial is 403 — the same status for a
    draft and a missing slide, so the probe is no existence oracle — and is
    never cached. ``/thumbs/`` and ``/assets/`` URIs keep the app-access-only
    verdict (thumbnail names rotate on unpublish; tour attachments must stay
    reachable by the admin editor's markdown <img> preview of a draft tour,
    which carries no bearer). With no header at all (a hand-edited nginx
    config that omits it, or a direct call) the verdict is app-access only.

    Why this endpoint exists: without it, keeping the gate intact would force
    those surfaces to fold the per-user session cookie into their HTTP cache
    key (``/dzi/``) or not be cached at all (``/thumbs/`` / ``/assets/``), so
    that a cache HIT could not bypass the auth gate. Under app-access auth that
    would mean a shared classroom load — a hundred or more viewers on the *same* slide/tour
    — hits the app once per user per tile / thumbnail / asset instead of once
    total. Routing the gate through this (separately cached) auth subrequest
    lets those responses be SHARED across all authenticated users while keeping
    the gate fully intact.

    Deliberately NOT under the ``/api/auth/`` bypass prefix (see
    ``app_auth.BYPASS_PREFIXES``): it must be gated exactly like the surfaces it
    guards so its verdict matches what a direct request would get. A path under
    ``/api/auth/`` would bypass the gate and make the probe always authorize.
    """
    original_uri = request.headers.get("x-original-uri")
    if original_uri is not None and not _dzi.edge_slide_access(
        original_uri,
        staff=bool(getattr(request.state, "staff_bearer", False)),
    ):
        return Response(status_code=403)
    return Response(status_code=204)


@router.post("/api/auth/login")
def auth_login(
    request: Request,
    response: Response,
    # max_length caps bound the cost of an unauthenticated request: without
    # them, a multi-MB password would burn pbkdf2 / LDAP-bind time before
    # any credential decision. 1024 / 255 are well above any legitimate
    # user input and match the auth.py constants.
    username: str = Form(..., max_length=255),
    password: str = Form(..., max_length=1024),
    db: Session = Depends(get_db),
):
    # Per-IP throttle FIRST, before any pbkdf2 (basic) / LDAP bind (ldap): the
    # app-access login is otherwise unbounded against brute-force / credential
    # stuffing, and each attempt burns real verify cost (and, under ldap, hits
    # the directory server). Keys on IP only, so the 429 leaks nothing about
    # which usernames exist. Shares one bucket with the Basic-header branch.
    _reject_cross_site_post(request)
    client_ip = get_client_ip(
        request.headers, request.client.host if request.client else ""
    )
    throttle_key = app_login_throttle_key(client_ip)
    retry_after = login_throttle.retry_after_seconds(
        throttle_key,
        max_failures=settings.app_login_ip_max_failures,
        window_seconds=settings.app_login_ip_window_seconds,
    )
    if retry_after > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    # One policy snapshot for the whole login: the credential is checked
    # against it and the cookie is stamped with its generation, so a policy
    # tightening that lands while the LDAP bind / pbkdf2 is in flight can't
    # promote a result the old policy produced into a new-generation cookie.
    snap = _app_auth.auth_snapshot()
    mode = snap.mode
    credential = None
    if mode == "ldap":
        ok = _app_auth.verify_ldap(username, password, cfg=snap.ldap)
    elif mode == "basic":
        # Allow logging in with the stored basic-auth users via form too,
        # in case an operator wants the same UX as LDAP. The cookie is bound
        # to the credential so deleting the user / resetting the password
        # ends the session (see app_auth._basic_session_live).
        credential = _app_auth.verify_basic_credential(username, password, db)
        ok = credential is not None
    else:
        raise HTTPException(status_code=400, detail="Login form not active for current mode")
    if not ok:
        login_throttle.record_failure(
            throttle_key,
            window_seconds=settings.app_login_ip_window_seconds,
            max_failures=settings.app_login_ip_max_failures,
            username=username,
        )
        # Failures only: successful viewer sign-ins arrive in classroom-sized
        # bursts and would crowd the admin history out of its row cap, while a
        # failed attempt with its source is what a brute-force investigation
        # needs. The password is never passed.
        # The name is kept only when it is a local viewer account (whose account
        # is being guessed at). Anything else - an LDAP identity this server
        # cannot vouch for, or a name no account has - is often a password
        # pasted into the wrong field, and the history shows it to every admin
        # and exports it to CSV: reduced to its first character and length.
        local = mode == "basic" and db.execute(
            select(AuthUser.id).where(AuthUser.username == username)
        ).first() is not None
        audit(
            None, "app.login.failure",
            target=username[:128] if local else _masked_unknown_username(username),
            mode=mode, ip=client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # A successful login forgives this IP's recent failures for THIS username
    # so a user who fumbled their password a few times before getting it right
    # isn't left one mistake away from the throttle. Failures against other
    # usernames stay counted: otherwise holding one valid account would reset
    # the budget for guessing the rest.
    login_throttle.clear_user(throttle_key, username)
    if _app_auth.session_generation() != snap.session_generation:
        # The cookie would be stamped with the superseded generation and be
        # refused on its first use; say so instead of reporting success.
        raise HTTPException(
            status_code=409,
            detail="Sign-in settings changed during login. Please try again.",
        )
    token = _app_auth.create_session_token(
        f"{mode}:{username}",
        credential=credential,
        generation=snap.session_generation,
    )
    response.set_cookie(
        _app_auth.SESSION_COOKIE,
        token,
        max_age=_app_auth.session_ttl(),
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    _reject_cross_site_post(request)
    secure = _is_secure_request(request)
    # Mirror the attributes used at set time so the deletion Set-Cookie is
    # not flagged as a different cookie by stricter clients / intermediaries.
    response.delete_cookie(
        _app_auth.SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    # When a shared cookie domain is configured, maybe_carry_cf_session mints a
    # *domain-scoped* wsi_session cookie. Browsers key cookies by
    # (name, domain, path), so the host-only deletion above does NOT clear it —
    # without this second deletion, "Log out" silently leaves the CF-carried
    # session valid until its TTL. No-op for stock host-only deployments.
    if _app_auth.SESSION_COOKIE_DOMAIN:
        response.delete_cookie(
            _app_auth.SESSION_COOKIE,
            path="/",
            domain=_app_auth.SESSION_COOKIE_DOMAIN,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
    return {"ok": True}


def _oauth_next_target(request: Request) -> str:
    """The validated post-login path from the next cookie, else ``"/"``."""
    raw = request.cookies.get(_app_auth.OAUTH_NEXT_COOKIE) or ""
    try:
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
    except Exception:
        return "/"
    return _app_auth.safe_next_path(decoded) or "/"


@router.get("/api/auth/oauth/start")
def auth_oauth_start(request: Request, next: str = Query("")):
    if _app_auth.current_mode() != "google_oauth":
        raise HTTPException(status_code=400, detail="Google OAuth is not the active mode")
    cfg = _app_auth._snapshot.google or _app_auth._EMPTY_MAP
    if not cfg.get("client_id"):
        raise HTTPException(status_code=400, detail="Google OAuth client is not configured")
    state = _app_auth.new_oauth_state()
    url = _app_auth.google_authorize_url(state, _oauth_redirect_uri(request))
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(
        _app_auth.OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        path="/api/auth",
    )
    # Remember where the visitor was headed (a shared sync / tour URL) so the
    # callback can send them there. Base64url keeps any query punctuation out
    # of the cookie syntax; the callback re-validates what it reads back.
    target = _app_auth.safe_next_path(next)
    if target:
        resp.set_cookie(
            _app_auth.OAUTH_NEXT_COOKIE,
            base64.urlsafe_b64encode(target.encode()).decode(),
            max_age=600,
            httponly=True,
            secure=_is_secure_request(request),
            samesite="lax",
            path="/api/auth",
        )
    else:
        # A previous start that carried a destination left its cookie behind
        # (10 min); a fresh sign-in with none must not inherit it.
        resp.delete_cookie(_app_auth.OAUTH_NEXT_COOKIE, path="/api/auth")
    return resp


@router.get("/api/auth/oauth/callback")
def auth_oauth_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    # Same single-snapshot rule as auth_login: the code exchange and JWKS
    # fetch below are network round-trips an allowed-domain change can land
    # during.
    snap = _app_auth.auth_snapshot()
    if snap.mode != "google_oauth":
        raise HTTPException(status_code=400, detail="Google OAuth is not the active mode")
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    expected = request.cookies.get(_app_auth.OAUTH_STATE_COOKIE)
    if not state or not expected or not _secrets_compare(state, expected):
        raise HTTPException(status_code=400, detail="OAuth state mismatch")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    tok = _app_auth.google_exchange_code(code, _oauth_redirect_uri(request))
    if not tok or "id_token" not in tok:
        raise HTTPException(status_code=401, detail="Token exchange failed")
    payload = _app_auth.verify_google_id_token(tok["id_token"])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid ID token")
    email = (payload.get("email") or "").lower()
    # Fail closed when the claim is missing: only treat the email as verified
    # if Google explicitly says so. `is True`, not truthiness: a string
    # "false" is truthy.
    if not email or payload.get("email_verified") is not True:
        raise HTTPException(status_code=403, detail="Email not verified")
    if not _app_auth.google_email_allowed(
        email, cfg=snap.google, hd=payload.get("hd")
    ):
        raise HTTPException(status_code=403, detail="Email not allowed")
    if _app_auth.session_generation() != snap.session_generation:
        raise HTTPException(
            status_code=409,
            detail="Sign-in settings changed during login. Please try again.",
        )
    session = _app_auth.create_session_token(
        f"google:{email}", generation=snap.session_generation
    )
    resp = RedirectResponse(url=_oauth_next_target(request), status_code=302)
    resp.set_cookie(
        _app_auth.SESSION_COOKIE,
        session,
        max_age=_app_auth.session_ttl(),
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        path="/",
    )
    for name in (_app_auth.OAUTH_STATE_COOKIE, _app_auth.OAUTH_NEXT_COOKIE):
        resp.delete_cookie(
            name,
            path="/api/auth",
            httponly=True,
            secure=_is_secure_request(request),
            samesite="lax",
        )
    return resp
