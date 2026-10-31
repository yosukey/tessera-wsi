"""Two-factor (TOTP) sign-in for staff accounts: mfa.py and routers/admin_mfa.py.

Covers the primitives (RFC 6238 codes, sealing under WSI_MFA_KEY, recovery
codes), enrolment, the two-step sign-in and its shared per-account budget,
turning the factor off (owner, admin reset, server CLI), and the fail-closed
behaviour without a key.
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from wsi_app import mfa
from wsi_app.app_auth import is_bypass_path
from wsi_app.auth import create_token, hash_password
from wsi_app.config import settings
from wsi_app.models import AuditLog, User

KEY = "k" * 64


@pytest.fixture(autouse=True)
def _mfa_key(monkeypatch):
    monkeypatch.setenv("WSI_MFA_KEY", KEY)


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


def _user(db_session, role="admin", password="pw-for-mfa-tests"):
    u = User(
        username=f"mfa-{role}-{os.urandom(4).hex()}",
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _h(user_or_token):
    tok = user_or_token if isinstance(user_or_token, str) else create_token(user_or_token)
    return {"Authorization": f"Bearer {tok}"}


def _enrol(client, user, password="pw-for-mfa-tests"):
    """Turn MFA on for ``user``; returns (secret, recovery_codes, token, step)."""
    r = client.post("/api/admin/me/mfa/setup", json={"current_password": password}, headers=_h(user))
    assert r.status_code == 200, r.text
    setup = r.json()
    step = mfa.current_step()
    r = client.post(
        "/api/admin/me/mfa/confirm",
        json={"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], step)},
        headers=_h(user),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return setup["secret"], body["recovery_codes"], body["access_token"], step


def _password_step(client, user, password="pw-for-mfa-tests"):
    return client.post("/api/admin/login", json={"username": user.username, "password": password})


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("t,code", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")])
def test_totp_matches_rfc6238_sha1_vectors(t, code):
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp_at(secret, t // 30) == code


def test_match_totp_window_and_replay():
    secret = mfa.new_secret()
    now = 1_700_000_000.0
    step = mfa.current_step(now)
    assert mfa.match_totp(secret, mfa.totp_at(secret, step), last_step=None, now=now) == step
    assert mfa.match_totp(secret, mfa.totp_at(secret, step - 1), last_step=None, now=now) == step - 1
    assert mfa.match_totp(secret, mfa.totp_at(secret, step + 2), last_step=None, now=now) is None
    # Spaces typed into the code are fine; an already-used step is not.
    spaced = mfa.totp_at(secret, step)[:3] + " " + mfa.totp_at(secret, step)[3:]
    assert mfa.match_totp(secret, spaced, last_step=None, now=now) == step
    assert mfa.match_totp(secret, mfa.totp_at(secret, step), last_step=step, now=now) is None


def test_seal_open_roundtrip_bound_to_user_and_key(monkeypatch):
    secret = mfa.new_secret()
    sealed = mfa.seal_secret(secret, 7)
    assert secret not in sealed
    assert mfa.open_secret(sealed, 7) == secret
    with pytest.raises(mfa.MfaSecretUnreadable):
        mfa.open_secret(sealed, 8)
    monkeypatch.setenv("WSI_MFA_KEY", "z" * 64)
    with pytest.raises(mfa.MfaSecretUnreadable):
        mfa.open_secret(sealed, 7)
    monkeypatch.setenv("WSI_MFA_KEY", "short")
    assert not mfa.is_configured()
    with pytest.raises(mfa.MfaKeyUnavailable):
        mfa.open_secret(sealed, 7)


def test_recovery_codes_single_use():
    codes, stored = mfa.new_recovery_codes()
    assert len(codes) == mfa.RECOVERY_CODE_COUNT
    assert all(c not in stored for c in codes)
    left = mfa.consume_recovery_code(stored, codes[3].lower().replace("-", " "))
    assert left is not None and mfa.recovery_codes_remaining(left) == len(codes) - 1
    assert mfa.consume_recovery_code(left, codes[3]) is None
    assert mfa.consume_recovery_code(stored, "123456") is None


def test_otpauth_uri():
    uri = mfa.otpauth_uri("ABC", "alice smith", "Lab WSI")
    assert uri.startswith("otpauth://totp/Lab%20WSI%3Aalice%20smith?")
    assert "secret=ABC" in uri and "issuer=Lab%20WSI" in uri


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["admin", "manager", "instructor"])
def test_enrolment_for_every_role(client, db_session, role):
    user = _user(db_session, role)
    me = client.get("/api/admin/me", headers=_h(user)).json()
    assert me["mfa"] == {"enabled": False, "enabled_at": None, "recovery_codes_remaining": 0, "available": True}
    old = _h(user)
    secret, codes, token, _ = _enrol(client, user)
    assert len(codes) == 10
    # Every other session ended; the one that enrolled got a fresh token.
    assert client.get("/api/admin/me", headers=old).status_code == 401
    me = client.get("/api/admin/me", headers=_h(token)).json()
    assert me["mfa"]["enabled"] is True and me["mfa"]["recovery_codes_remaining"] == 10
    db_session.expire_all()
    row = db_session.get(User, user.id)
    assert row.totp_secret_enc and secret not in row.totp_secret_enc
    assert db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.mfa.enable", AuditLog.actor_id == user.id)
    ).first()


def test_setup_requires_password_and_confirm_requires_code(client, db_session):
    user = _user(db_session)
    r = client.post("/api/admin/me/mfa/setup", json={"current_password": "wrong"}, headers=_h(user))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.wrongPassword"
    setup = client.post(
        "/api/admin/me/mfa/setup", json={"current_password": "pw-for-mfa-tests"}, headers=_h(user),
    ).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    r = client.post(
        "/api/admin/me/mfa/confirm",
        json={"setup_token": setup["setup_token"], "code": "000000"},
        headers=_h(user),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.invalidCode"
    db_session.expire_all()
    assert db_session.get(User, user.id).totp_secret_enc is None
    # Another account cannot confirm with this setup token.
    other = _user(db_session)
    r = client.post(
        "/api/admin/me/mfa/confirm",
        json={"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], mfa.current_step())},
        headers=_h(other),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.setupExpired"


def test_setup_refused_when_already_enabled(client, db_session):
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    r = client.post("/api/admin/me/mfa/setup", json={"current_password": "pw-for-mfa-tests"}, headers=_h(token))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Two-step sign-in
# ---------------------------------------------------------------------------
def test_login_without_mfa_is_unchanged(client, db_session):
    user = _user(db_session)
    r = _password_step(client, user)
    assert r.status_code == 200
    assert set(r.json()) == {"access_token"}


def test_login_with_totp(client, db_session):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    r = _password_step(client, user)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and "access_token" not in body
    # The intermediate token authenticates nothing.
    assert client.get("/api/admin/me", headers=_h(body["mfa_token"])).status_code == 401

    r = client.post("/api/admin/login/mfa", json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "adminMfa.err.invalidCode"

    code = mfa.totp_at(secret, step + 1)  # the enrolment spent `step`
    r = client.post("/api/admin/login/mfa", json={"mfa_token": body["mfa_token"], "code": code})
    assert r.status_code == 200, r.text
    assert client.get("/api/admin/me", headers=_h(r.json()["access_token"])).status_code == 200
    # The same code cannot be replayed, even with a fresh password step.
    body = _password_step(client, user).json()
    r = client.post("/api/admin/login/mfa", json={"mfa_token": body["mfa_token"], "code": code})
    assert r.status_code == 401
    assert db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.login.mfa_failure", AuditLog.target == user.username)
    ).first()


def test_login_with_recovery_code_single_use(client, db_session):
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 200
    assert r.json()["recovery_codes_remaining"] == 9
    token = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 401


def test_password_and_code_share_one_budget(client, db_session):
    """A correct password must not reset the per-account budget for a
    two-factor account, or the password's holder could guess codes forever:
    with a fresh password step before every guess, the wrong codes still add
    up to the lockout."""
    user = _user(db_session)
    _enrol(client, user)
    with _override_settings(admin_login_max_failures=4):
        statuses = []
        for _ in range(6):
            r = _password_step(client, user)
            if r.status_code != 200:
                statuses.append(("password", r.status_code))
                break
            r = client.post(
                "/api/admin/login/mfa", json={"mfa_token": r.json()["mfa_token"], "code": "000000"},
            )
            statuses.append(("code", r.status_code))
        # Four wrong codes trip the lockout; then even the password is refused.
        assert statuses == [("code", 401)] * 4 + [("password", 429)], statuses


def test_success_clears_the_budget(client, db_session):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    assert client.post(
        "/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)},
    ).status_code == 200
    db_session.expire_all()
    row = db_session.get(User, user.id)
    assert row.failed_login_count == 0 and row.lockout_until is None


def test_pending_token_void_after_reset(client, db_session, admin_user):
    user = _user(db_session, "manager")
    _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    r = client.post(
        f"/api/admin/users/{user.id}/mfa/reset", json={"current_password": "x"}, headers=_h(admin_user),
    )
    assert r.status_code == 200
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "000000"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "adminMfa.err.sessionExpired"
    # The password alone signs in now.
    assert "access_token" in _password_step(client, user).json()


def test_garbage_pending_token(client):
    r = client.post("/api/admin/login/mfa", json={"mfa_token": "nope", "code": "123456"})
    assert r.status_code == 401


def test_code_step_is_gated_like_the_password_step():
    # Inside the app-access gate, like /api/admin/login (test_admin_login_gate.py).
    assert not is_bypass_path("/api/admin/login/mfa")


# ---------------------------------------------------------------------------
# Turning it off
# ---------------------------------------------------------------------------
def test_owner_disable_needs_password_and_code(client, db_session):
    user = _user(db_session)
    secret, _, token, step = _enrol(client, user)
    r = client.post(
        "/api/admin/me/mfa/disable",
        json={"current_password": "wrong", "code": mfa.totp_at(secret, step + 1)},
        headers=_h(token),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.wrongPassword"
    r = client.post(
        "/api/admin/me/mfa/disable",
        json={"current_password": "pw-for-mfa-tests", "code": "000000"},
        headers=_h(token),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.invalidCode"
    r = client.post(
        "/api/admin/me/mfa/disable",
        json={"current_password": "pw-for-mfa-tests", "code": mfa.totp_at(secret, step + 1)},
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["mfa"]["enabled"] is False
    assert client.get("/api/admin/me", headers=_h(token)).status_code == 401
    assert client.get("/api/admin/me", headers=_h(r.json()["access_token"])).status_code == 200
    db_session.expire_all()
    row = db_session.get(User, user.id)
    assert row.totp_secret_enc is None and row.mfa_recovery_codes is None


def test_owner_disable_with_recovery_code(client, db_session):
    user = _user(db_session)
    _, codes, token, _ = _enrol(client, user)
    r = client.post(
        "/api/admin/me/mfa/disable",
        json={"current_password": "pw-for-mfa-tests", "code": codes[5]},
        headers=_h(token),
    )
    assert r.status_code == 200


def test_disable_code_guesses_are_bounded_despite_correct_password(client, db_session):
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    with _override_settings(admin_login_max_failures=3):
        for _ in range(3):
            r = client.post(
                "/api/admin/me/mfa/disable",
                json={"current_password": "pw-for-mfa-tests", "code": "000000"},
                headers=_h(token),
            )
            assert r.status_code == 400
        r = client.post(
            "/api/admin/me/mfa/disable",
            json={"current_password": "pw-for-mfa-tests", "code": "000000"},
            headers=_h(token),
        )
        assert r.status_code == 429


def test_regenerate_recovery_codes(client, db_session):
    user = _user(db_session)
    _, codes, token, _ = _enrol(client, user)
    r = client.post(
        "/api/admin/me/mfa/recovery-codes",
        json={"current_password": "pw-for-mfa-tests", "code": codes[0]},
        headers=_h(token),
    )
    assert r.status_code == 200
    fresh = r.json()["recovery_codes"]
    assert len(fresh) == 10 and not set(fresh) & set(codes)
    assert r.json()["mfa"]["recovery_codes_remaining"] == 10
    # Sessions opened with an old (maybe leaked) code end; this one goes on
    # with the fresh token it was handed.
    assert client.get("/api/admin/me", headers=_h(token)).status_code == 401
    token = r.json()["access_token"]
    # The old set is gone; the session that did it is still valid.
    login = _password_step(client, user).json()["mfa_token"]
    assert client.post("/api/admin/login/mfa", json={"mfa_token": login, "code": codes[1]}).status_code == 401
    login = _password_step(client, user).json()["mfa_token"]
    assert client.post("/api/admin/login/mfa", json={"mfa_token": login, "code": fresh[1]}).status_code == 200
    assert client.get("/api/admin/me", headers=_h(token)).status_code == 200


def test_admin_reset(client, db_session, admin_user):
    member = _user(db_session, "instructor")
    _, _, member_token, _ = _enrol(client, member)
    users = client.get("/api/admin/users", headers=_h(admin_user)).json()
    assert next(u for u in users if u["id"] == member.id)["mfa_enabled"] is True

    # Not for non-admins, not for oneself, not without the admin's password.
    manager = _user(db_session, "manager")
    assert client.post(
        f"/api/admin/users/{member.id}/mfa/reset", json={"current_password": "pw-for-mfa-tests"},
        headers=_h(manager),
    ).status_code == 403
    r = client.post(
        f"/api/admin/users/{admin_user.id}/mfa/reset", json={"current_password": "x"}, headers=_h(admin_user),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.cannotResetSelf"
    r = client.post(
        f"/api/admin/users/{member.id}/mfa/reset", json={"current_password": "wrong"}, headers=_h(admin_user),
    )
    assert r.status_code == 400

    r = client.post(
        f"/api/admin/users/{member.id}/mfa/reset", json={"current_password": "x"}, headers=_h(admin_user),
    )
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    assert client.get("/api/admin/me", headers=_h(member_token)).status_code == 401
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.mfa.reset", AuditLog.target == str(member.id))
    ).scalar_one()
    assert entry.actor_id == admin_user.id
    assert json.loads(entry.details)["target_username"] == member.username


def test_admin_can_reset_another_admin(client, db_session, admin_user):
    other = _user(db_session, "admin")
    _enrol(client, other)
    r = client.post(
        f"/api/admin/users/{other.id}/mfa/reset", json={"current_password": "x"}, headers=_h(admin_user),
    )
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False


def test_cli_reset(db_session, client):
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    spec = importlib.util.spec_from_file_location(
        "reset_mfa", Path(__file__).resolve().parents[1] / "tools" / "reset_mfa.py",
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    db_path = os.environ["DATABASE_URL"][len("sqlite:///"):]
    assert tool.reset(db_path, "no-such-user") == 1
    assert tool.reset(db_path, user.username) == 0
    db_session.expire_all()
    assert db_session.get(User, user.id).totp_secret_enc is None
    assert client.get("/api/admin/me", headers=_h(token)).status_code == 401
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.mfa.reset", AuditLog.target == str(user.id))
    ).scalar_one()
    # Filed under System like other actor-less events, marked as the CLI's.
    assert entry.actor_username == "" and entry.actor_id is None and entry.occurred_at is not None
    assert json.loads(entry.details)["via"] == "cli"
    # A second run is a no-op.
    assert tool.reset(db_path, user.username) == 0


# ---------------------------------------------------------------------------
# Without WSI_MFA_KEY
# ---------------------------------------------------------------------------
def test_without_key_enrolment_is_refused(client, db_session, monkeypatch):
    monkeypatch.delenv("WSI_MFA_KEY")
    user = _user(db_session)
    assert client.get("/api/admin/me", headers=_h(user)).json()["mfa"]["available"] is False
    r = client.post("/api/admin/me/mfa/setup", json={"current_password": "pw-for-mfa-tests"}, headers=_h(user))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "adminMfa.err.notConfigured"


def test_without_key_enrolled_login_fails_closed_without_charging(client, db_session, monkeypatch):
    user = _user(db_session)
    _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    db_session.expire_all()
    charged = db_session.get(User, user.id).failed_login_count
    monkeypatch.delenv("WSI_MFA_KEY")
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "123456"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "adminMfa.err.keyUnavailable"
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == charged


# ---------------------------------------------------------------------------
# Edge cases: code input, lockout accounting, recovery codes, CLI, races
# ---------------------------------------------------------------------------
def test_full_width_digits_are_accepted_not_a_500(client, db_session):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    wide = mfa.totp_at(secret, step + 1).translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    token = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": wide})
    assert r.status_code == 200, r.text
    token = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "１２３"})
    assert r.status_code == 401


def test_correct_password_at_the_limit_still_allows_the_code(client, db_session):
    """The password attempt that would trip the lockout is refunded when it is
    correct, so the code step can still be redeemed."""
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    with _override_settings(admin_login_max_failures=5):
        for _ in range(4):
            assert _password_step(client, user, password="wrong").status_code == 401
        token = _password_step(client, user).json()["mfa_token"]
        r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)})
        assert r.status_code == 200, r.text


def test_abandoned_sign_ins_do_not_accumulate(client, db_session):
    user = _user(db_session)
    _enrol(client, user)
    for _ in range(10):
        assert _password_step(client, user).json()["mfa_required"] is True
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0


def test_max_failures_one_still_lets_two_factor_accounts_in(client, db_session):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    with _override_settings(admin_login_max_failures=1):
        token = _password_step(client, user).json()["mfa_token"]
        r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)})
        assert r.status_code == 200, r.text


def test_pending_token_is_single_use(client, db_session):
    user = _user(db_session)
    secret, codes, _, step = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    assert client.post(
        "/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)},
    ).status_code == 200
    # The same pending token, with a different valid factor, buys nothing more.
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "adminMfa.err.sessionExpired"


def test_enabled_at_carries_an_offset(client, db_session):
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    enabled_at = client.get("/api/admin/me", headers=_h(token)).json()["mfa"]["enabled_at"]
    assert enabled_at.endswith("+00:00"), enabled_at


def test_otpauth_uri_spells_the_issuer_once():
    uri = mfa.otpauth_uri("ABC", "alice", "Tessera Lab")
    assert "issuer=Tessera%20Lab" in uri and "+" not in uri


def test_cli_finds_the_database_in_wsi_env(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "reset_mfa", Path(__file__).resolve().parents[1] / "tools" / "reset_mfa.py",
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    env = tmp_path / "wsi.env"
    env.write_text(
        "DATABASE_URL=sqlite:////var/lib/wsi/old.db\n"
        "#DATABASE_URL=sqlite:////commented.db\n"
        "DATABASE_URL='sqlite:////mnt/data/wsi.db'\n"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WSI_ENV_FILE", str(env))
    assert tool._default_db() == "/mnt/data/wsi.db"
    monkeypatch.setenv("WSI_ENV_FILE", str(tmp_path / "missing.env"))
    assert tool._default_db() == tool.DEFAULT_DB


def test_recovery_code_typed_with_an_ime():
    codes, stored = mfa.new_recovery_codes()
    wide = codes[2].translate(str.maketrans(
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-",
        "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺー",
    ))
    assert mfa.consume_recovery_code(stored, wide) is not None
    assert mfa.consume_recovery_code(stored, codes[2].replace("-", "−")) is not None


def test_cli_refuses_to_guess_when_wsi_env_is_unreadable(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "reset_mfa", Path(__file__).resolve().parents[1] / "tools" / "reset_mfa.py",
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WSI_ENV_FILE", str(tmp_path))  # a directory: open() fails, not "missing"
    assert tool.main(["alice"]) == 2
    assert "pass --db" in capsys.readouterr().err


def test_code_guesses_on_the_account_panel_survive_a_correct_password_elsewhere(client, db_session):
    """A correct password on another re-auth endpoint must not hand the
    holder of a token and the password a fresh budget of code guesses."""
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    with _override_settings(admin_login_max_failures=3):
        for _ in range(3):
            r = client.post(
                "/api/admin/me/mfa/disable",
                json={"current_password": "pw-for-mfa-tests", "code": "000000"},
                headers=_h(token),
            )
            assert r.status_code == 400
            # The password alone, accepted elsewhere (here: nothing to update).
            client.patch("/api/admin/me", json={"current_password": "pw-for-mfa-tests"}, headers=_h(token))
        r = client.post(
            "/api/admin/me/mfa/disable",
            json={"current_password": "pw-for-mfa-tests", "code": "000000"},
            headers=_h(token),
        )
        assert r.status_code == 429


def test_regenerate_refused_for_a_session_revoked_in_flight(client, db_session, monkeypatch):
    user = _user(db_session)
    _, codes, token, _ = _enrol(client, user)
    import wsi_app.routers.admin_mfa as am
    real = am._reauth_with_code

    def revoke_then_check(*a, **kw):
        out = real(*a, **kw)
        # The session is revoked (e.g. a password change in another tab)
        # between the checks and the write.
        from wsi_app.db import SessionLocal
        s = SessionLocal()
        s.execute(am.update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
        s.commit()
        s.close()
        return out

    monkeypatch.setattr(am, "_reauth_with_code", revoke_then_check)
    r = client.post(
        "/api/admin/me/mfa/recovery-codes",
        json={"current_password": "pw-for-mfa-tests", "code": codes[0]},
        headers=_h(token),
    )
    assert r.status_code == 409
    # The correct code's slot was handed back, not left as a failure.
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0


def test_wrong_key_refuses_recovery_codes_too_without_charging(client, db_session, monkeypatch):
    """A secret that does not open means (almost always) the wrong
    WSI_MFA_KEY, under which recovery codes cannot match either: refuse with
    503 before the account's budget is charged, as for a TOTP code."""
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    db_session.expire_all()
    charged = db_session.get(User, user.id).failed_login_count
    monkeypatch.setenv("WSI_MFA_KEY", "w" * 64)
    for _ in range(settings.admin_login_max_failures + 1):
        r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": codes[0]})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "adminMfa.err.keyUnavailable"
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == charged


