"""Shared pytest fixtures for the wsi_app test suite.

The production app reads ``DATABASE_URL``, ``DATA_DIR`` and the other paths it
writes to at import time via ``wsi_app.config.Settings``. To keep tests fully
isolated they are all pointed at a per-session temporary directory **before**
importing anything from ``wsi_app`` — unconditionally, so a shell that has
/etc/wsi/wsi.env loaded can never aim the suite's destructive tests (restores,
resets, deletes) at the live database. The directory is removed when the
session ends. Each test gets a clean ``RoomManager`` and an admin token bound
to a fresh user row.
"""
from __future__ import annotations

import os
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Optional

import pytest

# IMPORTANT: env vars must be in place before any wsi_app import so that the
# module-level engine / settings pick them up.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="wsi-tests-"))
# Everything the app writes outside the DB, overridden even when already set:
# with wsi.env sourced these would name /var/lib/wsi and /run/wsi.
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_ROOT}/wsi.db"
os.environ["DATA_DIR"] = str(_TMP_ROOT)
os.environ["BACKUP_DIR"] = str(_TMP_ROOT / "backups")
os.environ["RESTORE_REQUEST_PATH"] = str(_TMP_ROOT / "run" / "restore.request")
os.environ["RESTORE_RESULT_PATH"] = str(_TMP_ROOT / "run" / "restore.result")
os.environ["RESTORE_JOURNAL_PATH"] = ""
os.environ["RESTART_TRIGGER_PATH"] = str(_TMP_ROOT / "run" / "restart.trigger")
os.environ["RESTORE_RESTART_CMD"] = ""
# At least auth.MIN_JWT_SECRET_BYTES (32): PyJWT warns on every HS256
# sign/verify with a shorter key.
os.environ["JWT_SECRET"] = "test-secret-0123456789abcdef0123456789abcdef"
# Tests bypass the bootstrap-admin gate by inserting users directly via the
# admin_user fixture; they don't need a configured ADMIN_BOOTSTRAP_PASSWORD.
# Set the dev-bypass flag so module import never raises on the unconfigured
# default.
os.environ.setdefault("WSI_ALLOW_INSECURE_DEFAULTS", "1")
# Backup/restore tests run the apply path in-process so they don't depend on
# a service restart triggered by systemd.
os.environ.setdefault("BACKUP_DEV_INPROCESS_APPLY", "1")
# No free-space reserve under tour-asset uploads: the 1 GiB default would make
# every upload test answer 507 on a host / CI runner with less free than that.
# Not a setdefault, so a sourced wsi.env can't reinstate it either. The test of
# the reserve sets its own value (and fakes disk_usage).
os.environ["TOUR_ASSET_MIN_FREE_BYTES"] = "0"
# A cheap pbkdf2 work factor: the suite creates hundreds of users, and the
# production default (600,000 rounds, a few hundred ms per hash) would add
# minutes. The default itself and the in-place upgrade of an older hash are
# covered by test_password_rehash.py. Unconditional, like the paths above, so a
# sourced wsi.env cannot slow the suite down.
os.environ["WSI_PASSWORD_HASH_ROUNDS"] = "1000"

from wsi_app import version as _version  # noqa: E402

# Where the running version came from decides whether the app_version
# downgrade guards apply (a frontend/package.json fallback is not trusted). A
# checkout with release tags resolves through `git describe` and a shallow CI
# clone through package.json, so pin the source for the suite: the guards are
# exercised as in production, and the tests that cover the fallback set it
# themselves. Pin the version with it, as a VERSION stamp would hold a
# release: the package.json placeholder (0.0.0-dev) sorts below every release,
# so the tests' "older" recorded versions (0.0.1) would read as downgrades.
# Before any other wsi_app import, since modules copy APP_VERSION by name.
_version.APP_VERSION = "1.0.0"
_version.APP_VERSION_SOURCE = _version.SOURCE_VERSION_FILE

