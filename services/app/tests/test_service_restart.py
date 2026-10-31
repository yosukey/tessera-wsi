"""Settings changes propagate without a service restart.

Auth and IP-filter settings live in a per-worker snapshot. The worker that
handles a settings PATCH refreshes its own copy at once; every other worker —
and the separate wsi-sync process — converges through the periodic
``main._settings_refresh_loop``. A settings PATCH must therefore never restart
the service: a restart would SIGKILL in-flight slide uploads, downloads and
performance runs (systemd's TimeoutStopSec) even for a GA-id-only change.

The restart helper used for restore / reset apply
(``trigger_restart``) is pinned here too.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from wsi_app import app_auth as _app_auth
from wsi_app import main as _main
from wsi_app import network as _net
from wsi_app import service_restart
from wsi_app.admin_helpers import get_or_create_singleton
from wsi_app.config import settings
from wsi_app.db import SessionLocal
from sqlalchemy import delete

from wsi_app.auth import hash_password
from wsi_app.models import AuthSettings, AuthUser, NetworkSettings


@pytest.fixture
def _restore_cmd():
    saved = (settings.restore_restart_cmd, settings.restart_trigger_path)
    yield
    object.__setattr__(settings, "restore_restart_cmd", saved[0])
    object.__setattr__(settings, "restart_trigger_path", saved[1])


@pytest.fixture
def _reset_settings_rows():
    yield
    db = SessionLocal()
    try:
        ns = get_or_create_singleton(db, NetworkSettings)
        ns.ip_filter_enabled = False
        ns.ip_filter_default = "allow"
        ns.ip_rules = "[]"
        ns.ga_measurement_id = ""
        s = get_or_create_singleton(db, AuthSettings)
        s.mode = "none"
        s.login_message = ""
        db.commit()
        _net.refresh(db)
        _app_auth.refresh(db)
    finally:
        db.close()


@pytest.fixture
def no_restart(monkeypatch, tmp_path, _restore_cmd):
    """Record every way the app could ask for a restart."""
    calls = []
    monkeypatch.setattr(service_restart.subprocess, "Popen", lambda *a, **k: calls.append(a))
    real_write = service_restart.write_restart_trigger

    def _record_write():
        calls.append("trigger")
        real_write()

    monkeypatch.setattr(service_restart, "write_restart_trigger", _record_write)
    trigger = tmp_path / "restart.trigger"
    object.__setattr__(settings, "restart_trigger_path", str(trigger))
    object.__setattr__(settings, "restore_restart_cmd", "echo do-restart")
    return calls, trigger


def test_network_settings_patch_does_not_restart(client, admin_headers, no_restart, _reset_settings_rows):
    calls, trigger = no_restart
    r = client.patch(
        "/api/admin/network/settings",
        data={"ga_measurement_id": "G-ABCDEF1234"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        "/api/admin/network/settings",
        data={
            "ip_filter_enabled": "1",
            "ip_filter_default": "allow",
            "ip_rules": json.dumps([{"ip": "203.0.113.0/24", "action": "deny"}]),
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert calls == []
    assert not trigger.exists()


def test_auth_settings_patch_does_not_restart(client, admin_headers, no_restart, _reset_settings_rows):
    calls, trigger = no_restart
    # A switch to basic needs a login user to exist (or it is refused).
    db = SessionLocal()
    try:
        db.add(AuthUser(username="restart-check", password_hash=hash_password("longenoughpw")))
        db.commit()
    finally:
        db.close()
    try:
        r = client.patch(
            "/api/admin/auth/settings",
            data={"mode": "basic", "login_message": "hello"},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        # The handling worker gates on the new mode at once.
        assert _app_auth.current_mode() == "basic"
        assert calls == []
        assert not trigger.exists()
    finally:
        db = SessionLocal()
        try:
            db.execute(delete(AuthUser).where(AuthUser.username == "restart-check"))
            db.commit()
        finally:
            db.close()


def test_refresh_converges_a_change_committed_by_another_worker(_reset_settings_rows):
    """What every other worker (and wsi-sync) runs periodically: a change
    committed elsewhere reaches this process's snapshots."""
    db = SessionLocal()
    try:
        _net.refresh(db)
        _app_auth.refresh(db)
        assert _app_auth.current_mode() == "none"
        assert _net.ip_allowed("203.0.113.9")
        # Committed by "another worker": this process's snapshots are untouched.
        ns = get_or_create_singleton(db, NetworkSettings)
        ns.ip_filter_enabled = True
        ns.ip_filter_default = "allow"
        ns.ip_rules = json.dumps([{"ip": "203.0.113.0/24", "action": "deny"}])
        s = get_or_create_singleton(db, AuthSettings)
        s.mode = "basic"
        db.commit()
    finally:
        db.close()
    assert _app_auth.current_mode() == "none"
    assert _net.ip_allowed("203.0.113.9")

    _main._refresh_settings_snapshots()

    assert _app_auth.current_mode() == "basic"
    assert not _net.ip_allowed("203.0.113.9")


def test_refresh_loop_keeps_running_after_a_failed_refresh(monkeypatch):
    calls = {"n": 0}
    done = asyncio.Event()

    def _refresh():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        if calls["n"] >= 3:
            done.set()

    monkeypatch.setattr(_main, "_refresh_settings_snapshots", _refresh)

    async def _run():
        task = asyncio.create_task(_main._settings_refresh_loop(0.01))
        try:
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())
    assert calls["n"] >= 3


def test_trigger_restart_runs_the_configured_command(monkeypatch, _restore_cmd):
    calls = []
    monkeypatch.setattr(service_restart.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    object.__setattr__(settings, "restore_restart_cmd", "echo do-restart")
    service_restart.trigger_restart()
    assert calls == [["echo", "do-restart"]]


def test_trigger_restart_defaults_to_the_trigger_file(monkeypatch, tmp_path, _restore_cmd):
    calls = []
    monkeypatch.setattr(service_restart.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    trigger = tmp_path / "run" / "restart.trigger"
    object.__setattr__(settings, "restore_restart_cmd", "")
    object.__setattr__(settings, "restart_trigger_path", str(trigger))
    service_restart.trigger_restart()
    assert calls == []
    assert trigger.read_bytes() == b"restart\n"


def test_trigger_restart_refuses_an_unparseable_command(monkeypatch, tmp_path, _restore_cmd):
    """An explicit but unparseable RESTORE_RESTART_CMD is a failed kick
    (reported), never a silent fall back to the trigger file."""
    calls = []
    monkeypatch.setattr(service_restart.subprocess, "Popen", lambda *a, **k: calls.append(a))
    object.__setattr__(settings, "restart_trigger_path", str(tmp_path / "restart.trigger"))
    object.__setattr__(settings, "restore_restart_cmd", 'systemctl "restart')
    with pytest.raises(service_restart.RestartKickError, match="unparseable"):
        service_restart.trigger_restart()
    assert calls == []
    assert not (tmp_path / "restart.trigger").exists()


def test_trigger_restart_reports_a_command_that_fails_at_once(_restore_cmd):
    object.__setattr__(settings, "restore_restart_cmd", "/bin/false")
    with pytest.raises(service_restart.RestartKickError, match="exited with code 1"):
        service_restart.trigger_restart(observe_seconds=5.0)
    # Unobserved, the same command is only started.
    service_restart.trigger_restart()
