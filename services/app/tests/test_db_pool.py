"""Regression tests for the DB connection-pool sizing.

A crowd hitting cold-cache tiles + pageviews + settings reads would raise
``sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached``
if the pool were left at SQLAlchemy's default (15 connections), which is
smaller than the per-worker request concurrency. These tests pin the pool to
its configured, larger size and verify the concurrency ceiling stays below the
pool capacity.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio

from wsi_app.config import settings
from wsi_app.db import configure_request_concurrency, engine
import wsi_app.db as db_mod


def test_engine_pool_sized_from_settings():
    # The engine must reflect the configured pool, not SQLAlchemy's 5+10 default.
    assert engine.pool.size() == settings.db_pool_size
    assert engine.pool._max_overflow == settings.db_max_overflow
    assert engine.pool._timeout == settings.db_pool_timeout


def test_default_capacity_exceeds_request_concurrency():
    # The invariant that prevents the QueuePool timeout: a worker can never
    # admit more concurrent DB-touching requests than the pool can back.
    capacity = settings.db_pool_size + settings.db_max_overflow
    assert settings.db_request_threadpool_tokens < capacity


def test_configure_request_concurrency_sets_limiter():
    def _run():
        async def _inner():
            import anyio.to_thread

            configure_request_concurrency()
            return anyio.to_thread.current_default_thread_limiter().total_tokens

        return anyio.run(_inner)

    assert _run() == settings.db_request_threadpool_tokens


def test_configure_request_concurrency_warns_when_pool_too_small(monkeypatch, caplog):
    # Deliberately mis-size the pool below the token count; the app must warn
    # (and degrade to blocking checkout) rather than boot silently or crash.
    monkeypatch.setattr(
        db_mod,
        "settings",
        SimpleNamespace(
            db_request_threadpool_tokens=100,
            db_pool_size=5,
            db_max_overflow=10,
        ),
    )
    with caplog.at_level("WARNING", logger="wsi_app.db"):
        async def _inner():
            configure_request_concurrency()

        anyio.run(_inner)
    assert any("exceeds the DB pool capacity" in r.message for r in caplog.records)


def test_env_int_min_clamps_and_warns(monkeypatch, caplog):
    """WSI_REQUEST_THREADPOOL_TOKENS=0 would leave anyio with zero tokens
    (every sync endpoint would hang) and a negative WSI_DB_POOL_SIZE is not a
    pool."""
    import logging

    from wsi_app.config import env_int_min

    for bad in ("0", "-5"):
        monkeypatch.setenv("WSI_TEST_KNOB", bad)
        with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
            caplog.clear()
            assert env_int_min("WSI_TEST_KNOB", 40, 1) == 1
        assert "WSI_TEST_KNOB" in caplog.text
    monkeypatch.setenv("WSI_TEST_KNOB", "7")
    assert env_int_min("WSI_TEST_KNOB", 40, 1) == 7
    monkeypatch.delenv("WSI_TEST_KNOB")
    assert env_int_min("WSI_TEST_KNOB", 40, 1) == 40


def test_settings_clamp_threadpool_tokens_and_pool_size_from_env():
    """Settings are read once at import, so check it in a fresh interpreter."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "WSI_REQUEST_THREADPOOL_TOKENS": "0",
        "WSI_DB_POOL_SIZE": "-3",
    }
    out = subprocess.run(
        [sys.executable, "-c",
         "from wsi_app.config import settings as s;"
         "print(s.db_request_threadpool_tokens, s.db_pool_size)"],
        env=env, capture_output=True, text=True, check=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert out.stdout.split() == ["1", "1"]


def _settings_in_fresh_interpreter(extra_env: dict, expr: str) -> list[str]:
    import os
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "from wsi_app.config import settings as s;" + expr],
        env={**os.environ, **extra_env}, capture_output=True, text=True, check=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return out.stdout.split()


def test_max_overflow_keeps_minus_one_and_raises_other_negatives():
    """-1 is SQLAlchemy's "no overflow limit"; anything below it would shrink
    the pool under its size, and at or below -pool_size the first checkout
    would recurse until RecursionError."""
    expr = "print(s.db_max_overflow)"
    assert _settings_in_fresh_interpreter({"WSI_DB_MAX_OVERFLOW": "-1"}, expr) == ["-1"]
    assert _settings_in_fresh_interpreter({"WSI_DB_MAX_OVERFLOW": "-20"}, expr) == ["0"]
    assert _settings_in_fresh_interpreter({"WSI_DB_MAX_OVERFLOW": "0"}, expr) == ["0"]


def test_pool_timeout_is_at_least_one_second():
    expr = "print(s.db_pool_timeout)"
    for bad in ("0", "-5"):
        assert _settings_in_fresh_interpreter(
            {"WSI_DB_POOL_TIMEOUT_SECONDS": bad}, expr
        ) == ["1"]


def test_unlimited_overflow_never_warns_about_capacity(monkeypatch, caplog):
    """max_overflow=-1 must not compute capacity = pool_size - 1 and warn that
    the pool cannot back the token count, since it can back any."""
    monkeypatch.setattr(
        db_mod,
        "settings",
        SimpleNamespace(
            db_request_threadpool_tokens=100,
            db_pool_size=5,
            db_max_overflow=-1,
        ),
    )
    with caplog.at_level("WARNING", logger="wsi_app.db"):
        async def _inner():
            configure_request_concurrency()

        anyio.run(_inner)
    assert not any("exceeds the DB pool capacity" in r.message for r in caplog.records)


def test_engine_accepts_unlimited_overflow(tmp_path):
    """The value config lets through must be one SQLAlchemy can check out with."""
    from sqlalchemy import create_engine, text

    eng = create_engine(
        f"sqlite:///{tmp_path}/o.db", pool_size=1, max_overflow=-1, pool_timeout=1,
    )
    conns = [eng.connect() for _ in range(3)]  # past pool_size without blocking
    try:
        assert all(c.execute(text("SELECT 1")).scalar() == 1 for c in conns)
    finally:
        for c in conns:
            c.close()
        eng.dispose()