from fastapi.testclient import TestClient  # noqa: E402

from wsi_app.auth import create_token, hash_password  # noqa: E402
from wsi_app.db import Base, SessionLocal, engine  # noqa: E402
from wsi_app.main import app  # noqa: E402
from wsi_app.models import NetworkSettings, Slide, User  # noqa: E402
from wsi_app import sync_room  # noqa: E402
from wsi_app.routers import sync as _sync_router  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    """Remove the session's temporary data root (DB, data dir, backups)."""
    engine.dispose()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)

# Startup side-effects live in the FastAPI lifespan, so the schema is created
# only when the lifespan runs. Tests still want a one-shot client without
# wrapping every test in a context manager, so create the tables explicitly
# here once at session start.
Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def reset_room_manager():
    """Each test gets a pristine RoomManager.

    The router does ``from ..sync_room import manager``, binding the object at
    import time, so rebinding only ``sync_room.manager`` would leave the REST
    endpoints talking to the original manager for the whole session — rooms
    would leak between tests, and a test that seeded ``sync_room.manager._rooms``
    would inspect a manager the endpoints never touched. Rebind both names to
    the same fresh instance so seeding, assertions, and the endpoints all agree.
    """
    fresh = sync_room.RoomManager()
    sync_room.manager = fresh
    _sync_router.manager = fresh
    yield


@pytest.fixture(autouse=True)
def _reset_backup_estimate_cache():
    """The backup size estimate is reused for a few minutes per worker; a
    test that seeds rows must not read an earlier test's figures."""
    from wsi_app import backup as _backup

    _backup._estimate_cache.clear()
    yield
    _backup._estimate_cache.clear()


@pytest.fixture(autouse=True)
def _reset_slide_resolve_cache():
    """Clear the per-worker slide-metadata resolve cache between tests.

    The /dzi/ path caches public_id -> (id, is_published); without a reset
    a cached entry from one test could serve a stale publish state to the next
    (public_ids are random, so collisions are unlikely, but the reset makes
    the isolation explicit).
    """
    from wsi_app import slide_resolve_cache

    slide_resolve_cache._reset_for_tests()
    yield
    slide_resolve_cache._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """Clear the process-local per-IP login throttle between tests.

    The throttle keys on client IP and all TestClient requests share one
    host, so without this an accumulation of failed-login attempts across
    earlier tests could 429 a later test that expected a 401/200.
    """
    from wsi_app import login_throttle

    login_throttle.reset()
    yield
    login_throttle.reset()


@pytest.fixture(autouse=True)
def _reset_basic_credential_cache():
    """Tests delete and recreate AuthUser rows under the same username; a
    fingerprint (or remembered rejection) cached by an earlier test would
    otherwise outlive its row."""
    from wsi_app import app_auth

    app_auth._basic_cred_cache.clear()
    app_auth._basic_reject_cache.clear()
    yield
    app_auth._basic_cred_cache.clear()
    app_auth._basic_reject_cache.clear()


