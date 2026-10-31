from __future__ import annotations
import asyncio
import collections
import errno
import fcntl
import logging
import os
import posixpath
import re
import sys
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, NamedTuple, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as _fastapi_http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

_logger = logging.getLogger("wsi_app.startup")


class _AppStderrHandler(logging.StreamHandler):
    """stderr sink for the ``wsi_app`` tree that yields to a root config."""

    def emit(self, record: logging.LogRecord) -> None:
        # A root handler exists (uvicorn --log-config, pytest's capture): the
        # record propagates there, so writing it here as well would print it
        # twice. Checked per record because such handlers come and go.
        if logging.getLogger().handlers or getattr(record, "skip_stderr", False):
            return
        # Resolve at emit time so a swapped/closed sys.stderr is never used.
        self.stream = sys.stderr
        super().emit(record)


def _configure_app_logging() -> None:
    """Send ``wsi_app`` INFO+ to stderr, which systemd hands to journald.

    Nothing else configures this tree: uvicorn's default config wires only its
    own ``uvicorn.*`` loggers, and Python's last-resort stderr output (WARNING+
    only) stops as soon as error_log attaches its queue handler to ``wsi_app``.
    Without this, the audit trail's primary journald record (INFO) would never
    be written and WARNING/ERROR records would go only to the DB. Runs before the
    other app modules import so their import-time warnings are covered too.
    Idempotent across re-imports.
    """
    app_logger = logging.getLogger("wsi_app")
    if any(getattr(h, "_wsi_app_stderr", False) for h in app_logger.handlers):
        return
    handler = _AppStderrHandler(sys.stderr)
    handler._wsi_app_stderr = True
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    app_logger.addHandler(handler)
    # Only fill in an unset level; an explicit one (tests, an operator's
    # config) is left alone.
    if app_logger.level == logging.NOTSET:
        app_logger.setLevel(logging.INFO)


_configure_app_logging()


# One ``t`` query parameter: the signed-link download token (see
# app_auth.signed_download_valid), the one credential that travels in a URL.
# The key is matched as uvicorn logs it — the raw query string — so its
# percent-encoded spelling ``%74`` counts too: Starlette decodes that to ``t``
# and the token authenticates. Nothing else decodes to ``t`` (query keys are
# case-sensitive, so not ``T`` / ``%54``; ``%2574`` decodes only to ``%74``).
_DOWNLOAD_TOKEN_PARAM_RE = re.compile(r"(^|&)(t|%74)=[^&]*")


class _DownloadTokenRedactFilter(logging.Filter):
    """Mask ``t=`` in uvicorn's access-log line for the download routes.

    The SPA starts a download as a plain link (``…/download?t=<jwt>``) the
    browser navigates to, so the token sits in the request target. A token is
    good for an hour and can fetch a whole-install backup, and uvicorn's
    access log — on by default, into the journal — writes that target as is.
    nginx's access log masks the same parameter (wsi_combined in wsi.conf);
    this is its counterpart one hop in. uvicorn logs ``(client, method,
    path?query, http_version, status)``; only the third argument is touched,
    and only on an /api/admin/ path ending in /download, so every other line
    is left exactly as uvicorn wrote it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, query = args[2].partition("?")
            if sep and path.startswith("/api/admin/") and path.endswith("/download"):
                masked = _DOWNLOAD_TOKEN_PARAM_RE.sub(r"\1\2=[redacted]", query)
                if masked != query:
                    record.args = args[:2] + (f"{path}?{masked}",) + args[3:]
        return True


def _redact_download_tokens_in_access_log() -> None:
    """Attach :class:`_DownloadTokenRedactFilter` to ``uvicorn.access``.

    uvicorn configures its loggers before it imports the app (in each worker
    too), and dictConfig keeps an existing logger's filters, so attaching here
    at import holds for the life of the process. Idempotent across re-imports.
    """
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _DownloadTokenRedactFilter) for f in access.filters):
        access.addFilter(_DownloadTokenRedactFilter())


_redact_download_tokens_in_access_log()

from .config import settings, env_bool, env_int, ADMIN_BOOTSTRAP_PASSWORD_DEFAULT
from .db import engine, SessionLocal, configure_request_concurrency
from .models import Base, User, NetworkSettings
from .auth import (
    MANAGER_OR_ABOVE,
    MIN_JWT_SECRET_BYTES,
    ROLE_ADMIN,
    STAFF_ROLES,
    bearer_token,
    hash_password,
    jwt_secret_is_short,
    resolve_staff_user,
    validate_password_strength,
)
from .dzi import router as dzi_router
from .routers.admin_auth import router as admin_auth_router
from .routers.admin_backup import router as admin_backup_router
from .routers.admin_history import router as admin_history_router
from .routers.admin_logs import router as admin_logs_router
from .routers.admin_mfa import router as admin_mfa_router
from .routers.admin_metadata import router as admin_metadata_router
from .routers.admin_perf import router as admin_perf_router
from .routers.admin_settings import router as admin_settings_router
from .routers.admin_slide_transfer import router as admin_slide_transfer_router
from .routers.admin_slides import router as admin_slides_router
from .routers.admin_system import router as admin_system_router
from .routers.admin_tours import router as admin_tours_router
from .routers.admin_users import router as admin_users_router
from .routers.analytics import router as analytics_router
from .routers.app_auth import router as app_auth_router
from .routers.app_auth import app_login_throttle_key
from .routers.public import router as public_router
from .routers.sync import router as sync_router
from .routers import admin_backup as _admin_backup
from .routers import admin_slide_transfer as _admin_slide_transfer
from .app_meta import (
    assert_not_downgrade,
    assert_schema_not_downgrade,
    bootstrap_app_version,
    read_recorded_app_version,
    read_recorded_schema_version,
)
from . import migrations as _migrations
from .column_defaults import column_default_literal
from . import network as _net
from . import app_auth as _app_auth
from . import cf_access as _cf_access
from . import login_throttle as _login_throttle
from . import jobs as _jobs


def bootstrap_admin():
    """Create the initial admin user only when no admin exists yet.

    Idempotent first-launch seed: never overwrites an existing admin row, so
    password / username changes made through the UI survive restarts.

    Refuses to create an admin row if ADMIN_BOOTSTRAP_PASSWORD was not set
    by the operator. A missing env var would otherwise fall through to the
    sentinel default and seed a deterministic admin account on a fresh
    install. WSI_ALLOW_INSECURE_DEFAULTS=1 permits that insecure sentinel-seeded
    admin for local development.
    """
    db: Session = SessionLocal()
    try:
        # Existence probe only: installs routinely hold several admins, so
        # scalar_one_or_none() here would raise MultipleResultsFound and take
        # every worker down at the next boot.
        existing_admin = db.execute(
            select(User.id).where(User.role == "admin").limit(1)
        ).first()
        if existing_admin is not None:
            return
        username = settings.admin_bootstrap_username
        pw = settings.admin_bootstrap_password
        # Treat "unset" (config's sentinel default) and empty — the value
        # wsi.env.example ships — as "the operator forgot to configure this".
        # Refusing to create an admin in that state is what keeps a fresh
        # deploy from ending up with a known-credential account.
        if pw in (ADMIN_BOOTSTRAP_PASSWORD_DEFAULT, ""):
            if not env_bool("WSI_ALLOW_INSECURE_DEFAULTS", False):
                raise RuntimeError(
                    "ADMIN_BOOTSTRAP_PASSWORD is not set (or is empty) and no "
                    "admin user exists yet. "
                    "Set ADMIN_BOOTSTRAP_PASSWORD in /etc/wsi/wsi.env to a "
                    "strong random value before starting (the install.sh "
                    "script does this automatically). For local development "
                    "you may set WSI_ALLOW_INSECURE_DEFAULTS=1 to bypass."
                )
            # Dev-only path: emit a noisy warning so the operator can't miss
            # that they're about to seed an admin with a placeholder password.
            _logger.warning(
                "WSI_ALLOW_INSECURE_DEFAULTS=1: bootstrapping admin %s with "
                "placeholder password. CHANGE IT IMMEDIATELY.", username,
            )
            pw = "changeme"
        # Same policy as every other password entry point: a 1-character value
        # would seed a trivially guessable admin, and one over MAX_PASSWORD_LEN
        # would seed an admin nobody can log in as (the login form caps it).
        try:
            validate_password_strength(pw)
        except HTTPException as exc:
            raise RuntimeError(
                f"ADMIN_BOOTSTRAP_PASSWORD is not acceptable ({exc.detail}) and "
                "no admin user exists yet. Set ADMIN_BOOTSTRAP_PASSWORD in "
                "/etc/wsi/wsi.env to a value that meets the password policy "
                "before starting."
            ) from None
        user = db.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is not None:
            # The bootstrap username is already taken. If that row is itself an
            # admin the function would have returned above (existing_admin),
            # so reaching here means a NON-admin owns the name while the
            # install has zero admins — a silent recovery dead-end. Warn
            # loudly so the operator knows to pick a different
            # ADMIN_BOOTSTRAP_USERNAME or promote a user.
            if user.role != "admin":
                _logger.warning(
                    "No admin account exists, but ADMIN_BOOTSTRAP_USERNAME %r is "
                    "already held by a non-admin (role=%r); not creating a bootstrap "
                    "admin. Set a different ADMIN_BOOTSTRAP_USERNAME or promote an "
                    "existing user to recover.",
                    username, user.role,
                )
            return
        db.add(User(username=username, password_hash=hash_password(pw), role="admin"))
        try:
            db.commit()
        except IntegrityError:
            # Concurrency backstop for the best-effort-unlocked boot path
            # (see _startup_schema_lock): another worker inserted the admin /
            # username between the SELECTs above and this commit, tripping the
            # users.username UNIQUE constraint. That worker's row is
            # authoritative — roll back and treat this as a successful no-op
            # instead of crashing this worker's boot.
            db.rollback()
    finally:
        db.close()


def ensure_dirs():
    os.makedirs(os.path.join(settings.data_dir, "wsi"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "thumbs"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "cache"), exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "assets", "tours"), exist_ok=True)


def _load_network_cache() -> None:
    """Load NetworkSettings into the in-memory IP-filter cache and re-apply mDNS."""
    db: Session = SessionLocal()
    try:
        _net.refresh(db)
        ns = db.get(NetworkSettings, 1)
        # Apply unconditionally so a stale Avahi service file is *removed* when
        # mDNS is disabled (or the NetworkSettings row is absent — e.g. just
        # after a restore). Running the disable path too ensures a host stops
        # advertising _http._tcp as soon as the setting is off.
        _net.apply_mdns(bool(ns and ns.mdns_enabled))
    finally:
        db.close()


def _load_auth_cache() -> None:
    """Load AuthSettings into the in-memory app-access auth cache."""
    db: Session = SessionLocal()
    try:
        _app_auth.refresh(db)
    finally:
        db.close()


# How often each worker re-reads the auth + IP-filter settings from the DB into
# its in-memory snapshot: the bound on how long a settings change (an auth-mode
# switch, a new deny rule, a session revocation) takes to reach the workers that
# did not handle the PATCH. See _settings_refresh_loop for why this exists. The
# read is two primary-key lookups, so a short interval costs next to nothing.
_SETTINGS_REFRESH_SECONDS = env_int("WSI_SETTINGS_REFRESH_SECONDS", 5)


def _refresh_settings_snapshots() -> None:
    """Reload the in-memory auth + IP-filter snapshots from the DB.

    Runs periodically in EVERY worker (see :func:`_settings_refresh_loop`) so a
    settings PATCH handled by one wsi-app worker converges to all the others —
    and to the separate single-worker wsi-sync process, whose WS gate
    (``_ws_guard`` → ``current_mode`` / ``ip_allowed``) reads the same snapshot
    — WITHOUT a service restart. This is the only cross-worker propagation path
    for these settings (see service_restart's module docstring), so a settings
    edit never interrupts uploads, downloads or live-sync rooms.

    Unlike :func:`_load_network_cache` this deliberately does NOT re-apply mDNS
    (that would rewrite the avahi service file on every tick); an mDNS toggle
    still takes effect through the PATCH handler and the boot loader.
    """
    db: Session = SessionLocal()
    try:
        _net.refresh(db)
        _app_auth.refresh(db)
    finally:
        db.close()


async def _settings_refresh_loop(interval: float) -> None:
    """Background task: periodically refresh the settings snapshots off the
    event loop (the DB reads are synchronous, so they run in a thread to avoid
    stalling the sync worker's single event loop / all its live rooms)."""
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_refresh_settings_snapshots)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "periodic settings-snapshot refresh failed; continuing"
            )


