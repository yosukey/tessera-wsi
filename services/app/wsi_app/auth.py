import hashlib, os, secrets, time
from typing import Optional

import jwt
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from passlib.hash import pbkdf2_sha256
from fastapi import HTTPException, Depends, Request
from sqlalchemy.orm import Session
from .db import get_db
from .models import RolePermissions, User
from .config import env_bool, env_int

# ---------------------------------------------------------------------------
# Role constants. The role-based access model recognises three named roles
# in descending privilege order: ``admin`` (full system control), ``manager``
# (slide library curator: upload / replace / delete / metadata), and
# ``instructor`` (educator: viewing + publish toggle, tour CRUD, sync
# presenter). The two derived sets capture the most common gating shapes:
# ``STAFF_ROLES`` matches "any logged-in staff user" (every legitimate role),
# and ``MANAGER_OR_ABOVE`` matches "admin or manager" (the slide-library
# write surface). The instructor-only role is implicit (any STAFF that
# isn't admin/manager).
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_INSTRUCTOR = "instructor"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_MANAGER, ROLE_INSTRUCTOR})
STAFF_ROLES = VALID_ROLES
MANAGER_OR_ABOVE = frozenset({ROLE_ADMIN, ROLE_MANAGER})

# PBKDF2-SHA256 work factor for new password hashes. passlib's built-in default
# (29,000 rounds, about 10 ms) is far below what a leaked database or backup
# archive should have to resist; the current OWASP guidance for PBKDF2-HMAC-
# SHA256 is 600,000. A verify therefore costs a few hundred milliseconds of one
# core, paid per password sign-in (dashboard, or a viewer account in "basic"
# mode) and never per request: a session cookie / bearer token answers every
# later request. ``min_desired_rounds`` makes an older, cheaper hash report
# needs_update(), so a successful sign-in upgrades it in place (see
# rehash_if_outdated), and a hash made with MORE rounds (the setting was
# lowered) is brought down the same way. Lower WSI_PASSWORD_HASH_ROUNDS only on
# hardware where a sign-in storm (a whole class logging in at once) would
# otherwise queue; accounts that have not signed in since keep their old
# (above-setting) hash until they do, and a failed sign-in for such an account
# takes longer than one for an unknown username until then. A hash BELOW the
# setting is padded up to it on failure (verify_password), so raising the
# setting opens no such gap.
PASSWORD_HASH_ROUNDS = env_int("WSI_PASSWORD_HASH_ROUNDS", 600_000)
if PASSWORD_HASH_ROUNDS < 1000:
    raise RuntimeError(
        f"WSI_PASSWORD_HASH_ROUNDS must be at least 1000; got {PASSWORD_HASH_ROUNDS}"
    )

pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
    pbkdf2_sha256__default_rounds=PASSWORD_HASH_ROUNDS,
    pbkdf2_sha256__min_desired_rounds=PASSWORD_HASH_ROUNDS,
)

MIN_PASSWORD_LEN = 8
# Upper bound on any password input accepted. passlib's pbkdf2 first folds
# the password through one HMAC pre-hash, so a multi-MB payload would let a
# single unauthenticated request burn a large share of a core before
# any authentication decision is reached. 1024 chars is well beyond any
# legitimate user passphrase and bounds the attacker-controlled cost.
MAX_PASSWORD_LEN = 1024
# Upper bound on usernames. 255 matches the User.username column width;
# admin_settings.admin_add_auth_user already enforces a tighter 128 for
# AuthUser rows.
MAX_USERNAME_LEN = 255

_jwt_secret_env = os.getenv("JWT_SECRET")
_allow_insecure = env_bool("WSI_ALLOW_INSECURE_DEFAULTS", False)

