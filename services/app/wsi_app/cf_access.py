"""Cloudflare Access integration: trusted-header parsing and JWT verification.

When the app is fronted by Cloudflare Tunnel + Cloudflare Access, every
authenticated request carries:

* ``Cf-Access-Authenticated-User-Email`` — a convenience header carrying the
  end-user email. NOT covered by the JWT signature below, so the app never
  reads it; the trusted identity is the signed JWT's ``email`` claim (see
  ``email_from_claims``).
* ``Cf-Access-Jwt-Assertion`` — a signed JWT, issued by Cloudflare for this
  Access application, that carries the authenticated identity (``email`` /
  ``sub`` claims). The signing keys live at
  ``https://<team>.cloudflareaccess.com/cdn-cgi/access/certs``.
* ``Cf-Connecting-IP`` — the real client IP (handled in ``network.py``).

Trust model
-----------
The headers are only meaningful when the request originated from cloudflared.
Two conditions must hold for the email to be consumed:

1. **The hop is trusted** — the immediate peer (``request.client.host``)
   is in ``TRUSTED_PROXIES``. This blocks an attacker on the LAN side from
   forging the headers when the bypass IP path lets them reach the app
   directly without going through cloudflared.
2. **The JWT is cryptographically valid** — signature verifies against the
   team's JWKS, the ``aud`` claim matches the configured Application AUD
   tag, ``iss`` matches the team domain, and ``exp`` has not lapsed.

Either condition failing is treated as "no CF identity": the access-control
layer rejects with 401 (``cf_jwt_status() == "invalid"``) and the
analytics beacon records the row anonymous-with-anomaly. The app never
silently trusts just the email header — that would let a bypass-path
attacker impersonate any organization member by setting one HTTP header.

Configuration
-------------
The two env vars below are read once at import time:

* ``CF_ACCESS_TEAM_DOMAIN`` — e.g. ``your-team.cloudflareaccess.com``.
  Empty disables the entire integration (no CF header is ever consumed).
* ``CF_ACCESS_AUD`` — the Application AUD tag from the Access dashboard.
  When set, the JWT's ``aud`` claim must match it.

A missing PyJWT or a JWKS fetch failure is treated as "verification
failed" so the fail-closed property holds: the operator sees the header
but the app refuses to trust it.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

import jwt

logger = logging.getLogger("wsi_app.cf_access")

# Header names (case-insensitive in HTTP, but Starlette lowercases them).
CF_JWT_HEADER = "cf-access-jwt-assertion"
CF_CONNECTING_IP_HEADER = "cf-connecting-ip"

_TEAM_DOMAIN = (os.getenv("CF_ACCESS_TEAM_DOMAIN") or "").strip().lower()
_AUDIENCE = (os.getenv("CF_ACCESS_AUD") or "").strip()

# JWKS cache. Cloudflare rotates keys roughly daily; an hour TTL keeps the
# fetch rate negligible while still picking up new keys on the same day they
# appear (a request that hits an unknown ``kid`` triggers a forced refresh
# regardless of TTL — see ``verify_jwt`` below), but no more often than every
# ``_JWKS_FORCE_REFRESH_MIN_INTERVAL`` seconds: the token is attacker-chosen, so
# without a floor every request bearing a made-up ``kid`` would cost a
# synchronous HTTPS fetch to the team's JWKS endpoint.
_JWKS_TTL_SECONDS = 3600
_JWKS_FORCE_REFRESH_MIN_INTERVAL = 30.0
_jwks_cache: dict = {
    "client": None, "fetched_at": 0.0, "team_domain": "", "refreshed_mono": None,
}

# One-shot guard for the "CF_ACCESS_AUD is unset" warning. Without it the
# warning fires on every JWT-bearing request — a misconfigured deployment
# would flood logs at request rate. The flag is reset whenever the operator
# fixes the env (next request after a restart re-evaluates).
_aud_warning_emitted: bool = False


def is_enabled() -> bool:
    """Return True when CF Access integration is configured."""
    return bool(_TEAM_DOMAIN)


def _jwks_url() -> str:
    return f"https://{_TEAM_DOMAIN}/cdn-cgi/access/certs"


def _issuer() -> str:
    return f"https://{_TEAM_DOMAIN}"


def _jwks_client(force_refresh: bool = False):
    """Return a cached PyJWKClient pointed at the team's JWKS endpoint.

    Re-instantiates the client whenever the configured team domain changes
    (only relevant for tests that monkeypatch the module global) so the
    cache never serves keys signed by a different tenant.
    """
    now = time.time()
    cached = _jwks_cache.get("client")
    same_tenant = _jwks_cache.get("team_domain") == _TEAM_DOMAIN
    fresh = (now - _jwks_cache.get("fetched_at", 0.0)) < _JWKS_TTL_SECONDS
    if cached is not None and same_tenant and fresh and not force_refresh:
        return cached
    mono = time.monotonic()
    last = _jwks_cache.get("refreshed_mono")
    if (
        force_refresh
        and cached is not None
        and same_tenant
        and fresh
        and last is not None
        and mono - last < _JWKS_FORCE_REFRESH_MIN_INTERVAL
    ):
        # Refreshed moments ago: keep the current client (and its keys).
        return cached
    client = jwt.PyJWKClient(_jwks_url(), cache_keys=True)
    _jwks_cache["client"] = client
    _jwks_cache["fetched_at"] = now
    _jwks_cache["refreshed_mono"] = mono
    _jwks_cache["team_domain"] = _TEAM_DOMAIN
    return client


class VerifierUnavailable(Exception):
    """The JWKS endpoint could not be consulted (network failure, malformed
    key set): the assertion was neither confirmed nor refuted. Raised only to
    callers that ask (``raise_unavailable``) — a live socket's periodic
    re-check, which must ride out a blip instead of treating it as a
    revocation."""


def verify_jwt(assertion: str, *, raise_unavailable: bool = False) -> Optional[dict]:
    """Validate a Cf-Access-Jwt-Assertion. Returns the claims dict or None.

    Two-pass JWKS fetch handles the "key rotated since the last fetch"
    case: the first pass uses whatever keys are cached, and on a
    PyJWKClientError (kid not found) a refresh is forced once. Subsequent
    failures fall through to None — the verification path does not loop
    indefinitely on a malformed token.
    """
    if not is_enabled() or not assertion:
        return None
    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": _issuer(),
        "options": {"require": ["exp", "iat", "iss"]},
    }
    if _AUDIENCE:
        decode_kwargs["audience"] = _AUDIENCE
    else:
        # Without an AUD pin, any Access application in the same team
        # could mint a token the app would accept. Refuse to verify rather than
        # silently weakening the policy — operators must set CF_ACCESS_AUD.
        # Log only once per process so a misconfigured deployment doesn't
        # write a warning line on every JWT-bearing request.
        global _aud_warning_emitted
        if not _aud_warning_emitted:
            logger.warning(
                "CF_ACCESS_AUD is unset; refusing to verify Cf-Access-Jwt-Assertion. "
                "Set CF_ACCESS_AUD to the Application AUD tag from the Cloudflare "
                "Zero Trust dashboard. (Suppressing further warnings until restart.)"
            )
            _aud_warning_emitted = True
        return None
    for force_refresh in (False, True):
        try:
            client = _jwks_client(force_refresh=force_refresh)
            signing_key = client.get_signing_key_from_jwt(assertion)
            return jwt.decode(assertion, signing_key.key, **decode_kwargs)
        except jwt.PyJWKClientConnectionError:
            # The key set could not be fetched at all (PyJWT reports a
            # network failure as this PyJWKClientError subclass): retry once
            # with a fresh client, then it is an outage, not a bad token.
            if not force_refresh:
                continue
            logger.warning("CF Access JWKS endpoint unreachable", exc_info=True)
            if raise_unavailable:
                raise VerifierUnavailable() from None
            return None
        except jwt.PyJWKClientError:
            if force_refresh:
                return None
            continue
        except jwt.InvalidTokenError:
            return None
        except Exception:
            # Network failure to the JWKS endpoint, malformed JWKS body,
            # etc. Fail closed: the caller treats this as "verification
            # failed" and either rejects or records an anomaly.
            logger.warning("CF Access JWT verification raised", exc_info=True)
            if raise_unavailable:
                raise VerifierUnavailable() from None
            return None
    return None


def _is_trusted_hop(client_host: str) -> bool:
    """Return True if `client_host` is in TRUSTED_PROXIES.

    Resolved through the module attribute (rather than a top-level import
    of the frozenset) so tests that monkeypatch ``network._TRUSTED_PROXIES``
    take effect here without a separate patch on this module. The set
    itself is read from the ``TRUSTED_PROXIES`` env var at network-module
    import time, so a runtime env change still requires a service restart.
    """
    from . import network as _net
    return client_host in _net._TRUSTED_PROXIES


def cf_jwt_status(
    headers, client_host: str, *, raise_unavailable: bool = False
) -> Tuple[str, Optional[dict]]:
    """Classify the CF Access JWT on a request.

    Returns (status, claims):

    * ``("absent", None)`` — no JWT header at all. Normal for the
      bypass-IP path that skips Cloudflare entirely; callers should
      fall through to the app's own auth.
    * ``("valid", claims)`` — JWT verified. ``claims`` includes
      ``email`` / ``sub`` / ``identity_nonce`` etc.
    * ``("invalid", None)`` — JWT present but failed verification, or
      arrived from an untrusted hop, or CF integration is disabled
      while a JWT was nonetheless asserted. The access-control layer
      should reject; the analytics layer should record anonymous +
      anomaly.

    With ``raise_unavailable``, a JWKS fetch failure raises
    ``VerifierUnavailable`` instead of reading as ``"invalid"``.
    """
    assertion = headers.get(CF_JWT_HEADER) or ""
    if not assertion:
        return ("absent", None)
    if not is_enabled():
        # An assertion arrived but the app isn't configured to consume it.
        # Treating this as invalid (rather than absent) keeps a misconfigured
        # deployment from silently letting CF-issued tokens flow through
        # unverified; the operator notices via the access-denied response.
        return ("invalid", None)
    if not _is_trusted_hop(client_host):
        # The peer that handed the app this request is not cloudflared
        # (127.0.0.1). Anyone else asserting a CF JWT is bypassing the
        # tunnel — refuse to consume it.
        return ("invalid", None)
    claims = verify_jwt(assertion, raise_unavailable=raise_unavailable)
    if claims is None:
        return ("invalid", None)
    return ("valid", claims)


def is_via_cloudflare_tunnel(headers, client_host: str) -> bool:
    """Return True iff this request reached the app through cloudflared.

    nginx's geo block sets `$wsi_cf_connecting_ip` to the upstream
    `Cf-Connecting-IP` value only when the immediate peer is loopback
    (cloudflared on the same host); on every other peer it is scrubbed
    to "". So the presence of a non-empty `Cf-Connecting-IP` header
    inside the app is a faithful signal of "request arrived via
    Cloudflare Tunnel". Independent of whether CF Access SSO is
    configured — bare CF Tunnel + Bypass policy still sets this header.

    The trust check is duplicated here so the helper stays correct
    even if a future operator wires a different reverse-proxy chain
    that forwards `Cf-Connecting-IP` without the geo scrubbing.
    """
    if not _is_trusted_hop(client_host):
        return False
    return bool((headers.get(CF_CONNECTING_IP_HEADER) or "").strip())


def email_from_claims(claims: dict) -> str:
    """Return the user's email from the *verified* JWT claims.

    Only the ``email`` claim of the cryptographically-verified assertion is
    trusted. The separate ``Cf-Access-Authenticated-User-Email`` HTTP header
    is NOT covered by the JWT signature — Cloudflare sets it alongside the
    assertion, but nothing binds the two, so a caller on the trusted hop
    could attach an arbitrary value. Consuming it would let such a caller
    spoof the identity recorded by the analytics beacon or carried onto the
    LAN-direct host (``app_auth.maybe_carry_cf_session``). Identity-based
    Access policies always populate the ``email`` claim; service-token
    policies omit it, and for those the correct result is "no human
    identity" (""), recorded anonymous — not a forgeable header value.

    Returns the claim email (truncated to 255 chars) or "" when absent.
    """
    return (claims.get("email") or "").strip()[:255]