@pytest.fixture(autouse=True)
def _sigpipe_stays_ignored():
    """A test must not leave SIGPIPE at its default action.

    Python ignores SIGPIPE, so a write to a socket or pipe the other side has
    closed raises BrokenPipeError. A test that calls a command-line ``main()``
    which restores the default (tools/normalize_live_db.py does, so that
    ``| head`` ends quietly) in this process leaves it that way: from then on
    such a write kills the WHOLE pytest process (exit status 141), at whichever
    later test writes to a closed peer first (the sync bridge tests do) -- on a
    timing the test run happens to have, so it looks like a flaky CI.
    Restored here, and reported against the test that changed it."""
    if not hasattr(signal, "SIGPIPE"):
        yield
        return
    before = signal.getsignal(signal.SIGPIPE)
    yield
    if signal.getsignal(signal.SIGPIPE) != before:
        signal.signal(signal.SIGPIPE, before)
        pytest.fail(
            "this test left SIGPIPE at a different action than it found "
            "(a main() that calls signal.signal(SIGPIPE, SIG_DFL), run in-process?). "
            "Restore it in the test, e.g. with the fixture in tests/test_normalize_live_db.py: "
            "left alone, a later write to a closed socket kills the whole pytest run (exit 141).",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _analytics_mode_plain():
    """Force NetworkSettings.analytics_mode='plain' for the test session.

    Production defaults to "off" (no rows are written until the operator
    opts in); this suite asserts against the "plain" recording mode, so
    the singleton row is set here once. Tests that want to exercise the
    off/hashed paths can overwrite the row directly.
    """
    db = SessionLocal()
    try:
        row = db.get(NetworkSettings, 1)
        if row is None:
            row = NetworkSettings(id=1, analytics_mode="plain", analytics_notice_toast_enabled=True)
            db.add(row)
        else:
            row.analytics_mode = "plain"
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _make_user(db_session, *, role: str = "admin", prefix: Optional[str] = None) -> User:
    """Insert a staff user with the given role and return the persisted row.

    Defaults to ``role='admin'`` so the ``admin_user`` fixture keeps its
    admin semantics without touching every call site. Pass an
    explicit role to mint a manager or instructor.
    """
    user = User(
        username=f"{prefix or role}-{os.urandom(4).hex()}",
        password_hash=hash_password("x"),
        role=role,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def admin_user(db_session) -> User:
    return _make_user(db_session, role="admin", prefix="admin")


@pytest.fixture
def other_admin(db_session) -> User:
    return _make_user(db_session, role="admin", prefix="other")


@pytest.fixture
def admin_headers(admin_user) -> dict:
    return {"Authorization": f"Bearer {create_token(admin_user)}"}


@pytest.fixture
def other_admin_headers(other_admin) -> dict:
    return {"Authorization": f"Bearer {create_token(other_admin)}"}


@pytest.fixture
def manager_user(db_session) -> User:
    return _make_user(db_session, role="manager", prefix="manager")


@pytest.fixture
def instructor_user(db_session) -> User:
    return _make_user(db_session, role="instructor", prefix="instructor")


@pytest.fixture
def manager_headers(manager_user) -> dict:
    return {"Authorization": f"Bearer {create_token(manager_user)}"}


@pytest.fixture
def instructor_headers(instructor_user) -> dict:
    return {"Authorization": f"Bearer {create_token(instructor_user)}"}


@pytest.fixture
def staff_client(client, db_session):
    """Factory: returns an Authorization header dict for a user of the given role.

    Tests parametrising over admin/manager/instructor use this instead of
    repeating the per-role fixture wiring inline.
    """
    def _make(role: str = "admin") -> dict:
        u = _make_user(db_session, role=role, prefix=role)
        return {"Authorization": f"Bearer {create_token(u)}"}
    return _make


@pytest.fixture
def slide(db_session) -> Slide:
    s = Slide(title="Test Slide", catalog_id="TEST-001")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture
def attach_wsi(db_session):
    """Give a slide a real (dummy) WSI file on disk under ``DATA_DIR/wsi``.

    The publish endpoint refuses to publish a slide whose WSI body is
    missing, so tests that exercise the publish path attach a backing file
    via this helper. The bytes are a placeholder — the publish gate only
    checks for the file's existence, not that OpenSlide can read it.
    """
    from wsi_app.config import settings

    def _attach(slide: Slide, name: Optional[str] = None) -> Slide:
        wsi_dir = os.path.join(settings.data_dir, "wsi")
        os.makedirs(wsi_dir, exist_ok=True)
        fname = name or f"{slide.id}_dummy.svs"
        with open(os.path.join(wsi_dir, fname), "wb") as fh:
            fh.write(b"dummy-wsi")
        slide.wsi_filename = fname
        db_session.commit()
        db_session.refresh(slide)
        return slide

    return _attach
