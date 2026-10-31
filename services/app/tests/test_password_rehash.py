"""PBKDF2 work factor: the production default, and the in-place upgrade of a
hash made with fewer rounds when its owner next signs in.

The suite runs at WSI_PASSWORD_HASH_ROUNDS=1000 (conftest); the hashes below
are made at 500 rounds to stand in for "an older, cheaper hash".
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from passlib.hash import pbkdf2_sha256
from sqlalchemy import select

from wsi_app import app_auth
from wsi_app.auth import create_token, hash_password, pwd_context, rehash_if_outdated
from wsi_app.models import AuthUser, User

_APP_DIR = Path(__file__).resolve().parents[1]


def _old_hash(password: str) -> str:
    return pbkdf2_sha256.using(rounds=500).hash(password)


def _rounds(pw_hash: str) -> int:
    return pbkdf2_sha256.from_string(pw_hash).rounds


def test_default_work_factor_is_600k_rounds():
    env = {k: v for k, v in os.environ.items() if k != "WSI_PASSWORD_HASH_ROUNDS"}
    out = subprocess.run(
        [sys.executable, "-c",
         "from wsi_app import auth; print(auth.PASSWORD_HASH_ROUNDS)"],
        cwd=_APP_DIR, env={**env, "PYTHONPATH": str(_APP_DIR)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "600000"


def test_a_too_small_work_factor_refuses_to_start():
    env = {**os.environ, "WSI_PASSWORD_HASH_ROUNDS": "999", "PYTHONPATH": str(_APP_DIR)}
    res = subprocess.run(
        [sys.executable, "-c", "import wsi_app.auth"],
        cwd=_APP_DIR, env=env, capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "WSI_PASSWORD_HASH_ROUNDS" in res.stderr


def test_new_hashes_use_the_configured_rounds():
    assert _rounds(hash_password("anything-long-enough")) == 1000


def test_rehash_if_outdated_only_upgrades_cheaper_hashes():
    old = _old_hash("pw-12345678")
    upgraded = rehash_if_outdated("pw-12345678", old)
    assert upgraded is not None and _rounds(upgraded) == 1000
    assert pwd_context.verify("pw-12345678", upgraded)
    assert rehash_if_outdated("pw-12345678", upgraded) is None
    assert rehash_if_outdated("pw-12345678", "not-a-hash") is None


def _staff(db_session, password: str) -> User:
    user = User(
        username=f"rehash-{int(time.time() * 1000)}",
        password_hash=_old_hash(password),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_dashboard_login_upgrades_an_old_hash_without_signing_anyone_out(client, db_session):
    user = _staff(db_session, "correct-horse-1")
    earlier = create_token(user)
    tv = user.token_version

    r = client.post("/api/admin/login", json={"username": user.username, "password": "correct-horse-1"})
    assert r.status_code == 200, r.text

    db_session.expire_all()
    row = db_session.get(User, user.id)
    assert _rounds(row.password_hash) == 1000
    assert row.token_version == tv
    # A session minted before the upgrade is still good.
    assert client.get("/api/admin/me", headers={"Authorization": f"Bearer {earlier}"}).status_code == 200
    # And the new hash is what signs in next time.
    r2 = client.post("/api/admin/login", json={"username": user.username, "password": "correct-horse-1"})
    assert r2.status_code == 200


def test_a_wrong_password_leaves_the_old_hash_alone(client, db_session):
    user = _staff(db_session, "correct-horse-2")
    before = user.password_hash
    r = client.post("/api/admin/login", json={"username": user.username, "password": "wrong-password"})
    assert r.status_code == 401
    db_session.expire_all()
    assert db_session.get(User, user.id).password_hash == before


def test_viewer_account_hash_is_upgraded_and_the_new_cookie_fingerprint_follows_it(db_session):
    db_session.add(AuthUser(username="rehash-viewer", password_hash=_old_hash("viewer-pass-1")))
    db_session.commit()
    old_fp = app_auth.basic_credential_fingerprint(
        db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-viewer")).scalar_one()
    )

    fp = app_auth.verify_basic_credential("rehash-viewer", "viewer-pass-1", db_session)

    stored = db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-viewer")).scalar_one()
    assert _rounds(stored) == 1000
    assert fp == app_auth.basic_credential_fingerprint(stored)
    assert fp != old_fp
    # A second sign-in verifies against the upgraded hash and changes nothing.
    assert app_auth.verify_basic_credential("rehash-viewer", "viewer-pass-1", db_session) == fp


def test_viewer_upgrade_fails_closed_when_the_hash_changed_underneath(db_session, monkeypatch):
    db_session.add(AuthUser(username="rehash-race", password_hash=hash_password("new-password-1")))
    db_session.commit()
    stale = _old_hash("old-password-1")
    # The password was verified against a hash that a reset has since replaced.
    monkeypatch.setattr(app_auth, "_verified_basic_hash", lambda u, p, db: stale)
    assert app_auth.verify_basic_credential("rehash-race", "old-password-1", db_session) is None
    stored = db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-race")).scalar_one()
    assert pwd_context.verify("new-password-1", stored)


def test_viewer_sibling_signin_that_loses_the_upgrade_swap_still_signs_in(db_session, monkeypatch):
    """A class signing in to one shared account at once: the first sign-in
    upgrades the hash, every sibling that had read the old one loses the swap.
    With the right password they must still get in, on the sibling's hash."""
    mine = hash_password("class-pass-1")
    db_session.add(AuthUser(username="rehash-class", password_hash=mine))
    db_session.commit()
    stale = _old_hash("class-pass-1")  # what this sign-in read before the sibling's upgrade landed
    monkeypatch.setattr(app_auth, "_verified_basic_hash", lambda u, p, db: stale)

    fp = app_auth.verify_basic_credential("rehash-class", "class-pass-1", db_session)

    assert fp == app_auth.basic_credential_fingerprint(mine)
    stored = db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-class")).scalar_one()
    assert stored == mine


