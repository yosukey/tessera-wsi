"""Two-factor (TOTP) sign-in for staff accounts.

* ``POST /api/admin/login/mfa`` — the code step of the dashboard sign-in, for
  an account whose password step (``/api/admin/login``) answered
  ``mfa_required``. Takes a 6-digit code or a one-time recovery code.
* ``/api/admin/me/mfa/*`` — the owner of an account turns it on (setup +
  confirm), off, or replaces the recovery codes. Any role; off by default.
* ``POST /api/admin/users/{id}/mfa/reset`` — an admin turns it off for
  another member who lost their device. Not for themselves: their own goes
  through ``disable``, which wants a code. (That is a guard against the
  shortcut, not a boundary: a stolen admin token can create another admin
  and reset from there — an admin token is full control of the install.)

Every change of an account's second factor (on, off, reset) bumps its
``token_version``, ending the account's other sessions; the endpoints the
owner calls hand back a fresh token for the session that made the change.
Secrets and recovery codes are handled in ``mfa.py``.
"""
from __future__ import annotations

import hmac
import logging
import time
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import login_throttle, mfa
from ..admin_helpers import api_error, audit, coerce_aware, utcnow
from ..auth import (
    MFA_PENDING_AUDIENCE,
    MFA_SETUP_AUDIENCE,
    _token_version_matches,
    create_mfa_token,
    create_token,
    decode_mfa_token,
    staff_token_amr,
    get_current_user,
    require_admin,
)
from ..config import settings
from ..db import get_db
from ..models import User
from ..network import get_client_ip
from ..schemas import MfaConfirmIn, MfaLoginIn, MfaReauthIn, MfaResetIn, MfaSetupIn
from .admin_auth import (
    account_locked,
    clear_exempt_failures,
    lockout_exemption,
    record_login_failure,
    refund_login_attempt,
    refuse_if_ip_throttled,
    refuse_if_throttled,
    remember_device,
    reserve_login_attempt,
    verify_reauth_password,
)
from .admin_users import _user_payload

router = APIRouter()
_logger = logging.getLogger("wsi_app.admin_mfa")

# Every column of the second factor, as "off".
_MFA_CLEARED = {
    "totp_secret_enc": None,
    "totp_enabled_at": None,
    "totp_last_step": None,
    "mfa_recovery_codes": None,
}


# When each account's unreadable-secret ERROR was last logged (per worker):
# while the key is missing or wrong, every attempt would log one, and a
# password holder can repeat the code step (it charges nothing then) to push
# real errors out of the capped error log.
_KEY_ERROR_LOGGED: dict = {}
_KEY_ERROR_EVERY_SECONDS = 600


def _log_key_error(user_id: int, message: str) -> None:
    # Per account and message: a "not configured" line must not hide the
    # different "does not open" diagnosis once a (wrong) key is put in.
    now = time.monotonic()
    key = (user_id, message)
    if now - _KEY_ERROR_LOGGED.get(key, -_KEY_ERROR_EVERY_SECONDS) >= _KEY_ERROR_EVERY_SECONDS:
        _KEY_ERROR_LOGGED[key] = now
        if len(_KEY_ERROR_LOGGED) > 10_000:
            _KEY_ERROR_LOGGED.clear()
        _logger.error(message, user_id)


def _secret_for(user: User) -> str:
    """The account's TOTP secret; 503 when this install cannot open it. Asked
    before the account's budget is charged, for a recovery code too: a secret
    that does not open almost always means a WSI_MFA_KEY other than the one
    it was sealed under, and the recovery-code digests are keyed by that same
    key, so trying them would only charge the budget for codes that cannot
    match — and a missing or wrong key is the operator's to fix, and must not
    lock accounts out while they do."""
    try:
        return mfa.open_secret(user.totp_secret_enc, user.id)
    except mfa.MfaKeyUnavailable:
        _log_key_error(user.id, "account %s has two-factor sign-in on, but WSI_MFA_KEY is not configured")
    except mfa.MfaSecretUnreadable:
        _log_key_error(
            user.id,
            "the two-factor secret of account %s does not open under WSI_MFA_KEY "
            "(sealed under a different key?)",
        )
    raise api_error(503, "adminMfa.err.keyUnavailable")