def _warn_open_admin_register() -> None:
    """Loud startup warning if ALLOW_ADMIN_REGISTER is left enabled.

    The /api/admin/register endpoint creates a *new admin user* without any
    authentication whenever no admin account exists. It exists for
    first-launch / disaster-recovery flows only; left on, any moment the
    users table holds no admin (a restore of such a DB, the last admin
    deleted out-of-band) hands the admin boundary to whoever calls it first.
    Surface that loudly in the logs so operators can't miss it.
    """
    if not settings.allow_admin_register:
        return
    banner = (
        "ALLOW_ADMIN_REGISTER=1 is set: while no admin account exists, "
        "/api/admin/register is OPEN and will create one for any "
        "unauthenticated caller. Disable this in production by unsetting "
        "ALLOW_ADMIN_REGISTER (or setting it to 0) in /etc/wsi/wsi.env."
    )
    _logger.warning("=" * 80)
    _logger.warning("SECURITY: %s", banner)
    _logger.warning("=" * 80)


def _warn_short_jwt_secret() -> None:
    """Loud startup warning for a JWT_SECRET below MIN_JWT_SECRET_BYTES.

    The secret signs every staff token and app-access cookie, so a short one
    is open to offline brute force from any single token. Booting is not
    refused — an operator-supplied short secret must not stop the service —
    so the operator is told how to replace it instead.
    """
    if not jwt_secret_is_short():
        return
    banner = (
        f"JWT_SECRET is shorter than {MIN_JWT_SECRET_BYTES} bytes, so staff "
        "tokens and app-access session cookies can be forged once it is "
        "brute-forced. Replace it in /etc/wsi/wsi.env with the output of "
        "`openssl rand -hex 48` and restart (every session must sign in "
        "again; see docs/operations.md -> Rotating JWT_SECRET)."
    )
    _logger.warning("=" * 80)
    _logger.warning("SECURITY: %s", banner)
    _logger.warning("=" * 80)


def _warn_restore_ipc_dir() -> None:
    """Verify that the restore-apply IPC directory is writable at boot.

    ``request_apply`` writes ``settings.restore_request_path`` and the
    swap-on-boot phase reads it back. On a typical install
    ``/etc/tmpfiles.d/wsi.conf`` recreates ``/run/wsi`` as ``wsi:wsi`` on
    every boot. When that tmpfiles.d drop-in is missing (slim base image,
    operator removed it during cleanup), the directory does not exist —
    and the first restore-apply click weeks later fails with a
    ``PermissionError`` from inside ``os.makedirs`` that the operator has
    no easy way to correlate to the boot-time misconfiguration.

    Surface the problem loudly at startup so the failure mode is
    self-evident the moment the service comes up. Best-effort: if the
    directory exists today but is not writable by the running user, a
    warning is still emitted. The app does not attempt to fix the
    permissions itself — that's privileged work for ``install.sh``.

    Skipped in the wsi-sync process (``WSI_PROCESS_ROLE=sync``, set by its
    unit): it runs this same app but never applies a restore, and its sandbox
    deliberately grants no write access to ``/run/wsi`` — the check would
    report that as a fault on every start.
    """
    if os.environ.get("WSI_PROCESS_ROLE", "") == "sync":
        return
    req_path = settings.restore_request_path
    parent = os.path.dirname(req_path) or "."
    if not os.path.isdir(parent):
        _logger.warning("=" * 80)
        _logger.warning(
            "RESTORE: %r does not exist. The restore-apply endpoint will "
            "fail until the directory is created (typically by "
            "/etc/tmpfiles.d/wsi.conf at boot). Re-run infra/install.sh "
            "or recreate it manually with the appropriate ownership.",
            parent,
        )
        _logger.warning("=" * 80)
        return
    if not os.access(parent, os.W_OK):
        _logger.warning("=" * 80)
        _logger.warning(
            "RESTORE: %r exists but is not writable by the running user. "
            "Fix the ownership (typically ``chown wsi:wsi`` matching the "
            "service User=) or restore-apply will fail with a permission "
            "error.",
            parent,
        )
        _logger.warning("=" * 80)


def _warn_backup_dir() -> None:
    """Verify the backup directory is writable at boot.

    ``BACKUP_DIR`` is env-tunable, but the systemd units' ``ReadWritePaths``
    list only the default tree. Point it at, say, ``/mnt/backup-volume`` and
    every write fails with EROFS under ``ProtectSystem=strict`` — a failure
    whose real cause (the unit's sandbox, not the filesystem) is invisible from
    the app's own logs. Say so at startup instead, in the same shape as
    :func:`_warn_restore_ipc_dir`.

    BOTH units need the access, which is why the message names both: the
    background-job store lives in ``BACKUP_DIR/.jobs``, and ``wsi-sync`` runs
    the same ``wsi_app.main:app`` (so the same boot janitor calls
    ``jobs.store.prune_terminal()``). ``JobStore._persist_dir`` swallows the
    failure and returns None: the janitor then silently skips the store, and
    every job claim is refused with a 503 (``jobs.JobStoreError``) rather than
    run without the cross-worker record — so the cause is worth naming here,
    at boot, instead of at the first backup click.

    Best-effort and non-fatal: an operator may fix the unit and restart, and a
    server that cannot back up must still serve slides.
    """
    d = settings.backup_dir
    advice = (
        "If BACKUP_DIR points outside %r, add it to ReadWritePaths= in BOTH "
        "wsi-app.service and wsi-sync.service — ProtectSystem=strict makes "
        "every other path read-only, and the job store lives under it."
    )
    try:
        os.makedirs(d, exist_ok=True)
        writable = os.access(d, os.W_OK)
    except OSError as exc:
        _logger.warning("=" * 80)
        _logger.warning(
            "BACKUP: %r could not be created (%s). Backups, restores and the "
            "job store will fail. " + advice,
            d, exc, settings.data_dir,
        )
        _logger.warning("=" * 80)
        return
    if not writable:
        _logger.warning("=" * 80)
        _logger.warning(
            "BACKUP: %r exists but is not writable by the running user. Fix the "
            "ownership (``chown`` to match the service User=). " + advice,
            d, settings.data_dir,
        )
        _logger.warning("=" * 80)


def _reconcile_interrupted_restore_apply() -> None:
    """Refuse to start on a data directory a restore apply left half-swapped.

    A restore/reset apply journals its moves (``backup.restore_journal_path``)
    and removes the journal only once the tree is consistent again, so its
    presence at boot means an apply was cut short (SIGKILL, power loss, ...).
    In production start-wsi-app.sh reconciles it before exec'ing uvicorn and
    will not start the app while it remains — but wsi-sync runs this same app
    without that script, and at boot it is started alongside wsi-app (After=
    only orders the fork), i.e. possibly mid-reconciliation. So outside dev
    this refuses to start (systemd retries after RestartSec) rather than open
    the DB. With ``BACKUP_DEV_INPROCESS_APPLY`` on there is no start script,
    so the dev in-process apply's journal is reconciled here instead (under the
    schema lock, so concurrent dev workers cannot both try).
    """
    from . import backup as _backup
    path = _backup.restore_journal_path()
    if not os.path.lexists(path):
        return
    if settings.backup_dev_inprocess_apply:
        _backup.recover_interrupted_apply()
        return
    raise RuntimeError(
        f"an interrupted restore apply is recorded at {path!r}: the data "
        "directory may be half-swapped. start-wsi-app.sh reconciles it before "
        "wsi-app starts; refusing to serve until it has (see docs/operations.md, "
        "'Recovering from an interrupted restore')."
    )


