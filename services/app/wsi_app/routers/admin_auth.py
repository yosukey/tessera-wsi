"""Admin authentication: login, account info, registration."""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import os
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import app_auth, login_throttle, mfa, network
from ..admin_helpers import CTRL_CHAR_RE, audit, coerce_aware, staff_username_taken, utcnow
from ..auth import (
    MAX_USERNAME_LEN,
    KNOWN_DEVICE_COOKIE_PATH,
    KNOWN_DEVICE_TTL_SECONDS,
    MFA_PENDING_AUDIENCE,
    bearer_token,
    staff_token_amr,
    create_mfa_token,
    create_known_device_token,
    create_token,
    dummy_verify_password,
    get_current_user,
    hash_password,
    is_known_device,
    known_device_cookie_name,
    rehash_if_outdated,
    validate_password_strength,
    verify_password,
)
from ..config import settings
from ..db import get_db
from ..models import User
from ..network import get_client_ip
from ..schemas import AdminMeUpdateIn, LoginIn, LoginOut
from .admin_users import role_permissions_payload

router = APIRouter()
_logger = logging.getLogger("wsi_app.admin_auth")

# admin_register is intentionally a one-shot bootstrap path: it is only
# reachable when ALLOW_ADMIN_REGISTER=1 *and* the users table has zero
# admins. Two near-simultaneous calls could both pass the gate-check
# before either committed, ending up with two admin rows.
#
# Two layers of mutual exclusion:
#   1. ``_admin_register_lock`` (threading.Lock) serialises calls within
#      a single uvicorn worker process.
#   2. ``_register_file_lock()`` (fcntl.flock) serialises calls across
#      worker processes — start-wsi-app.sh launches uvicorn with
#      ``--workers N`` so the in-process lock alone is insufficient.
# Both must be held before the gate-check / insert / commit critical
# section so that two distinct admin usernames arriving on different
# workers cannot both pass "no admin exists yet" and create duplicate
# admin rows.
_admin_register_lock = threading.Lock()


def _register_lock_path() -> str:
    return os.path.join(settings.data_dir, ".admin_register.lock")


