"""The dashboard account lockout and the two ways a sign-in stands outside it.

The lockout (admin_auth.reserve_login_attempt) stops password guessing spread
over many addresses, but on its own it lets anyone who knows a username keep
the real owner out indefinitely. A browser that has signed in as the account
before carries a signed known-device cookie; the server itself (loopback) is
the documented way back in. Either is neither refused by the lockout nor
charged to it - the per-address throttle still applies.
"""
from __future__ import annotations

import contextlib
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from wsi_app import mfa
from wsi_app.auth import (
    create_known_device_token,
    create_token,
    hash_password,
    is_known_device,
    known_device_cookie_name,
)
from wsi_app.config import settings
from wsi_app.main import app
from wsi_app.models import AuditLog, User
from wsi_app.routers import admin_auth

PASSWORD = "owner-password-1"


@contextlib.contextmanager
def _override_settings(**values):
    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


def _user(db_session, password=PASSWORD):
    u = User(
        username=f"lock-{os.urandom(4).hex()}",
        password_hash=hash_password(password),
        role="admin",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _login(client, user, password=PASSWORD):
    return client.post("/api/admin/login", json={"username": user.username, "password": password})


def _lock_out(attacker, user):
    """Five wrong passwords from a browser that has never signed in."""
    for _ in range(settings.admin_login_max_failures):
        assert _login(attacker, user, "not-the-password").status_code == 401
    assert _login(attacker, user, PASSWORD).status_code == 429


def _row(db_session, user) -> User:
    db_session.expire_all()
    return db_session.get(User, user.id)


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------
def test_known_device_token_is_bound_to_account_and_generation():
    tok = create_known_device_token(7, 3)
    assert is_known_device(tok, 7, 3)
    assert not is_known_device(tok, 8, 3)       # another account
    assert not is_known_device(tok, 7, 4)       # token_version moved on
    assert not is_known_device("junk", 7, 3)
    assert not is_known_device(None, 7, 3)


def test_known_device_token_is_not_a_staff_token(client):
    tok = create_known_device_token(1, 0)
    assert client.get("/api/admin/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


# ---------------------------------------------------------------------------
# A sign-in leaves the cookie; nothing else does
# ---------------------------------------------------------------------------
def test_successful_sign_in_sets_a_scoped_httponly_cookie(db_session):
    user = _user(db_session)
    c = TestClient(app)
    r = _login(c, user)
    assert r.status_code == 200
    header = r.headers.get("set-cookie", "")
    name = known_device_cookie_name(user.id)
    assert header.startswith(f"{name}=")
    low = header.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/api/admin/login" in low
    assert "domain=" not in low


def test_failed_sign_in_sets_no_cookie(db_session):
    user = _user(db_session)
    r = _login(TestClient(app), user, "wrong-password")
    assert r.status_code == 401
    assert "wsi_kd_" not in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# The lockout still holds for everyone else
# ---------------------------------------------------------------------------
def test_unknown_browser_stays_locked_out_even_with_the_right_password(db_session):
    user = _user(db_session)
    _lock_out(TestClient(app), user)
    assert _login(TestClient(app), user, PASSWORD).status_code == 429


def test_another_accounts_cookie_does_not_exempt(db_session):
    owner = _user(db_session)
    other = _user(db_session)
    other_client = TestClient(app)
    assert _login(other_client, other).status_code == 200
    _lock_out(TestClient(app), owner)

    thief = TestClient(app)
    # Their own cookie, presented under the locked account's cookie name.
    thief.cookies.set(
        known_device_cookie_name(owner.id),
        other_client.cookies.get(known_device_cookie_name(other.id)),
        path="/api/admin/login",
    )
    assert _login(thief, owner, PASSWORD).status_code == 429


# ---------------------------------------------------------------------------
# The owner's own browser is let in, and that lifts the lock
# ---------------------------------------------------------------------------
def test_known_browser_signs_in_during_a_lockout_and_clears_it(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200

    _lock_out(TestClient(app), user)
    assert _row(db_session, user).lockout_until is not None

    r = _login(owner, user)
    assert r.status_code == 200 and r.json()["access_token"]
    row = _row(db_session, user)
    assert row.lockout_until is None and row.failed_login_count == 0
    # ...which also frees everyone else.
    assert _login(TestClient(app), user).status_code == 200

    rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.login.success", AuditLog.actor_id == user.id)
    ).scalars().all()
    # Visible in the history: the one sign-in that got past a standing lockout.
    assert [r for r in rows if "known_device" in (r.details or "")]


def test_known_browser_with_a_wrong_password_is_refused_but_not_charged(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    _lock_out(TestClient(app), user)
    before = _row(db_session, user)
    locked_until, count = before.lockout_until, before.failed_login_count

    assert _login(owner, user, "typo-password").status_code == 401   # not 429
    after = _row(db_session, user)
    assert after.lockout_until == locked_until and after.failed_login_count == count


def test_a_password_change_ends_the_trust(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    # token_version moves on (password change, suspension, two-factor change).
    row = _row(db_session, user)
    row.token_version = int(row.token_version or 0) + 1
    db_session.commit()
    _lock_out(TestClient(app), user)
    assert _login(owner, user).status_code == 429


def test_the_per_address_throttle_still_applies_to_a_known_browser(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    with _override_settings(admin_login_ip_max_failures=3, admin_login_ip_window_seconds=300):
        for _ in range(3):
            assert _login(owner, user, "typo-password").status_code == 401
        assert _login(owner, user, PASSWORD).status_code == 429


# ---------------------------------------------------------------------------
# The server itself
# ---------------------------------------------------------------------------
def test_loopback_client_is_outside_the_lockout(db_session, monkeypatch):
    user = _user(db_session)
    _lock_out(TestClient(app), user)
    monkeypatch.setattr(admin_auth, "get_client_ip", lambda headers, peer="": "127.0.0.1")
    r = _login(TestClient(app), user)
    assert r.status_code == 200
    assert _row(db_session, user).lockout_until is None


def test_loopback_wrong_password_is_not_charged(db_session, monkeypatch):
    user = _user(db_session)
    monkeypatch.setattr(admin_auth, "get_client_ip", lambda headers, peer="": "::1")
    for _ in range(settings.admin_login_max_failures + 2):
        assert _login(TestClient(app), user, "typo-password").status_code in (401, 429)
    row = _row(db_session, user)
    assert row.lockout_until is None and row.failed_login_count == 0


# ---------------------------------------------------------------------------
# Two-factor accounts: the code step is exempt the same way
# ---------------------------------------------------------------------------
@pytest.fixture
def _mfa_key(monkeypatch):
    monkeypatch.setenv("WSI_MFA_KEY", "k" * 64)


def _enrol(client, user):
    h = {"Authorization": f"Bearer {create_token(user)}"}
    r = client.post("/api/admin/me/mfa/setup", json={"current_password": PASSWORD}, headers=h)
    assert r.status_code == 200, r.text
    setup = r.json()
    r = client.post(
        "/api/admin/me/mfa/confirm",
        json={"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], mfa.current_step())},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return setup["secret"]


def test_known_browser_completes_a_two_factor_sign_in_during_a_lockout(db_session, _mfa_key):
    user = _user(db_session)
    owner = TestClient(app)
    secret = _enrol(owner, user)
    db_session.expire_all()
    user = db_session.get(User, user.id)

    def sign_in_with_code():
        # The code that enrolled the account spent its TOTP step, and a step
        # can only be used once: rewind the replay guard between sign-ins.
        row = _row(db_session, user)
        row.totp_last_step = None
        db_session.commit()
        r = _login(owner, user)
        assert r.status_code == 200 and r.json().get("mfa_required"), r.text
        return owner.post(
            "/api/admin/login/mfa",
            json={"mfa_token": r.json()["mfa_token"], "code": mfa.totp_at(secret, mfa.current_step())},
        )

    # The first full sign-in (password + code) leaves the cookie.
    assert sign_in_with_code().status_code == 200
    assert known_device_cookie_name(user.id) in owner.cookies

    _lock_out(TestClient(app), user)
    assert _row(db_session, user).lockout_until is not None
    r = sign_in_with_code()
    assert r.status_code == 200, r.text
    assert _row(db_session, user).lockout_until is None


# ---------------------------------------------------------------------------
# The known-device cookie is a bearer token: its exemption has a budget
# ---------------------------------------------------------------------------
def _copied_cookie_client(owner, user):
    """A client that presents the owner's known-device cookie, as someone who
    copied it from the owner's browser would, from an address of their own."""
    thief = TestClient(app)
    thief.cookies.set(
        known_device_cookie_name(user.id),
        owner.cookies.get(known_device_cookie_name(user.id)),
        path="/api/admin/login",
    )
    return thief


def test_a_copied_known_device_cookie_buys_a_bounded_number_of_free_guesses(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    thief = _copied_cookie_client(owner, user)
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR

    # (The per-address throttle is not what is under test: lift it.)
    with _override_settings(admin_login_ip_max_failures=10_000):
        for _ in range(budget):
            assert _login(thief, user, "guess").status_code == 401
        row = _row(db_session, user)
        assert row.failed_login_count == 0 and row.lockout_until is None  # free so far

        # The budget is spent: the ordinary lockout counts again...
        for _ in range(settings.admin_login_max_failures):
            assert _login(thief, user, "guess").status_code == 401
        # ...and holds, whatever the cookie says.
        assert _login(thief, user, PASSWORD).status_code == 429
        assert _login(owner, user, PASSWORD).status_code == 429


def test_the_owners_successful_sign_in_gives_the_budget_back(db_session):
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR
    with _override_settings(admin_login_ip_max_failures=10_000):
        for _ in range(budget - 1):
            assert _login(owner, user, "typo-password").status_code == 401
        assert _login(owner, user).status_code == 200  # the owner is at the keyboard
        for _ in range(budget - 1):
            assert _login(owner, user, "typo-password").status_code == 401
    row = _row(db_session, user)
    assert row.failed_login_count == 0 and row.lockout_until is None


def test_a_wrong_two_factor_code_draws_on_the_same_budget(db_session, _mfa_key):
    user = _user(db_session)
    owner = TestClient(app)
    secret = _enrol(owner, user)
    db_session.expire_all()
    user = db_session.get(User, user.id)
    row = _row(db_session, user)
    row.totp_last_step = None
    db_session.commit()
    r = _login(owner, user)
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    r = owner.post(
        "/api/admin/login/mfa",
        json={"mfa_token": r.json()["mfa_token"], "code": mfa.totp_at(secret, mfa.current_step())},
    )
    assert r.status_code == 200, r.text  # leaves the cookie

    thief = _copied_cookie_client(owner, user)
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR
    valid = mfa.totp_at(secret, mfa.current_step())
    wrong = "000000" if valid != "000000" else "111111"
    tv = int(_row(db_session, user).token_version or 0)
    with _override_settings(admin_login_ip_max_failures=10_000):
        r = _login(thief, user)  # the password is known to them; the code is not
        assert r.status_code == 200 and r.json().get("mfa_required"), r.text
        token = r.json()["mfa_token"]
        # The password step took the first slot; the code guesses take the rest.
        for _ in range(budget - 1):
            assert thief.post("/api/admin/login/mfa", json={"mfa_token": token, "code": wrong}).status_code == 401
        assert _row(db_session, user).failed_login_count == 0  # free so far
        # The budget is spent: the cookie no longer exempts (and this call takes nothing).
        assert admin_auth.lockout_exemption(_fake_request(thief, user), "203.0.113.9", user.id, tv) is None


def _fake_request(client, user):
    from starlette.requests import Request

    cookie = client.cookies.get(known_device_cookie_name(user.id))
    return Request({
        "type": "http", "method": "POST", "path": "/api/admin/login", "query_string": b"",
        "headers": [(b"cookie", f"{known_device_cookie_name(user.id)}={cookie}".encode())],
    })


# ---------------------------------------------------------------------------
# Loopback: read from the real headers, and not when a proxy on this host
# fronts nginx (every client then looks like 127.0.0.1)
# ---------------------------------------------------------------------------
def _nginx_peer():
    """A request as nginx makes it: from 127.0.0.1, so X-Real-IP is honoured."""
    return TestClient(app, client=("127.0.0.1", 50000))


def test_an_ssh_tunnel_reads_as_loopback_through_the_real_headers(db_session):
    user = _user(db_session)
    _lock_out(TestClient(app), user)
    r = _nginx_peer().post(
        "/api/admin/login",
        json={"username": user.username, "password": PASSWORD},
        headers={"X-Real-IP": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"},
    )
    assert r.status_code == 200, r.text


def test_a_proxy_on_this_host_does_not_turn_its_clients_into_loopback(db_session):
    user = _user(db_session)
    _lock_out(TestClient(app), user)
    # Caddy / `tailscale serve` on the same host, in front of nginx: nginx's
    # peer and X-Real-IP are 127.0.0.1, and the real client is in the chain.
    r = _nginx_peer().post(
        "/api/admin/login",
        json={"username": user.username, "password": PASSWORD},
        headers={"X-Real-IP": "127.0.0.1", "X-Forwarded-For": "203.0.113.9, 127.0.0.1"},
    )
    assert r.status_code == 429, r.text


# ---------------------------------------------------------------------------
# Two-factor password step under an exemption must not touch the count
# ---------------------------------------------------------------------------
def test_an_exempt_password_step_leaves_the_failure_count_alone(db_session, _mfa_key):
    user = _user(db_session)
    owner = TestClient(app)
    secret = _enrol(owner, user)
    db_session.expire_all()
    user = db_session.get(User, user.id)
    row = _row(db_session, user)
    row.totp_last_step = None
    db_session.commit()
    r = _login(owner, user)
    r = owner.post(
        "/api/admin/login/mfa",
        json={"mfa_token": r.json()["mfa_token"], "code": mfa.totp_at(secret, mfa.current_step())},
    )
    assert r.status_code == 200, r.text  # known browser now

    # Someone else's failures stand on the account (below the limit).
    row = _row(db_session, user)
    row.failed_login_count = 3
    db_session.commit()
    r = _login(owner, user)  # right password, known browser, code still owed
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    # Not charged, so nothing to hand back: the count is exactly what it was.
    assert _row(db_session, user).failed_login_count == 3


# --- the budget: atomic, taken before the check, and out of reach of a client ---
def test_slots_are_taken_atomically_when_many_attempts_arrive_together():
    """Parallel guesses must not all pass a check made before any of them is
    charged. One step under a lock: exactly ``max_slots`` of 200 racing callers win."""
    import threading

    from wsi_app import login_throttle

    won = []
    start = threading.Barrier(200)

    def attempt():
        start.wait()
        won.append(login_throttle.reserve_slot("race-key", max_slots=20, window_seconds=60))

    threads = [threading.Thread(target=attempt) for _ in range(200)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(won) == 20


def test_a_slot_frees_up_when_its_window_has_passed(monkeypatch):
    from wsi_app import login_throttle

    clock = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: clock[0])
    assert login_throttle.reserve_slot("k", max_slots=1, window_seconds=60)
    assert not login_throttle.reserve_slot("k", max_slots=1, window_seconds=60)
    clock[0] += 61
    assert login_throttle.reserve_slot("k", max_slots=1, window_seconds=60)


def test_every_attempt_under_the_cookie_takes_a_slot_even_a_right_one(db_session):
    """Slots are taken before the password is checked: the owner's right password
    is attempt 1, and 'budget' attempts later the cookie stops exempting."""
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR
    with _override_settings(admin_login_ip_max_failures=10_000):
        for _ in range(budget - 1):
            assert _login(owner, user, "typo-password").status_code == 401
        # The next attempt has the last slot; a success gives them all back.
        assert _login(owner, user).status_code == 200
        assert _login(owner, user, "typo-password").status_code == 401
        assert _row(db_session, user).failed_login_count == 0


def test_a_client_cannot_name_the_budget_through_its_address(db_session):
    """get_client_ip takes a header value only if it is an address, but a budget
    kept under a string in the same table as client addresses could still be
    named, and emptied, by a stranger were that ever relaxed: the slots live in
    a table of their own, whatever a request sends."""
    user = _user(db_session)
    owner = TestClient(app)
    assert _login(owner, user).status_code == 200
    other = _user(db_session)
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR
    tv = int(_row(db_session, user).token_version or 0)
    with _override_settings(admin_login_ip_max_failures=10_000):
        for _ in range(budget + 5):
            _nginx_peer().post(
                "/api/admin/login",
                json={"username": other.username, "password": "wrong-password"},
                headers={"Cf-Connecting-IP": admin_auth._known_device_budget_key(user.id)},
            )
    assert admin_auth.lockout_exemption(_fake_request(owner, user), "198.51.100.1", user.id, tv) == "known_device"


def test_a_completed_two_factor_sign_in_gives_the_budget_back(db_session, _mfa_key):
    from wsi_app import login_throttle

    user = _user(db_session)
    owner = TestClient(app)
    secret = _enrol(owner, user)
    db_session.expire_all()
    user = db_session.get(User, user.id)

    def sign_in_with_code():
        row = _row(db_session, user)
        row.totp_last_step = None
        db_session.commit()
        r = _login(owner, user)
        assert r.status_code == 200 and r.json().get("mfa_required"), r.text
        return owner.post(
            "/api/admin/login/mfa",
            json={"mfa_token": r.json()["mfa_token"], "code": mfa.totp_at(secret, mfa.current_step())},
        )

    assert sign_in_with_code().status_code == 200  # leaves the cookie
    budget = settings.admin_login_max_failures * admin_auth.KNOWN_DEVICE_BUDGET_FACTOR
    key = admin_auth._known_device_budget_key(user.id)
    for _ in range(budget - 2):  # two slots left: the password step and the code step
        assert login_throttle.reserve_slot(key, max_slots=budget, window_seconds=900)
    assert sign_in_with_code().status_code == 200
    tv = int(_row(db_session, user).token_version or 0)
    req = _fake_request(owner, user)
    granted = sum(admin_auth.lockout_exemption(req, "203.0.113.9", user.id, tv) == "known_device" for _ in range(budget + 3))
    assert granted == budget  # the whole budget again -- and no more