def test_viewer_signin_survives_a_failed_upgrade_write(db_session, monkeypatch):
    """The upgrade is an optimisation: a write that fails (a locked database
    under a burst of sign-ins) must not turn a correct password away."""
    from sqlalchemy.exc import OperationalError

    old = _old_hash("class-pass-2")
    db_session.add(AuthUser(username="rehash-locked", password_hash=old))
    db_session.commit()

    def locked(*_a, **_k):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(app_auth, "update", locked)

    fp = app_auth.verify_basic_credential("rehash-locked", "class-pass-2", db_session)

    assert fp == app_auth.basic_credential_fingerprint(old)
    stored = db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-locked")).scalar_one()
    assert stored == old  # untouched; the next sign-in retries the upgrade


# --- failed sign-ins cost the same for every kind of account --------------------
# A wrong password for an account whose hash was made with fewer rounds than
# the current setting would verify in a fraction of the time a wrong password
# for an unknown username (or an already-upgraded account) takes, so latency
# alone would list the usernames that exist. verify_password pads such a failure up to the current setting.
def test_a_wrong_password_for_a_cheaper_hash_is_padded_up_to_the_current_cost(monkeypatch):
    from wsi_app import auth

    spent: list[int] = []
    monkeypatch.setattr(auth, "_burn_rounds", spent.append)

    assert auth.verify_password("wrong-password", _old_hash("right-password")) is False
    assert spent == [auth.PASSWORD_HASH_ROUNDS - 500]


def test_no_padding_for_a_right_password_or_a_hash_at_the_current_cost(monkeypatch):
    from wsi_app import auth

    spent: list[int] = []
    monkeypatch.setattr(auth, "_burn_rounds", spent.append)

    assert auth.verify_password("right-password", _old_hash("right-password")) is True
    assert auth.verify_password("wrong-password", auth.hash_password("right-password")) is False
    assert auth.verify_password("wrong-password", "not-a-hash") is False
    assert spent == []


def test_padding_costs_what_the_missing_rounds_cost():
    """_burn_rounds really is PBKDF2 work, linear in the rounds."""
    from wsi_app import auth

    def took(rounds: int) -> float:
        t = time.perf_counter()
        auth._burn_rounds(rounds)
        return time.perf_counter() - t

    small, large = min(took(50_000) for _ in range(3)), min(took(500_000) for _ in range(3))
    assert 5 < large / small < 20  # 10x the rounds: ~10x the time


def test_a_hash_above_the_setting_is_brought_down_on_the_next_sign_in():
    """The operator lowered WSI_PASSWORD_HASH_ROUNDS: the hash made before that
    is rewritten at the new count when its owner signs in, like an upgrade."""
    high = pbkdf2_sha256.using(rounds=5000).hash("some-password-1")
    again = rehash_if_outdated("some-password-1", high)
    assert again is not None and _rounds(again) == 1000
    assert rehash_if_outdated("some-password-1", again) is None


# --- the dashboard's in-place upgrade never undoes a concurrent change ---------
def _concurrent_change(monkeypatch, user_id, **changes):
    """Make a committed change land between the password check and the upgrade
    write (what a password change / sign-out-everywhere racing a sign-in does)."""
    from wsi_app.db import SessionLocal
    from wsi_app.routers import admin_auth

    real = rehash_if_outdated

    def racing(pw, pw_hash):
        upgraded = real(pw, pw_hash)
        other = SessionLocal()
        try:
            row = other.get(User, user_id)
            for k, v in changes.items():
                setattr(row, k, v(row) if callable(v) else v)
            other.commit()
        finally:
            other.close()
        return upgraded

    monkeypatch.setattr(admin_auth, "rehash_if_outdated", racing)