if not _jwt_secret_env:
    if not _allow_insecure:
        raise RuntimeError(
            "JWT_SECRET is unset. "
            "Set a strong random secret in /etc/wsi/wsi.env before starting "
            "(generate one with: openssl rand -hex 48). "
            "For local development, set WSI_ALLOW_INSECURE_DEFAULTS=1 to opt "
            "into a per-process random secret (tokens will not survive a restart)."
        )
    # Dev escape hatch: mint a fresh random secret for this process. A
    # per-process random secret invalidates every issued token on restart,
    # which is fine for development. The code does NOT fall back to a
    # baked-in literal — a baked-in literal would risk leaking into
    # production via an unedited wsi.env.
    JWT_SECRET = secrets.token_hex(48)
else:
    JWT_SECRET = _jwt_secret_env

# RFC 7518 §3.2: an HS256 key must be at least as long as the hash output
# (256 bits). A shorter secret is not refused — an operator-supplied one in
# wsi.env would then stop the service from booting — but startup logs a
# SECURITY warning for it (see main._warn_short_jwt_secret).
MIN_JWT_SECRET_BYTES = 32


def jwt_secret_is_short() -> bool:
    """True when the configured JWT_SECRET is below MIN_JWT_SECRET_BYTES.

    Always False under WSI_ALLOW_INSECURE_DEFAULTS: that development mode
    opts out of production secret hygiene, and its per-process fallback secret
    is long enough anyway."""
    if _allow_insecure:
        return False
    return len(JWT_SECRET.encode("utf-8")) < MIN_JWT_SECRET_BYTES


JWT_ALG = "HS256"
JWT_TTL_SECONDS = env_int("JWT_TTL_SECONDS", 86400)

# Short-lived signed tokens used to authorize a single archive/WSI download
# from a plain <a href download> click. The frontend cannot send an
# Authorization header on a direct navigation, so the backend mints a
# per-archive token that it accepts via a `?t=` query string.
#
# The TTL must outlast the *whole* transfer, not just the click: these
# downloads are multi-GB (and target TB-scale WSI/backup archives), so a
# stalled connection that the browser resumes with a fresh Range request
# minutes later must still present a valid token or the resume 401s and the
# user is forced to restart from byte zero. One hour is generous enough for a
# slow link to finish (or resume) a large file while still bounding a leaked
# URL's reuse window; each token stays scoped to a single archive id.
# Operator-tunable via WSI_DOWNLOAD_TOKEN_TTL_SECONDS for unusually large
# libraries / slow links.
DOWNLOAD_TOKEN_TTL_SECONDS = env_int("WSI_DOWNLOAD_TOKEN_TTL_SECONDS", 3600)


def validate_password_strength(pw: str) -> None:
    """Enforce the password policy.

    Raises HTTPException(422) when the password is shorter than
    MIN_PASSWORD_LEN or longer than MAX_PASSWORD_LEN. Centralised so
    admin and AuthUser endpoints stay in sync if the policy is tightened
    in the future.
    """
    n = len(pw or "")
    if n < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"password: at least {MIN_PASSWORD_LEN} characters",
        )
    if n > MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"password: at most {MAX_PASSWORD_LEN} characters",
        )


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw or "")


def hash_rounds(pw_hash: str) -> Optional[int]:
    """The PBKDF2 round count recorded in ``pw_hash``; None if it is not one."""
    try:
        return pbkdf2_sha256.from_string(pw_hash).rounds
    except (UnknownHashError, ValueError, TypeError):
        return None


def _burn_rounds(rounds: int) -> None:
    """Spend the time ``rounds`` PBKDF2 iterations take (the cost is linear)."""
    hashlib.pbkdf2_hmac("sha256", b"timing-equalization", b"timing-equalization", rounds)


