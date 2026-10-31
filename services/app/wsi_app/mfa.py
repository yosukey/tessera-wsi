"""Optional TOTP second factor for staff (dashboard) sign-in.

Every staff account may turn on a time-based one-time password (RFC 6238,
the scheme authenticator apps implement: HMAC-SHA1, 6 digits, 30 s steps).
It is off by default for every role and only the account's owner can turn it
on, from the account page; an admin can only turn it *off* for another member
(lost phone), and ``services/app/tools/reset_mfa.py`` does the same from the
server when no admin can sign in.

Storage
-------
* The TOTP secret is sealed with AES-256-GCM under a key derived (HKDF-SHA256)
  from ``WSI_MFA_KEY`` in wsi.env, with the user id as associated data, so a
  copy of the database alone (a backup, a stolen file) does not yield the
  second factor. Without the key no secret can be sealed or opened: enrolment
  is refused and a sign-in of an enrolled account fails closed (503) until the
  operator restores the key or resets that account's MFA.
* Recovery codes are kept as HMAC-SHA256 digests under a second key derived
  from the same ``WSI_MFA_KEY``. A code carries 50 bits; an unkeyed hash of
  that would be brute-forceable offline from a leaked database.

The key is read from the environment on every use (it is cheap), so a test or
an operator fixing wsi.env and restarting needs nothing else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
import unicodedata
from typing import List, Optional, Tuple
from urllib.parse import quote, urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .admin_helpers import coerce_aware

_logger = logging.getLogger("wsi_app.mfa")

MFA_KEY_ENV = "WSI_MFA_KEY"
# Same floor as auth.MIN_JWT_SECRET_BYTES: install.sh writes 64 hex chars.
MIN_MFA_KEY_BYTES = 32

TOTP_PERIOD = 30
TOTP_DIGITS = 6
# Steps accepted either side of the server's current one, for phone clock
# drift and the time it takes to type the code.
TOTP_SKEW_STEPS = 1
# 160 bits, the RFC 4226 recommendation; 32 base32 characters.
SECRET_BYTES = 20

RECOVERY_CODE_COUNT = 10
# No 0/O/1/I/L, so a code copied from paper reads back unambiguously.
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
RECOVERY_CODE_LEN = 10

_SEALED_PREFIX = "v1."
_NONCE_BYTES = 12

# What people type between the halves of a code: spaces, and the hyphen in
# any of the forms an IME or a word processor produces (NFKC leaves the
# katakana prolonged-sound mark, the Unicode hyphens / dashes and the minus
# sign as they are; it folds the full-width hyphen-minus and space).
_CODE_SEPARATORS = frozenset(" \t-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u30fc")

_short_key_warned = False


class MfaKeyUnavailable(Exception):
    """WSI_MFA_KEY is unset or too short: nothing can be sealed or opened."""


class MfaSecretUnreadable(Exception):
    """A sealed secret did not open under the configured key (a different
    WSI_MFA_KEY than the one it was sealed with, or a corrupt value)."""


def _key_material() -> Optional[bytes]:
    global _short_key_warned
    raw = (os.getenv(MFA_KEY_ENV) or "").strip().encode("utf-8")
    if not raw:
        return None
    if len(raw) < MIN_MFA_KEY_BYTES:
        if not _short_key_warned:
            _logger.warning(
                "%s is shorter than %d bytes; two-factor sign-in is unavailable "
                "until it is replaced (generate one with: openssl rand -hex 32)",
                MFA_KEY_ENV, MIN_MFA_KEY_BYTES,
            )
            _short_key_warned = True
        return None
    return raw


def is_configured() -> bool:
    """True when WSI_MFA_KEY is set to a usable value."""
    return _key_material() is not None


def _derive(info: bytes) -> bytes:
    material = _key_material()
    if material is None:
        raise MfaKeyUnavailable()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(material)


def _aad(user_id: int) -> bytes:
    return f"tessera-totp-user:{int(user_id)}".encode("ascii")


def seal_secret(secret_b32: str, user_id: int) -> str:
    """Encrypt a base32 TOTP secret for storage on ``users.totp_secret_enc``."""
    key = _derive(b"tessera-mfa-totp-secret-v1")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, secret_b32.encode("ascii"), _aad(user_id))
    return _SEALED_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii").rstrip("=")


def open_secret(sealed: str, user_id: int) -> str:
    """Decrypt a value made by :func:`seal_secret` for the same user id."""
    key = _derive(b"tessera-mfa-totp-secret-v1")
    if not sealed or not sealed.startswith(_SEALED_PREFIX):
        raise MfaSecretUnreadable()
    body = sealed[len(_SEALED_PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        plain = AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], _aad(user_id))
        return plain.decode("ascii")
    except (InvalidTag, ValueError, UnicodeDecodeError):
        raise MfaSecretUnreadable() from None


def new_secret() -> str:
    """A fresh random TOTP secret, base32 without padding."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _secret_bytes(secret_b32: str) -> bytes:
    s = secret_b32.strip().upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // TOTP_PERIOD)