@contextmanager
def _startup_schema_lock():
    """Serialise the boot-time schema setup across uvicorn workers.

    Production starts uvicorn with ``--workers N`` (see
    infra/systemd/start-wsi-app.sh) and ``lifespan`` runs in *every* worker,
    so ``Base.metadata.create_all`` and ``_apply_column_migrations`` all race
    on the first boot after an upgrade — exactly when columns are actually
    missing and DDL is emitted. Both are read-then-write (reflect the live
    schema, then ``CREATE TABLE`` / ``ALTER TABLE ADD|DROP COLUMN`` the
    delta). SQLite's ``busy_timeout`` does NOT save this: each worker opens a
    deferred transaction that snapshots the *pre-migration* schema, so a
    second worker that already read "column missing" raises
    ``SQLITE_BUSY_SNAPSHOT`` (not retried) or a duplicate-column error when it
    tries to apply the same delta after the first worker committed.

    A host-local ``fcntl.flock`` (same primitive as
    ``jobs.JobStore._cross_worker_lock``) serialises the whole block: the
    lock is held until the DB transaction has committed, so by the time the
    next worker acquires it the reflected schema is already current and the
    migration is a no-op. The lock file lives under ``data_dir``, which every
    worker on the host shares — as it shares the SQLite file itself, so a
    host-local lock covers every process that can open the DB.

    Best-effort: if the lock directory can't be created/opened (e.g. an
    unwritable ``data_dir``), fall through without locking rather than block
    boot.
    """
    locks_dir = os.path.join(settings.data_dir, "locks")
    lock_fd = None
    try:
        os.makedirs(locks_dir, exist_ok=True)
        lock_fd = os.open(
            os.path.join(locks_dir, "schema-migration.lock"),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        _logger.warning(
            "could not acquire startup schema lock; proceeding without "
            "cross-worker serialisation", exc_info=True,
        )
        if lock_fd is not None:
            os.close(lock_fd)
            lock_fd = None
    try:
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def reconcile_schema(bind) -> None:
    """Bring the database behind ``bind`` (an Engine) up to this binary's
    schema: the boot's schema steps, in the boot's order.

    The lifespan runs this on the live DB under ``_startup_schema_lock``, as
    does the dev in-process apply after its swap; a restore first runs it on a
    throwaway copy of the staged DB (``backup.trial_schema_reconcile``), so
    an archive this binary cannot upgrade is refused while the live data is
    still in place instead of crash-looping the service after the swap.
    Raises on anything that must keep the app from serving the DB.
    """
    # Refuse a downgrade BEFORE any schema work. _apply_column_migrations
    # runs destructive DROP COLUMN for columns the running ORM no longer
    # declares, so an older binary booting against a newer DB would delete
    # the newer release's data and commit before bootstrap_app_version's own
    # downgrade guard ever runs. Checking here aborts while the data is still
    # intact.
    assert_not_downgrade(bind)
    # Schema-precise downgrade guard: block if the DB records a higher
    # schema_version than this binary can produce (same intact-data rationale
    # as the app_version guard above; see the migrations pkg).
    assert_schema_not_downgrade(bind, _migrations.CURRENT_SCHEMA_VERSION)
    # Read the recorded schema_version before create_all builds the app_meta
    # table; run_migrations starts from it (an unstamped DB from the baseline).
    recorded_sv = read_recorded_schema_version(bind)
    # A database below the baseline has no migration path.
    _migrations.assert_schema_floor(
        recorded_sv,
        app_version_recorded=read_recorded_app_version(bind) is not None,
    )
    Base.metadata.create_all(bind=bind)
    # ADD phase first so a data migration can populate freshly-added columns;
    # then ordered migrations (which stamp schema_version); then the DROP
    # phase so migrations could read soon-to-be-dropped columns.
    _apply_column_migrations(phase="add", bind=bind)
    _migrations.run_migrations(bind, recorded_sv=recorded_sv)
    _apply_column_migrations(phase="drop", bind=bind)
    # Then indexes, so the index set is reconciled against the final column
    # set (the DROP phase above removes indexes that reference stale columns).
    _reconcile_indexes(bind=bind)


def _apply_column_migrations(phase: str = "both", bind=None) -> None:
    """Reconcile the live SQLite schema with the ORM's column set.

    ``Base.metadata.create_all`` only creates missing *tables*, never missing
    columns, so a database that predates a column addition would 500 the
    moment any INSERT/UPDATE referenced it. This reflects the live shape
    instead and ALTERs in whatever the model declares but the table lacks —
    no hand-maintained list, so a newly added field cannot be forgotten.

    ``phase`` selects which half runs:

    * ``"add"``  — only ADD columns the ORM declares but the table lacks.
    * ``"drop"`` — only DROP columns the table has but the ORM no longer
      declares.
    * ``"both"`` — add then drop in one pass (default; used by direct
      callers/tests).

    The lifespan splits the two around ``migrations.run_migrations`` so a data
    migration can back-fill a freshly-ADDed column and still read a
    soon-to-be-DROPped one before it disappears.

    ``bind`` is the Engine to reconcile; the app's own by default.
    """
    do_add = phase in ("add", "both")
    do_drop = phase in ("drop", "both")
    # SQLite-only build (enforced in config.py): the PRAGMA-based schema
    # reflection and ALTER TABLE statements below are SQLite-specific by design.
    with (engine if bind is None else bind).begin() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.exec_driver_sql(
                f"PRAGMA table_info({table.name})"
            ).fetchall()
            if not rows:
                continue  # table doesn't exist yet — create_all will handle it
            existing = {r[1] for r in rows}
            model_cols = {c.name for c in table.columns}
            if do_add:
                for col in table.columns:
                    if col.name in existing:
                        continue
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN '
                        + _column_add_clause(col)
                    )
                    _warn_unapplied_column_constraints(table, col)
            # Data transforms the reflection reconciler can't express
            # (back-fills, renames, table rebuilds) live in the ``migrations``
            # package and run in the lifespan *between* this ADD phase and the
            # DROP phase below — see ``migrations.run_migrations``.
            if not do_drop:
                continue

            # Drop columns the model no longer declares. Without this, an
            # INSERT that omits a stale NOT NULL column raises IntegrityError
            # → 500. SQLite has no `ALTER COLUMN DROP NOT NULL`, so a full
            # table rebuild would be the only alternative; DROP COLUMN is
            # the lighter hammer.
            #
            # If DROP COLUMN fails (typically because the column is part of
            # a UNIQUE / INDEX / FK that SQLite cannot rewrite in place),
            # the app refuses to start rather than continue. A stale column
            # that's still present but missing from the ORM mapping is a
            # latent IntegrityError waiting to fire on the next INSERT —
            # surfacing the misconfiguration at boot lets the operator
            # author a proper migration (table rebuild via the temp-copy
            # pattern) instead of debugging it from 500s in production.
            for stale in existing - model_cols:
                # SQLite refuses `DROP COLUMN` when the column is part of an
                # index. Pre-drop any auto-detected index that references the
                # stale column so a plain `index=True` on such a column does
                # not block the migration. UNIQUE
                # constraints / FKs still fall through to the error path
                # below — those need a proper table rebuild.
                idx_rows = conn.exec_driver_sql(
                    f"PRAGMA index_list({table.name})"
                ).fetchall()
                for idx_row in idx_rows:
                    idx_name = idx_row[1]
                    info = conn.exec_driver_sql(
                        f'PRAGMA index_info("{idx_name}")'
                    ).fetchall()
                    if any(ic[2] == stale for ic in info):
                        try:
                            conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{idx_name}"')
                            _logger.warning(
                                "schema migration: dropped index %s on stale column %s.%s",
                                idx_name, table.name, stale,
                            )
                        except Exception:
                            # Fall through to DROP COLUMN, which will then
                            # raise with a clearer instruction to the operator.
                            pass
                try:
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" DROP COLUMN "{stale}"'
                    )
                    _logger.warning(
                        "schema migration: dropped stale column %s.%s",
                        table.name, stale,
                    )
                except Exception as exc:
                    _logger.exception(
                        "schema migration: could not drop stale column %s.%s",
                        table.name, stale,
                    )
                    raise RuntimeError(
                        f"schema migration cannot drop stale column "
                        f"{table.name}.{stale}: {exc}. "
                        f"The column is likely part of a UNIQUE constraint "
                        f"or FK that SQLite cannot ALTER in place. Add a "
                        f"numbered migration in the wsi_app.migrations package "
                        f"that rebuilds {table.name!r} without the column."
                    ) from exc


def _warn_unapplied_column_constraints(table, col) -> None:
    """Log the constraints an ``ADD COLUMN`` just left out.

    SQLite's ADD COLUMN cannot carry UNIQUE, and an added REFERENCES clause is
    not something this reconciler emits, so a column added in place gets no
    UniqueConstraint (including a bare ``unique=True`` without ``index=True``,
    which SQLAlchemy models as a constraint, not an Index) and no foreign key /
    ON DELETE CASCADE. :func:`_reconcile_indexes` restores declared *indexes*
    only. No current column hits this — every such constraint sits on a column
    its table was created with — so this just keeps a future one from
    silently losing its backstop; the fix is a numbered table-rebuild
    migration.
    """
    unique_by_index = {
        tuple(c.name for c in idx.columns) for idx in table.indexes if idx.unique
    }
    missing = [
        "UNIQUE(" + ", ".join(names) + ")"
        for names in (
            tuple(c.name for c in uc.columns)
            for uc in table.constraints
            if isinstance(uc, UniqueConstraint) and col.name in uc.columns
        )
        if names not in unique_by_index
    ]
    missing += [
        f"FOREIGN KEY -> {fk.target_fullname}"
        + (f" ON DELETE {fk.ondelete}" if fk.ondelete else "")
        for fk in col.foreign_keys
    ]
    if missing:
        _logger.warning(
            "schema migration: added column %s.%s without %s — ALTER TABLE ADD "
            "COLUMN cannot create it. Add a numbered migration in the "
            "wsi_app.migrations package that rebuilds %r if it matters.",
            table.name, col.name, "; ".join(missing), table.name,
        )


# Indexes a past release may have declared and this one does not, as
# (table, index name). _reconcile_indexes only ever *creates* — it leaves an
# undeclared index alone, since an operator may have added one of their own —
# so a DB created by an older release would keep maintaining a retired index
# on every INSERT forever. Each one listed here is dropped by name instead.
# Idempotent (IF EXISTS), and harmless across a downgrade: the older binary's
# own reconcile re-creates the index it declares.
_RETIRED_INDEXES: tuple[tuple[str, str], ...] = (
    # Superseded by ix_page_views_kind_occurred_at, of which `kind` is the
    # prefix: every lookup it served, the composite serves too (see
    # models.PageView), and it cost each beacon INSERT one more index update.
    ("page_views", "ix_page_views_kind"),
)