def _state_pins(state: tuple) -> list:
    """UPDATE predicates that hold only while the two-factor state is still
    ``state`` (see mfa.two_factor_state)."""
    sealed, last_step, stored = state
    return [
        User.totp_secret_enc == sealed,
        User.totp_last_step.is_(None) if last_step is None else User.totp_last_step == last_step,
        User.mfa_recovery_codes.is_(None) if stored is None else User.mfa_recovery_codes == stored,
    ]


def _match_code(code: str, secret: str, state: tuple):
    """Check a TOTP or recovery code against ``state`` (mfa.two_factor_state).

    Returns ``(method, where, values)``: the predicates and new column values
    of the conditional UPDATE that spends the code (a TOTP step is recorded so
    it cannot be replayed; a recovery code is removed), or None when the code
    is wrong. The predicates pin ``state``, so the UPDATE only lands while it
    is unchanged and two concurrent requests cannot both spend one code.
    """
    _, last_step, stored = state
    where = _state_pins(state)
    if mfa.looks_like_recovery_code(code):
        remaining = mfa.consume_recovery_code(stored, code)
        if remaining is None:
            return None
        return "recovery", where, {"mfa_recovery_codes": remaining}
    step = mfa.match_totp(secret, code, last_step=last_step)
    if step is None:
        return None
    return "totp", where, {"totp_last_step": step}


def _account(user: User) -> SimpleNamespace:
    """The account's identity as read now, for audit rows and tokens after a
    commit (see mfa.two_factor_state)."""
    return SimpleNamespace(id=user.id, username=user.username, role=user.role)


def _minted(account, token_version: int) -> SimpleNamespace:
    """What a token handed back after a commit is minted from: the
    token_version this request's own UPDATE committed, never the row read back
    after it. A sign-out-everywhere or suspension committing in between would
    otherwise be read back, and the token would survive it."""
    return SimpleNamespace(
        id=account.id, username=account.username, role=account.role, token_version=token_version,
    )


def _charge_attempt(db: Session, user_id: int, on_refused) -> bool:
    """Reserve a slot in the account's sign-in budget for a code check (the
    database-backed lockout of admin_auth.reserve_login_attempt, exact across
    workers). While the account is locked, ``on_refused()`` records the
    attempt and the request gets 429. Returns ``tripped``."""
    now = utcnow()
    reserved, tripped, locked_until = reserve_login_attempt(db, user_id, now)
    if not reserved:
        on_refused()
        raise account_locked(locked_until, now)
    return tripped