def test_dashboard_upgrade_does_not_overwrite_a_password_changed_meanwhile(client, db_session, monkeypatch):
    user = _staff(db_session, "correct-horse-3")
    new_hash = hash_password("a-different-password")
    _concurrent_change(monkeypatch, user.id, password_hash=new_hash)

    r = client.post("/api/admin/login", json={"username": user.username, "password": "correct-horse-3"})

    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(User, user.id).password_hash == new_hash


def test_dashboard_upgrade_does_not_touch_an_account_signed_out_meanwhile(client, db_session, monkeypatch):
    user = _staff(db_session, "correct-horse-4")
    before = user.password_hash
    _concurrent_change(monkeypatch, user.id, token_version=lambda row: int(row.token_version or 0) + 1)

    r = client.post("/api/admin/login", json={"username": user.username, "password": "correct-horse-4"})

    assert r.status_code == 200, r.text
    db_session.expire_all()
    # token_version moved on, so the proof is stale for the upgrade write: the
    # old hash stays and nothing else changes (the next sign-in upgrades it).
    assert db_session.get(User, user.id).password_hash == before


def test_viewer_failed_write_with_a_sibling_upgrade_landed_signs_in_on_the_sibling_hash(db_session, monkeypatch):
    """The write timed out (a locked database) but the sibling's upgrade of the
    same password had landed: the sign-in must follow the row, not the hash it
    read earlier -- else its cookie fits no other worker and the sibling's fits
    none here."""
    from sqlalchemy.exc import OperationalError

    from wsi_app.db import SessionLocal

    old = _old_hash("class-pass-3")
    sibling = hash_password("class-pass-3")
    db_session.add(AuthUser(username="rehash-sibling", password_hash=old))
    db_session.commit()
    app_auth._basic_cred_cache.pop("rehash-sibling", None)

    def locked_but_sibling_won(*_a, **_k):
        other = SessionLocal()
        try:
            other.execute(
                AuthUser.__table__.update().where(AuthUser.username == "rehash-sibling").values(password_hash=sibling)
            )
            other.commit()
        finally:
            other.close()
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(app_auth, "update", locked_but_sibling_won)

    fp = app_auth.verify_basic_credential("rehash-sibling", "class-pass-3", db_session)

    assert fp == app_auth.basic_credential_fingerprint(sibling)
    assert app_auth._basic_cred_cache["rehash-sibling"][0] == fp


def test_viewer_failed_write_and_failed_reread_signs_in_but_caches_nothing(db_session, monkeypatch):
    from sqlalchemy.exc import OperationalError

    old = _old_hash("class-pass-4")
    db_session.add(AuthUser(username="rehash-blind", password_hash=old))
    db_session.commit()
    app_auth._basic_cred_cache.pop("rehash-blind", None)

    def locked(*_a, **_k):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(app_auth, "_verified_basic_hash", lambda u, p, db: old)  # (it reads with select too)
    monkeypatch.setattr(app_auth, "update", locked)
    monkeypatch.setattr(app_auth, "select", locked)  # the re-read fails too

    fp = app_auth.verify_basic_credential("rehash-blind", "class-pass-4", db_session)

    assert fp == app_auth.basic_credential_fingerprint(old)
    # The row was never read back, so this fingerprint is not known to be its.
    assert "rehash-blind" not in app_auth._basic_cred_cache


def test_viewer_lost_swap_and_failed_reread_refuses_rather_than_trusting_the_old_hash(db_session, monkeypatch):
    """The password was reset while this sign-in was verifying the OLD one (the
    swap is lost), and then the row cannot be read back to check. Signing in
    on the hash that was just proven would admit a password that has been
    replaced: refuse, and let the user try again."""
    from sqlalchemy.exc import OperationalError

    stale = _old_hash("old-password-9")
    db_session.add(AuthUser(username="rehash-reset", password_hash=hash_password("new-password-9")))
    db_session.commit()
    app_auth._basic_cred_cache.pop("rehash-reset", None)

    def locked(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(app_auth, "_verified_basic_hash", lambda u, p, db: stale)
    monkeypatch.setattr(app_auth, "select", locked)  # the re-read fails

    assert app_auth.verify_basic_credential("rehash-reset", "old-password-9", db_session) is None
    assert "rehash-reset" not in app_auth._basic_cred_cache
    # ... and the session is still usable afterwards.
    monkeypatch.undo()
    stored = db_session.execute(select(AuthUser.password_hash).where(AuthUser.username == "rehash-reset")).scalar_one()
    assert pwd_context.verify("new-password-9", stored)
