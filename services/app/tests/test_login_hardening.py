"""Tests for the admin-login / account-revocation hardening.

Covers the login / account-revocation behaviours:

* token_version revocation — a password change invalidates every prior JWT
  and hands the caller a fresh one (admin_auth.admin_update_me).
* user suspend / re-enable (admin_users.admin_update_user) with the
  self / last-admin guards, and immediate token revocation on disable.
* per-IP failed-login throttle (login_throttle) that bounds attempts spread
  across many usernames, and is cleared by a successful login.
* the locked-out branch writes an audit row.
* admin_register rejects an empty / whitespace username.
"""
from __future__ import annotations

import contextlib
import time

import pytest

from sqlalchemy import delete, select

from wsi_app.auth import create_token, hash_password
from wsi_app.config import settings
from wsi_app.models import AuditLog, User


@contextlib.contextmanager
def _override_settings(**values):
    """Temporarily override fields on the frozen Settings singleton."""
    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


# ---------------------------------------------------------------------------
# token_version revocation
# ---------------------------------------------------------------------------
def test_password_change_revokes_old_token_and_returns_fresh_one(client, db_session):
    user = User(
        username=f"rotate-{int(time.time()*1000)}",
        password_hash=hash_password("old-password"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    old_token = create_token(user)
    old_headers = {"Authorization": f"Bearer {old_token}"}

    # Sanity: the token works before the change.
    assert client.get("/api/admin/me", headers=old_headers).status_code == 200

    r = client.patch(
        "/api/admin/me",
        json={"current_password": "old-password", "new_password": "brand-new-pass"},
        headers=old_headers,
    )
    assert r.status_code == 200, r.text
    fresh = r.json().get("access_token")
    assert fresh and fresh != old_token

    # The old token is now revoked (its tv no longer matches the bumped column).
    assert client.get("/api/admin/me", headers=old_headers).status_code == 401
    # The freshly minted token works.
    assert client.get(
        "/api/admin/me", headers={"Authorization": f"Bearer {fresh}"}
    ).status_code == 200


def test_me_reauth_is_ip_throttled_and_audited(client, db_session):
    """A stolen bearer token must not allow unbounded brute-force of the real
    password via PATCH /api/admin/me: the current-password re-auth is per-IP
    throttled (429 once the window fills) and each miss writes an audit row."""
    user = User(
        username=f"reauth-{int(time.time()*1000)}",
        password_hash=hash_password("real-secret"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    headers = {"Authorization": f"Bearer {create_token(user)}"}

    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        for _ in range(3):
            r = client.patch(
                "/api/admin/me",
                json={"current_password": "guess", "new_password": "whatever-123"},
                headers=headers,
            )
            assert r.status_code == 400, r.text
        # The 4th wrong-password attempt is throttled before verify_password.
        r = client.patch(
            "/api/admin/me",
            json={"current_password": "guess", "new_password": "whatever-123"},
            headers=headers,
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    # Failures were audited under a dedicated action.
    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "admin.account.reauth_failure",
            AuditLog.target == str(user.id),
        )
    ).scalars().all()
    assert len(rows) >= 3


def test_me_reauth_throttle_does_not_lock_admin_login(client, db_session):
    """The re-auth throttle uses its own IP keyspace, so exhausting it must NOT
    lock the same IP out of /api/admin/login (no self-DoS lever for a token
    holder)."""
    user = User(
        username=f"reauth-iso-{int(time.time()*1000)}",
        password_hash=hash_password("real-secret"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    headers = {"Authorization": f"Bearer {create_token(user)}"}

    with _override_settings(
        reauth_ip_max_failures=3,
        reauth_ip_window_seconds=300,
        admin_login_ip_max_failures=30,
        admin_login_ip_window_seconds=300,
    ):
        for _ in range(4):
            client.patch(
                "/api/admin/me",
                json={"current_password": "guess", "new_password": "whatever-123"},
                headers=headers,
            )
        # Admin login from the same IP is unaffected by the reauth bucket.
        r = client.post(
            "/api/admin/login",
            json={"username": user.username, "password": "real-secret"},
        )
        assert r.status_code == 200, r.text


def test_me_reauth_budget_is_not_reset_by_another_accounts_success(client, db_session):
    """Guesses made with a victim's (leaked) bearer token must keep counting
    even when the same IP interleaves a successful re-auth of its OWN account:
    the bucket is per account, so the success may not forgive the other one."""
    stamp = int(time.time() * 1000)
    victim = User(username=f"reauth-v-{stamp}", password_hash=hash_password("victim-secret"),
                  role="admin", is_active=True)
    mine = User(username=f"reauth-m-{stamp}", password_hash=hash_password("my-secret-1"),
                role="admin", is_active=True)
    db_session.add_all([victim, mine])
    db_session.commit()
    db_session.refresh(victim)
    db_session.refresh(mine)
    victim_h = {"Authorization": f"Bearer {create_token(victim)}"}
    mine_h = {"Authorization": f"Bearer {create_token(mine)}"}

    codes = []
    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        for i in range(8):
            codes.append(client.patch(
                "/api/admin/me",
                json={"current_password": f"guess-{i}", "new_password": "whatever-123"},
                headers=victim_h,
            ).status_code)
            # A successful re-auth of the attacker's own account in between.
            ok = client.patch(
                "/api/admin/me",
                json={"current_password": "my-secret-1", "username": f"reauth-m-{stamp}-{i}"},
                headers=mine_h,
            )
            assert ok.status_code == 200, ok.text
    assert 429 in codes, codes


def test_me_reauth_budget_is_per_token_not_per_source_ip(client, db_session, monkeypatch):
    """A leaked token's holder must not get a fresh re-auth budget by
    rotating source addresses: the bucket is the token's, wherever the
    guesses come from."""
    from wsi_app import network as _network

    monkeypatch.setattr(
        _network, "_TRUSTED_PROXIES", frozenset(_network._TRUSTED_PROXIES | {"testclient"})
    )
    user = User(username=f"reauth-ip-{int(time.time()*1000)}",
                password_hash=hash_password("real-secret"), role="admin", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    headers = {"Authorization": f"Bearer {create_token(user)}"}

    codes = []
    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        for i in range(4):
            codes.append(client.patch(
                "/api/admin/me",
                json={"current_password": f"guess-{i}", "new_password": "whatever-123"},
                headers={**headers, "X-Real-IP": f"198.51.100.{i + 1}"},
            ).status_code)
    assert codes == [400, 400, 400, 429]


def test_leaked_token_cannot_block_the_owners_password_change(client, db_session, monkeypatch):
    """Changing the password is how the owner revokes a leaked token. Whoever
    holds that token must not be able to spend the re-auth budget so that the
    owner, signed in afresh, is refused that change with a 429."""
    user = User(username=f"reauth-own-{int(time.time()*1000)}",
                password_hash=hash_password("real-secret"), role="admin", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    real_time = time.time
    with monkeypatch.context() as m:
        # Minted a while ago (tokens of one account minted within the same
        # second are byte-identical).
        m.setattr(time, "time", lambda: real_time() - 120)
        leaked = {"Authorization": f"Bearer {create_token(user)}"}

    with _override_settings(reauth_ip_max_failures=3, reauth_ip_window_seconds=300):
        codes = [
            client.patch(
                "/api/admin/me",
                json={"current_password": f"guess-{i}", "new_password": "whatever-123"},
                headers=leaked,
            ).status_code
            for i in range(4)
        ]
        assert codes == [400, 400, 400, 429]
        # The JWT decoder also accepts the signature's padded spelling; it is
        # the same token, so it gets no fresh budget.
        padded = {"Authorization": leaked["Authorization"] + "="}
        r = client.patch(
            "/api/admin/me",
            json={"current_password": "guess-padded", "new_password": "whatever-123"},
            headers=padded,
        )
        assert r.status_code == 429, r.text

        r = client.post("/api/admin/login", json={"username": user.username, "password": "real-secret"})
        assert r.status_code == 200, r.text
        owner = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.patch(
            "/api/admin/me",
            json={"current_password": "real-secret", "new_password": "brand-new-pass-9"},
            headers=owner,
        )
        assert r.status_code == 200, r.text

    r = client.get("/api/admin/me", headers=leaked)
    assert (r.status_code, r.json()["detail"]) == (401, "Invalid token")


def test_ip_throttle_limit_above_1024_still_trips():
    """Were the per-IP history capped at 1024 entries whatever the limit, a
    configured *_IP_MAX_FAILURES above that could never be reached and the
    throttle would silently stop working."""
    from wsi_app import login_throttle

    key = "unit:203.0.113.9"
    for i in range(1500):
        assert login_throttle.retry_after_seconds(key, max_failures=1500, window_seconds=300) == 0
        login_throttle.record_failure(key, window_seconds=300, max_failures=1500, username=f"u{i}")
    assert login_throttle.retry_after_seconds(key, max_failures=1500, window_seconds=300) > 0


def test_retry_after_dates_from_the_failure_that_keeps_the_ip_blocked(monkeypatch):
    """With more failures in the window than the limit, the IP stays blocked
    until the count drops below the limit — not merely until the oldest
    failure ages out, which would under-report Retry-After."""
    from wsi_app import login_throttle

    now = {"t": 1000.0}
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now["t"])
    key = "unit:203.0.113.10"
    for offset in (0, 10, 20, 30, 40):
        now["t"] = 1000.0 + offset
        login_throttle.record_failure(key, window_seconds=100, max_failures=3, username=f"u{offset}")
    # Failures at +0, +10 and +20 must all expire before fewer than 3 remain.
    assert login_throttle.retry_after_seconds(key, max_failures=3, window_seconds=100) == 80
    now["t"] = 1000.0 + 119
    assert login_throttle.retry_after_seconds(key, max_failures=3, window_seconds=100) == 1
    now["t"] = 1000.0 + 121
    assert login_throttle.retry_after_seconds(key, max_failures=3, window_seconds=100) == 0


def test_repeated_secret_counts_once_distinct_secrets_each_count():
    """A failure repeating an already-counted (username, secret) pair within
    the window is not counted again; any other username or secret is."""
    from wsi_app import login_throttle

    key = "unit:203.0.113.11"
    for _ in range(10):
        login_throttle.record_failure(
            key, window_seconds=300, max_failures=3, username="student", secret="old-pass"
        )
    assert login_throttle.retry_after_seconds(key, max_failures=3, window_seconds=300) == 0
    login_throttle.record_failure(
        key, window_seconds=300, max_failures=3, username="student", secret="guess-1"
    )
    login_throttle.record_failure(
        key, window_seconds=300, max_failures=3, username="other", secret="old-pass"
    )
    assert login_throttle.retry_after_seconds(key, max_failures=3, window_seconds=300) > 0


# ---------------------------------------------------------------------------
# IPv6 clients are throttled per /64
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ip, bucket", [
    ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("2001:DB8:1:3::1", "2001:db8:1:3::/64"),
    ("fe80::1%eth0", "fe80::/64"),
    # An IPv4 peer reported by a dual-stack socket is that IPv4 address.
    ("::ffff:192.0.2.7", "192.0.2.7"),
    ("192.0.2.7", "192.0.2.7"),
    ("testclient", "testclient"),
    ("", ""),
])
def test_ip_bucket(ip, bucket):
    from wsi_app import login_throttle

    assert login_throttle.ip_bucket(ip) == bucket


def _proxied_client(ip: str):
    """A client behind the trusted loopback hop, so X-Real-IP is honoured."""
    from fastapi.testclient import TestClient

    from wsi_app.main import app

    c = TestClient(app, client=("127.0.0.1", 50000))
    c.headers["X-Real-IP"] = ip
    return c


def test_admin_login_throttle_is_not_dodged_by_rotating_within_a_64():
    with _override_settings(admin_login_ip_max_failures=3, admin_login_ip_window_seconds=300):
        for i in range(3):
            assert _proxied_client(f"2001:db8:1:2::{i + 1:x}").post(
                "/api/admin/login", json={"username": f"nobody-{i}", "password": "wrong"},
            ).status_code == 401
        # A fresh address in the same /64 is the same client.
        assert _proxied_client("2001:db8:1:2:dead:beef:0:1").post(
            "/api/admin/login", json={"username": "nobody-x", "password": "wrong"},
        ).status_code == 429
        # Another /64 (another site / line) is not.
        assert _proxied_client("2001:db8:1:3::1").post(
            "/api/admin/login", json={"username": "nobody-y", "password": "wrong"},
        ).status_code == 401


def test_app_login_throttle_is_not_dodged_by_rotating_within_a_64(monkeypatch):
    from wsi_app import app_auth as _app_auth

    monkeypatch.setattr(_app_auth, "_snapshot", _app_auth._AuthSnapshot(mode="basic"))
    with _override_settings(app_login_ip_max_failures=3, app_login_ip_window_seconds=300):
        for i in range(3):
            assert _proxied_client(f"2001:db8:9:9::{i + 1:x}").post(
                "/api/auth/login", data={"username": f"nobody-{i}", "password": "wrong"},
            ).status_code == 401
        assert _proxied_client("2001:db8:9:9::ff").post(
            "/api/auth/login", data={"username": "nobody-x", "password": "wrong"},
        ).status_code == 429


def test_username_only_change_does_not_revoke_token(client, db_session):
    """Renaming (no password change) must NOT log the caller out."""
    user = User(
        username=f"rename-{int(time.time()*1000)}",
        password_hash=hash_password("keep-this-pass"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    headers = {"Authorization": f"Bearer {create_token(user)}"}

    r = client.patch(
        "/api/admin/me",
        json={"current_password": "keep-this-pass", "username": f"renamed-{user.id}"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "access_token" not in r.json()
    # Same token still valid — no needless re-login on a username change.
    assert client.get("/api/admin/me", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# suspend / re-enable
# ---------------------------------------------------------------------------
def test_disable_user_revokes_token_and_blocks_login(client, admin_headers, db_session):
    target = User(
        username=f"victim-{int(time.time()*1000)}",
        password_hash=hash_password("victim-pass"),
        role="instructor",
        is_active=True,
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    target_token = create_token(target)

    # Disable via the admin PATCH endpoint.
    r = client.patch(
        f"/api/admin/users/{target.id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False

    # The still-cryptographically-valid token is rejected (is_active + tv).
    assert client.get(
        "/api/admin/me", headers={"Authorization": f"Bearer {target_token}"}
    ).status_code == 401
    # And a fresh login is refused while disabled (401, not a server error).
    assert client.post(
        "/api/admin/login",
        json={"username": target.username, "password": "victim-pass"},
    ).status_code == 401

    # Re-enable; login works again.
    r = client.patch(
        f"/api/admin/users/{target.id}",
        json={"is_active": True},
        headers=admin_headers,
    )
    assert r.status_code == 200 and r.json()["is_active"] is True
    assert client.post(
        "/api/admin/login",
        json={"username": target.username, "password": "victim-pass"},
    ).status_code == 200


def test_cannot_disable_self(client, admin_headers, admin_user):
    r = client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert r.status_code == 400
    assert "yourself" in (r.json().get("detail") or "").lower()


def test_second_admin_can_disable_the_first(client, db_session, admin_user):
    """With two active admins, one can suspend the other (the last-admin
    guard only bites when zero active admins would remain — and that case is
    caught earlier by the self-guard, mirroring the delete path)."""
    db_session.execute(delete(User).where(User.role == "admin", User.id != admin_user.id))
    db_session.commit()

    second = User(
        username=f"second-admin-{int(time.time()*1000)}",
        password_hash=hash_password("x"),
        role="admin",
        is_active=True,
    )
    db_session.add(second)
    db_session.commit()
    db_session.refresh(second)
    second_headers = {"Authorization": f"Bearer {create_token(second)}"}

    # second disables the original — fine, two active admins existed.
    r = client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"is_active": False},
        headers=second_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False

    # `second` is now the sole active admin; it cannot suspend itself
    # (self-guard), so the management surface stays reachable.
    r = client.patch(
        f"/api/admin/users/{second.id}",
        json={"is_active": False},
        headers=second_headers,
    )
    assert r.status_code == 400


def test_disable_requires_admin(client, manager_headers, instructor_headers, db_session):
    target = User(
        username=f"t-{int(time.time()*1000)}",
        password_hash=hash_password("x"),
        role="instructor",
        is_active=True,
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    assert client.patch(
        f"/api/admin/users/{target.id}", json={"is_active": False}, headers=manager_headers
    ).status_code == 403
    assert client.patch(
        f"/api/admin/users/{target.id}", json={"is_active": False}, headers=instructor_headers
    ).status_code == 403


# ---------------------------------------------------------------------------
# per-IP throttle
# ---------------------------------------------------------------------------
def test_ip_throttle_blocks_credential_stuffing_across_usernames(client):
    """Failures spread across DISTINCT unknown usernames (which never trip the
    per-user lockout) are bounded by the per-IP throttle."""
    with _override_settings(admin_login_ip_max_failures=3, admin_login_ip_window_seconds=300):
        for i in range(3):
            r = client.post(
                "/api/admin/login",
                json={"username": f"nobody-{i}", "password": "wrong-guess"},
            )
            assert r.status_code == 401, r.text
        # The next attempt — still a different username — is throttled by IP.
        r = client.post(
            "/api/admin/login",
            json={"username": "nobody-final", "password": "wrong-guess"},
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_successful_login_clears_ip_throttle(client, db_session):
    user = User(
        username=f"good-{int(time.time()*1000)}",
        password_hash=hash_password("correct-horse"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    with _override_settings(admin_login_ip_max_failures=3, admin_login_ip_window_seconds=300):
        # Two failed attempts at this account (a typo-prone user).
        for _ in range(2):
            assert client.post(
                "/api/admin/login",
                json={"username": user.username, "password": "nope"},
            ).status_code == 401
        # A correct login clears that username's failures from the IP.
        assert client.post(
            "/api/admin/login",
            json={"username": user.username, "password": "correct-horse"},
        ).status_code == 200
        # The counter is reset: three more failures are allowed before a block.
        for i in range(3):
            assert client.post(
                "/api/admin/login",
                json={"username": f"again-{i}", "password": "nope"},
            ).status_code == 401


def test_successful_login_keeps_failures_against_other_usernames(client, db_session):
    """Interleaving one success per failure must not keep the IP unthrottled
    while it guesses other usernames."""
    user = User(
        username=f"mine-{int(time.time()*1000)}",
        password_hash=hash_password("correct-horse"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    with _override_settings(admin_login_ip_max_failures=3, admin_login_ip_window_seconds=300):
        codes = []
        for i in range(3):
            codes.append(client.post(
                "/api/admin/login",
                json={"username": f"target-{i}", "password": "guess"},
            ).status_code)
            codes.append(client.post(
                "/api/admin/login",
                json={"username": user.username, "password": "correct-horse"},
            ).status_code)
        assert codes == [401, 200, 401, 200, 401, 429]


# ---------------------------------------------------------------------------
# locked-out branch audits
# ---------------------------------------------------------------------------
def test_locked_out_attempt_is_audited(client, db_session):
    from wsi_app.admin_helpers import utcnow
    from datetime import timedelta

    user = User(
        username=f"locked-audit-{int(time.time()*1000)}",
        password_hash=hash_password("whatever"),
        role="admin",
        is_active=True,
        lockout_until=utcnow() + timedelta(minutes=5),
    )
    db_session.add(user)
    db_session.commit()

    r = client.post(
        "/api/admin/login",
        json={"username": user.username, "password": "wrong"},
    )
    assert r.status_code == 429

    row = db_session.execute(
        select(AuditLog)
        .where(AuditLog.action == "admin.login.failure", AuditLog.target == user.username)
        .order_by(AuditLog.id.desc())
    ).scalars().first()
    assert row is not None
    assert "locked_out" in (row.details or "")


# ---------------------------------------------------------------------------
# The per-user budget is reserved before the password is verified
# ---------------------------------------------------------------------------
def _lockout_user(db_session, prefix: str) -> User:
    user = User(
        username=f"{prefix}-{int(time.time()*1000)}",
        password_hash=hash_password("correct-horse"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_lockout_trips_on_limit_and_refuses_correct_password(client, db_session):
    user = _lockout_user(db_session, "seq-lock")
    with _override_settings(admin_login_max_failures=3, admin_login_ip_max_failures=0):
        for _ in range(3):
            assert client.post(
                "/api/admin/login",
                json={"username": user.username, "password": "wrong"},
            ).status_code == 401
        r = client.post(
            "/api/admin/login",
            json={"username": user.username, "password": "correct-horse"},
        )
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 1


def test_success_releases_reserved_slot(client, db_session):
    user = _lockout_user(db_session, "seq-ok")
    with _override_settings(admin_login_max_failures=3, admin_login_ip_max_failures=0):
        for _ in range(2):
            client.post("/api/admin/login", json={"username": user.username, "password": "wrong"})
        assert client.post(
            "/api/admin/login",
            json={"username": user.username, "password": "correct-horse"},
        ).status_code == 200
    db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.lockout_until is None


def test_parallel_guesses_cannot_exceed_lockout_budget(client, db_session, monkeypatch):
    """With the lock check run before the (slow) verify and the failure
    counted after it, a burst of parallel requests would all pass the check
    and all have their guess verified. At most max_failures may reach the
    verifier."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from wsi_app.routers import admin_auth as admin_auth_router

    user = _lockout_user(db_session, "par-lock")
    real_verify = admin_auth_router.verify_password
    calls = []
    calls_lock = threading.Lock()

    def slow_verify(password, hashed):
        with calls_lock:
            calls.append(password)
        time.sleep(0.2)  # widen any check-then-count window
        return real_verify(password, hashed)

    monkeypatch.setattr(admin_auth_router, "verify_password", slow_verify)

    # Read the ORM attribute once, here: the worker threads must not touch the
    # test's Session (a lazy reload of an expired attribute from several
    # threads at once raises or, with some SQLAlchemy builds, crashes).
    username = user.username

    def attempt(i):
        return client.post(
            "/api/admin/login",
            json={"username": username, "password": f"guess-{i}"},
        ).status_code

    with _override_settings(admin_login_max_failures=3, admin_login_ip_max_failures=0):
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(attempt, range(10)))

    assert len(calls) <= 3, calls
    assert statuses.count(401) <= 3
    assert statuses.count(429) >= 7
    db_session.refresh(user)
    assert user.lockout_until is not None


# ---------------------------------------------------------------------------
# admin_register username validation
# ---------------------------------------------------------------------------
def test_register_rejects_empty_username(client, db_session):
    # admin_register is gated on ALLOW_ADMIN_REGISTER AND zero existing admins.
    db_session.execute(delete(User))
    db_session.commit()
    with _override_settings(allow_admin_register=True):
        for bad in ("", "   "):
            r = client.post(
                "/api/admin/register",
                json={"username": bad, "password": "longenoughpw"},
            )
            assert r.status_code == 422, (bad, r.text)
        # A valid username still works.
        r = client.post(
            "/api/admin/register",
            json={"username": "first-admin", "password": "longenoughpw"},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# login vs. concurrent credential change
# ---------------------------------------------------------------------------
def _race_user(db_session, name):
    user = User(
        username=f"{name}-{int(time.time()*1000)}",
        password_hash=hash_password("old-password"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _change_during_verify(monkeypatch, user_id, mutate):
    """Commit ``mutate(row)`` from another session while pbkdf2 is "running"."""
    from wsi_app.db import SessionLocal
    from wsi_app.routers import admin_auth

    real = admin_auth.verify_password

    def hooked(plain, hashed):
        ok = real(plain, hashed)
        other = SessionLocal()
        try:
            mutate(other.get(User, user_id))
            other.commit()
        finally:
            other.close()
        return ok

    monkeypatch.setattr(admin_auth, "verify_password", hooked)


def test_login_racing_a_password_change_gets_no_token(client, db_session, monkeypatch):
    """A password change (hash + token_version bump) committed between the
    verify and the success path must not hand the old-password prover a token
    for the NEW generation."""
    user = _race_user(db_session, "race-pw")

    def mutate(row):
        row.password_hash = hash_password("new-password")
        row.token_version = int(row.token_version) + 1

    _change_during_verify(monkeypatch, user.id, mutate)
    r = client.post(
        "/api/admin/login",
        json={"username": user.username, "password": "old-password"},
    )
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()


def test_login_racing_a_token_version_bump_gets_no_token(client, db_session, monkeypatch):
    user = _race_user(db_session, "race-tv")
    _change_during_verify(
        monkeypatch, user.id, lambda row: setattr(row, "token_version", int(row.token_version) + 1)
    )
    r = client.post(
        "/api/admin/login",
        json={"username": user.username, "password": "old-password"},
    )
    assert r.status_code == 401, r.text


def test_login_without_race_still_issues_current_generation_token(client, db_session):
    user = _race_user(db_session, "race-none")
    r = client.post(
        "/api/admin/login",
        json={"username": user.username, "password": "old-password"},
    )
    assert r.status_code == 200, r.text
    assert client.get(
        "/api/admin/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    ).status_code == 200


# --- a client cannot choose the string the throttles are keyed by -------------------
def test_a_forwarded_ip_header_that_is_not_an_address_is_ignored():
    from wsi_app import network

    # (an IPv6 zone id is the one place ipaddress takes free text after an address)
    for bad in ("mfa-setup:7", "reauth:1:abc", "known-device:3", "203.0.113.5, 10.0.0.1", "", "  ", "999.1.1.1", "<script>",
                "::1%mfa-setup:7 any text", "fe80::1%eth0", "::1%" + "x" * 5000):
        assert network.get_client_ip({"cf-connecting-ip": bad}, "127.0.0.1") == "127.0.0.1", bad
        assert network.get_client_ip({"x-real-ip": bad}, "127.0.0.1") == "127.0.0.1", bad


def test_a_bad_cf_header_falls_through_to_a_good_real_ip_header():
    from wsi_app import network

    assert network.get_client_ip({"cf-connecting-ip": "oops", "x-real-ip": "198.51.100.4"}, "127.0.0.1") == "198.51.100.4"


def test_real_addresses_are_still_used_as_sent():
    from wsi_app import network

    for good in ("203.0.113.9", "2001:db8::1", "::1", " 198.51.100.2 "):
        assert network.get_client_ip({"cf-connecting-ip": good}, "127.0.0.1") == good.strip()
    # (and only from a trusted hop)
    assert network.get_client_ip({"cf-connecting-ip": "203.0.113.9"}, "198.51.100.200") == "198.51.100.200"


def test_a_stranger_cannot_fill_another_accounts_mfa_setup_throttle(db_session, monkeypatch):
    """The throttle of enabling two-factor is keyed "mfa-setup:<id>" in the same
    table as client addresses; naming it through Cf-Connecting-IP would lock the
    account's owner out of turning two-factor on."""
    import os

    from fastapi.testclient import TestClient

    from wsi_app import mfa
    from wsi_app.auth import create_token, hash_password
    from wsi_app.main import app
    from wsi_app.models import User

    monkeypatch.setenv("WSI_MFA_KEY", "k" * 64)
    u = User(username="victim-" + os.urandom(3).hex(), password_hash=hash_password("Victim-password-1"),
             role="admin", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    peer = TestClient(app, client=("127.0.0.1", 50000))
    # (more attempts than the budget allows, whatever REAUTH_IP_MAX_FAILURES is set to;
    # the sign-in's own per-address limit is out of the way)
    with _override_settings(admin_login_ip_max_failures=10_000):
        for spelling in (f"mfa-setup:{u.id}", f"::1%mfa-setup:{u.id}"):
            for _ in range(settings.reauth_ip_max_failures + 2):
                r = peer.post("/api/admin/login", json={"username": "nobody", "password": "x"},
                              headers={"Cf-Connecting-IP": spelling})
                assert r.status_code == 401, r.text
    h = {"Authorization": f"Bearer {create_token(u)}"}
    c = TestClient(app)
    r = c.post("/api/admin/me/mfa/setup", json={"current_password": "Victim-password-1"}, headers=h)
    assert r.status_code == 200, r.text
    setup = r.json()
    r = c.post("/api/admin/me/mfa/confirm",
               json={"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], mfa.current_step())},
               headers=h)
    assert r.status_code == 200, r.text