def _reconcile_indexes(bind=None) -> None:
    """Create indexes the ORM declares but the live SQLite schema lacks
    (behind ``bind``, an Engine; the app's own by default), and drop the
    retired ones in ``_RETIRED_INDEXES``.

    ``Base.metadata.create_all`` builds a table's indexes only when it creates
    the table itself, and ``ALTER TABLE ADD COLUMN`` cannot carry UNIQUE — so
    on every DB upgraded in place, a column added by
    :func:`_apply_column_migrations` arrives *bare*, with none of the
    ``unique=True`` / ``index=True`` indexes the model declares. Nothing else
    creates them.

    That silently disarms the UNIQUE backstops other code explicitly relies on
    for race-freedom rather than merely for speed:

      * ``slides.wsi_filename_norm`` — the upload/replace handlers let two
        concurrent uploads of the same filename both pass an in-Python
        duplicate check and count on the IntegrityError to reject the loser.
      * ``slides.public_id`` / ``tours.public_id`` — the mint sites are wrapped
        in retry-on-IntegrityError loops that never fire without the index, so
        a collision is stored instead of re-minted.

    plus the plain lookup indexes every public tile request goes through.

    A UNIQUE index that cannot be created because the table *already* holds
    duplicates is logged at ERROR and skipped, not raised: the DB has been
    running without the constraint (the gap the index closes), and refusing
    to boot would turn stale duplicate rows into a total outage. The operator
    needs to de-duplicate and restart to get the backstop back.

    Creating an index on a large existing table (a composite added to
    ``page_views`` on a DB upgraded in place) reads the whole table once, at
    the first boot that declares it; every later boot finds it present.
    """
    with (engine if bind is None else bind).begin() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.exec_driver_sql(
                f"PRAGMA table_info({table.name})"
            ).fetchall()
            if not rows:
                continue  # table doesn't exist yet — create_all handled it
            live_cols = {r[1] for r in rows}
            existing_idx = {
                r[1]
                for r in conn.exec_driver_sql(
                    f"PRAGMA index_list({table.name})"
                ).fetchall()
            }
            declared_idx = {index.name for index in table.indexes}
            for retired_table, retired in _RETIRED_INDEXES:
                # Never drop one the model has since declared again.
                if retired_table != table.name or retired in declared_idx:
                    continue
                if retired in existing_idx:
                    conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{retired}"')
                    _logger.warning(
                        "schema migration: dropped retired index %s on %s",
                        retired, table.name,
                    )
            for index in table.indexes:
                if index.name in existing_idx:
                    continue
                # Skip an index whose columns aren't all live yet: the DROP
                # phase may still be pending, or a column-add was skipped.
                names = [getattr(c, "name", None) for c in index.columns]
                if not names or not all(n in live_cols for n in names):
                    continue
                try:
                    index.create(bind=conn, checkfirst=True)
                except Exception as exc:
                    _logger.error(
                        "schema migration: could not create %sindex %s on %s(%s): "
                        "%s. Rows violating it already exist; de-duplicate them "
                        "and restart to restore the constraint.",
                        "UNIQUE " if index.unique else "",
                        index.name, table.name, ", ".join(names), exc,
                    )
                else:
                    _logger.warning(
                        "schema migration: created missing %sindex %s on %s(%s)",
                        "UNIQUE " if index.unique else "",
                        index.name, table.name, ", ".join(names),
                    )


def _warn_nullability_divergence() -> None:
    """Log any column the ORM declares NOT NULL that is nullable on disk.

    The reconciler only ever ADDs and DROPs columns — it cannot change an
    existing column's nullability, because SQLite has no
    ``ALTER COLUMN ... SET NOT NULL`` and the only route is a full table
    rebuild. So the two ways a DB comes into being can disagree:
    ``create_all`` emits the strict shape, while a column added later by
    :func:`_apply_column_migrations` arrives nullable whenever its default
    isn't a constant literal (``func.now()``, or a Python-side default such
    as ``_mint_public_id``) — see :func:`_column_add_clause`.

    Nothing here is load-bearing at runtime: the ORM supplies a value on
    every insert either way. What is lost is the DB-level backstop against a
    write that bypasses the ORM, and — more practically — the guarantee that
    a restored archive has the same shape as a fresh install. Reporting it at
    boot is what keeps that divergence from going unnoticed for months; the
    fix is a numbered migration that rebuilds the table.

    Never raises: a shape that has been serving traffic must keep serving it.
    """
    diverged: list[str] = []
    try:
        with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                rows = conn.exec_driver_sql(
                    f"PRAGMA table_info({table.name})"
                ).fetchall()
                if not rows:
                    continue  # table doesn't exist yet — create_all handles it
                live_not_null = {r[1]: bool(r[3]) for r in rows}
                for col in table.columns:
                    if col.nullable or col.name not in live_not_null:
                        continue
                    if not live_not_null[col.name]:
                        diverged.append(f"{table.name}.{col.name}")
    except Exception:
        _logger.warning(
            "could not check column nullability against the ORM", exc_info=True,
        )
        return

    if diverged:
        _logger.warning(
            "schema drift: %d column(s) the ORM declares NOT NULL are nullable "
            "in this database (%s). The boot-time reconciler cannot fix this — "
            "SQLite needs a table rebuild. Add a numbered migration in the "
            "wsi_app.migrations package if the constraint matters.",
            len(diverged), ", ".join(sorted(diverged)),
        )


def _column_add_clause(col) -> str:
    """Render the column-definition tail of an `ALTER TABLE ADD COLUMN`.

    SQLite ALTER TABLE rejects function-valued DEFAULTs (e.g. now()), so
    columns whose default isn't a literal have the DEFAULT dropped and the
    column added nullable. The ORM still stamps a value on insert via the
    runtime default; existing rows just keep NULL until they're rewritten.
    """
    type_sql = col.type.compile(dialect=engine.dialect)
    parts = [f'"{col.name}"', type_sql]
    default_sql = column_default_literal(col)
    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
        if not col.nullable:
            parts.append("NOT NULL")
    return " ".join(parts)