def verify_password(pw: str, pw_hash: str) -> bool:
    """Whether ``pw`` matches ``pw_hash``.

    A WRONG password for a hash made with fewer rounds than the dummy hash uses
    (an account that has not signed in since the work factor went up) is padded
    by the missing rounds, so the failure takes as long as one for an unknown
    username (dummy_verify_password) or for an already-upgraded account.
    Without that, the response time alone would tell which usernames exist: ~20
    ms against ~200 ms."""
    try:
        ok = pwd_context.verify(pw or "", pw_hash)
    except (UnknownHashError, ValueError):
        return False
    if not ok:
        rounds = hash_rounds(pw_hash)
        if rounds is not None and rounds < _DUMMY_ROUNDS:
            _burn_rounds(_DUMMY_ROUNDS - rounds)
    return ok


def rehash_if_outdated(pw: str, pw_hash: str) -> Optional[str]:
    """A fresh hash of ``pw`` when ``pw_hash`` was made with a different number
    of rounds than the current setting (fewer: an upgrade; more: the operator
    lowered WSI_PASSWORD_HASH_ROUNDS), else None. Call it only after ``pw`` has
    been verified against ``pw_hash``; the caller stores the result with a
    compare-and-swap on ``pw_hash`` so a password change committed meanwhile is
    never undone."""
    rounds = hash_rounds(pw_hash)
    if rounds is None or rounds == PASSWORD_HASH_ROUNDS:
        return None
    return hash_password(pw)


# Pre-computed hash used to defeat user-enumeration timing attacks. When the
# admin login endpoint is given a username that does not exist in the DB, it
# still performs a verify_password() against this dummy hash so the response
# time is statistically indistinguishable from the "wrong password for
# existing user" case. The plaintext is irrelevant — the hash is never going
# to match a real password — but it must use the same algorithm/cost as
# real password hashes for the timing to line up, hence the same pwd_context.
_DUMMY_PASSWORD_HASH = pwd_context.hash("dummy-password-for-timing-equalization")
_DUMMY_ROUNDS = PASSWORD_HASH_ROUNDS


def dummy_verify_password(pw: str) -> None:
    """Run a verify against a throwaway hash so callers can equalize login latency."""
    try:
        pwd_context.verify(pw or "", _DUMMY_PASSWORD_HASH)
    except (UnknownHashError, ValueError):
        pass


def create_token(user: User, *, amr: Optional[str] = None) -> str:
    """A staff session token. ``amr`` names how a two-factor sign-in proved
    its second factor, when it matters later: "recovery" (a recovery code)
    lets that session replace the codes after spending the last one
    (routers/admin_mfa.py)."""
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        # Token-revocation generation; see User.token_version. A freshly
        # constructed (not-yet-flushed) User has token_version=None until the
        # column default fires on INSERT, so coerce to 0.
        "tv": int(getattr(user, "token_version", 0) or 0),
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    if amr:
        payload["amr"] = amr
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def staff_token_amr(authorization_header: str) -> Optional[str]:
    """The ``amr`` claim of a request's (already accepted) staff token."""
    token = bearer_token(authorization_header or "")
    payload = _decode_staff_payload(token) if token else None
    amr = payload.get("amr") if payload else None
    return amr if isinstance(amr, str) else None


# ---------------------------------------------------------------------------
# Two-factor intermediate tokens
# ---------------------------------------------------------------------------
# Both carry an ``aud`` claim, which is exactly what keeps them from ever
# authenticating anything else: the staff decoder below rejects every
# aud-bearing token, the app-access cookie decoder demands aud="app-access",
# and the download-token decoder demands a ``purpose``.
#
# * mfa-pending: handed out by /api/admin/login when the password was right
#   but the account has a second factor; /api/admin/login/mfa trades it plus
#   a code for the real staff token.
# * mfa-setup: handed out with the QR code during enrolment; carries the new
#   secret (sealed with WSI_MFA_KEY) until the first code confirms it, so
#   nothing is written to the account before that.
MFA_PENDING_AUDIENCE = "mfa-pending"
MFA_SETUP_AUDIENCE = "mfa-setup"


