"""HMAC-based pseudonymisation of analytics user identities.

The hash is computed at write time so the database stores
``hmac_sha256(salt, lower(email))`` instead of a verbatim email when the
operator selects ``analytics_mode="hashed"``. The salt lives in
``ANALYTICS_HASH_SALT`` (in ``/etc/wsi/wsi.env``) and never leaks to the
admin UI: there is no GET endpoint that returns it, no admin form to
edit it, and the public ``/api/viewer/settings`` payload only carries a
boolean availability flag. Because the salt is unknown to anyone with
DB-only access, a leak of ``page_views`` / ``slide_dwell_events`` /
``tour_sessions`` cannot be reversed with a rainbow table of the
organisation's known emails.

Salt rotation is a deliberate operator decision rather than a built-in
flow: a new salt would break correlation across the rotation boundary,
and choosing "when" to break that correlation belongs to the operator,
not to the code. To rotate, edit ``ANALYTICS_HASH_SALT`` and restart;
the existing rows keep their old hashes (a hash collision across salts
is cryptographically negligible).

The dev escape hatch (``WSI_ALLOW_INSECURE_DEFAULTS=1``) mints an
ephemeral per-process salt so local tests in ``hashed`` mode don't need
the operator to edit ``/etc/wsi/wsi.env`` — every restart invalidates
existing hashes, which is fine for development but exactly the wrong
behaviour for production.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from typing import Optional

from .config import env_bool

logger = logging.getLogger("wsi_app.analytics_hash")

_SALT_ENV = "ANALYTICS_HASH_SALT"
_ALLOW_INSECURE_ENV = "WSI_ALLOW_INSECURE_DEFAULTS"

# A salt shorter than this is a guessable secret, not a key: with it an attacker
# holding a DB leak could brute-force the (small) email space. install.sh
# generates 64 hex chars (`openssl rand -hex 32`).
MIN_SALT_LEN = 32


def _validated_salt(raw: Optional[str]) -> Optional[str]:
    """The configured salt, or None when unset or too short to be safe.

    A too-short salt is treated exactly like a missing one (hashed mode is not
    selectable, the dev ephemeral fallback applies) and logged loudly, rather
    than silently pseudonymising with a weak key.
    """
    salt = (raw or "").strip()
    if not salt:
        return None
    if len(salt) < MIN_SALT_LEN:
        logger.warning(
            "%s is only %d characters; at least %d are required, so it is "
            "ignored and treated as unset. Generate one with: openssl rand -hex 32",
            _SALT_ENV, len(salt), MIN_SALT_LEN,
        )
        return None
    return salt


_configured_salt: Optional[str] = _validated_salt(os.getenv(_SALT_ENV))
_ephemeral_salt: Optional[str] = None

if _configured_salt is None and env_bool(_ALLOW_INSECURE_ENV, False):
    # Dev-only path: stable for the lifetime of the process so the admin UI
    # can switch to "hashed" without forcing the operator to edit wsi.env.
    # NEVER triggers in production because the systemd unit does not pass
    # WSI_ALLOW_INSECURE_DEFAULTS.
    _ephemeral_salt = secrets.token_hex(32)
    logger.warning(
        "%s unset; %s=1 — generated ephemeral analytics salt for this "
        "process (hashes will not survive restart)",
        _SALT_ENV, _ALLOW_INSECURE_ENV,
    )


def salt_available() -> bool:
    """Is a usable salt configured (either persistent or ephemeral)?

    The admin UI calls this (indirectly, via the public settings endpoint)
    to decide whether the "Pseudonymous" radio is selectable. The PATCH
    handler also enforces it so a misconfigured environment cannot silently
    switch to "hashed" and crash on the first beacon.
    """
    return bool(_configured_salt or _ephemeral_salt)


def _salt() -> str:
    if _configured_salt:
        return _configured_salt
    if _ephemeral_salt:
        return _ephemeral_salt
    raise RuntimeError(
        f"{_SALT_ENV} is unset; pseudonymous analytics mode requires a "
        "salt. Set ANALYTICS_HASH_SALT in /etc/wsi/wsi.env (generate one "
        "with: openssl rand -hex 32), or set "
        "WSI_ALLOW_INSECURE_DEFAULTS=1 for an ephemeral dev salt."
    )


def compute_user_hash(email: str) -> str:
    """Return ``hmac_sha256(salt, lower(email))`` as a 64-char hex digest.

    Strips whitespace and lowercases so ``"Foo@Example.com"`` and
    ``" foo@example.com "`` map to the same identifier, matching the
    case-insensitive identity that CF Access / Google OAuth surface.
    Returns the empty string for an empty input so callers that pass an
    empty username through still see "no identifier" rather than a
    "hash of empty string" that looks like a real user.

    A single HMAC-SHA256 (rather than a stretched KDF like PBKDF2) is a
    deliberate choice, not an oversight: this runs on the hottest public
    endpoint — every access beacon — so per-call key-stretching would add
    CPU to every pageview, and changing the construction would break
    correlation continuity for every hash already stored (the same user
    would map to a different digest before/after). Stretching only buys
    resistance to offline brute-force of the *input space*, which matters
    when the secret is weak; here the secret is a high-entropy 256-bit salt
    that never reaches the DB or the admin UI, so reversing a leaked digest
    requires the salt AND the ciphertext to leak together (see module
    docstring). Under that threat model the marginal benefit of stretching
    does not justify the per-beacon latency and the continuity break.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return ""
    return hmac.new(_salt().encode(), normalized.encode(), hashlib.sha256).hexdigest()