class _BootLogBuffer(logging.Handler):
    """Collects what the ``wsi_app`` tree logs while the lifespan sets up the
    schema, for the error-log capture to replay once ``error_logs`` exists.

    Bounded: a boot that logs more than this keeps only its newest records in
    the DB (journald, fed by the stderr handler, still has every one).
    """

    _CAPACITY = 1000

    def __init__(self) -> None:
        super().__init__()
        self._records: "collections.deque[logging.LogRecord]" = collections.deque(
            maxlen=self._CAPACITY
        )

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)

    @classmethod
    def attach(cls) -> "_BootLogBuffer":
        handler = cls()
        logging.getLogger("wsi_app").addHandler(handler)
        return handler

    def detach(self) -> list[logging.LogRecord]:
        """Stop collecting; return what was collected, oldest first."""
        logging.getLogger("wsi_app").removeHandler(self)
        return list(self._records)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # All startup side-effects live here so importing the module is free
    # of I/O. This keeps unit tests fast (they can decide whether to
    # instantiate the lifespan via `with TestClient(app) as c:`) and lets
    # uvicorn workers re-run the same setup deterministically on each
    # restart. bootstrap_admin and the cache loaders are themselves
    # idempotent, so running them on every worker boot is safe.
    #
    # Hold what the ``wsi_app`` tree logs until the error-log capture can
    # take it: the schema block below may be what creates error_logs (or adds
    # its columns), and the capture's writer thread must not contend with that
    # DDL for the SQLite write lock. Replayed into the capture right after.
    boot_log = _BootLogBuffer.attach()
    try:
        # Pin per-worker sync-request concurrency to the connection-pool
        # capacity so a crowd can't admit more concurrent DB-touching requests
        # than the pool can back (long holds otherwise surface as QueuePool
        # checkout timeouts under load). Runs inside the event loop, before
        # any request is served.
        configure_request_concurrency()
        # Hold a cross-worker lock around schema creation + column migrations
        # so concurrent worker boots (uvicorn --workers N) can't race the same
        # CREATE/ALTER and crash with duplicate-column / snapshot errors on the
        # first boot after an upgrade. See _startup_schema_lock for details.
        with _startup_schema_lock():
            # Before anything opens the DB: a restore apply that died mid-swap
            # may have left the data directory half-applied. Opening it now
            # would serve that mix — and on a parked-but-not-yet-promoted
            # wsi.db path, SQLite would create an empty DB that makes the
            # rollback ambiguous.
            _reconcile_interrupted_restore_apply()
            # The downgrade guards, create_all, the column ADD phase, the
            # numbered migrations, the DROP phase and the indexes — held under
            # the same lock so a concurrent worker can't slip a migration in
            # between the guards and the DDL. One function, so a restore can
            # try exactly these steps on a copy of the staged DB before the
            # swap (backup.trial_schema_reconcile).
            reconcile_schema(engine)
            # Finally a read-only check for the one kind of drift neither
            # phase can repair.
            _warn_nullability_divergence()
            # Seed the admin and stamp the app version under the SAME
            # cross-worker lock so concurrent worker boots on a fresh install
            # can't both run the "no admin yet -> INSERT" path and crash the
            # loser on the users.username UNIQUE constraint (nor race the
            # app_meta upsert). bootstrap_admin is additionally
            # IntegrityError-safe to cover the best-effort path where the lock
            # could not be acquired.
            bootstrap_admin()
            # Reconcile DB-recorded app version with the running binary. May
            # raise if the DB is from a newer binary.
            bootstrap_app_version(engine)
            # A changed DZI_TILE_SIZE / DZI_OVERLAP re-cuts every pyramid under
            # the same tile URLs: move every slide to a new generation (new
            # URLs) before any tile is served.
            from . import dzi as _dzi
            _dzi.reconcile_tile_geometry(engine)
            # A restore that could not sign out the sessions issued before it
            # left a marker: retry that now, before any request is
            # authenticated, and refuse to serve while it keeps failing. Then
            # a restore whose database had no auth_settings row (the retry
            # may be one) left the session generation it moved to beside the
            # DB; record it before the auth cache below is loaded.
            from . import backup as _backup
            _backup.retry_pending_session_revocation()
            _backup.apply_session_generation_floor()
    finally:
        boot_backlog = boot_log.detach()
    # error_logs now exists with its full column set: start the DB error-log
    # capture (a logging handler on the ``wsi_app`` tree → error_logs table)
    # before anything else runs, seeded with the records buffered above, and
    # apply its retention caps. Best-effort: a capture-setup failure must
    # never keep the app from booting, and journald keeps the records
    # regardless.
    try:
        from . import error_log as _error_log
        _error_log.install_error_log_capture(backlog=boot_backlog)
        _error_log.prune_at_boot()
    except Exception:
        _logger.exception("error-log capture setup at startup failed; continuing")
    ensure_dirs()
    _load_network_cache()
    _load_auth_cache()
    _warn_open_admin_register()
    _warn_short_jwt_secret()
    # Surface a missing / unwritable restore IPC directory at boot. The
    # ``request_apply`` path writes ``RESTORE_REQUEST_PATH`` (default
    # ``/run/wsi/restore.request``) and would otherwise fail with a
    # cryptic 500 the first time the operator clicks "Apply restore" —
    # weeks after the underlying tmpfiles.d / systemd misconfiguration.
    _warn_restore_ipc_dir()
    _warn_backup_dir()
    # Reclaim disk from abandoned restore stages and prune ancient job
    # records so has_running() doesn't keep parsing thousands of stale
    # entries. Both are best-effort; failures are logged but never fatal.
    try:
        from . import backup as _backup
        from . import jobs as _jobs
        _backup.reclaim_abandoned_job_files()
        _backup.sweep_stale_staging()
        _backup.sweep_rollback_trash()
        _jobs.store.prune_terminal()
    except Exception:
        _logger.exception("backup/jobs janitor at startup failed; continuing")
    # Reap placeholder slide rows (empty wsi_filename) left behind when a
    # process died mid-upload/import, plus their orphan WSI/thumb files. The
    # in-process cleanup can't run if the worker was SIGKILLed, so a boot sweep
    # is the backstop. Best-effort; never fatal.
    try:
        from .admin_helpers import reap_orphan_slide_reservations
        with SessionLocal() as db:
            reap_orphan_slide_reservations(db)
    except Exception:
        _logger.exception(
            "orphan slide reservation reaper at startup failed; continuing"
        )
    # Settle the staged / parked files of an upload or WSI replace whose
    # worker was killed mid-swap (see reap_interrupted_slide_swaps).
    try:
        from .admin_helpers import reap_interrupted_slide_swaps
        with SessionLocal() as db:
            reap_interrupted_slide_swaps(db)
    except Exception:
        _logger.exception("interrupted slide swap reaper at startup failed; continuing")
    # Apply the audit-history retention caps (WSI_AUDIT_LOG_MAX_ROWS /
    # _MAX_AGE_DAYS). Best-effort like the janitors above; the table is
    # also pruned opportunistically as events are written.
    try:
        from . import audit_log as _audit_log
        _audit_log.prune_at_boot()
    except Exception:
        _logger.exception("audit_logs prune at startup failed; continuing")
    # Apply the analytics retention caps (WSI_ANALYTICS_LOG_MAX_ROWS /
    # _MAX_AGE_DAYS) to the high-frequency page_views / slide_dwell_events /
    # tour_sessions tables. Best-effort like the janitors above; the tables are
    # also pruned opportunistically (throttled) as beacons are written.
    try:
        from . import analytics as _analytics
        _analytics.prune_at_boot()
    except Exception:
        _logger.exception("analytics prune at startup failed; continuing")
    # Live-sync orphan-room reaper (idle until rooms are created).
    from .sync_room import manager as _sync_manager
    from .routers.sync import shutdown_rdns_executor
    await _sync_manager.start_gc()
    # Periodically re-read auth / IP-filter settings so a PATCH on one worker
    # converges everywhere (including the separate wsi-sync process) without a
    # restart. Disabled when the interval is <= 0 (dev / tests): every process
    # but the one that handled a PATCH then keeps its startup snapshot.
    settings_refresh_task: "asyncio.Task | None" = None
    if _SETTINGS_REFRESH_SECONDS > 0:
        settings_refresh_task = asyncio.create_task(
            _settings_refresh_loop(_SETTINGS_REFRESH_SECONDS)
        )
    try:
        yield
    finally:
        if settings_refresh_task is not None:
            settings_refresh_task.cancel()
            try:
                await settings_refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        await _sync_manager.stop_gc()
        # Release the reverse-DNS thread pool. Without this, threads blocked
        # in libc's gethostbyaddr would keep the process alive past worker
        # graceful-shutdown, forcing systemd to fall back on KillMode timeout.
        shutdown_rdns_executor()
        # Detach the error-log handler and stop its listener thread so
        # repeated `with TestClient(app):` cycles don't stack duplicates and
        # the worker can exit cleanly.
        try:
            from . import error_log as _error_log
            _error_log.shutdown_error_log_capture()
        except Exception:
            _logger.exception("error-log capture shutdown failed; continuing")


# openapi_url=None also drops /docs and /redoc (both render that schema).
app = FastAPI(
    title="Tessera",
    lifespan=lifespan,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)


async def _log_unhandled_exception(request: Request, exc: Exception):
    """Capture uncaught request exceptions into ``error_logs`` before the
    default 500 goes out.

    There is no global handler otherwise, so an unhandled exception would be
    logged only on uvicorn's own ``uvicorn.error`` logger — outside the
    ``wsi_app`` tree the DB error-log handler listens on — and would never
    reach the admin log surface. Instead it is logged on a ``wsi_app.unhandled``
    logger (path + method only in the ``extra``; never the request body, to
    bound PII in the stored row), and the SAME plain-text 500 Starlette would
    have produced is then returned. Starlette re-raises the original exception
    after this response is sent, so uvicorn/journald still logs it exactly as
    before — this handler only adds the DB capture (``skip_stderr`` keeps the
    traceback from reaching journald a second time via the app's own stderr
    handler).
    """
    logging.getLogger("wsi_app.unhandled").error(
        "unhandled exception during %s %s",
        request.method, request.url.path,
        exc_info=exc,
        extra={
            "request_path": request.url.path,
            "request_method": request.method,
            "skip_stderr": True,
        },
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


async def _handle_overflow_error(request: Request, exc: Exception):
    """Map an out-of-range integer to 422 rather than letting it 500.

    SQLite binds integers as signed 64-bit, so any id or ordinal at/above 2**63
    raises OverflowError at bind time — on an otherwise well-formed request.
    ``public.py`` bounds the ids it accepts with ``_MAX_DB_ID``, but the admin
    routers take dozens of ``int`` path and form parameters and bounding each
    one individually is both noisy and easy to forget on the next endpoint
    added. Handling the bind failure centrally gives all of them the 422 the
    request actually deserves, and stops a caller from filling ``error_logs``
    with 500s just by sending oversized numbers.

    Still logged (at warning, without a stack) so a genuine internal overflow
    — one not caused by caller input — remains visible on the admin log surface
    instead of being silently reported as a client error.
    """
    logging.getLogger("wsi_app.unhandled").warning(
        "integer out of range during %s %s: %s",
        request.method, request.url.path, exc,
        extra={"request_path": request.url.path, "request_method": request.method},
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Numeric value out of range"},
    )


_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})


async def _handle_http_exception(request: Request, exc: StarletteHTTPException):
    """FastAPI's own handler, except for a body that could not be spooled for
    lack of disk space.

    Starlette writes a multipart body to a spool file before the endpoint
    runs, and FastAPI turns ANY failure there into a 400 "There was an error
    parsing the body" — so a disk that filled up mid-upload (several uploads
    at once, a backup running beside them) would read as a malformed request, in
    English, instead of the localised 507 the endpoints answer for the same
    condition. That 400 keeps the OSError as its cause."""
    cause = exc.__cause__
    if (
        exc.status_code == 400
        and isinstance(cause, OSError)
        and cause.errno in _NO_SPACE_ERRNOS
    ):
        from .admin_helpers import error_detail

        _logger.warning(
            "request body for %s %s could not be spooled: %s",
            request.method, request.url.path, cause,
        )
        return JSONResponse(
            status_code=507,
            content={"detail": error_detail("adminDash.apiErr.uploadNoSpace")},
        )
    return await _fastapi_http_exception_handler(request, exc)


app.add_exception_handler(StarletteHTTPException, _handle_http_exception)

# Starlette resolves handlers by walking the exception's MRO, so the specific
# OverflowError registration below is chosen ahead of the Exception fallback.
app.add_exception_handler(OverflowError, _handle_overflow_error)

# Register on the broadest exception type: this is the fallback 500 path,
# so it never shadows the HTTPException / validation handlers FastAPI wires
# up for expected 4xx responses.
app.add_exception_handler(Exception, _log_unhandled_exception)


# The fire-and-forget analytics beacons. All three share one contract — a
# beacon must never 4xx the SPA, it degrades to an anonymous row instead — so
# the CF-Access "invalid assertion" 401 below has to exempt all three or the
# event streams the dashboard shows side by side diverge for exactly the
# population whose CF assertion went stale.
_ANALYTICS_BEACON_PATHS = frozenset({
    "/api/analytics/pageview",
    "/api/analytics/slide-dwell",
    "/api/analytics/tour-event",
})


class _UploadRoute(NamedTuple):
    """An /api/admin/ form route the pre-auth checks beyond "any staff". Each
    field only lets it refuse early (see _AdminUploadPreauthMiddleware); the
    endpoint's own checks stay the authority."""

    pattern: "re.Pattern[str]"
    # Roles the endpoint's require_* dependency admits.
    roles: frozenset[str]
    # Largest body the route accepts, as a getter so a settings change is
    # honoured.
    cap: Callable[[], int]
    # Which disk the declared body must fit before it is read (see
    # _upload_no_space): "data" — a slide upload, spooled and then staged
    # under data_dir/wsi (admin_helpers.upload_space_short); "spool" — a
    # restore / slide-import archive. Those are later copied into
    # BACKUP_DIR/.staging, whose copy and extraction check their own space,
    # but Starlette first spools the whole body to TMPDIR, which systemd
    # points at DATA_DIR/.upload-tmp — the data volume, not the backup one
    # (admin_helpers.archive_upload_space_short). No other check covers that.
    space_check: str
    # The 409 details the endpoint answers while the single background-job
    # slot is taken / while a restore-apply waits for its restart, or None
    # for a route that does not claim the slot.
    busy_detail: Any
    pending_detail: Any