@router.post("/api/admin/login/mfa")
def admin_login_mfa(body: MfaLoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = get_client_ip(
        request.headers, request.client.host if request.client else ""
    )
    throttle_ip = login_throttle.ip_bucket(client_ip)
    refuse_if_ip_throttled(throttle_ip)

    claims = decode_mfa_token(body.mfa_token, MFA_PENDING_AUDIENCE)
    user = db.get(User, claims["sub"]) if claims else None
    # The password step's proof is void once the account changed: a password
    # change, suspension or MFA reset bumps token_version. And it is good for
    # one redemption only: a successful one spends a TOTP step or a recovery
    # code, which moves the fingerprint stamped on the token (so a captured
    # pending token plus a later code buys nothing without the password).
    if (
        user is None
        or not user.is_active
        or user.totp_secret_enc is None
        or not _token_version_matches(claims, user)
        or not isinstance(claims.get("mf"), str)
        or not hmac.compare_digest(claims["mf"], mfa.state_fingerprint(user))
    ):
        raise api_error(401, "adminMfa.err.sessionExpired")
    # The state the fingerprint vouched for: the code is checked against it,
    # and the UPDATE that spends the code lands only while it is still this.
    state = mfa.two_factor_state(user)
    account = _account(user)
    secret = _secret_for(user)

    # The code draws on the same per-account budget as the password step,
    # whose earlier failures a correct password did not clear (see
    # admin_auth.admin_login).
    def refused():
        record_login_failure(throttle_ip, account.username)
        audit(None, "admin.login.mfa_failure", target=account.username, locked_out=True, ip=client_ip)

    # An attempt outside the lockout (a known browser of this account, or the
    # server itself: admin_auth.lockout_exemption) is neither refused by it nor
    # charged to it, so a lockout someone else set cannot keep the owner from
    # finishing a sign-in whose password was right. The per-address throttle
    # above and below still applies.
    verified_tv = int(claims["tv"])
    exemption = lockout_exemption(request, client_ip, account.id, verified_tv)
    was_locked = bool(
        exemption
        and coerce_aware(user.lockout_until)
        and coerce_aware(user.lockout_until) > utcnow()
    )
    tripped = False if exemption else _charge_attempt(db, account.id, refused)

    match = _match_code(body.code, secret, state)
    method = None
    if match is not None:
        method, where, values = match
        res = db.execute(
            update(User)
            .where(
                User.id == account.id,
                User.token_version == verified_tv,
                User.is_active.is_(True),
                *where,
            )
            .values(failed_login_count=0, lockout_until=None, **values)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if res.rowcount != 1:
            # The code was right, but the state it was checked against moved
            # on meanwhile: the same sign-in submitted twice, or the account
            # changed (suspended, password changed). Not a wrong guess — hand
            # the slot back and count nothing against the address — but on
            # the record, and this sign-in goes back to its start.
            if not exemption:
                refund_login_attempt(db, account.id, tripped)
            audit(None, "admin.login.mfa_failure", target=account.username, stale=True, ip=client_ip)
            raise api_error(401, "adminMfa.err.sessionExpired")
    if method is None:
        record_login_failure(throttle_ip, account.username)
        audit(None, "admin.login.mfa_failure", target=account.username, locked=tripped, ip=client_ip)
        raise api_error(401, "adminMfa.err.invalidCode")

    login_throttle.clear_user(throttle_ip, account.username)
    clear_exempt_failures(account.id)
    out = {"access_token": create_token(SimpleNamespace(
        id=account.id,
        username=account.username,
        role=account.role,
        token_version=verified_tv,
    ), amr="recovery" if method == "recovery" and values["mfa_recovery_codes"] == "[]" else None)}
    extra = {}
    if method == "recovery":
        extra["recovery_codes_remaining"] = mfa.recovery_codes_remaining(values["mfa_recovery_codes"])
    audit(account, "admin.login.success", ip=client_ip, mfa=method, **extra,
          **({"lockout_bypass": exemption} if was_locked else {}))
    remember_device(response, request, account.id, verified_tv)
    return {**out, **extra}


# ---------------------------------------------------------------------------
# The account owner: turn on / off, replace recovery codes
# ---------------------------------------------------------------------------
def _reauth_with_code(request: Request, db: Session, user: User, body: MfaReauthIn, **audit_extra):
    """Password and a current code (TOTP or recovery), for turning the second
    factor off or replacing the recovery codes. Returns ``(match, account)``,
    the account as read before anything committed (see mfa.two_factor_state).

    A wrong password counts in the request token's re-auth bucket
    (verify_reauth_password). A code, once the password is right, is charged
    against the account's sign-in budget like the code step of a sign-in —
    the database-backed lockout, exact across workers and token rotations —
    so this panel is no softer door for guessing codes than the sign-in is.
    The caller's UPDATE releases the slot when it spends the code (and hands
    it back when that UPDATE does not land). The cost: while the account is
    locked — which wrong passwords at the sign-in can do — this panel waits
    out the lockout too."""
    if not verify_reauth_password(request, user, body.current_password, **audit_extra):
        raise api_error(400, "adminMfa.err.wrongPassword")
    state = mfa.two_factor_state(user)
    account = _account(user)
    if not mfa.normalize_code(body.code):
        if (
            mfa.recovery_codes_remaining(state[2]) == 0
            and staff_token_amr(request.headers.get("authorization", "")) == "recovery"
        ):
            # Signed in with the last recovery code (only that sign-in's
            # token carries amr="recovery"): there is no code left to give,
            # so this session may replace the codes (or turn two-factor off)
            # on the password alone — otherwise following the sign-in's
            # "generate new codes" advice would be impossible, and only
            # another admin or the server could help. The predicates pin the
            # state, so it holds only while no code is left; an active
            # lockout is waited out like any code check, and the counters
            # are left as they are (account.tripped None: nothing charged).
            if coerce_aware(user.lockout_until) and coerce_aware(user.lockout_until) > utcnow():
                raise account_locked(coerce_aware(user.lockout_until), utcnow())
            account.tripped = None
            return ("recovery_session", _state_pins(state), {}), account
        # Nothing to check: refused before the budget is charged.
        raise api_error(400, "adminMfa.err.codeRequired")
    secret = _secret_for(user)

    def refused():
        audit(account, "admin.mfa.code_failure", target=str(account.id), locked_out=True, **audit_extra)

    tripped = _charge_attempt(db, account.id, refused)
    match = _match_code(body.code, secret, state)
    if match is None:
        audit(account, "admin.mfa.code_failure", target=str(account.id), locked=tripped, **audit_extra)
        raise api_error(400, "adminMfa.err.invalidCode")
    account.tripped = tripped
    return match, account


def _released(account) -> dict:
    """Counter columns for an UPDATE that spends a panel code: the charged
    slot (and any lockout it set) released. Nothing when nothing was charged
    (the recovery-session shortcut)."""
    return {} if account.tripped is None else {"failed_login_count": 0, "lockout_until": None}


def _account_changed() -> HTTPException:
    return api_error(409, "adminMfa.err.accountChanged")


@router.post("/api/admin/me/mfa/setup")
def admin_mfa_setup(
    body: MfaSetupIn,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Start enrolment: a new secret and its QR-code URI. Nothing is stored
    on the account until /confirm proves the authenticator app has it."""
    if not mfa.is_configured():
        raise api_error(503, "adminMfa.err.notConfigured")
    if user.totp_secret_enc is not None:
        raise api_error(409, "adminMfa.err.alreadyEnabled")
    if not verify_reauth_password(request, user, body.current_password, purpose="mfa.setup"):
        raise api_error(400, "adminMfa.err.wrongPassword")
    secret = mfa.new_secret()
    ttl = settings.mfa_setup_ttl_seconds
    return {
        "secret": secret,
        "otpauth_uri": mfa.otpauth_uri(secret, user.username, settings.mfa_issuer),
        "issuer": settings.mfa_issuer,
        "account": user.username,
        "setup_token": create_mfa_token(
            MFA_SETUP_AUDIENCE, user.id, user.token_version, ttl,
            sec=mfa.seal_secret(secret, user.id),
        ),
        "expires_in": ttl,
    }


@router.post("/api/admin/me/mfa/confirm")
def admin_mfa_confirm(
    body: MfaConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Finish enrolment with the first code from the app: turns the second
    factor on and returns the recovery codes (shown this once only)."""
    claims = decode_mfa_token(body.setup_token, MFA_SETUP_AUDIENCE)
    if claims is None or claims["sub"] != user.id or not _token_version_matches(claims, user):
        raise api_error(400, "adminMfa.err.setupExpired")
    if user.totp_secret_enc is not None:
        raise api_error(409, "adminMfa.err.alreadyEnabled")
    sealed = claims.get("sec")
    try:
        secret = mfa.open_secret(sealed if isinstance(sealed, str) else "", user.id)
    except mfa.MfaKeyUnavailable:
        raise api_error(503, "adminMfa.err.notConfigured")
    except mfa.MfaSecretUnreadable:
        raise api_error(400, "adminMfa.err.setupExpired")
    # Whoever calls this normally holds the secret (it is in the QR code),
    # but the request proves only the setup token, which carries it sealed:
    # bound the guesses all the same. In memory and apart from the sign-in
    # budget, so mistyped first codes never lock the account out of sign-in.
    setup_key = f"mfa-setup:{user.id}"
    refuse_if_throttled(
        setup_key,
        max_failures=settings.reauth_ip_max_failures,
        window_seconds=settings.reauth_ip_window_seconds,
    )
    step = mfa.match_totp(secret, body.code, last_step=None)
    if step is None:
        login_throttle.record_failure(
            setup_key,
            window_seconds=settings.reauth_ip_window_seconds,
            max_failures=settings.reauth_ip_max_failures,
        )
        raise api_error(400, "adminMfa.err.invalidCode")

    codes, stored = mfa.new_recovery_codes()
    read_tv = int(user.token_version or 0)
    res = db.execute(
        update(User)
        .where(User.id == user.id, User.token_version == read_tv, User.totp_secret_enc.is_(None))
        .values(
            totp_secret_enc=sealed,
            totp_enabled_at=utcnow(),
            totp_last_step=step,
            mfa_recovery_codes=stored,
            # End the account's other sessions: they were opened without
            # the factor the account now requires.
            token_version=read_tv + 1,
        )
        .execution_options(synchronize_session="fetch")
    )
    if res.rowcount != 1:
        db.rollback()
        raise _account_changed()
    minted = _minted(user, read_tv + 1)
    db.commit()
    login_throttle.clear_ip(setup_key)
    db.refresh(user)
    audit(user, "admin.mfa.enable", target=str(user.id))
    return {
        "recovery_codes": codes,
        "access_token": create_token(minted),
        "mfa": mfa.status_payload(user),
    }


@router.post("/api/admin/me/mfa/disable")
def admin_mfa_disable(
    body: MfaReauthIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.totp_secret_enc is None:
        raise api_error(409, "adminMfa.err.notEnabled")
    read_tv = int(user.token_version or 0)
    (method, where, _values), account = _reauth_with_code(request, db, user, body, purpose="mfa.disable")
    res = db.execute(
        update(User)
        .where(User.id == account.id, User.token_version == read_tv, User.is_active.is_(True), *where)
        .values(token_version=read_tv + 1, **_released(account), **_MFA_CLEARED)
        .execution_options(synchronize_session="fetch")
    )
    if res.rowcount != 1:
        db.rollback()
        if account.tripped is not None:
            refund_login_attempt(db, account.id, account.tripped)
        raise _account_changed()
    db.commit()
    db.refresh(user)
    audit(user, "admin.mfa.disable", target=str(user.id), method=method)
    return {"access_token": create_token(_minted(account, read_tv + 1)), "mfa": mfa.status_payload(user)}


@router.post("/api/admin/me/mfa/recovery-codes")
def admin_mfa_regenerate_recovery_codes(
    body: MfaReauthIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace every recovery code with a fresh set (the old ones stop
    working), e.g. once several have been used — or one may have leaked, so
    the account's other sessions end too (any opened with an old code); this
    one gets a fresh token."""
    if user.totp_secret_enc is None:
        raise api_error(409, "adminMfa.err.notEnabled")
    read_tv = int(user.token_version or 0)
    (method, where, values), account = _reauth_with_code(
        request, db, user, body, purpose="mfa.recovery_codes",
    )
    codes, stored = mfa.new_recovery_codes()
    res = db.execute(
        update(User)
        # The session that asked must still be good: codes are not handed to
        # a token revoked (password change, suspension) while this ran.
        .where(User.id == account.id, User.token_version == read_tv, User.is_active.is_(True), *where)
        .values(**{
            **values, "mfa_recovery_codes": stored, "token_version": read_tv + 1, **_released(account),
        })
        .execution_options(synchronize_session="fetch")
    )
    if res.rowcount != 1:
        db.rollback()
        if account.tripped is not None:
            refund_login_attempt(db, account.id, account.tripped)
        raise _account_changed()
    db.commit()
    db.refresh(user)
    audit(user, "admin.mfa.recovery_codes_regenerate", target=str(user.id), method=method)
    return {
        "recovery_codes": codes,
        "access_token": create_token(_minted(account, read_tv + 1)),
        "mfa": mfa.status_payload(user),
    }


# ---------------------------------------------------------------------------
# An admin, for another member
# ---------------------------------------------------------------------------
@router.post("/api/admin/users/{user_id}/mfa/reset")
def admin_reset_user_mfa(
    user_id: int,
    body: MfaResetIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Turn another member's second factor off (lost device, no recovery
    code). They sign in with the password alone afterwards and may enrol
    again; their open sessions end, and a lockout their attempts at old codes
    left behind is lifted with it (the admin vouches for them)."""
    if user_id == admin.id:
        raise api_error(400, "adminMfa.err.cannotResetSelf")
    target: Optional[User] = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not verify_reauth_password(
        request, admin, body.current_password, purpose="mfa.reset", target_user=str(user_id),
    ):
        raise api_error(400, "adminMfa.err.wrongPassword")
    if target.totp_secret_enc is None:
        return _user_payload(target)
    target_username, target_role = target.username, target.role
    res = db.execute(
        update(User)
        .where(User.id == user_id)
        # Incremented in SQL, not from the value read: a password change
        # committing in between would otherwise get its generation reused.
        .values(
            token_version=User.token_version + 1,
            failed_login_count=0,
            lockout_until=None,
            **_MFA_CLEARED,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if res.rowcount != 1:
        # Deleted by another admin meanwhile.
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        admin,
        "admin.mfa.reset",
        target=str(user_id),
        target_username=target_username,
        role=target_role,
    )
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _user_payload(target)