def totp_at(secret_b32: str, step: int) -> str:
    """The RFC 6238 code for one time step."""
    digest = hmac.new(_secret_bytes(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def normalize_code(code: str) -> str:
    """Strip the spaces / hyphens people type into a code, upper-case it.

    NFKC first: a code typed with a Japanese IME arrives as full-width digits
    and letters ("１２３４５６"), which fold to their ASCII forms here."""
    folded = unicodedata.normalize("NFKC", code or "")
    return "".join(ch for ch in folded if ch not in _CODE_SEPARATORS).upper()


def match_totp(
    secret_b32: str, code: str, *, last_step: Optional[int], now: Optional[float] = None,
) -> Optional[int]:
    """The time step ``code`` is valid for, or None.

    A step at or before ``last_step`` (the last one accepted for this account)
    never matches, so a code observed in use cannot be replayed while it is
    still inside its window. Every candidate is compared, in constant time, so
    the timing does not tell which step was close.
    """
    code = normalize_code(code)
    # isascii too: isdigit() admits other scripts' digits, which
    # hmac.compare_digest refuses to compare (TypeError).
    if len(code) != TOTP_DIGITS or not (code.isascii() and code.isdigit()):
        return None
    here = current_step(now)
    matched: Optional[int] = None
    for step in range(here - TOTP_SKEW_STEPS, here + TOTP_SKEW_STEPS + 1):
        if hmac.compare_digest(totp_at(secret_b32, step), code):
            if last_step is None or step > last_step:
                matched = step
    return matched


def otpauth_uri(secret_b32: str, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI authenticator apps read from the QR code."""
    label = quote(f"{issuer}:{account}", safe="")
    # quote_via=quote: a space is %20 in the query as in the label; some
    # authenticator apps show a "+" literally.
    query = urlencode({
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": TOTP_DIGITS,
        "period": TOTP_PERIOD,
    }, quote_via=quote)
    return f"otpauth://totp/{label}?{query}"


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------
def looks_like_recovery_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == RECOVERY_CODE_LEN and all(ch in _RECOVERY_ALPHABET for ch in c)


def _recovery_key() -> bytes:
    return _derive(b"tessera-mfa-recovery-code-v1")


def _recovery_digest(code: str, key: Optional[bytes] = None) -> str:
    key = key if key is not None else _recovery_key()
    return hmac.new(key, normalize_code(code).encode("ascii"), hashlib.sha256).hexdigest()


def new_recovery_codes() -> Tuple[List[str], str]:
    """Fresh one-time codes (``XXXXX-XXXXX``) and their stored JSON form."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LEN))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    key = _recovery_key()
    return codes, json.dumps([_recovery_digest(c, key) for c in codes])


def _stored_digests(stored: Optional[str]) -> List[str]:
    try:
        value = json.loads(stored or "[]")
    except ValueError:
        return []
    return [d for d in value if isinstance(d, str)] if isinstance(value, list) else []


def recovery_codes_remaining(stored: Optional[str]) -> int:
    return len(_stored_digests(stored))


def consume_recovery_code(stored: Optional[str], code: str) -> Optional[str]:
    """The stored JSON with ``code`` removed, or None when it is not one of
    the unused codes. The caller writes the result back conditionally on
    ``stored`` being unchanged, so a code cannot be spent twice concurrently."""
    if not looks_like_recovery_code(code):
        return None
    digest = _recovery_digest(code)
    digests = _stored_digests(stored)
    hit = None
    for i, d in enumerate(digests):
        if hmac.compare_digest(d, digest):
            hit = i
    if hit is None:
        return None
    return json.dumps(digests[:hit] + digests[hit + 1:])


def status_payload(user) -> dict:
    """The account's two-factor state, for /api/admin/me. ``available`` says
    whether this install can enrol at all (WSI_MFA_KEY configured)."""
    enabled = user.totp_secret_enc is not None
    # SQLite hands the stored UTC value back naive.
    enabled_at = coerce_aware(user.totp_enabled_at)
    return {
        "enabled": enabled,
        "enabled_at": enabled_at.isoformat() if enabled and enabled_at else None,
        "recovery_codes_remaining": recovery_codes_remaining(user.mfa_recovery_codes) if enabled else 0,
        "available": is_configured(),
    }


def two_factor_state(user) -> tuple:
    """``(sealed secret, last TOTP step, stored recovery codes)``: the
    account's spendable two-factor state, the one list of it that the
    pending token's fingerprint and the code-spending UPDATEs both use
    (routers/admin_mfa.py). Read before anything commits: a commit expires
    the ORM row."""
    return user.totp_secret_enc, user.totp_last_step, user.mfa_recovery_codes


def state_fingerprint(user) -> str:
    """A digest of :func:`two_factor_state`. Stamped on the sign-in's pending
    token so the token is good for one redemption: spending a code changes
    it."""
    raw = "|".join(str(v) for v in two_factor_state(user))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
