"""Ask systemd to restart wsi-app (used by restore / reset apply).

The app runs under ``NoNewPrivileges=true`` and cannot restart itself, so it
drops ``settings.restart_trigger_path`` (``write_restart_trigger``), which the
root-owned wsi-restart.path unit watches; ``trigger_restart`` runs a custom
``restore_restart_cmd`` instead when one is set.

Settings changes do NOT restart the service. Auth and IP-filter settings are
held in a per-worker snapshot (``app_auth._snapshot`` / ``network._snapshot``):
the worker that handles the PATCH refreshes its own at once, and every worker
-- including the separate wsi-sync process, which a restart never reached --
re-reads both from the DB every ``WSI_SETTINGS_REFRESH_SECONDS``
(``main._settings_refresh_loop``). Everything else on those settings rows is
read per request. A restart would only add an outage and kill in-flight
uploads, downloads and jobs for no faster convergence.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time

from .config import settings

_logger = logging.getLogger("wsi_app.service_restart")


def write_restart_trigger() -> None:
    """Create ``settings.restart_trigger_path`` so the wsi-restart.path systemd
    unit restarts wsi-app.

    This is the privilege-safe replacement for shelling out to ``sudo
    systemctl``: the app runs under ``NoNewPrivileges=true`` and cannot use
    setuid sudo, so instead it drops a file that a root-owned path unit watches
    (the same pattern as wsi-avahi-reload.path). The parent directory is
    provided out-of-band by ``/etc/tmpfiles.d/wsi.conf`` and listed in the
    unit's ``ReadWritePaths``; ``exist_ok=True`` makes this a no-op when it is
    already present. Raises ``OSError`` on failure so callers that must clean up
    shared IPC state (restore-apply) can react.
    """
    path = settings.restart_trigger_path
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, b"restart\n")
        os.fsync(fd)
    finally:
        os.close(fd)


class RestartKickError(RuntimeError):
    """The service restart could not be triggered; the message says why."""


def trigger_restart(observe_seconds: float = 0.0) -> None:
    """Restart wsi-app to carry out a restore / reset apply.

    By default (``RESTORE_RESTART_CMD`` empty) this drops the trigger file
    that the root-owned wsi-restart.path unit acts on (see
    :func:`write_restart_trigger`). A non-empty ``RESTORE_RESTART_CMD`` is run
    instead, detached into its own session so the SIGTERM the restart sends
    this worker does not reach it. With ``observe_seconds`` the command is
    watched that long, so one that exits non-zero right away (a masked unit, a
    failing ``systemctl``) is reported rather than ignored; one still running
    by then is assumed to be carrying out the restart.

    Raises :class:`RestartKickError` when the restart was not triggered: the
    trigger file could not be written, the command is unparseable (an
    explicit setting is not second-guessed by falling back to the trigger
    file), could not be started, or exited non-zero within the window.
    """
    try:
        cmd = shlex.split(settings.restore_restart_cmd or "")
    except ValueError as exc:
        raise RestartKickError(f"RESTORE_RESTART_CMD is unparseable ({exc})") from None
    if not cmd:
        try:
            write_restart_trigger()
        except OSError as exc:
            raise RestartKickError(f"could not write restart trigger: {exc}") from exc
        return
    try:
        proc = subprocess.Popen(
            cmd,
            close_fds=True,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise RestartKickError(f"could not run the restart command {cmd!r}: {exc}") from exc
    deadline = time.monotonic() + observe_seconds
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is None:
            time.sleep(0.05)
            continue
        if rc != 0:
            raise RestartKickError(f"restart command {cmd!r} exited with code {rc}")
        return