def test_correct_password_of_a_two_factor_account_is_audited(client, db_session):
    user = _user(db_session)
    _enrol(client, user)
    _password_step(client, user)
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.login.mfa_required", AuditLog.target == user.username)
    ).scalar_one()
    # Not attributed to the account: the owner has not been proved yet.
    assert row.actor_id is None


def _reset_tool():
    spec = importlib.util.spec_from_file_location(
        "reset_mfa", Path(__file__).resolve().parents[1] / "tools" / "reset_mfa.py",
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


@pytest.mark.parametrize("url,expected", [
    ("sqlite:////mnt/data/wsi.db", "/mnt/data/wsi.db"),
    ("sqlite+pysqlite:////srv/wsi/wsi.db", "/srv/wsi/wsi.db"),
    ("sqlite:////var/lib/wsi/wsi.db?timeout=30", "/var/lib/wsi/wsi.db"),
])
def test_cli_reads_database_urls_like_the_app(monkeypatch, url, expected):
    tool = _reset_tool()
    monkeypatch.setenv("DATABASE_URL", url)
    assert tool._default_db() == expected


@pytest.mark.parametrize("url", ["sqlite:///relative.db", "postgresql://x/y", "sqlite://"])
def test_cli_refuses_to_guess_from_an_odd_database_url(monkeypatch, url, capsys):
    tool = _reset_tool()
    monkeypatch.setenv("DATABASE_URL", url)
    assert tool.main(["alice"]) == 2
    assert "pass --db" in capsys.readouterr().err


def test_cli_refuses_root_from_a_copy_the_service_user_owns(tmp_path, monkeypatch, capsys):
    tool = _reset_tool()
    db = tmp_path / "wsi.db"
    db.write_bytes(b"")
    monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
    real_stat = tool.os.stat

    class _St:
        def __init__(self, st, uid):
            self._st, self.st_uid = st, uid

        def __getattr__(self, name):
            return getattr(self._st, name)

    # The database and this script both "belong" to uid 999 (the service user).
    monkeypatch.setattr(tool.os, "stat", lambda p, *a, **k: _St(real_stat(p, *a, **k), 999))
    assert tool.reset(str(db), "alice") == 2
    assert "own clone" in capsys.readouterr().err


def test_fresh_tokens_buy_no_fresh_code_guesses(client, db_session):
    """Rotating the token (a password change hands back a new one) must not
    reset the panel's budget of wrong codes."""
    user = _user(db_session)
    _, _, token, _ = _enrol(client, user)
    pw = "pw-for-mfa-tests"
    with _override_settings(admin_login_max_failures=3):
        for i in range(3):
            r = client.post(
                "/api/admin/me/mfa/disable", json={"current_password": pw, "code": "000000"}, headers=_h(token),
            )
            assert r.status_code == 400
            new_pw = f"rotated-password-{i}"
            r = client.patch(
                "/api/admin/me", json={"current_password": pw, "new_password": new_pw}, headers=_h(token),
            )
            assert r.status_code == 200
            token, pw = r.json()["access_token"], new_pw
        r = client.post(
            "/api/admin/me/mfa/disable", json={"current_password": pw, "code": "000000"}, headers=_h(token),
        )
        assert r.status_code == 429


def test_panel_code_guesses_count_against_the_sign_in_lockout(client, db_session):
    """Wrong codes on the account panel spend the same database-backed budget
    as the sign-in's code step, so the panel is no softer door; a spent code
    there releases it."""
    user = _user(db_session)
    secret, _, token, step = _enrol(client, user)
    with _override_settings(admin_login_max_failures=3):
        for _ in range(2):
            assert client.post(
                "/api/admin/me/mfa/recovery-codes",
                json={"current_password": "pw-for-mfa-tests", "code": "000000"}, headers=_h(token),
            ).status_code == 400
        db_session.expire_all()
        assert db_session.get(User, user.id).failed_login_count == 2
        r = client.post(
            "/api/admin/me/mfa/recovery-codes",
            json={"current_password": "pw-for-mfa-tests", "code": mfa.totp_at(secret, step + 1)},
            headers=_h(token),
        )
        assert r.status_code == 200, r.text
        db_session.expire_all()
        assert db_session.get(User, user.id).failed_login_count == 0


def test_confirm_guesses_are_bounded(client, db_session):
    user = _user(db_session)
    setup = client.post(
        "/api/admin/me/mfa/setup", json={"current_password": "pw-for-mfa-tests"}, headers=_h(user),
    ).json()
    with _override_settings(reauth_ip_max_failures=3):
        for _ in range(3):
            assert client.post(
                "/api/admin/me/mfa/confirm", json={"setup_token": setup["setup_token"], "code": "000000"},
                headers=_h(user),
            ).status_code == 400
        r = client.post(
            "/api/admin/me/mfa/confirm",
            json={"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], mfa.current_step())},
            headers=_h(user),
        )
        assert r.status_code == 429
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0


def test_admin_reset_lifts_a_lockout(client, db_session, admin_user):
    member = _user(db_session, "instructor")
    _enrol(client, member)
    with _override_settings(admin_login_max_failures=2):
        token = _password_step(client, member).json()["mfa_token"]
        for _ in range(2):
            client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "000000"})
        assert _password_step(client, member).status_code == 429
        assert client.post(
            f"/api/admin/users/{member.id}/mfa/reset", json={"current_password": "x"}, headers=_h(admin_user),
        ).status_code == 200
        r = _password_step(client, member)
        assert r.status_code == 200 and "access_token" in r.json()