def create_mfa_token(audience: str, user_id: int, token_version: int, ttl_seconds: int, **claims) -> str:
    now = int(time.time())
    payload = {
        "aud": audience,
        "sub": str(int(user_id)),
        "tv": int(token_version or 0),
        "iat": now,
        "exp": now + int(ttl_seconds),
        **claims,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_mfa_token(token: str, audience: str) -> Optional[dict]:
    """Verify a token from :func:`create_mfa_token`; None on any failure.

    The caller still checks ``sub`` / ``tv`` against the live row."""
    try:
        payload = jwt.decode(
            token or "",
            JWT_SECRET,
            algorithms=[JWT_ALG],
            audience=audience,
            options={"require": ["exp", "sub", "tv"]},
        )
        payload["sub"] = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None
    return payload


# ---------------------------------------------------------------------------
# Known-device cookie
# ---------------------------------------------------------------------------
# The dashboard locks an ACCOUNT after a few failed sign-ins (admin_auth). That
# stops guessing spread over many addresses, but it lets anyone who knows a
# username keep the real owner out by failing on purpose every 15 minutes. A
# signed-in browser therefore leaves a cookie behind that proves "this browser
# has signed in as this account before": an attempt carrying it is neither
# refused by the lockout nor charged to it (the per-address throttle still
# applies). It is the same kind of token as the two-factor ones above — an
# ``aud`` claim keeps it from ever passing for a staff token — and ``tv`` ties
# it to the account's token_version, so a password change, suspension or
# two-factor change ends the trust along with every session.
KNOWN_DEVICE_AUDIENCE = "known-device"
KNOWN_DEVICE_TTL_SECONDS = env_int("WSI_KNOWN_DEVICE_TTL_SECONDS", 30 * 86400)
# One cookie per account, so two staff members sharing a browser keep both.
KNOWN_DEVICE_COOKIE_PREFIX = "wsi_kd_"
# Scoped to the two sign-in endpoints; nothing else ever sees it.
KNOWN_DEVICE_COOKIE_PATH = "/api/admin/login"


def known_device_cookie_name(user_id: int) -> str:
    return f"{KNOWN_DEVICE_COOKIE_PREFIX}{int(user_id)}"


def create_known_device_token(user_id: int, token_version: int) -> str:
    return create_mfa_token(KNOWN_DEVICE_AUDIENCE, user_id, token_version, KNOWN_DEVICE_TTL_SECONDS)


def is_known_device(token: Optional[str], user_id: int, token_version: int) -> bool:
    """True when ``token`` is a known-device token minted for exactly this
    account at its current token_version."""
    if not token:
        return False
    claims = decode_mfa_token(token, KNOWN_DEVICE_AUDIENCE)
    if not claims or claims["sub"] != int(user_id):
        return False
    claimed = claims.get("tv")
    return (
        isinstance(claimed, int)
        and not isinstance(claimed, bool)
        and claimed == int(token_version or 0)
    )


def _decode_staff_payload(token: str) -> Optional[dict]:
    """Verify a staff JWT's signature/expiry and return its claims dict.

    The token half of :func:`resolve_staff_user`:

    * Verify the HMAC signature with JWT_SECRET / JWT_ALG.
    * Require an ``exp`` claim.
    * Reject tokens that carry an ``aud`` claim — only app-access session
      cookies have one (``aud="app-access"``), and those must never count as
      staff authentication. ``verify_aud=False`` keeps PyJWT from raising on
      the staff tokens (which omit ``aud``); aud-bearing tokens are rejected
      explicitly below.

    Returns ``None`` on any failure.
    """
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],
            options={"verify_aud": False, "require": ["exp"]},
        )
    except jwt.PyJWTError:
        return None
    if "aud" in payload:
        return None
    return payload