# The patterns end in "$", NOT "\\Z", on purpose: "$" also matches before a
# trailing newline, and so do Starlette's route patterns (and nginx's PCRE
# locations), so ".../wsi%0A" reaches the endpoint. A stricter pattern here
# would classify that request as "not an upload" and let its whole body be read
# before the endpoint's 403 / 413 / 507 / 409, which is exactly what this gate
# exists to prevent. These must accept everything the router sends to the
# endpoints (tests/test_upload_preauth.py pins the parity).
_UPLOAD_ROUTES: tuple[_UploadRoute, ...] = (
    _UploadRoute(
        pattern=re.compile(r"^/api/admin/restore$"),
        roles=frozenset({ROLE_ADMIN}),
        cap=lambda: settings.backup_max_upload_bytes,
        space_check="spool",
        busy_detail=_admin_backup._JOB_BUSY_DETAIL,
        pending_detail=_admin_backup._RESTORE_PENDING_DETAIL,
    ),
    _UploadRoute(
        pattern=re.compile(r"^/api/admin/slides/import$"),
        roles=frozenset({ROLE_ADMIN}),
        cap=lambda: settings.backup_max_upload_bytes,
        space_check="spool",
        busy_detail=_admin_slide_transfer._TRANSFER_BUSY_DETAIL,
        pending_detail=_admin_slide_transfer._RESTORE_PENDING_DETAIL,
    ),
    _UploadRoute(
        pattern=re.compile(r"^/api/admin/slides/(?:upload|[0-9]+/wsi)$"),
        roles=MANAGER_OR_ABOVE,
        cap=lambda: settings.wsi_max_upload_bytes,
        space_check="data",
        busy_detail=None,
        pending_detail=None,
    ),
)

# The multipart envelope (boundary, part headers, small form fields) adds a few
# hundred bytes to the file, hence the slack: the size check only has to catch
# a body that is hopeless, the endpoint still enforces the exact file cap.
_UPLOAD_CAP_SLACK = 1024 * 1024

def _upload_route(path: str) -> Optional[_UploadRoute]:
    for route in _UPLOAD_ROUTES:
        if route.pattern.match(path):
            return route
    return None


def _upload_no_space(route: _UploadRoute, declared: int) -> bool:
    """True when an upload's declared body would not fit where it is about to
    be written: a slide upload the data volume (spool + staged copy, see
    admin_helpers.upload_space_short), a restore / slide-import archive the
    spool directory with a reserve left free (see
    admin_helpers.archive_upload_space_short)."""
    from .admin_helpers import archive_upload_space_short, upload_space_short

    if route.space_check == "data":
        return upload_space_short(declared)
    if route.space_check == "spool":
        return archive_upload_space_short(declared)
    return False


def _upload_stream_no_space(route: _UploadRoute, received: int) -> bool:
    """True when a body that declared no length, ``received`` bytes of which are
    already spooled, can no longer be taken: the volume must hold what is still
    to be written, which is NOT the whole upload again — the bytes that have
    arrived are on disk, so the free space measured now already excludes them.
    Still to come: the next stretch of the body (``_UPLOAD_SPACE_RECHECK_BYTES``)
    and, for a slide, the staged copy of what is in (one more ``received``;
    the pre-body check asked for two copies because neither existed yet)."""
    from .admin_helpers import archive_upload_space_short, upload_space_short

    ahead = _UPLOAD_SPACE_RECHECK_BYTES
    if route.space_check == "data":
        return upload_space_short(received + ahead, factor=1)
    if route.space_check == "spool":
        return archive_upload_space_short(ahead)
    return False


def _upload_busy_detail(route: _UploadRoute) -> Any:
    """The 409 detail the route's endpoint would answer right now, or None."""
    if route.busy_detail is None:
        return None
    if _admin_backup._restore_apply_pending():
        return route.pending_detail
    if _jobs.store.has_running():
        return route.busy_detail
    return None


def _upload_preauth_status(
    path: str, authorization: str, content_length: str
) -> Optional[tuple[int, Any]]:
    """(status, detail) to refuse an admin form request with, or None to let
    it in. Blocking — a DB read, statvfs of the data volumes, a scan of the
    job store — so the middleware runs it on the threadpool."""
    token = bearer_token(authorization)
    if not token:
        return 401, "Missing token"
    db = SessionLocal()
    try:
        user = resolve_staff_user(token, db)
        role = user.role if user is not None else None
    finally:
        db.close()
    if role is None:
        return 401, "Invalid token"
    route = _upload_route(path)
    if route is None:
        return None if role in STAFF_ROLES else (403, "Forbidden")
    if role not in route.roles:
        return 403, "Forbidden"
    try:
        declared: Optional[int] = int(content_length)
    except ValueError:
        # No (usable) Content-Length, e.g. a chunked body: the endpoint
        # checks the size and the space once the body is spooled.
        declared = None
    if declared is not None and declared > route.cap() + _UPLOAD_CAP_SLACK:
        # Authorised, but the declared size is already over the cap: answer
        # before Starlette spools gigabytes to disk only for the endpoint to
        # refuse them.
        return 413, "Upload too large"
    if declared is not None and _upload_no_space(route, declared):
        # Same idea for the disk: the spool plus the staged copy of a slide
        # upload need room for twice the body; a restore / import archive's
        # spool needs room for it plus a reserve.
        from .admin_helpers import error_detail

        return 507, error_detail("adminDash.apiErr.uploadNoSpace")
    busy = _upload_busy_detail(route)
    if busy is not None:
        # The endpoint refuses before it reads the file too, but FastAPI has
        # parsed (spooled) the whole body by the time it runs.
        return 409, busy
    return None


# A body with no Content-Length (chunked) has nothing to check before it is
# read, so the checks run as it arrives. Free space is re-measured each time
# this many more bytes have come in.
_UPLOAD_SPACE_RECHECK_BYTES = 64 * 1024 * 1024


def _bounded_upload_receive(receive, route: _UploadRoute, *, declared: bool):
    """``receive`` for an upload route that enforces, while the body streams in,
    what _upload_preauth_status can only judge from the headers.

    The pre-body checks read Content-Length; a chunked request has none, so it
    would pass them all and be spooled to the data volume in full (nginx lets
    restore / import bodies of any size through) before the endpoint's own
    checks refused it. This refuses it as soon as the bytes received exceed the
    route's cap (413) and, for a body that declared no length, as soon as the
    volume cannot hold what has arrived (507). Raising from ``receive`` surfaces
    as the HTTPException the endpoint's body parsing re-raises, so the client
    gets the same JSON error as a declared-size refusal and the parser stops
    reading."""
    limit = route.cap() + _UPLOAD_CAP_SLACK
    received = 0
    rechecked_at = 0

    async def bounded_receive():
        nonlocal received, rechecked_at
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(status_code=413, detail="Upload too large")
            if not declared and received - rechecked_at >= _UPLOAD_SPACE_RECHECK_BYTES:
                rechecked_at = received
                if await run_in_threadpool(_upload_stream_no_space, route, received):
                    from .admin_helpers import error_detail

                    raise HTTPException(status_code=507, detail=error_detail("adminDash.apiErr.uploadNoSpace"))
        return message

    return bounded_receive