def test_code_step_survives_the_account_being_deleted_meanwhile(client, db_session, monkeypatch):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    import wsi_app.routers.admin_mfa as am
    real = am.reserve_login_attempt

    def charge_then_delete(db, user_id, now):
        out = real(db, user_id, now)
        from wsi_app.db import SessionLocal
        s = SessionLocal()
        s.delete(s.get(User, user_id))
        s.commit()
        s.close()
        return out

    monkeypatch.setattr(am, "reserve_login_attempt", charge_then_delete)
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)})
    assert r.status_code == 401


def test_pre_sign_in_rows_show_under_the_account_in_history(client, db_session, admin_user):
    user = _user(db_session)
    _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "000000"})
    r = client.get(f"/api/admin/history?actor={user.username}", headers=_h(admin_user))
    assert r.status_code == 200, r.text
    actions = {row["action"] for row in r.json()["items"]}
    assert {"admin.login.mfa_required", "admin.login.mfa_failure"} <= actions


def _spend_all_but_one(client, user, codes):
    """Sign in with every recovery code but the last; returns the last."""
    for c in codes[:-1]:
        t = _password_step(client, user).json()["mfa_token"]
        assert client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": c}).status_code == 200
    return codes[-1]


def test_last_recovery_code_session_can_replace_the_codes(client, db_session):
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    last = _spend_all_but_one(client, user, codes)
    t = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": last})
    assert r.status_code == 200 and r.json()["recovery_codes_remaining"] == 0
    session = r.json()["access_token"]
    # No code is left to give: this session may replace them with the password.
    r = client.post(
        "/api/admin/me/mfa/recovery-codes",
        json={"current_password": "pw-for-mfa-tests", "code": ""}, headers=_h(session),
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["recovery_codes"]) == 10
    # The fresh token carries no amr: the shortcut was for the one sign-in.
    r2 = client.post(
        "/api/admin/me/mfa/recovery-codes",
        json={"current_password": "pw-for-mfa-tests", "code": ""}, headers=_h(r.json()["access_token"]),
    )
    assert r2.status_code == 400