def _token_version_matches(payload: dict, user: User) -> bool:
    """True when a token's ``tv`` claim matches the user's live token_version.

    The claim is mandatory and must be a plain integer. Every minting path
    (:func:`create_token`, the download-token minters) stamps it, so a token
    without a well-formed ``tv`` is malformed or forged. It is deliberately
    NOT defaulted to 0: 0 is the *valid* starting generation, so defaulting
    would let a token with the claim stripped out pass for any user who has
    never been revoked. ``bool`` is excluded explicitly because it is an
    ``int`` subclass in Python, and ``True`` would otherwise read as 1.

    The other side is coerced with ``or 0`` for the opposite reason: a freshly
    constructed, not-yet-flushed ``User`` carries ``None`` until the column
    default fires on INSERT.
    """
    claimed = payload.get("tv")
    if not isinstance(claimed, int) or isinstance(claimed, bool):
        return False
    return claimed == int(user.token_version or 0)


def resolve_staff_user(token: str, db: Session) -> Optional[User]:
    """Decode a staff JWT and return the live, non-revoked ``User`` row.

    Centralises the "is this token still good?" decision shared by the
    FastAPI dependency (``get_current_user``), the app-access bypass check
    (``app_auth.staff_token_valid``), and the sync WS presenter gate
    (``sync._resolve_active_presenter``). A token is good only when:

    * its signature/expiry/aud pass (``_decode_staff_payload``),
    * the ``sub`` user still exists and is active, and
    * its ``tv`` claim matches the user's current ``token_version`` — so a
      password change / forced logout that bumped the column revokes the
      token immediately rather than at natural expiry.

    Role-level authorisation (admin vs manager vs instructor) is still the
    caller's job; this only proves identity + liveness + non-revocation.
    Returns ``None`` on any failure.
    """
    payload = _decode_staff_payload(token)
    if payload is None:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    if not _token_version_matches(payload, user):
        return None
    return user


# ---------------------------------------------------------------------------
# Signed download tokens (?t=...)
# ---------------------------------------------------------------------------
# The SPA can't attach an Authorization header to a plain ``<a download>``
# click, so the three download endpoints (slide / backup / export) also accept
# a short-lived capability token on the query string. Query strings leak more
# readily than headers — nginx access logs, browser history, Referer — so the
# token carries the minting user's identity (``uid``) and revocation counter
# (``tv``) and is re-checked against the live row on every use. Without that
# binding a leaked URL would stay good for its whole TTL even after the
# operator disabled the account or reset the password, i.e. there would be no
# way to revoke a link that hands out multi-GB WSI files and whole-install
# backups.

def _create_download_token(purpose: str, user: User, **claims) -> str:
    now = int(time.time())
    payload = {
        "purpose": purpose,
        "iat": now,
        "exp": now + DOWNLOAD_TOKEN_TTL_SECONDS,
        "uid": int(user.id),
        "tv": int(user.token_version or 0),
        **claims,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def _decode_download_token(token: str, purpose: str, db: Session) -> Optional[dict]:
    """Verify signature, expiry, purpose, and the minting user's live state.

    Returns the payload, or None if the token is unusable for any reason.
    Fails closed on a token with no ``uid`` claim: it cannot be checked
    against a live user, so it is treated as malformed.
    """
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],
            options={"verify_aud": False, "require": ["exp"]},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("purpose") != purpose:
        return None
    try:
        uid = int(payload["uid"])
    except (KeyError, TypeError, ValueError):
        return None
    user = db.get(User, uid)
    if not user or not user.is_active:
        return None
    if not _token_version_matches(payload, user):
        return None
    return payload


def create_backup_download_token(backup_id: str, user: User) -> str:
    """Mint a short-lived JWT scoped to a single backup archive download."""
    return _create_download_token("backup_download", user, backup_id=backup_id)


def decode_backup_download_token(token: str, expect_backup_id: str, db: Session) -> bool:
    """Verify a backup-download token. Returns True iff valid for this id."""
    payload = _decode_download_token(token, "backup_download", db)
    return bool(payload) and payload.get("backup_id") == expect_backup_id


