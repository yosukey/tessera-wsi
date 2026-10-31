import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, create_engine, event, exc, literal
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

logger = logging.getLogger("wsi_app.db")

# This build is SQLite-only (enforced in config.py). check_same_thread=False
# lets the connection pool be shared across FastAPI's threadpool workers; all
# concurrency control happens at the SQLite layer via WAL + busy_timeout.
#
# Pool sizing (see config.py for the full rationale): the default SQLAlchemy
# QueuePool is size 5 + overflow 10 = 15 connections, which is smaller than
# the number of sync requests a single worker runs concurrently (the anyio
# threadpool admits up to ``db_request_threadpool_tokens``). When a crowd
# floods a worker with cold-cache tile + pageview + settings requests, the
# excess requests block on connection checkout for ``pool_timeout`` seconds
# and then raise ``QueuePool limit ... reached, connection timed out``. Sizing
# the pool above the threadpool ceiling keeps a connection available for every
# admitted request. SQLite WAL connections are cheap, so the generous default
# costs little.
#
# hide_parameters: SQLAlchemy otherwise renders every bound value into its
# exception text ("[parameters: (...)]"), and that text lands in journald and
# the error_logs table — secrets included (LDAP bind password, password
# hashes, session tokens) that the admin UI treats as write-only.
engine = create_engine(
    settings.database_url,
    future=True,
    hide_parameters=True,
    connect_args={"check_same_thread": False},
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Base(DeclarativeBase):
    pass

@event.listens_for(engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, _):
    cur = dbapi_connection.cursor()
    # Set busy_timeout FIRST: the journal_mode=WAL transition itself takes a
    # write lock when the DB isn't already in WAL mode (first boot / after a
    # restore), so it must run with a non-zero busy timeout or it fails
    # immediately with SQLITE_BUSY when another worker holds the lock at
    # that instant — the multi-worker first-boot race the startup schema
    # lock otherwise works to serialise.
    cur.execute("PRAGMA busy_timeout=5000")
    # WAL: concurrent readers + one writer across uvicorn worker processes.
    cur.execute("PRAGMA journal_mode=WAL")
    # Enforce ON DELETE CASCADE declared on the ORM relationships.
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL mode is safe with NORMAL synchronous; faster than FULL.
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()

def install_replaced_file_guard(eng) -> None:
    """Never hand out a pooled connection to a DB file that was swapped away.

    A restore / reset (and tools/normalize_live_db.py) replaces wsi.db by
    moving a new file onto the path. An open SQLite handle keeps following the
    old inode — now parked under .pre-restore-* — so a process that outlives
    the swap (wsi-sync, if nothing restarted it) would keep reading the old
    rows and writing into the parked copy, where they are silently lost.
    Comparing the file's identity at checkout costs one stat(); on a mismatch
    DisconnectionError makes the pool discard that connection and open a fresh
    one on the file now at the path.
    """
    path = eng.url.database
    if not path or path == ":memory:" or path.startswith("file:"):
        return

    def identity() -> Optional[tuple[int, int]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    @event.listens_for(eng, "connect")
    def _remember_db_file(_dbapi_connection, connection_record):
        connection_record.info["wsi_db_file"] = identity()

    @event.listens_for(eng, "checkout")
    def _drop_connection_to_replaced_db(_dbapi_connection, connection_record, _proxy):
        opened = connection_record.info.get("wsi_db_file")
        if opened is None:
            return
        current = identity()
        if current is not None and current != opened:
            logger.warning(
                "SQLite file %s was replaced since this connection opened it; "
                "reconnecting", path,
            )
            raise exc.DisconnectionError("database file replaced")


install_replaced_file_guard(engine)


# Closed (cleared) while the dev in-process restore apply swaps wsi.db
# (backup.inprocess_apply), open (set) otherwise. Between parking the live DB
# and its siblings and promoting the staged one the path is missing, and a
# connection opened then — any request thread of this process — makes SQLite
# create an empty wsi.db plus a fresh -wal / -shm right where the restored
# database must land. The promote refuses to land beside them
# (restore_apply._refuse_occupied_db_promote) and the swap rolls back, which
# then meets "both exist" at the parked DB's way home and fails closed: one
# stray request would jam the swap behind a journal only a restart reconciles.
# So a new connection waits for the swap here, before the DBAPI connect that
# would create those files (the ``do_connect`` event; ``connect`` fires only
# after it), and then opens whichever database the swap left at the path.
# The wait is bounded: should the swap hang, a request proceeds after
# DB_SWAP_WAIT_SECONDS rather than pinning its thread forever, and the
# promote's refusal is the backstop again. Pooled connections are not held
# back here: engine.dispose() closes the idle ones before the gate closes,
# and the replaced-file guard above drops any opened on the old file.
DB_SWAP_WAIT_SECONDS = 30.0
_db_swap_open = threading.Event()
_db_swap_open.set()


@contextmanager
def db_swap_gate():
    """Hold back new connections of this process's engines for the duration
    (see :data:`DB_SWAP_WAIT_SECONDS`). Not re-entrant; the in-process apply
    runs one at a time (the job slot)."""
    _db_swap_open.clear()
    try:
        yield
    finally:
        _db_swap_open.set()


def install_db_swap_wait(eng) -> None:
    """Make ``eng`` wait at the :func:`db_swap_gate` before connecting."""

    @event.listens_for(eng, "do_connect")
    def _wait_for_db_swap(_dialect, _conn_rec, _cargs, _cparams):
        if not _db_swap_open.is_set() and not _db_swap_open.wait(DB_SWAP_WAIT_SECONDS):
            logger.warning(
                "a database swap is still in progress after %.0f s; connecting anyway",
                DB_SWAP_WAIT_SECONDS,
            )
        return None  # carry on with the default DBAPI connect


install_db_swap_wait(engine)


def utc_ts_bound(dt: datetime):
    """A datetime window bound, bound as SQLite text in the stored format.

    ``server_default=func.now()`` columns (page_views / audit_logs /
    error_logs ``occurred_at``) hold ``CURRENT_TIMESTAMP`` text with no
    fractional part, but SQLAlchemy binds a datetime as
    ``YYYY-MM-DD HH:MM:SS.ffffff``. Text comparison then puts a row stamped at
    exactly a whole-second bound (e.g. a day boundary) *below* the bound: it
    fails ``>= since`` and passes ``< until``, landing in the previous window.
    Dropping a zero fraction makes the bound compare equal to that row (and a
    fractional ``.ffffff`` row still sorts after it), so half-open windows
    split cleanly. Aware values are converted to UTC first.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    text = dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.microsecond:
        text += f".{dt.microsecond:06d}"
    return literal(text, String)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def configure_request_concurrency() -> None:
    """Pin the per-worker sync-endpoint concurrency to the configured token
    count and verify it stays below the connection-pool capacity.

    FastAPI runs sync endpoints (and the DB reads the auth middleware offloads)
    on anyio's *default* thread limiter. Setting its ``total_tokens`` here makes
    the ceiling explicit and operator-tunable instead of relying on anyio's
    built-in default, which could drift out from under the pool on a dependency
    upgrade. Must be called from within the running event loop (the limiter is
    resolved per async backend) — the app calls it once from ``lifespan``.

    If the pool cannot back the configured concurrency this only *warns*: a
    too-small pool degrades to blocking on connection checkout rather than
    refusing to boot, and an operator who deliberately raised the token count
    past the pool gets a clear pointer to the knob to fix.
    """
    import anyio.to_thread

    tokens = settings.db_request_threadpool_tokens
    capacity = settings.db_pool_size + settings.db_max_overflow
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = tokens
    # max_overflow -1 is SQLAlchemy's unbounded overflow (config.py lets it
    # through): the pool then backs any token count.
    if settings.db_max_overflow >= 0 and tokens > capacity:
        logger.warning(
            "WSI_REQUEST_THREADPOOL_TOKENS=%d exceeds the DB pool capacity "
            "(pool_size %d + max_overflow %d = %d); saturated workers will "
            "block on connection checkout and may raise QueuePool timeouts. "
            "Raise WSI_DB_POOL_SIZE / WSI_DB_MAX_OVERFLOW or lower the token "
            "count.",
            tokens, settings.db_pool_size, settings.db_max_overflow, capacity,
        )