def test_the_shortcut_needs_no_codes_left(client, db_session):
    """Even a session from the last code loses the shortcut once codes exist
    again (e.g. replaced from another session without revoking this one)."""
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    last = _spend_all_but_one(client, user, codes)
    t = _password_step(client, user).json()["mfa_token"]
    session = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": last}).json()["access_token"]
    _, stored = mfa.new_recovery_codes()
    db_session.expire_all()
    row = db_session.get(User, user.id)
    row.mfa_recovery_codes = stored  # codes exist; token_version untouched
    db_session.commit()
    r = client.post(
        "/api/admin/me/mfa/recovery-codes",
        json={"current_password": "pw-for-mfa-tests", "code": ""}, headers=_h(session),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.codeRequired"


def test_no_code_shortcut_for_a_session_not_from_a_recovery_code(client, db_session):
    user = _user(db_session)
    secret, codes, _, step = _enrol(client, user)
    _spend_all_but_one(client, user, codes)
    t = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": codes[-1]})
    assert r.status_code == 200
    # A session that signed in with an authenticator code gets no shortcut,
    # even with no recovery code left.
    t = _password_step(client, user).json()["mfa_token"]
    r = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": mfa.totp_at(secret, step + 1)})
    totp_session = r.json()["access_token"]
    r = client.post(
        "/api/admin/me/mfa/disable",
        json={"current_password": "pw-for-mfa-tests", "code": ""}, headers=_h(totp_session),
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "adminMfa.err.codeRequired"
    # ... and an empty code costs nothing against the sign-in budget.
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0


def test_only_the_sign_in_with_the_last_code_gets_the_shortcut(client, db_session):
    import jwt as _jwt
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    t = _password_step(client, user).json()["mfa_token"]
    early = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": codes[0]}).json()["access_token"]
    assert "amr" not in _jwt.decode(early, options={"verify_signature": False})
    last = _spend_all_but_one(client, user, codes[1:])
    t = _password_step(client, user).json()["mfa_token"]
    final = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": last}).json()["access_token"]
    assert _jwt.decode(final, options={"verify_signature": False})["amr"] == "recovery"
    # The early session gets nothing although no code is left now.
    r = client.post(
        "/api/admin/me/mfa/disable", json={"current_password": "pw-for-mfa-tests", "code": ""}, headers=_h(early),
    )
    assert r.status_code == 400


def test_the_shortcut_survives_a_password_change(client, db_session):
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    last = _spend_all_but_one(client, user, codes)
    t = _password_step(client, user).json()["mfa_token"]
    session = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": last}).json()["access_token"]
    r = client.patch(
        "/api/admin/me", json={"current_password": "pw-for-mfa-tests", "new_password": "a-new-password"},
        headers=_h(session),
    )
    session = r.json()["access_token"]
    r = client.post(
        "/api/admin/me/mfa/recovery-codes", json={"current_password": "a-new-password", "code": ""},
        headers=_h(session),
    )
    assert r.status_code == 200, r.text


def test_the_shortcut_waits_out_a_lockout_and_leaves_the_count(client, db_session):
    user = _user(db_session)
    _, codes, _, _ = _enrol(client, user)
    last = _spend_all_but_one(client, user, codes)
    t = _password_step(client, user).json()["mfa_token"]
    session = client.post("/api/admin/login/mfa", json={"mfa_token": t, "code": last}).json()["access_token"]
    with _override_settings(admin_login_max_failures=3):
        # Wrong passwords from someone else - a browser that never signed in as
        # this account, so it has no known-device cookie - lock the account.
        from fastapi.testclient import TestClient
        from wsi_app.main import app
        stranger = TestClient(app)
        for _ in range(3):
            _password_step(stranger, user, password="wrong")
        r = client.post(
            "/api/admin/me/mfa/recovery-codes", json={"current_password": "pw-for-mfa-tests", "code": ""},
            headers=_h(session),
        )
        assert r.status_code == 429
        db_session.expire_all()
        assert db_session.get(User, user.id).lockout_until is not None


def test_a_lost_race_at_the_code_step_is_not_a_wrong_guess(client, db_session, monkeypatch):
    user = _user(db_session)
    secret, _, _, step = _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    import wsi_app.routers.admin_mfa as am
    real = am._match_code

    def spent_meanwhile(code, secret_, state):
        out = real(code, secret_, state)
        from wsi_app.db import SessionLocal
        s = SessionLocal()
        s.execute(am.update(User).where(User.id == user.id).values(totp_last_step=step + 5))
        s.commit()
        s.close()
        return out

    monkeypatch.setattr(am, "_match_code", spent_meanwhile)
    r = client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": mfa.totp_at(secret, step + 1)})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "adminMfa.err.sessionExpired"
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0
    # On the record, but marked as stale rather than a wrong code.
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin.login.mfa_failure", AuditLog.target == user.username)
    ).scalar_one()
    assert json.loads(row.details).get("stale") is True


def test_a_409_after_tripping_the_lockout_lifts_it(client, db_session, monkeypatch):
    user = _user(db_session)
    _, codes, token, _ = _enrol(client, user)
    with _override_settings(admin_login_max_failures=3):
        for _ in range(2):  # someone else's wrong passwords: one short of the limit
            assert _password_step(client, user, password="wrong").status_code == 401
        import wsi_app.routers.admin_mfa as am
        real = am._reauth_with_code

        def then_changed(*a, **kw):
            out = real(*a, **kw)
            from wsi_app.db import SessionLocal
            s = SessionLocal()
            s.execute(am.update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
            s.commit()
            s.close()
            return out

        monkeypatch.setattr(am, "_reauth_with_code", then_changed)
        r = client.post(
            "/api/admin/me/mfa/disable",
            json={"current_password": "pw-for-mfa-tests", "code": codes[0]}, headers=_h(token),
        )
        assert r.status_code == 409
        db_session.expire_all()
        row = db_session.get(User, user.id)
        assert row.lockout_until is None and row.failed_login_count == 2


def test_a_tripped_refund_does_not_undo_a_concurrent_success(db_session):
    from wsi_app.routers.admin_auth import refund_login_attempt
    user = _user(db_session)
    # A concurrent success already reset count and lockout.
    user.failed_login_count, user.lockout_until = 0, None
    db_session.commit()
    with _override_settings(admin_login_max_failures=5):
        refund_login_attempt(db_session, user.id, tripped=True)
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_count == 0


def test_cli_reset_clears_what_the_admin_reset_clears():
    """The stdlib-only CLI cannot import the app, so it repeats the reset as
    SQL: keep the two in step."""
    import re
    from wsi_app.routers.admin_mfa import _MFA_CLEARED
    src = (Path(__file__).resolve().parents[1] / "tools" / "reset_mfa.py").read_text()
    # The one UPDATE statement (adjacent string literals joined).
    m = re.search(r'"(UPDATE users SET .*?WHERE id = \?)",', re.sub(r'"\s*\n\s*"', "", src), re.S)
    assert m, "reset UPDATE not found"
    sets = dict(
        (k.strip(), v.strip())
        for k, v in (a.split("=", 1) for a in m.group(1)[len("UPDATE users SET "):].split(" WHERE")[0].split(","))
    )
    expected = {**{c: "NULL" for c in _MFA_CLEARED}, "failed_login_count": "0",
                "lockout_until": "NULL", "token_version": "token_version + 1"}
    assert sets == expected


def test_key_failure_is_logged_once_not_per_attempt(client, db_session, monkeypatch, caplog):
    import logging
    import wsi_app.routers.admin_mfa as am
    monkeypatch.setattr(am, "_KEY_ERROR_LOGGED", {})
    user = _user(db_session)
    _enrol(client, user)
    token = _password_step(client, user).json()["mfa_token"]
    monkeypatch.setenv("WSI_MFA_KEY", "w" * 64)
    with caplog.at_level(logging.ERROR, logger="wsi_app.admin_mfa"):
        for _ in range(5):
            assert client.post("/api/admin/login/mfa", json={"mfa_token": token, "code": "123456"}).status_code == 503
    assert sum("does not open" in r.getMessage() for r in caplog.records) == 1


@contextlib.contextmanager
def _sign_out_everywhere_once(user_id, committed_tv):
    """Commit a sign-out-everywhere (token_version bump) right after the
    request's own change to ``committed_tv`` commits, before anything reads the
    row back. Yields a list that holds True once it has run."""
    from sqlalchemy import event, update

    from wsi_app.db import SessionLocal

    raced = []

    def bump(_session):
        if raced:
            return
        other = SessionLocal()
        try:
            if other.get(User, user_id).token_version != committed_tv:
                return
            raced.append(True)
            other.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))
            other.commit()
        finally:
            other.close()

    event.listen(SessionLocal, "after_commit", bump)
    try:
        yield raced
    finally:
        event.remove(SessionLocal, "after_commit", bump)