@contextlib.contextmanager
def _register_file_lock():
    """Cross-process exclusive lock for admin bootstrap.

    Falls open (i.e. yields without blocking) when fcntl is unavailable —
    Ubuntu is the deployment target, so this just defends test/non-Linux
    environments against an import-time crash; the in-process lock still
    applies there.
    """
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        yield
        return
    try:
        os.makedirs(settings.data_dir, exist_ok=True)
    except OSError as exc:
        # If the data dir cannot even be created the code will fail later
        # with a more specific error; just skip the cross-process gate.
        _logger.warning("admin_register: data_dir not writable (%s); cross-process lock skipped", exc)
        yield
        return
    fd = os.open(_register_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _masked_unknown_username(username: str) -> str:
    """The audit target for a login to a username no account has.

    Such a "username" is often a password typed into the wrong field, and the
    history shows it to every admin and exports it to CSV. A name that is an
    account is kept as it is (that is whose account is being guessed at); an
    unknown one is reduced to its first character and its length — enough to
    tell a spray of names apart, not enough to read a mistyped password.
    """
    if not username:
        return ""
    return f"{username[:1]}… ({len(username)} chars)"


# The known-device cookie is a bearer token: whoever copies it (from a shared
# computer the owner once signed in on) holds the exemption from any address.
# So the exemption has a budget of its own, per account: this many attempts made
# under it in one lockout period, after which the ordinary lockout applies
# again (to the owner too, as without the exemption) until the window
# passes or a sign-in under it succeeds. A slot is taken when the attempt
# starts, before its password or code is checked (login_throttle.reserve_slot,
# atomic), as the account's own lockout does: counting only the failures
# afterwards would let parallel guesses all pass the check first. Process-local like
# the per-address throttle, so the ceiling is this times the worker count.
KNOWN_DEVICE_BUDGET_FACTOR = 4

_LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def _known_device_budget_key(user_id: int) -> str:
    return f"known-device:{user_id}"


def clear_exempt_failures(user_id: int) -> None:
    """A sign-in completed: the account's owner is at the keyboard."""
    login_throttle.clear_slots(_known_device_budget_key(user_id))


def lockout_exemption(request: Request, client_ip: str, user_id: int, token_version: int) -> Optional[str]:
    """Why this sign-in attempt is neither refused by, nor charged to, the
    account's lockout; None when the lockout applies as usual.

    The lockout exists to stop password guessing spread over many addresses.
    Its price is that anyone who knows a username can lock that account out
    for good by failing on purpose every 15 minutes, so two kinds of attempt
    stand outside it. Both still verify the password, and both still count
    against the per-address throttle (``refuse_if_ip_throttled`` /
    ``record_login_failure``); only the per-account budget is skipped:

    * ``"known_device"`` - the request carries the cookie a successful
      sign-in of THIS account left in this browser (``remember_device``), signed
      and tied to the account's token_version. Someone guessing from elsewhere
      does not have it; someone who copied it does, so failures made under it
      take slots of a budget of their own (see KNOWN_DEVICE_BUDGET_FACTOR) and,
      once that is spent, the exemption lapses. "Sign out everywhere" ends it outright.
    * ``"loopback"`` - the client is the server itself (an SSH tunnel to
      127.0.0.1, the documented way back in), so an account can always be
      reached by whoever holds the machine. Only when nothing says otherwise:
      a proxy on this host that fronts nginx (Caddy, ``tailscale serve``) makes
      every client look like 127.0.0.1, but it reports the real one in
      ``X-Forwarded-For``, and a hop in that chain outside the loopback range
      means the request did not come from the machine itself.
    """
    try:
        if (
            ipaddress.ip_address((client_ip or "").strip()).is_loopback
            and app_auth._forwarded_chain_within(request.headers, _LOOPBACK_NETWORKS)
        ):
            return "loopback"
    except ValueError:
        pass
    if is_known_device(request.cookies.get(known_device_cookie_name(user_id)), user_id, token_version):
        # Takes a slot of the budget: call it once per attempt, and only for an
        # attempt that goes on to check a credential.
        if login_throttle.reserve_slot(
            _known_device_budget_key(user_id),
            max_slots=max(1, settings.admin_login_max_failures) * KNOWN_DEVICE_BUDGET_FACTOR,
            window_seconds=settings.admin_login_lockout_seconds,
        ):
            return "known_device"
    return None


def remember_device(response: Response, request: Request, user_id: int, token_version: int) -> None:
    """Leave the known-device cookie (see ``lockout_exemption``) in the
    browser that just completed a full sign-in. Host-only, HttpOnly, scoped to
    the sign-in endpoints, and Secure whenever the request came in over https."""
    proto = network.forwarded_proto(request.headers, request.url.scheme)
    response.set_cookie(
        known_device_cookie_name(user_id),
        create_known_device_token(user_id, token_version),
        max_age=KNOWN_DEVICE_TTL_SECONDS,
        httponly=True,
        secure=proto == "https",
        samesite="strict",
        path=KNOWN_DEVICE_COOKIE_PATH,
    )


def reserve_login_attempt(db: Session, user_id: int, now) -> tuple[bool, bool, Optional[object]]:
    """Charge one attempt against an active account's sign-in budget before
    its credential is checked; ``(reserved, tripped, locked_until)``.

    One conditional UPDATE. Checking the lockout first and counting the
    failure only after the (slow) pbkdf2 would let N parallel requests all pass
    the check and all be verified (22 guesses against a limit of 5 in one
    burst). Each attempt is pre-charged as a failure; the slot that
    reaches the limit also sets lockout_until (and zeroes the counter), so
    every concurrent attempt after it matches no row and is refused
    (``reserved`` False, with the lockout's end in ``locked_until``). A
    correct credential clears both. An expired lockout is simply overwritten.
    Shared by the password step and the two-factor step, which draw on the
    same budget.
    """
    max_failures = settings.admin_login_max_failures
    trip = User.failed_login_count + 1 >= max_failures
    res = db.execute(
        update(User)
        .where(
            User.id == user_id,
            or_(User.lockout_until.is_(None), User.lockout_until <= now),
        )
        .values(
            failed_login_count=case((trip, 0), else_=User.failed_login_count + 1),
            lockout_until=case(
                (trip, now + timedelta(seconds=settings.admin_login_lockout_seconds)),
                else_=None,
            ),
        )
        .returning(User.lockout_until)
        .execution_options(synchronize_session=False)
    ).first()
    db.commit()
    if res is not None:
        return True, res[0] is not None, None
    locked_until = coerce_aware(db.execute(
        select(User.lockout_until).where(User.id == user_id)
    ).scalar_one_or_none())
    return False, False, locked_until


def lockout_retry_after(locked_until, now) -> int:
    """Retry-After for an attempt refused by an account lockout. The row can
    also miss because a concurrent success cleared the lockout between the
    UPDATE and the read; that attempt is refused anyway (the caller simply
    retries)."""
    if locked_until and locked_until > now:
        return max(1, int((locked_until - now).total_seconds()))
    return 1


def account_locked(locked_until, now) -> HTTPException:
    """The 429 for an attempt refused by an account lockout."""
    return HTTPException(
        status_code=429,
        detail="Too many failed login attempts. Try again later.",
        headers={"Retry-After": str(lockout_retry_after(locked_until, now))},
    )


def refund_values(tripped: bool) -> dict:
    """Column values that hand back one reserved slot. An attempt whose slot
    set the lockout (``tripped``) lifts it and leaves the count one short of
    the limit; any other goes down by one, floored at zero."""
    if tripped:
        return {
            "failed_login_count": max(settings.admin_login_max_failures - 1, 0),
            "lockout_until": None,
        }
    return {"failed_login_count": case(
        (User.failed_login_count > 0, User.failed_login_count - 1), else_=0,
    )}


def refund_login_attempt(db: Session, user_id: int, tripped: bool = False) -> None:
    """Hand back a reserved slot, for an attempt whose credential was right
    but whose write did not land. A tripped slot is handed back only while
    its lockout still stands: a concurrent success that already cleared
    count and lockout must not get a near-limit count written over it."""
    where = [User.id == user_id]
    if tripped:
        where.append(User.lockout_until.is_not(None))
    db.execute(
        update(User)
        .where(*where)
        .values(**refund_values(tripped))
        .execution_options(synchronize_session=False)
    )
    db.commit()


def refuse_if_throttled(
    key: str, *, max_failures: int, window_seconds: int,
    detail: str = "Too many failed attempts. Try again later.",
) -> None:
    """429 (with Retry-After) while ``key``'s failures fill its window."""
    retry_after = login_throttle.retry_after_seconds(
        key, max_failures=max_failures, window_seconds=window_seconds,
    )
    if retry_after > 0:
        raise HTTPException(
            status_code=429, detail=detail, headers={"Retry-After": str(retry_after)},
        )


def refuse_if_ip_throttled(throttle_ip: str) -> None:
    """429 before any credential work when this client's failed dashboard
    sign-ins (password or two-factor code) fill its window."""
    refuse_if_throttled(
        throttle_ip,
        max_failures=settings.admin_login_ip_max_failures,
        window_seconds=settings.admin_login_ip_window_seconds,
        detail="Too many failed login attempts. Try again later.",
    )


def record_login_failure(throttle_ip: str, username: str) -> None:
    """Count one failed dashboard sign-in step against the client's window."""
    login_throttle.record_failure(
        throttle_ip,
        window_seconds=settings.admin_login_ip_window_seconds,
        max_failures=settings.admin_login_ip_max_failures,
        username=username,
    )


@router.post("/api/admin/login", response_model=LoginOut, response_model_exclude_none=True)
def admin_login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = get_client_ip(
        request.headers, request.client.host if request.client else ""
    )
    # The throttle's key for this client: the address itself, or its /64 for
    # IPv6 (login_throttle.ip_bucket), so rotating through a /64 buys no fresh
    # budget. The audit rows below keep the exact address.
    throttle_ip = login_throttle.ip_bucket(client_ip)

    # Per-IP throttle FIRST: the per-user lockout below only protects a
    # single existing/active username, so an attacker spreading guesses
    # across many usernames (or hammering unknown accounts) is otherwise
    # never slowed and burns one pbkdf2 per request. Reject before any hash
    # work when this IP has too many recent failures. The throttle keys on
    # IP, not username, so the 429 here leaks nothing about which usernames
    # exist.
    refuse_if_ip_throttled(throttle_ip)

    user = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    now = utcnow()

    # Reserve this attempt's slot in the per-user budget BEFORE verifying the
    # password (see reserve_login_attempt). Disabled accounts are not mutated
    # (they can never succeed, and stale state would outlive a re-enable).
    reserved = False
    tripped = False
    locked_until = None
    exemption = None
    was_locked = False
    if user is not None and user.is_active:
        exemption = lockout_exemption(request, client_ip, user.id, int(user.token_version or 0))
        if exemption is None:
            reserved, tripped, locked_until = reserve_login_attempt(db, user.id, now)
        else:
            # Outside the account's lockout (see lockout_exemption): nothing is
            # charged, so nothing is refunded below, and a lockout someone else
            # set does not apply. The password is still verified and the
            # per-address throttle still counts a miss.
            reserved = True
            was_locked = bool(coerce_aware(user.lockout_until) and coerce_aware(user.lockout_until) > now)

    # Reject during active lockout regardless of credential correctness so
    # attackers can't probe whether a candidate password happens to match.
    # The password verifier still runs on the locked branch — without it
    # the lockout response returns in microseconds while every other path
    # spends its time in passlib's pbkdf2, which by itself would let an
    # attacker distinguish "locked existing user" from "unknown user" by
    # response latency. (The status-code difference 429 vs 401 remains a
    # residual leak after lockout is triggered, but that is intrinsic to
    # per-user lockout and the operator-visible UX value of Retry-After
    # outweighs hiding it.)
    if user is not None and user.is_active and not reserved:
        dummy_verify_password(body.password)
        # Record the attempt: a locked-out branch that logs nothing
        # would make the noisiest phase of a brute-force attempt
        # invisible to the admin history (which advertises login events),
        # and to the per-IP throttle that should keep counting a
        # persistent attacker even while one username is locked.
        record_login_failure(throttle_ip, body.username)
        audit(
            None,
            "admin.login.failure",
            target=body.username,
            locked_out=True,
            ip=client_ip,
        )
        raise account_locked(locked_until, now)

    # Always exercise the password hash verifier exactly once, regardless
    # of whether the lookup found a row, so an attacker can't tell "no
    # such user" from "user exists, wrong password" by timing the response.
    # passlib's pbkdf2 takes hundreds of milliseconds, which dwarfs every other
    # branch in this handler, so short-circuiting on `not user` would be
    # easily observable on a LAN.
    if reserved:
        # Snapshot the credential generation BEFORE the slow verify: a password
        # change / revocation committed while pbkdf2 runs must not turn this
        # proof of the OLD password into a token for the NEW generation.
        verified_hash = user.password_hash
        verified_tv = int(user.token_version or 0)
        verified_role = user.role
        verified_id = user.id
        # The second factor this sign-in will owe, from the same snapshot:
        # MFA turned off meanwhile bumps token_version, failing the check below.
        # Its fingerprint goes on the pending token (read now: the commits
        # below expire the row, and the token must vouch for this state).
        verified_mfa = user.totp_secret_enc
        verified_mf = mfa.state_fingerprint(user) if verified_mfa is not None else None
        password_ok = verify_password(body.password, verified_hash)
    else:
        dummy_verify_password(body.password)
        password_ok = False

    if not password_ok:
        # Count this failure against the source IP regardless of whether the
        # username exists, so credential-stuffing across usernames is bounded.
        record_login_failure(throttle_ip, body.username)
        if reserved:
            # Already counted by the reservation above.
            audit(
                None,
                "admin.login.failure",
                target=body.username,
                locked=tripped,
                ip=client_ip,
            )
        elif user is not None:
            # Disabled accounts can never succeed, so incrementing the
            # failure counter or setting lockout_until would only leave
            # stale state behind once the account is re-enabled. Audit
            # the attempt but do not mutate the row.
            audit(
                None,
                "admin.login.failure",
                target=body.username,
                inactive_user=True,
                ip=client_ip,
            )
        else:
            audit(
                None, "admin.login.failure", target=_masked_unknown_username(body.username),
                unknown_user=True, ip=client_ip,
            )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # The generation that was verified must still be current: if the password
    # or token_version changed (or the account was disabled) meanwhile, the
    # proof is stale and nothing is issued.
    generation = (
        User.id == user.id,
        User.password_hash == verified_hash,
        User.token_version == verified_tv,
        User.is_active.is_(True),
    )
    if verified_mfa is None:
        # Release the pre-charged slot (and any lockout it or a concurrent
        # attempt set): the correct password proves the caller is the user.
        release = {"failed_login_count": 0, "lockout_until": None}
    else:
        # Two-factor account: the password alone proves nothing yet, so the
        # failures already counted (per account and per IP) stay until the
        # code is accepted too (admin_mfa.admin_login_mfa) — clearing them
        # here would let someone holding the password reset the budget with
        # every sign-in and guess codes without bound. Only this attempt's own
        # slot is handed back, since the password was right: otherwise every
        # sign-in would cost two slots, abandoned ones would pile up, and a
        # correct password at the limit would lock the account it just
        # proved (refund_values: a lockout that slot set is undone, the count
        # left one short of the limit, so the code still gets its one try).
        release = refund_values(tripped) if exemption is None else {"failed_login_count": User.failed_login_count}
    # Compare-and-swap on the generation that was verified.
    cas = db.execute(
        update(User)
        .where(*generation)
        .values(**release)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    generation_ok = cas.rowcount != 0
    if not generation_ok:
        record_login_failure(throttle_ip, body.username)
        audit(
            None,
            "admin.login.failure",
            target=body.username,
            credential_changed=True,
            ip=client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # The password is proven and the generation is current: bring a hash made
    # with an older (cheaper) work factor up to the current one. Best effort and
    # a compare-and-swap on the very hash that was verified, so a password
    # change committed meanwhile is never overwritten; token_version is not
    # touched, so no session is signed out by it.
    upgraded = rehash_if_outdated(body.password, verified_hash)
    if upgraded is not None:
        try:
            db.execute(
                update(User)
                .where(
                    User.id == verified_id,
                    User.password_hash == verified_hash,
                    User.token_version == verified_tv,
                )
                .values(password_hash=upgraded)
                .execution_options(synchronize_session=False)
            )
            db.commit()
        except Exception:
            db.rollback()
            _logger.warning("password hash upgrade failed for user id %s", verified_id, exc_info=True)
    if verified_mfa is not None:
        # The password was right: worth a row of its own, since a leaked
        # password shows here even when whoever has it never tries a code.
        # Actor-less like the other events before the sign-in is complete:
        # whoever sent it has not proved to be the account's owner yet.
        audit(None, "admin.login.mfa_required", target=body.username, ip=client_ip,
              **({"lockout_bypass": exemption} if exemption and was_locked else {}))
        return LoginOut(mfa_required=True, mfa_token=create_mfa_token(
            MFA_PENDING_AUDIENCE, verified_id, verified_tv, settings.mfa_login_ttl_seconds,
            # Single use: redeeming it spends a TOTP step or a recovery code,
            # which changes this fingerprint (admin_mfa.admin_login_mfa).
            mf=verified_mf,
        ))
    # A successful login forgives this IP's recent failures so a user who
    # fumbled their password a few times before getting it right is not left
    # one mistake away from the per-IP throttle.
    login_throttle.clear_user(throttle_ip, body.username)
    clear_exempt_failures(verified_id)
    # Mint from the verified snapshot, not a fresh read of the row (the
    # commit above expired it; an account deleted since would raise).
    account = SimpleNamespace(
        id=verified_id, username=body.username, role=verified_role, token_version=verified_tv,
    )
    audit(account, "admin.login.success", ip=client_ip,
          **({"lockout_bypass": exemption} if exemption and was_locked else {}))
    remember_device(response, request, verified_id, verified_tv)
    return LoginOut(access_token=create_token(account))


def reauth_throttle_key(request: Request, user: User) -> str:
    """The re-auth throttle bucket of this request's token (see
    verify_reauth_password)."""
    token_digest = hashlib.sha256(
        (bearer_token(request.headers.get("authorization", "")) or "").rstrip("=").encode()
    ).hexdigest()[:16]
    return f"reauth:{user.id}:{token_digest}"


def verify_reauth_password(
    request: Request,
    user: User,
    password: str,
    *,
    password_hash: Optional[str] = None,
    **audit_extra,
) -> bool:
    """Check a signed-in staff user's current password, as a second factor
    for a sensitive action; False when it is wrong.

    Every such check is a brute-force surface of its own: a stolen/leaked
    bearer token would otherwise let an attacker guess the account's real
    password unbounded (each miss just 4xx's), bypassing BOTH the per-user
    lockout and the per-IP throttle that guard /api/admin/login, then use the
    recovered password to seize durable control. So it is bounded by a
    throttle on its OWN keyspace — deliberately NOT the per-user lockout, so a
    token holder can't lock the real admin out of the login page — raising
    429 (with Retry-After) once that is exhausted, before the password is even
    checked; and each miss is audited (``admin.account.reauth_failure``, with
    ``audit_extra``) so the attempt is visible in the admin history.

    Keyed on the presented token, not the source IP: the guesses come from
    whoever holds the token, and folding the IP in would hand every address
    an attacker rotates through a fresh budget. Not on the account alone
    either: a leaked token's holder could then burn the budget and keep the
    real owner — signed in afresh, with a token of their own — from the
    password change that revokes the leaked one. Only a correct password
    mints a token, so holding one buys no further guesses. The account id
    stays in the key, and clear_ip on success only forgives THIS token's
    bucket. Trailing "=" is stripped first: the JWT decoder also accepts the
    padded spelling of a signature, which must not open a second bucket for
    the same token.

    One bucket per token across every endpoint that re-authenticates (the
    account page, restore apply, reset): spread over several, the guesses
    would otherwise get one budget each.

    ``password_hash`` is the hash to check against (a caller that must pin
    the generation it verified passes its snapshot); ``user.password_hash``
    by default. Blocking (pbkdf2 and an audit write): async endpoints call it
    through ``run_in_threadpool``.
    """
    throttle_key = reauth_throttle_key(request, user)
    refuse_if_throttled(
        throttle_key,
        max_failures=settings.reauth_ip_max_failures,
        window_seconds=settings.reauth_ip_window_seconds,
    )
    if not verify_password(password, user.password_hash if password_hash is None else password_hash):
        login_throttle.record_failure(
            throttle_key,
            window_seconds=settings.reauth_ip_window_seconds,
            max_failures=settings.reauth_ip_max_failures,
        )
        audit(user, "admin.account.reauth_failure", target=str(user.id), **audit_extra)
        return False
    login_throttle.clear_ip(throttle_key)
    return True


@router.get("/api/admin/me")
def admin_me(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "role_permissions": role_permissions_payload(db),
        "mfa": mfa.status_payload(user),
    }


@router.patch("/api/admin/me")
def admin_update_me(
    body: AdminMeUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Snapshot the credential generation the re-auth below is checked against.
    # A password change commits through a compare-and-swap on exactly these two
    # values, so two overlapping changes that both read generation n cannot
    # both land as n+1 — the second would otherwise leave the first change's
    # freshly minted token valid after the password moved on again, and would
    # have been authorised by a password hash that was already superseded.
    read_hash = user.password_hash
    read_tv = int(user.token_version or 0)
    # Throttled and audited: see verify_reauth_password.
    if not verify_reauth_password(request, user, body.current_password, password_hash=read_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_username = (body.username or "").strip()
    new_password = body.new_password or ""
    username_changed = bool(new_username and new_username != user.username)

    # Reject control characters (C0/DEL), matching admin_create_user and
    # admin_settings.admin_add_auth_user. This rename flows straight into the
    # audit log (recorded just below), the admin history CSV export and
    # journald; an embedded ESC / NUL could smuggle terminal escapes or
    # truncate the value in those sinks. The schema already caps the length at
    # MAX_USERNAME_LEN, but does not filter control chars.
    if new_username and CTRL_CHAR_RE.search(new_username):
        raise HTTPException(status_code=422, detail="username: invalid characters")

    if not new_username and not new_password:
        raise HTTPException(status_code=400, detail="Nothing to update")

    if new_password:
        validate_password_strength(new_password)
        # Revoke every previously-issued token for this account: a password
        # change must not leave old sessions (or a token leaked before the
        # change) usable for the rest of their 24h TTL. The caller's current
        # token is re-minted below with the bumped version so this request's
        # own session survives seamlessly.
        #
        # Conditional on the generation the re-auth was verified against (see
        # read_hash / read_tv above): if another password change, suspension
        # or forced logout committed in between, nothing matches and the
        # caller must re-authenticate against the current state instead.
        swapped = db.execute(
            update(User)
            .where(
                User.id == user.id,
                User.password_hash == read_hash,
                User.token_version == read_tv,
            )
            .values(
                password_hash=hash_password(new_password),
                token_version=read_tv + 1,
            )
            .execution_options(synchronize_session="fetch")
        )
        if swapped.rowcount != 1:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="The account changed while this request was in flight. Sign in again and retry.",
            )

    if username_changed:
        # Case-insensitive, matching admin_create_user: renaming to a spelling
        # that differs from an existing account only by case would produce the
        # same two-near-identical-accounts confusion.
        if staff_username_taken(db, new_username, exclude_id=user.id):
            raise HTTPException(status_code=400, detail="Username already in use")
        user.username = new_username

    # Mint from this request's own outcome, not a fresh read of the row after
    # the commit (which expires it): a sign-out-everywhere, suspension or other
    # bump committed in between would otherwise be read back, and the token
    # handed out below would carry that newer generation and survive the
    # revocation it was meant to fall to.
    account = SimpleNamespace(
        id=user.id,
        username=new_username if username_changed else user.username,
        role=user.role,
        token_version=read_tv + 1,
    )

    try:
        db.commit()
    except IntegrityError:
        # Lost the SELECT-then-UPDATE race against another concurrent rename
        # to the same username; surface the same 400 the create/register
        # paths produce rather than bubbling a 500 with an aborted txn.
        db.rollback()
        raise HTTPException(status_code=400, detail="Username already in use")
    audit(
        user,
        "admin.account.update",
        target=str(user.id),
        username_changed=username_changed,
        password_changed=bool(new_password),
    )
    resp = {
        "ok": True,
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "role_permissions": role_permissions_payload(db),
    }
    if new_password:
        # The password bump just invalidated the caller's existing token
        # (its tv no longer matches). Hand back a fresh one minted against
        # the new token_version so the dashboard can swap it in without
        # forcing an immediate re-login of the actor who made the change.
        # Keep how this session proved its second factor (create_token's
        # amr): a session that spent the last recovery code must not lose
        # its way to new codes by changing the password first.
        resp["access_token"] = create_token(account, amr=staff_token_amr(request.headers.get("authorization", "")))
    return resp


@router.post("/api/admin/me/sign-out-everywhere")
def admin_sign_out_everywhere(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """End every session of the signed-in account, this one included.

    Staff tokens are stateless and live for ``JWT_TTL_SECONDS``, and the
    dashboard's "Log out" only forgets the token in the browser that pressed
    it. This is the server-side counterpart for a shared computer, a lost
    device or a token that may have been copied: it bumps the account's
    token_version (the same generation a password change, suspension or
    two-factor change bumps), so every token issued so far stops working at
    once, and the known-device cookies tied to that generation stop vouching
    for their browsers. No fresh token is handed back - the caller signs in
    again.
    """
    account = SimpleNamespace(id=user.id, username=user.username, role=user.role)
    db.execute(
        update(User)
        .where(User.id == account.id)
        .values(token_version=User.token_version + 1)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    audit(account, "admin.account.sign_out_everywhere", target=str(account.id))
    return {"ok": True}


@router.post("/api/admin/register")
def admin_register(body: LoginIn, db: Session = Depends(get_db)):
    if not settings.allow_admin_register:
        raise HTTPException(status_code=403, detail="Disabled")
    # Mirror admin_create_user's input validation so the bootstrap path can't
    # mint an admin with an empty/whitespace-only or over-long username (the
    # shared LoginIn schema only caps the length, it doesn't require non-empty).
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="username must not be empty")
    if len(username) > MAX_USERNAME_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"username: at most {MAX_USERNAME_LEN} characters",
        )
    # Serialise the gate-check + insert so two concurrent first-launch
    # callers can't both pass "no admin exists yet" and create duplicate
    # admins. See the lock declaration above for scope rationale.
    validate_password_strength(body.password)
    with _admin_register_lock, _register_file_lock():
        # Re-check the gate inside both locks: another worker may have
        # created the first admin while this request waited on flock.
        if db.execute(select(User).where(User.role == "admin")).first():
            raise HTTPException(status_code=403, detail="Disabled")
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Exists")
        u = User(username=username, password_hash=hash_password(body.password), role="admin")
        db.add(u)
        try:
            db.commit()
        except IntegrityError:
            # Another worker process beat this one to the unique-username constraint.
            db.rollback()
            raise HTTPException(status_code=400, detail="Exists")
    return {"ok": True}