class _AdminUploadPreauthMiddleware:
    """Authenticate /api/admin/ form requests before their body is read.

    FastAPI parses a File()/Form() body before it resolves dependencies.
    Starlette spools every multipart file part to a temp file as it does, and
    holds urlencoded fields in memory (up to 1000 fields of 1 MiB each) — so
    without this, require_admin & co. only run after the whole body (up to
    the nginx cap: unlimited for restore and import, 8 GiB for slides) has
    already been written to disk or buffered. Any client, logged in or not,
    could fill the disk or memory with parallel requests that are rejected
    only afterwards. Every form endpoint under /api/admin/ is staff-only, so
    an unauthenticated or under-privileged request is refused here, with the
    same status and detail the endpoint's own dependency would give, before a
    byte of the body is consumed. (Other content types never reach a form
    parser: FastAPI hands a form endpoint an empty form without reading.)

    The upload routes in _UPLOAD_ROUTES are refused early for what their
    endpoints would otherwise only find out after the whole body: a declared
    size over the route's cap (413), a slide that would not fit the data
    volume (507), or the background-job slot a restore / import needs being
    taken (409). A restore / import archive that would not fit the upload
    spool is refused with that 507 too.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("method") in ("POST", "PUT", "PATCH", "DELETE")
            and scope.get("path", "").startswith("/api/admin/")
        ):
            headers = Headers(scope=scope)
            ctype = headers.get("content-type", "").lower()
            if ctype.startswith(("multipart/", "application/x-www-form-urlencoded")):
                refusal = await run_in_threadpool(
                    _upload_preauth_status,
                    scope["path"],
                    headers.get("authorization", ""),
                    headers.get("content-length", ""),
                )
                if refusal is not None:
                    status, detail = refusal
                    response = JSONResponse(status_code=status, content={"detail": detail})
                    await response(scope, receive, send)
                    return
                route = _upload_route(scope["path"])
                if route is not None:
                    receive = _bounded_upload_receive(
                        receive, route, declared=bool(headers.get("content-length", "").strip()),
                    )
        await self.app(scope, receive, send)


# Innermost of the middlewares (registered first): the IP filter and the
# app-access gate still run before it, exactly as for every other request.
app.add_middleware(_AdminUploadPreauthMiddleware)


def _staff_auth_refusal(request: Request, bearer_presented: bool) -> Optional[JSONResponse]:
    """The gate's answer to an /api/admin/* request that nothing admitted, or
    None for any other path.

    It is the 401 ``auth.get_current_user`` gives the same request under
    mode=none: "Invalid token" when its Bearer failed the staff check
    (expired, revoked, malformed), "Missing token" when it carried none — a
    bookmarked or newly opened dashboard page loads before any staff login.
    The dashboard recognises a dead or absent staff session by those details
    in every mode (frontend ``isStaffTokenRejection``) and returns to its own
    login screen. The gate's generic "Authentication required" — and, under
    basic, its ``WWW-Authenticate`` challenge, which pops the browser's native
    credential dialog over the request — describe an app-access login the
    admin API never asks for.

    That holds for a signed-link download (``GET …/download?t=``) too: one
    with a valid token was admitted before this point (see
    ``app_auth.signed_download_valid``), and an expired or forged one is not
    rescued by signing in to the app — the route would still refuse it."""
    path = request.url.path
    if not path.startswith("/api/admin/"):
        return None
    if path in _app_auth.ADMIN_LOGIN_PATHS:
        # The dashboard sign-in is inside the app-access gate: say that the
        # app sign-in comes first, so the login form sends the visitor there
        # instead of reporting wrong credentials.
        from .admin_helpers import error_detail

        return JSONResponse(
            status_code=401, content={"detail": error_detail("admin.err.appAuthRequired")},
        )
    detail = "Invalid token" if bearer_presented else "Missing token"
    return JSONResponse(status_code=401, content={"detail": detail})


# NOTE on ordering: Starlette inserts each @app.middleware at the front of
# the stack, so the LAST-registered middleware is the outermost and runs
# first. ip_filter_middleware is therefore defined *after* this one so the
# network ACL is the outermost gate: a blocked IP is rejected with 403
# before any auth work runs (CF JWT verification, basic-auth password
# hashing, LDAP binds, per-request DB sessions). Keep ip_filter_middleware
# defined last if you add more middleware here.
@app.middleware("http")
async def app_auth_middleware(request: Request, call_next):
    # ONE settings snapshot for the whole request (see app_auth.auth_snapshot):
    # the Basic-header branch below verifies under ``mode`` and stamps the
    # cookie it mints with this snapshot's generation. Reading the generation
    # separately, later, would let a basic->ldap switch landing mid-request mint a
    # ``basic:`` cookie carrying the NEW generation — one the switch's bump
    # was meant to revoke, accepted under ldap for its whole TTL.
    auth_snap = _app_auth.auth_snapshot()
    mode = auth_snap.mode
    path = request.url.path or "/"

    # Bypass paths (health check, app login, OAuth callback) are always
    # reachable so unauthenticated users can hit the app login endpoints.
    # The dashboard login is not among them (app_auth.ADMIN_LOGIN_PATHS).
    if _app_auth.is_bypass_path(path):
        return await call_next(request)

    # Staff (admin / manager / instructor) authenticated with the existing JWT
    # bearer token bypass both the CF Access gate and the app-auth gate so they
    # aren't forced to log in twice (or three times, with CF in front). All
    # staff — not just admins — are admitted here: the token-keyed draft-preview
    # bypass (staff_token_valid in routers/public.py and dzi.py) is only
    # reachable once the request clears this middleware, so gating here at
    # admin-only would 401 the managers/instructors that bypass was built for
    # before their request ever reaches the route.
    auth_header = request.headers.get("authorization", "")
    # Only a Bearer header can carry a staff token, so any other scheme (every
    # Basic-header request under basic mode) skips the check outright.
    # staff_token_valid opens a SessionLocal and runs a synchronous DB read
    # (existence + is_active + token_version re-check, so a disabled / demoted /
    # password-reset user's stale JWT stops bypassing immediately); offload it
    # to the threadpool so a concurrent writer holding the SQLite lock
    # (busy_timeout up to 5s) can't freeze the whole event loop. Mirrors the
    # Basic password-verify offload below.
    bearer_presented = bearer_token(auth_header) is not None
    if bearer_presented and await run_in_threadpool(_app_auth.staff_token_valid, auth_header):
        # Recorded for /api/edge-authz, whose per-slide check admits a draft
        # slide's tiles only for a staff bearer: reusing this verdict spares
        # the probe a second users-table read on every draft-preview tile.
        request.state.staff_bearer = True
        return await call_next(request)

    # A signed-link download (``…/download?t=``) carries a staff credential of
    # its own and no Authorization header, since the browser navigates to it;
    # see app_auth.signed_download_valid. Only the three download routes pay
    # for the threadpool hop and the users-table read.
    download_token = request.query_params.get("t")
    if (
        download_token
        and path.endswith("/download")
        and await run_in_threadpool(
            _app_auth.signed_download_valid, request.method, path, download_token
        )
    ):
        return await call_next(request)

    # Cloudflare Access gate (independent of `mode`). Runs whenever the
    # integration is configured; Path A (external user via CF Tunnel)
    # carries a verified Cf-Access-Jwt-Assertion and is admitted here,
    # while Path B (internal user via CF bypass) carries no JWT and
    # falls through to the mode-based auth below.
    client_host = request.client.host if request.client else "127.0.0.1"
    # cf_jwt_status -> verify_jwt -> PyJWKClient performs a *synchronous*
    # urllib HTTPS fetch of the JWKS whenever the cache is cold/stale (~every
    # 5 min, plus on any unknown kid), with a default 30s socket timeout.
    # Running it inline would freeze this worker's entire event loop while the
    # fetch is in flight, so offload it to the threadpool like the Basic
    # password verify below.
    # No assertion header (the common LAN / bypass case) short-circuits to
    # "absent" without the threadpool hop — cf_jwt_status would return that
    # immediately anyway.
    if request.headers.get(_cf_access.CF_JWT_HEADER):
        cf_status, _cf_claims = await run_in_threadpool(
            _cf_access.cf_jwt_status, request.headers, client_host
        )
    else:
        cf_status, _cf_claims = "absent", None
    if cf_status == "valid":
        # CF Access already authenticated this user. Skip mode-based
        # checks entirely — the verified email is captured by the
        # analytics beacon via probe_identity() on each pageview.
        response = await call_next(request)
        # Carry the verified identity onto the LAN-direct host: when a
        # shared cookie domain is configured, mint a session cookie scoped
        # to it so the public -> LAN redirect lands already
        # authenticated and the LAN host records the same email. Skipped when
        # WSI_SESSION_COOKIE_DOMAIN is unset (stock deployments unaffected).
        # Threadpool: it decodes the incoming session cookie, and a basic
        # cookie is re-checked against the stored credential (a DB read on a
        # cache miss).
        email = _cf_access.email_from_claims(_cf_claims or {})
        if email and _app_auth.SESSION_COOKIE_DOMAIN:
            await run_in_threadpool(
                _app_auth.maybe_carry_cf_session, request, response, email
            )
        return response
    if cf_status == "invalid" and path not in _ANALYTICS_BEACON_PATHS:
        # The request asserted a CF JWT that failed verification, or arrived
        # from an untrusted hop. 401 so a stale / forged assertion can't leak
        # through.
        return JSONResponse(
            status_code=401,
            content={"detail": "Cloudflare Access verification failed"},
        )
    # Fall through to the mode-based auth. Two ways to land here:
    #
    #   cf_status == "absent" — no CF JWT at all, so Path B (internal user via
    #   the CF bypass) works unchanged.
    #
    #   cf_status == "invalid" on an analytics beacon — the beacon is
    #   best-effort and must not 4xx the SPA, so a stale assertion degrades to
    #   an anonymous row (+ WARN) instead of a rejection. It deliberately falls
    #   through to the gate below rather than being admitted outright: admitting
    #   it would mean anyone could reach the beacon on a gated install just by
    #   attaching a junk Cf-Access-Jwt-Assertion header, since a junk header is
    #   exactly what classifies as "invalid". Sending no header at all is
    #   already gated here, and short-circuiting would make a forged header
    #   strictly more powerful than none — so the beacon is admitted only
    #   when the request would have been authorised anyway (mode=none, a
    #   valid session cookie, break-glass, LAN-direct).

    # Break-glass exemption (opt-in via WSI_BREAKGLASS_NOAUTH): a plaintext,
    # non-tunnel request — the documented raw-IP HTTP fallback — skips the
    # app-access gate so it stays usable when TLS/DNS for the LAN-direct host
    # is down (and, under google_oauth, because interactive login cannot
    # complete against a private-IP redirect URI). No-op unless the flag is on;
    # the qualifying signals are nginx-set and not client-forgeable. See
    # app_auth.breakglass_exempt() for the trust argument.
    if _app_auth.breakglass_exempt(request.headers, client_host):
        return await call_next(request)

    # Direct LAN-IP exemption (opt-in via the dashboard toggle): a request
    # reaching the app by its bare private-IP literal from a LAN client — and
    # not via the Cloudflare Tunnel — skips the gate. Treated as an emergency /
    # trusted-LAN access route; no-op unless the operator enabled it. See
    # app_auth.lan_direct_exempt() for the trust argument.
    if _app_auth.lan_direct_exempt(request.headers, client_host):
        return await call_next(request)

    if mode == "none":
        return await call_next(request)

    if mode == "basic":
        # Accept either an existing session cookie (issued by the form-based
        # /api/auth/login path) or a fresh HTTP Basic header. Without the
        # cookie branch, /api/auth/login succeeds and sets wsi_session but
        # every subsequent request still 401s because the middleware would
        # only consult the Authorization header — making form-login a no-op.
        cookie = request.cookies.get(_app_auth.SESSION_COOKIE)
        # Threadpool: a basic cookie is re-checked against the stored
        # credential (a short cached DB read on a miss).
        if cookie and await run_in_threadpool(_app_auth.decode_session_token, cookie):
            return await call_next(request)
        creds = _app_auth.parse_basic_header(auth_header)
        if creds:
            # Per-IP throttle on the Basic-header brute-force surface, sharing
            # the same bucket as POST /api/auth/login. Only requests that carry
            # a Basic header AND lack a valid session cookie reach the password
            # verify, so form-login clients (which present the cookie) never
            # touch this. Pre-check before the pbkdf2 verify so a throttled IP
            # pays nothing.
            throttle_key = app_login_throttle_key(
                _net.get_client_ip(request.headers, client_host)
            )
            retry_after = _login_throttle.retry_after_seconds(
                throttle_key,
                max_failures=settings.app_login_ip_max_failures,
                window_seconds=settings.app_login_ip_window_seconds,
            )
            if retry_after > 0:
                if path == "/api/edge-authz":
                    # nginx's auth_request only understands 2xx / 401 / 403
                    # and turns anything else into a 500 for the tile,
                    # thumbnail or asset being gated. A throttled IP is
                    # simply not authenticated here.
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required"},
                    )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many failed login attempts. Try again later."},
                    headers={"Retry-After": str(retry_after)},
                )

            # Stamp the cookie with the generation this check ran under (see
            # app_auth.auth_snapshot): a revoke landing mid-verify then leaves
            # it stale, and the browser simply re-sends its Basic header.
            verified_generation = auth_snap.session_generation

            def _check():
                db = SessionLocal()
                try:
                    return _app_auth.verify_basic_credential(creds[0], creds[1], db)
                except BaseException:
                    # Roll back so the session doesn't leave a half-finished
                    # transaction sitting on its connection.
                    db.rollback()
                    raise
                finally:
                    db.close()
            credential = await run_in_threadpool(_check)
            if credential:
                _login_throttle.clear_user(throttle_key, creds[0])
                # The one place a Basic username is proven: the analytics
                # beacon attributes rows to this, never to the raw header
                # (which is never checked for exempted / CF-admitted requests).
                request.state.verified_basic_user = creds[0]
                response = await call_next(request)
                # Hand the browser a session cookie so its next requests take
                # the cheap cookie branch above instead of a pbkdf2 verify
                # each (and, with nginx bypassing the shared cache only for
                # Bearer, every tile / thumbnail stays a shared-cache HIT).
                # Same subject and attributes as the form login.
                response.set_cookie(
                    _app_auth.SESSION_COOKIE,
                    _app_auth.create_session_token(
                        f"basic:{creds[0]}",
                        credential=credential,
                        generation=verified_generation,
                    ),
                    max_age=_app_auth.session_ttl(),
                    httponly=True,
                    secure=_net.forwarded_proto(request.headers, request.url.scheme) == "https",
                    samesite="lax",
                    path="/",
                )
                return response
            # Passing the secret counts a repeat of this exact credential only
            # once per window: a tab still replaying a rotated password sends
            # it with every tile's edge-authz subrequest and would otherwise
            # lock the whole IP (a classroom behind NAT) out, correct new
            # password included. Each distinct guess still counts, and
            # verify_basic_credential remembers the rejection, so the repeats
            # cost no pbkdf2 either.
            _login_throttle.record_failure(
                throttle_key,
                window_seconds=settings.app_login_ip_window_seconds,
                max_failures=settings.app_login_ip_max_failures,
                username=creds[0],
                secret=creds[1],
            )
        refusal = _staff_auth_refusal(request, bearer_presented)
        if refusal is not None:
            return refusal
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="WSI", charset="UTF-8"'},
        )

    # mode in ("ldap", "google_oauth"): cookie-based session.
    cookie = request.cookies.get(_app_auth.SESSION_COOKIE)
    if cookie and _app_auth.decode_session_token(cookie):
        return await call_next(request)
    if _app_auth.is_gated_path(path):
        refusal = _staff_auth_refusal(request, bearer_presented)
        if refusal is not None:
            return refusal
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    # Otherwise the SPA shell is allowed through so the frontend can render
    # /login (the app-side router redirects unauthenticated users there).
    return await call_next(request)


# Registered last so it sits outermost in the middleware stack (see the
# ordering note above app_auth_middleware): the IP filter is a network ACL
# and must gate every request before the auth layer does any work.
@app.middleware("http")
async def ip_filter_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else "127.0.0.1"
    ip = _net.get_client_ip(request.headers, client_host)
    if not _net.ip_allowed(ip):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})
    return await call_next(request)


# API + DZI routes. Public read endpoints are served by their own router so
# the unauthenticated surface is easy to reason about; admin/mutation routes
# are split by domain across the routers/ subpackage.
app.include_router(public_router)
app.include_router(app_auth_router)
app.include_router(admin_auth_router)
app.include_router(admin_mfa_router)
app.include_router(admin_backup_router)
app.include_router(admin_slides_router)
app.include_router(admin_slide_transfer_router)
app.include_router(admin_metadata_router)
app.include_router(admin_tours_router)
app.include_router(admin_users_router)
app.include_router(admin_settings_router)
app.include_router(admin_system_router)
app.include_router(admin_history_router)
app.include_router(admin_logs_router)
app.include_router(admin_perf_router)
app.include_router(analytics_router)
app.include_router(sync_router)
app.include_router(dzi_router)

# Health check endpoint must be registered before the catch-all static mount
# so that it is reachable in both dev and production mode.
@app.get("/api/health")
def health():
    return {"ok": True}

# Static thumbs / tour assets. check_dir=False keeps StaticFiles from
# raising at app-construction time when the data directory hasn't been
# created yet (lifespan creates it). The mount still serves files
# correctly once the directory exists.
app.mount(
    "/thumbs",
    StaticFiles(directory=os.path.join(settings.data_dir, "thumbs"), check_dir=False),
    name="thumbs",
)

class _TourAssetFiles(StaticFiles):
    """StaticFiles for uploaded tour assets, served with defensive headers.

    Assets are author-uploaded and live on the app's own origin. The upload
    path makes a file's extension agree with its content, but files placed
    by hand (or restored from a backup) may not, and StaticFiles
    picks the Content-Type from the extension alone. So anything that is not
    one of the media types an asset is meant to be is forced to download
    under a sandboxing CSP — never rendered as a same-origin document — and
    ``nosniff`` stops a browser from second-guessing the declared type. The
    headers ride through nginx (and its cache) to the client unchanged.
    """

    _INLINE_EXTS = frozenset((
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf",
        ".mp3", ".ogg", ".oga", ".flac", ".wav", ".m4a", ".m4b",
        ".mp4", ".m4v", ".mov",
    ))

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if os.path.splitext(str(full_path))[1].lower() not in self._INLINE_EXTS:
            response.headers["Content-Disposition"] = "attachment"
            response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


app.mount(
    "/assets",
    _TourAssetFiles(directory=os.path.join(settings.data_dir, "assets"), check_dir=False),
    name="assets",
)

# The built SPA: ``make frontend-build`` copies frontend/dist here, and
# install.sh publishes each build into a sibling ``static.rel.<n>`` directory
# that ``static`` is atomically re-pointed at.
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
static_dir = os.path.abspath(static_dir)

# Paths that must never fall back to the SPA shell. /static-assets/ is Vite's
# build-asset dir (vite.config.ts assetsDir): a request for a chunk that is not
# there comes from a page loaded before a deploy replaced the build, and
# answering it with index.html and a 200 hands the browser HTML as JavaScript —
# a blank page — instead of the failed chunk load the SPA reloads on.
_NON_SPA_PREFIXES = ("/api/", "/dzi/", "/thumbs/", "/assets/", "/static-assets/")
_HASHED_ASSET_DIR = "static-assets"
# Every file Vite writes under static-assets/ has a content hash in its name,
# so a URL there never changes meaning and may be cached for good. index.html
# is what names the current hashes: it must be revalidated on every load, or a
# browser keeps booting the previous build (and its since-deleted chunks).
_CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
_CACHE_REVALIDATE = "no-cache"


def _spa_handler(static_dir: str):
    """Build the ASGI handler that serves the SPA out of ``static_dir``."""
    index_html = os.path.join(static_dir, "index.html")

    # Registered as Starlette's router default rather than as a catch-all
    # `/{path:path}` route. A catch-all always matches as Match.FULL, which
    # prevents Starlette's redirect_slashes from running — so
    # `/api/admin/slides/` (trailing slash) would never get a chance to
    # redirect to the canonical `/api/admin/slides`. As a default, this handler
    # is only invoked after every registered route has been tried and
    # redirect_slashes has had its chance, so trailing-slash variants of real
    # API routes resolve naturally to a 307 onto the canonical path.
    async def serve_spa(scope, receive, send):
        # Starlette's router.default also receives WebSocket scopes that fell
        # through the registered routes; replying with an HTTP-shaped
        # JSONResponse on a `websocket` scope produces a protocol error. Send
        # the websocket-close ASGI events instead so the client gets a clean
        # 1006 instead of a half-open socket.
        scope_type = scope.get("type")
        if scope_type == "websocket":
            try:
                await send({"type": "websocket.close", "code": 4404})
            except Exception:
                pass
            return
        if scope_type != "http":
            return
        path = scope.get("path", "/")
        rel_path = path.lstrip("/")
        # Serve an actual file from the static directory if it exists.
        if rel_path:
            try:
                # Resolved per request, not once: install.sh publishes a build
                # by re-pointing the `static` symlink at a new release dir and
                # reaps the old one, so a root resolved at import would keep
                # naming the deleted dir and turn every file of the new build
                # into the SPA fallback until the next restart.
                static_real = os.path.realpath(static_dir)
                candidate = os.path.realpath(os.path.join(static_dir, rel_path))
                # Guard against path traversal.
                is_servable = (
                    candidate.startswith(static_real + os.sep)
                    and os.path.isfile(candidate)
                )
            except (ValueError, OSError):
                # e.g. an embedded NUL byte in the percent-decoded path
                # (`/%00foo`): realpath/isfile raise ValueError, which would
                # otherwise surface as a 500 + traceback. Not a servable file.
                is_servable = False
            if is_servable:
                # Judged on the resolved file, so a dot-segment path can't
                # get index.html cached as an immutable asset.
                rel = os.path.relpath(candidate, static_real)
                headers = None
                if rel == "index.html":
                    headers = {"Cache-Control": _CACHE_REVALIDATE}
                elif rel.startswith(_HASHED_ASSET_DIR + os.sep):
                    headers = {"Cache-Control": _CACHE_IMMUTABLE}
                await FileResponse(candidate, headers=headers)(scope, receive, send)
                return
        # API / DZI / asset paths that reached the default have no matching
        # route and must not silently return the SPA shell. Judge the prefix
        # on the path with slashes merged and dot segments resolved — the form
        # nginx keys its cache on. Otherwise "//dzi/<tile>" misses the prefix
        # test, gets index.html with a 200, and nginx files that HTML under the
        # real tile's cache key.
        canonical = posixpath.normpath(re.sub(r"/+", "/", "/" + path))
        if any(
            path.startswith(p) or (canonical + "/").startswith(p)
            for p in _NON_SPA_PREFIXES
        ):
            await JSONResponse(status_code=404, content={"detail": "Not Found"})(scope, receive, send)
            return
        # Fall back to index.html so React Router handles the path client-side.
        await FileResponse(
            index_html, headers={"Cache-Control": _CACHE_REVALIDATE}
        )(scope, receive, send)

    return serve_spa


if os.path.isdir(static_dir):
    app.router.default = _spa_handler(static_dir)
else:
    @app.get("/")
    def dev_root_info():
        return {
            "detail": "Frontend is not served by the backend in dev mode.",
            "viewer": "http://localhost:5173/",
            "admin": "http://localhost:5173/admin",
            "health": "/api/health",
        }