@pytest.mark.parametrize("endpoint", ["confirm", "disable", "recovery-codes"])
def test_returned_token_does_not_outlive_a_sign_out_committed_meanwhile(client, db_session, endpoint):
    """Each of these hands back a fresh token for the generation its own change
    committed; a sign-out-everywhere landing just after that commit must end
    the returned token too, not be read back into it."""
    user = _user(db_session)
    if endpoint == "confirm":
        r = client.post("/api/admin/me/mfa/setup", json={"current_password": "pw-for-mfa-tests"}, headers=_h(user))
        assert r.status_code == 200, r.text
        setup = r.json()
        url = "/api/admin/me/mfa/confirm"
        body = {"setup_token": setup["setup_token"], "code": mfa.totp_at(setup["secret"], mfa.current_step())}
        token = create_token(user)
    else:
        _secret, codes, token, _step = _enrol(client, user)
        url = f"/api/admin/me/mfa/{endpoint}"
        body = {"current_password": "pw-for-mfa-tests", "code": codes[0]}
    db_session.expire_all()
    tv = db_session.get(User, user.id).token_version

    with _sign_out_everywhere_once(user.id, tv + 1) as raced:
        r = client.post(url, json=body, headers=_h(token))
    assert r.status_code == 200, r.text
    assert raced
    assert client.get("/api/admin/me", headers=_h(r.json()["access_token"])).status_code == 401