def create_export_download_token(export_id: str, user: User) -> str:
    """Mint a short-lived JWT scoped to a single slide-export bundle download."""
    return _create_download_token("slide_export_download", user, export_id=export_id)


def decode_export_download_token(token: str, expect_export_id: str, db: Session) -> bool:
    """Verify a slide-export-download token. Returns True iff valid for this id."""
    return export_download_token_user(token, expect_export_id, db) is not None


def export_download_token_user(token: str, expect_export_id: str, db: Session) -> Optional[User]:
    """The (live, active) user who minted a slide-export-download token valid
    for this export id, or None — so the download can be audited against
    them rather than as an anonymous token use."""
    payload = _decode_download_token(token, "slide_export_download", db)
    if not payload or payload.get("export_id") != expect_export_id:
        return None
    return db.get(User, int(payload["uid"]))


def create_slide_download_token(slide_id: int, user: User) -> str:
    """Mint a short-lived JWT scoped to a single slide WSI download."""
    return _create_download_token("slide_download", user, slide_id=int(slide_id))


def decode_slide_download_token(token: str, expect_slide_id: int, db: Session) -> bool:
    """Verify a slide-download token. Returns True iff valid for this slide."""
    payload = _decode_download_token(token, "slide_download", db)
    return bool(payload) and payload.get("slide_id") == int(expect_slide_id)


def bearer_token(authorization_header: str) -> Optional[str]:
    """Strip a `Bearer ` prefix from an Authorization header. Returns None if absent.

    Header values may carry leading OWS per RFC 7230; Starlette already
    trims most cases, but callers that hand-construct the string (the WS
    upgrade gate, tests, custom proxies) should not silently 401 just
    because of surrounding whitespace.
    """
    header = (authorization_header or "").strip()
    if not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    token = bearer_token(auth)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    user = resolve_staff_user(token, db)
    if user is None:
        # Covers an invalid/expired token, a deleted/disabled user, and a
        # token revoked by a token_version bump — all of which must read as
        # "not authenticated" to the client.
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_manager_or_above(user: User = Depends(get_current_user)) -> User:
    """Gate an endpoint to admins and managers.

    Used by the slide-library write surface (upload / replace / delete /
    metadata edit) where instructors must be locked out.
    """
    if user.role not in MANAGER_OR_ABOVE:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_staff(user: User = Depends(get_current_user)) -> User:
    """Gate an endpoint to any logged-in staff user.

    Matches admin / manager / instructor — every legitimate role. Used by
    read-only slide endpoints, the publish toggle, tour CRUD, and sync
    presenter session creation, all of which instructors must be able
    to drive.
    """
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_analytics_access(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Gate a read-only analytics endpoint.

    Admins always pass. Managers and instructors are admitted only when
    the corresponding ``{role}_can_view_analytics`` flag in the
    ``RolePermissions`` singleton is set; the flag is operator-controlled
    so an admin can extend analytics visibility to a non-admin role
    without bumping the role itself.
    """
    if user.role == ROLE_ADMIN:
        return user
    rp = db.get(RolePermissions, 1)
    if user.role == ROLE_MANAGER and rp and rp.manager_can_view_analytics:
        return user
    if user.role == ROLE_INSTRUCTOR and rp and rp.instructor_can_view_analytics:
        return user
    raise HTTPException(status_code=403, detail="Forbidden")


def require_server_access(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Gate a read-only server-info endpoint.

    Same shape as ``require_analytics_access`` but consults the
    ``{role}_can_view_server`` flags. Admins always pass.
    """
    if user.role == ROLE_ADMIN:
        return user
    rp = db.get(RolePermissions, 1)
    if user.role == ROLE_MANAGER and rp and rp.manager_can_view_server:
        return user
    if user.role == ROLE_INSTRUCTOR and rp and rp.instructor_can_view_server:
        return user
    raise HTTPException(status_code=403, detail="Forbidden")
