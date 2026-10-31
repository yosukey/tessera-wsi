"""Admin Performance page: the server benchmark engine and the /api/admin/perf
endpoints (permissions, single-run guard, orphan reaping, cancel, history,
browser-test results, and the ping / payload probes)."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import tifffile
from sqlalchemy import delete

from wsi_app import dzi, perf_bench
from wsi_app.models import AuditLog, PerfRun, RolePermissions, Slide

from tests.test_szi_utils import _build_szi

FAST = dict(tiles_per_band=5, max_concurrency=2, step_seconds=1.0)


@pytest.fixture(autouse=True)
def _clean_perf_runs(db_session):
    db_session.execute(delete(PerfRun))
    db_session.execute(delete(RolePermissions))
    db_session.commit()
    yield
    db_session.execute(delete(PerfRun))
    db_session.execute(delete(RolePermissions))
    db_session.commit()


@pytest.fixture(autouse=True)
def _private_job_store(tmp_path, monkeypatch):
    """The job records the overlap checks read: this test's own, not those
    other tests' backups left under the shared BACKUP_DIR."""
    from wsi_app import jobs

    d = tmp_path / ".jobs"
    d.mkdir()
    monkeypatch.setattr(jobs.store, "_persist_dir", lambda: str(d))
    return jobs.store


def _add_slide(db_session, name: str, writer) -> Slide:
    from wsi_app.config import settings

    wsi_dir = os.path.join(settings.data_dir, "wsi")
    os.makedirs(wsi_dir, exist_ok=True)
    s = Slide(title=f"perf {name}", catalog_id=f"PERF-{uuid.uuid4().hex[:6]}")
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    fname = f"{s.id}_{name}"
    path = os.path.join(wsi_dir, fname)
    writer(path)
    s.wsi_filename = fname
    s.file_size_bytes = os.path.getsize(path)
    db_session.commit()
    db_session.refresh(s)
    return s


def _write_ome(path: str) -> None:
    # Noise keeps the JPEG pyramid well above the 64 KiB random-read size,
    # like a real slide (smooth gradients would compress to a few KB).
    base = np.random.default_rng(0).integers(0, 255, (900, 1200, 3), dtype=np.uint8)
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(base, subifds=2, tile=(256, 256), compression="jpeg", photometric="rgb", metadata={"axes": "YXS"})
        tw.write(base[::2, ::2].copy(), subfiletype=1, tile=(256, 256), compression="jpeg", photometric="rgb")
        tw.write(base[::4, ::4].copy(), subfiletype=1, tile=(256, 256), compression="jpeg", photometric="rgb")


@pytest.fixture
def perf_slides(db_session):
    token = uuid.uuid4().hex[:8]
    ome = _add_slide(db_session, f"perf-{token}.ome.tif", _write_ome)
    szi = _add_slide(db_session, f"perf-{token}.szi", lambda p: _build_szi(__import__("pathlib").Path(p), 1200, 900))
    return ome, szi


def _run_sync(db_session, slide_ids, **overrides):
    params = perf_bench.BenchParams(slide_ids=slide_ids, **{**FAST, **overrides}).normalised()
    targets = perf_bench.resolve_targets(db_session, params.slide_ids)
    row = perf_bench.create_run(db_session, "tester", params, targets)
    perf_bench.execute_run(row.id, targets, params)
    db_session.expire_all()
    return db_session.get(PerfRun, row.id)


def _result(row: PerfRun) -> dict:
    import json

    return json.loads(row.result_json)


def _open_fds_to(path: str) -> list[str]:
    fd_dir = "/proc/self/fd"
    out = []
    for fd in os.listdir(fd_dir):
        try:
            if os.readlink(os.path.join(fd_dir, fd)) == path:
                out.append(fd)
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------


def test_stats_ms_percentiles():
    s = perf_bench.stats_ms([1, 2, 3, 4, 100])
    assert s["count"] == 5 and s["min"] == 1 and s["max"] == 100 and s["p50"] == 3
    assert s["p90"] == pytest.approx(61.6)
    assert perf_bench.stats_ms([]) == {"count": 0}


def test_concurrency_steps():
    assert perf_bench._concurrency_steps(1) == [1]
    assert perf_bench._concurrency_steps(8) == [1, 2, 4, 8]
    assert perf_bench._concurrency_steps(6) == [1, 2, 4, 6]


def test_cpu_split_from_cpu_times_deltas():
    T = namedtuple("T", "user nice system idle iowait irq softirq steal guest guest_nice")
    a = T(100, 0, 50, 800, 50, 0, 0, 0, 10, 0)
    b = T(160, 0, 70, 880, 90, 0, 0, 0, 20, 0)  # +80 busy, +80 idle, +40 iowait
    busy, iowait = perf_bench._cpu_split(a, b)
    assert busy == pytest.approx(40.0) and iowait == pytest.approx(20.0)
    assert perf_bench._cpu_split(a, a) == (0.0, 0.0)
    # Linux can step iowait backwards; a negative delta counts as zero.
    back = T(160, 0, 70, 880, 30, 0, 0, 0, 20, 0)
    busy, iowait = perf_bench._cpu_split(a, back)
    assert iowait == 0.0 and 0.0 <= busy <= 100.0


def test_params_are_clamped(monkeypatch):
    monkeypatch.setattr(perf_bench, "effective_cpus", lambda: 64.0)
    p = perf_bench.BenchParams(slide_ids=[1, 2, 3, 4], tiles_per_band=1000, max_concurrency=99, step_seconds=0.1).normalised()
    assert p.slide_ids == [1, 2, 3] and p.tiles_per_band == 200 and p.max_concurrency == 32 and p.step_seconds == 1.0
    auto = perf_bench.BenchParams(max_concurrency=0).normalised()
    assert auto.max_concurrency == 8


def test_concurrency_follows_the_effective_cpus_not_the_host(monkeypatch):
    """A container held to 1.5 CPUs on a big host: automatic is 2 threads and
    an explicit 32 is capped at twice the effective CPUs (at least 4), not
    32 render threads inside a worker serving viewers on 1.5 CPUs."""
    monkeypatch.setattr(perf_bench.os, "sched_getaffinity", lambda pid: set(range(64)), raising=False)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: 1.5)
    assert perf_bench.effective_cpus() == 1.5
    assert perf_bench.BenchParams(max_concurrency=0).normalised().max_concurrency == 2
    assert perf_bench.BenchParams(max_concurrency=32).normalised().max_concurrency == 4
    # An affinity mask (cpuset / taskset) limits it the same way.
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(perf_bench.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert perf_bench.effective_cpus() == 3
    assert perf_bench.concurrency_cap() == 6
    assert perf_bench.BenchParams(max_concurrency=32).normalised().max_concurrency == 6


def test_cpu_busy_is_measured_against_the_effective_cpus(monkeypatch):
    """On a host with more CPUs than the process may use, busy % comes from
    the process's (or cgroup's) CPU time over the effective CPUs: the
    host-wide figure of a 2-CPU container on a 16-core host would never pass
    ~12 %, so a saturated container would read as idle (threadBound advice)."""
    monkeypatch.setattr(perf_bench.psutil, "cpu_count", lambda logical=True: 16)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_usage_s", lambda: None)
    clock = perf_bench._CpuClock(cpus=2.0)
    assert clock.basis == "process"
    T = namedtuple("T", "user nice system idle iowait")
    host_a, host_b = T(0, 0, 0, 0, 0), T(20, 0, 0, 140, 0)  # host-wide: 12.5 % busy
    # 1.8 CPU-seconds in one second of wall time on 2 effective CPUs: 90 %.
    busy, iowait = clock.split((host_a, 10.0, 100.0), (host_b, 11.8, 101.0))
    assert busy == 90.0 and iowait == 0.0
    # A failed usage read falls back to the host's figure for that interval.
    assert clock.split((host_a, None, 100.0), (host_b, 11.8, 101.0))[0] == 12.5
    # Never above 100 % (two snapshots' clocks are read a moment apart).
    assert clock.split((host_a, 0.0, 100.0), (host_b, 5.0, 101.0))[0] == 100.0
    # The whole host available: the host's own split, other processes included.
    assert perf_bench._CpuClock(cpus=16.0).basis == "host"
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_usage_s", lambda: 123.0)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: 2.0)
    assert perf_bench._CpuClock(cpus=2.0).basis == "cgroup"
    # And the advice judges cores by the effective count: 2 threads saturating
    # 2 effective CPUs is CPU-bound, whatever the host has.
    steps = [
        {"concurrency": 1, "tiles_per_s": 10.0, "cpu_busy_pct": 50.0, "iowait_pct": 0.0},
        {"concurrency": 2, "tiles_per_s": 11.0, "cpu_busy_pct": 95.0, "iowait_pct": 0.0},
    ]
    codes = [a["code"] for a in perf_bench.summarise([], steps, {}, cores=2.0)[1]]
    assert "cpuBound" in codes and "threadBound" not in codes


def test_an_affinity_mask_alone_reads_busy_from_the_process(monkeypatch):
    """`taskset -c 0,1` on a 16-core host, no cgroup quota: the cgroup's
    usage counts every process in it (the other workers, unpinned), so
    dividing it by this process's 2 CPUs would overstate busy — up to a clamped
    100 % and a false cpuBound. Only a quota that sets the count makes the
    cgroup's usage a share of it."""
    monkeypatch.setattr(perf_bench.psutil, "cpu_count", lambda logical=True: 16)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_usage_s", lambda: 123.0)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: None)
    # The cgroup may run on all 16; this process alone is pinned to 2.
    monkeypatch.setattr(perf_bench, "_cgroup_cpuset_count", lambda: 16)
    monkeypatch.setattr(perf_bench, "_affinity_count", lambda: 2)
    assert perf_bench._CpuClock(cpus=2.0).basis == "process"
    # A quota larger than the mask: the mask set the count, still process.
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: 8.0)
    assert perf_bench._CpuClock(cpus=2.0).basis == "process"
    # A (fractional) quota that is the limit: the cgroup's usage.
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: 1.5)
    assert perf_bench._CpuClock(cpus=1.5).basis == "cgroup"
    # And by default the clock takes the count effective_cpus() reports.
    monkeypatch.setattr(perf_bench.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: None)
    clock = perf_bench._CpuClock()
    assert (clock.cpus, clock.basis) == (2, "process")


def test_a_cgroup_cpuset_reads_busy_from_the_cgroup(monkeypatch):
    """Docker --cpuset-cpus=0-3 / systemd AllowedCPUs= with no quota: every
    process in the cgroup shares those 4 CPUs, so the other workers' load on
    them belongs in the figure — the cgroup's usage, not this process's."""
    monkeypatch.setattr(perf_bench.psutil, "cpu_count", lambda logical=True: 16)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_usage_s", lambda: 123.0)
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(perf_bench, "_cgroup_cpuset_count", lambda: 4)
    monkeypatch.setattr(perf_bench, "_affinity_count", lambda: 4)
    assert perf_bench._CpuClock(cpus=4.0).basis == "cgroup"
    # Unreadable cgroup usage: the process's own time is all there is.
    monkeypatch.setattr(perf_bench, "_cgroup_cpu_usage_s", lambda: None)
    assert perf_bench._CpuClock(cpus=4.0).basis == "process"


@pytest.mark.parametrize("text,count", [
    ("0-3\n", 4), ("0-3,8,10-11", 7), ("5", 1), ("", None), ("x-1", None), ("3-1", None),
])
def test_parse_cpu_list(text, count):
    assert perf_bench._parse_cpu_list(text) == count


def test_advice_rules():
    slides = [{"open": {"first_ms": 50}, "disk": {"sequential": {"mb_per_s": 40.0}, "random_64k_ms": {"count": 10, "p95": 30.0}},
               "bands": [{"band": "high", "total_ms": {"count": 5, "p95": 800.0}}]}]
    scaling = [
        {"concurrency": 1, "tiles_per_s": 10.0, "cpu_busy_pct": 30.0, "iowait_pct": 0.0},
        {"concurrency": 2, "tiles_per_s": 19.0, "cpu_busy_pct": 95.0, "iowait_pct": 0.0},
        {"concurrency": 4, "tiles_per_s": 20.0, "cpu_busy_pct": 97.0, "iowait_pct": 0.0},
    ]
    summary, advice = perf_bench.summarise(slides, scaling, {"iowait_pct": {"avg": 25.0}})
    codes = [a["code"] for a in advice]
    assert summary["peak_tiles_per_s"] == 20.0 and summary["peak_concurrency"] == 4
    assert advice[0] == {"code": "saturation", "params": {"concurrency": 2, "tilesPerSec": 20.0}}
    assert {"cpuBound", "diskSlow", "diskSeekSlow", "ioWaitHigh", "tileSlow"} <= set(codes)
    assert "noBottleneck" not in codes

    # One step measures no scaling, so there is no "levels off at 1" finding.
    calm = [{"concurrency": 1, "tiles_per_s": 10.0, "cpu_busy_pct": 30.0, "iowait_pct": 0.0}]
    _, advice = perf_bench.summarise([], calm, {})
    assert [a["code"] for a in advice] == ["noBottleneck"]
    # Nothing measured at all: no finding either way.
    assert perf_bench.summarise([], [], {}) == ({}, [])


def test_thread_bound_advice_needs_idle_disk_and_room_for_workers(monkeypatch):
    monkeypatch.setattr(perf_bench.psutil, "cpu_count", lambda logical=True: 16)
    monkeypatch.setattr(perf_bench, "effective_cpus", lambda: 16.0)
    flat = [
        {"concurrency": 1, "tiles_per_s": 10.0, "cpu_busy_pct": 5.0, "iowait_pct": 0.0},
        {"concurrency": 2, "tiles_per_s": 11.0, "cpu_busy_pct": 6.0, "iowait_pct": 0.0},
    ]
    codes = lambda advice: [a["code"] for a in advice]  # noqa: E731
    assert "threadBound" in codes(perf_bench.summarise([], flat, {})[1])
    # Two threads blocked on a disk are only ~12 % iowait on 16 cores: still
    # the disk, not the GIL.
    disk_bound = [dict(r, iowait_pct=11.0) for r in flat]
    assert "threadBound" not in codes(perf_bench.summarise([], disk_bound, {})[1])
    slow_disk = [{"disk": {"sequential": {"mb_per_s": 40.0}, "random_64k_ms": {"count": 0}}}]
    assert "threadBound" not in codes(perf_bench.summarise(slow_disk, flat, {})[1])
    # Already as many workers as cores: more workers is not the advice.
    assert "threadBound" not in codes(perf_bench.summarise([], flat, {}, workers=16)[1])
    # A whole CPU's worth of iowait on a many-core host is high iowait.
    assert "ioWaitHigh" in codes(perf_bench.summarise([], flat, {"iowait_pct": {"avg": 8.0}})[1])


class _StubReporter:
    def should_stop(self):
        return False


def test_cold_disk_phase_reads_and_evicts_the_slide_file(tmp_path, monkeypatch):
    """A slide is one file: the disk phase measures that file, and a cold run
    evicts it from the page cache before reading."""
    size = 300 * 1024
    slide = tmp_path / "s.svs"
    slide.write_bytes(os.urandom(size))
    target = perf_bench.Target(1, "t", str(slide), "wsi", "", 0, 0, size)
    dropped = []
    monkeypatch.setattr(perf_bench, "_drop_page_cache", dropped.append)
    out = perf_bench._measure_disk(target, True, random.Random(0), _StubReporter())
    assert (out["file"], out["file_bytes"]) == ("s.svs", size)
    assert out["sequential"]["bytes"] == size
    assert out["random_64k_ms"]["count"] > 0
    assert dropped and set(dropped) == {str(slide)}

    dropped.clear()
    perf_bench._measure_disk(target, False, random.Random(0), _StubReporter())
    assert dropped == []


def test_stuck_scaling_thread_keeps_its_handle_open(monkeypatch):
    import threading

    release = threading.Event()

    class Blocking:
        closed = False

        def band_levels(self):
            return {"high": (0, 1, 1)}

        def random_tile(self, ref, rng):
            return (0, 0, 0)

        def render(self, key):
            release.wait(10)
            return 1.0, 0.0, 10

        def close(self):
            self.closed = True

    monkeypatch.setattr(perf_bench, "_JOIN_GRACE_SECONDS", 0.2)
    h = Blocking()
    params = perf_bench.BenchParams(max_concurrency=2, step_seconds=0.1)
    steps, workers = [], []
    perf_bench._measure_scaling([h], params, 1, lambda f: None, _StubReporter(), steps, workers)
    stuck = [t for t in workers if t.is_alive()]
    # The first step's thread never came back: no second step, and the
    # handle must stay open until it does.
    assert [s["concurrency"] for s in steps] == [1] and steps[0]["stalled_threads"] == 1
    assert len(stuck) == 1
    closer = threading.Thread(target=perf_bench._close_after, args=(1, stuck, [h]))
    closer.start()
    closer.join(0.3)
    assert not h.closed
    release.set()
    closer.join(5)
    assert h.closed


class _StubHandle:
    """A slide handle whose tile renders the test scripts. It records a
    close() that lands while a render is in flight, and any render after the
    close; such a render also ends its thread (SystemExit gets past the
    worker's ``except Exception``) so a worker nobody stopped cannot spin on
    after the test."""

    vendor = "stub"

    def __init__(self, render=None):
        self._render = render or (lambda key: (1.0, 0.0, 10))
        self._lock = threading.Lock()
        self.active = 0
        self.closed = False
        self.closed_mid_render = False
        self.used_after_close = False

    def band_levels(self):
        return {band: (0, 4, 4) for band, _ in perf_bench.BANDS}

    @staticmethod
    def level_number(ref):
        return ref[0]

    @staticmethod
    def random_tile(ref, rng):
        return (ref[0], rng.randrange(ref[1]), rng.randrange(ref[2]))

    def latched(self, exc):
        return False

    def render(self, key):
        if self.closed:
            self.used_after_close = True
            raise SystemExit
        with self._lock:
            self.active += 1
        try:
            return self._render(key)
        finally:
            with self._lock:
                self.active -= 1

    def close(self):
        with self._lock:
            self.closed_mid_render = self.closed_mid_render or self.active > 0
            self.closed = True


def _run_stub(db_session, monkeypatch, handle, **overrides) -> PerfRun:
    """execute_run() on one slide whose open / disk phases are faked and
    whose tiles come from ``handle``. Parameters are not normalised, so the
    parallel steps can be shorter than the 1 s floor."""
    monkeypatch.setattr(perf_bench, "_measure_open", lambda target, cold, lane=None: (handle, {"first_ms": 1.0, "repeat_ms": 1.0}))
    monkeypatch.setattr(perf_bench, "_measure_disk", lambda target, cold, rng, reporter: {
        "sequential": {"bytes": 1, "seconds": 0.001, "mb_per_s": 500.0}, "random_64k_ms": {"count": 0},
    })
    row = _running_row(db_session)
    target = perf_bench.Target(1, "stub", "/nonexistent", "wsi", "", 0, 0, 0)
    params = perf_bench.BenchParams(**{"slide_ids": [1], "tiles_per_band": 5, "max_concurrency": 2,
                                       "step_seconds": 0.3, **overrides})
    perf_bench.execute_run(row.id, [target], params)
    db_session.expire_all()
    return db_session.get(PerfRun, row.id)


def test_thread_start_failure_never_closes_a_handle_under_a_worker(db_session, monkeypatch):
    """A Thread.start() refused part-way through a parallel step (the thread
    limit) must not escape _measure_scaling without stopping or joining the
    step's threads already running, or execute_run — never told about
    them — would close the slide handle while one was inside render()."""
    entered = threading.Event()
    release = threading.Event()
    blocker = {}

    def render(key):
        if threading.current_thread() is blocker.get("t"):
            entered.set()
            release.wait(10)
        return 1.0, 0.0, 10

    h = _StubHandle(render)
    real_thread = threading.Thread
    starts = [0]

    class LimitedThread(real_thread):
        def start(self):
            if getattr(self._target, "__name__", "") == "worker":
                starts[0] += 1
                if starts[0] == 2:
                    # The first thread of the two-thread step: it parks in
                    # render(), like a read on slow storage.
                    blocker["t"] = self
                elif starts[0] == 3:
                    # Its sibling is refused while it is still in there.
                    assert entered.wait(5)
                    raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(perf_bench.threading, "Thread", LimitedThread)
    monkeypatch.setattr(perf_bench, "_JOIN_GRACE_SECONDS", 0.3)
    try:
        row = _run_stub(db_session, monkeypatch, h)
        assert row.state == "failed" and "can't start new thread" in row.error
        # The run is recorded, but the parked worker still holds the handle.
        assert not h.closed and not h.closed_mid_render
    finally:
        release.set()
    # Once it returns it sees the stop request, and only then is the
    # handle closed.
    blocker["t"].join(5)
    assert not blocker["t"].is_alive()
    deadline = time.monotonic() + 5
    while not h.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert h.closed and not h.closed_mid_render and not h.used_after_close


def test_scaling_stops_its_threads_when_the_step_raises():
    class FailingReporter:
        calls = 0

        def should_stop(self):
            self.calls += 1
            if self.calls > 1:  # past the check before the step starts
                raise RuntimeError("progress store gone")
            return False

    h = _StubHandle()
    steps, workers = [], []
    params = perf_bench.BenchParams(max_concurrency=1, step_seconds=5.0)
    with pytest.raises(RuntimeError, match="progress store gone"):
        perf_bench._measure_scaling([h], params, 1, lambda f: None, FailingReporter(), steps, workers)
    # Told to stop and joined on the way out, not left rendering.
    assert len(workers) == 1 and not workers[0].is_alive()


def test_parallel_phase_that_renders_nothing_fails_the_run(db_session, monkeypatch):
    """Every parallel render failing (here: the reader breaks after the
    per-band phase) must not be stored as a succeeded, complete run with "no
    bottleneck", the failed steps dropping out of the rated ones unnoticed."""
    run_thread = threading.current_thread()

    def render(key):
        if threading.current_thread() is not run_thread:
            raise RuntimeError("reader broke")
        return 1.0, 0.0, 10

    row = _run_stub(db_session, monkeypatch, _StubHandle(render))
    res = _result(row)
    assert row.state == "failed" and row.error == perf_bench.RUN_ERROR_PARALLEL_FAILED
    assert [s["tiles"] for s in res["scaling"]] == [0, 0]
    assert all(s["errors"] > 0 for s in res["scaling"])
    codes = [a["code"] for a in res["advice"]]
    assert codes[0] == "tileErrors" and "noBottleneck" not in codes
    assert "peak_tiles_per_s" not in res["summary"]


def test_latched_reader_is_not_hammered_until_the_step_ends(db_session, monkeypatch):
    """A reader whose error is permanent (an OpenSlide handle after its first
    read error) is dropped after that error instead of failing every render
    until the step's deadline; with no reader left the phase stops."""

    class Latched(Exception):
        pass

    run_thread = threading.current_thread()

    def render(key):
        if threading.current_thread() is not run_thread:
            raise Latched("sticky")
        return 1.0, 0.0, 10

    h = _StubHandle(render)
    h.latched = lambda exc: isinstance(exc, Latched)
    row = _run_stub(db_session, monkeypatch, h, step_seconds=2.0)
    res = _result(row)
    assert [(s["concurrency"], s["tiles"], s["errors"]) for s in res["scaling"]] == [(1, 0, 1)]
    step = res["scaling"][0]
    assert step["readers_failed"] is True and step["seconds"] < 2.0
    assert row.state == "failed" and row.error == perf_bench.RUN_ERROR_PARALLEL_FAILED
    assert res["truncated"] is True


def test_parallel_errors_are_a_finding_and_rule_out_no_bottleneck():
    slides = [{"open": {"first_ms": 5.0}, "bands": [{"band": "high", "total_ms": {"count": 5, "p95": 20.0}}]}]
    step = {"concurrency": 1, "cpu_busy_pct": 30.0, "iowait_pct": 0.0}
    codes = lambda advice: [a["code"] for a in advice]  # noqa: E731

    _, advice = perf_bench.summarise(slides, [dict(step, tiles=0, errors=40, tiles_per_s=0.0)], {})
    assert advice[0] == {"code": "tileErrors", "params": {"errors": 40, "attempts": 40}}
    assert "noBottleneck" not in codes(advice)
    # Some tiles lost: still not a clean bill of health.
    _, advice = perf_bench.summarise(slides, [dict(step, tiles=90, errors=10, tiles_per_s=30.0)], {})
    assert advice[0] == {"code": "tileErrors", "params": {"errors": 10, "attempts": 100}}
    assert "noBottleneck" not in codes(advice)
    # The same run without errors has nothing to report.
    _, advice = perf_bench.summarise(slides, [dict(step, tiles=100, errors=0, tiles_per_s=33.0)], {})
    assert codes(advice) == ["noBottleneck"]


def test_only_an_openslide_handle_latches_its_read_error():
    import openslide

    from wsi_app.ome_tiff import OmeTiffSlide

    h = object.__new__(perf_bench._DzHandle)
    h.osr = object.__new__(openslide.OpenSlide)
    assert h.latched(openslide.OpenSlideError("bad tile"))
    assert not h.latched(ValueError("no such tile"))
    # The OME-TIFF reader raises the same class for one bad segment only.
    h.osr = object.__new__(OmeTiffSlide)
    assert not h.latched(openslide.OpenSlideError("bad segment"))
    assert not perf_bench._SziHandle.latched(OSError("bad member"))


def test_szi_handle_measures_only_canonical_levels(tmp_path):
    """A negative or non-canonical level directory is no level the viewer can
    request; the SZI reader must not offer it to the tile phases either."""
    import pathlib
    import zipfile

    p = tmp_path / "junk.szi"
    _build_szi(pathlib.Path(p), 256, 256)  # levels 0..8
    with zipfile.ZipFile(p, "a", compression=zipfile.ZIP_STORED) as zf:
        for head in ("-5", "-1000000000000", "08", "+8"):
            zf.writestr(f"image_files/{head}/0_0.jpeg", b"x")
    h = perf_bench._SziHandle(str(p))
    try:
        assert sorted(h._levels) == list(range(9))
        assert all(n.startswith(f"image_files/{lvl}/") for lvl, names in h._levels.items() for n in names)
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Engine end to end
# ---------------------------------------------------------------------------


def test_run_measures_ome_and_szi_without_touching_the_tile_cache(db_session, perf_slides):
    ome, szi = perf_slides
    from wsi_app.config import settings

    before = dzi.get_dz_cache_info()["entries"]
    row = _run_sync(db_session, [ome.id, szi.id])
    assert row.state == "succeeded", row.error
    assert row.finished_at is not None
    res = _result(row)
    assert res["schema"] == perf_bench.SCHEMA
    assert [s["id"] for s in res["slides"]] == [ome.id, szi.id]
    wsi_entry, szi_entry = res["slides"]
    assert wsi_entry["format"] == "wsi" and wsi_entry["vendor"] == "ome-tiff"
    assert szi_entry["format"] == "szi"
    for entry in res["slides"]:
        assert "error" not in entry
        assert entry["open"]["first_ms"] > 0
        assert entry["disk"]["sequential"]["bytes"] == entry["file_bytes"]
        assert entry["disk"]["random_64k_ms"]["count"] > 0
        assert [b["band"] for b in entry["bands"]] == ["high", "mid", "low"]
        assert all(b["total_ms"]["count"] > 0 for b in entry["bands"])
    assert wsi_entry["bands"][0]["encode_ms"]["count"] > 0
    assert [s["concurrency"] for s in res["scaling"]] == [1, 2]
    assert all(s["tiles"] > 0 and s["errors"] == 0 for s in res["scaling"])
    assert res["summary"]["peak_tiles_per_s"] > 0
    assert res["advice"] and res["advice"][0]["code"] == "saturation"
    assert res["environment"]["cpu_logical"] >= 1
    # The conditions an exported run is read against travel with it.
    assert res["environment"]["viewer_rendering"] == perf_bench.viewer_rendering(db_session)
    assert res["environment"]["kernel"]
    assert "load_avg_start" in res["system"] and "load_avg_end" in res["system"]
    assert res["truncated"] is False
    # Latencies are below the HTTP layer, and the result says so.
    assert res["scope"] == "below_http"
    # A warm run's band latencies follow a read of (here) the whole file.
    for entry in res["slides"]:
        assert entry["warm_bytes"] == entry["file_bytes"] and entry["warm_fraction"] == 1.0
        assert all(b["errors"] == 0 and b["skipped"] == 0 for b in entry["bands"])
    for step in res["scaling"]:
        assert 0.0 <= step["fresh_tile_pct"] <= 100.0
    assert res["summary"]["job_overlap"] is False
    assert res["system"]["cpu_basis"] in ("host", "cgroup", "process")
    assert res["environment"]["cpu_effective"] > 0
    # The summary also has its own column, for the history list.
    import json as _json
    assert _json.loads(row.summary_json) == res["summary"]
    # The benchmark used its own handles: the production cache is untouched
    # and nothing stays open on the slide files afterwards.
    assert dzi.get_dz_cache_info()["entries"] == before
    for s in (ome, szi):
        assert _open_fds_to(os.path.join(settings.data_dir, "wsi", s.wsi_filename)) == []


def test_scaling_leaves_szi_out_when_other_slides_are_measured(db_session, perf_slides, monkeypatch):
    ome, szi = perf_slides
    calls = {"n": 0}
    real = perf_bench._SziHandle.render

    def counting(self, key):
        calls["n"] += 1
        return real(self, key)

    monkeypatch.setattr(perf_bench._SziHandle, "render", counting)
    row = _run_sync(db_session, [ome.id, szi.id])
    assert row.state == "succeeded"
    res = _result(row)
    band_reads = sum(b["total_ms"]["count"] for b in res["slides"][1]["bands"])
    # Only the per-band phase touched the SZI; the parallel-scaling phase
    # rendered the OME-TIFF alone.
    assert calls["n"] == band_reads
    assert all(s["tiles"] > 0 for s in res["scaling"])

    calls["n"] = 0
    only_szi = _run_sync(db_session, [szi.id], max_concurrency=1)
    assert only_szi.state == "succeeded"
    szi_bands = sum(b["total_ms"]["count"] for b in _result(only_szi)["slides"][0]["bands"])
    assert calls["n"] > szi_bands  # an SZI-only run still measures scaling on it


def test_cold_run_succeeds(db_session, perf_slides):
    ome, _ = perf_slides
    row = _run_sync(db_session, [ome.id], cold=True, max_concurrency=1)
    assert row.state == "succeeded", row.error
    assert _result(row)["params"]["cold"] is True
    # Cold mode drops the file from the page cache: nothing is "warm".
    assert "warm_fraction" not in _result(row)["slides"][0]


def test_a_large_slide_is_flagged_partly_warm(db_session, perf_slides, monkeypatch):
    """Warm mode reads only the first _SEQ_READ_BYTES before the band phase,
    so a larger slide's band latencies are partly warm at best."""
    ome, _ = perf_slides
    monkeypatch.setattr(perf_bench, "_SEQ_READ_BYTES", 100_000)
    row = _run_sync(db_session, [ome.id], max_concurrency=1)
    entry = _result(row)["slides"][0]
    assert entry["warm_bytes"] == 100_000
    assert entry["warm_fraction"] == round(100_000 / entry["file_bytes"], 3) < 1.0


def test_cancel_flag_stops_the_run(db_session, perf_slides):
    ome, _ = perf_slides
    params = perf_bench.BenchParams(slide_ids=[ome.id], **FAST).normalised()
    targets = perf_bench.resolve_targets(db_session, params.slide_ids)
    row = perf_bench.create_run(db_session, "tester", params, targets)
    assert perf_bench.request_cancel(db_session, row.id) is True
    perf_bench.execute_run(row.id, targets, params)
    db_session.expire_all()
    row = db_session.get(PerfRun, row.id)
    assert row.state == "cancelled"
    assert _result(row)["truncated"] is True
    assert perf_bench.request_cancel(db_session, row.id) is False


def test_unmeasurable_slide_is_reported_per_slide(db_session, perf_slides, tmp_path):
    ome, _ = perf_slides
    bad = _add_slide(db_session, f"broken-{uuid.uuid4().hex[:6]}.svs", lambda p: open(p, "wb").write(b"not a slide"))
    row = _run_sync(db_session, [bad.id, ome.id], max_concurrency=1)
    assert row.state == "succeeded"
    res = _result(row)
    assert res["slides"][0]["error"].startswith("open failed")
    assert "error" not in res["slides"][1]

    only_bad = _run_sync(db_session, [bad.id], max_concurrency=1)
    assert only_bad.state == "failed" and only_bad.error == perf_bench.RUN_ERROR_NO_SLIDE
    assert "noBottleneck" not in [a["code"] for a in _result(only_bad)["advice"]]


def test_resolve_targets_errors(db_session, perf_slides):
    with pytest.raises(perf_bench.BenchError) as e:
        perf_bench.resolve_targets(db_session, [99_999_999])
    assert e.value.code == "adminPerf.err.unknownSlide" and e.value.status == 404
    nofile = Slide(title="no file", catalog_id=f"NF-{uuid.uuid4().hex[:6]}")
    db_session.add(nofile)
    db_session.commit()
    with pytest.raises(perf_bench.BenchError) as e:
        perf_bench.resolve_targets(db_session, [nofile.id])
    assert e.value.code == "adminPerf.err.slideFileMissing"
    auto = perf_bench.resolve_targets(db_session, [])
    assert 1 <= len(auto) <= perf_bench.MAX_SLIDES
    assert all(os.path.isfile(t.path) for t in auto)


# ---------------------------------------------------------------------------
# Single-run guard and orphans
# ---------------------------------------------------------------------------


def _running_row(db_session, **kw) -> PerfRun:
    row = PerfRun(kind="server", state="running", actor_username="x", pid=kw.pop("pid", os.getpid()), **kw)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_second_start_is_refused_while_one_runs(client, admin_headers, db_session, perf_slides):
    ome, _ = perf_slides
    _running_row(db_session)
    r = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id], **FAST}, headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "adminPerf.err.alreadyRunning"


def test_orphaned_run_is_reaped(client, admin_headers, db_session):
    dead = _running_row(db_session, pid=_dead_pid())
    body = client.get("/api/admin/perf/runs", headers=admin_headers).json()
    assert body["running_id"] is None
    db_session.expire_all()
    row = db_session.get(PerfRun, dead.id)
    assert row.state == "failed" and row.error == perf_bench.RUN_ERROR_INTERRUPTED


def test_reaper_leaves_a_run_that_finished_meanwhile(db_session, monkeypatch):
    from wsi_app.db import SessionLocal

    row = _running_row(db_session)

    def finishes_then_dead(pid):
        # The run's own final write lands between the reaper's SELECT and
        # its UPDATE.
        other = SessionLocal()
        try:
            other.get(PerfRun, row.id).state = "succeeded"
            other.commit()
        finally:
            other.close()
        return False

    monkeypatch.setattr(perf_bench, "_pid_alive", finishes_then_dead)
    assert perf_bench.reap_orphans(db_session) == 0
    db_session.expire_all()
    assert db_session.get(PerfRun, row.id).state == "succeeded"


def test_implausibly_old_running_row_is_reaped(db_session):
    old = _running_row(db_session)
    old.created_at = datetime.now(timezone.utc) - timedelta(seconds=perf_bench._ORPHAN_AGE_SECONDS + 5)
    db_session.commit()
    assert perf_bench.reap_orphans(db_session) == 1
    db_session.expire_all()
    assert db_session.get(PerfRun, old.id).state == "failed"


def test_history_is_pruned(db_session, perf_slides, monkeypatch):
    ome, _ = perf_slides
    monkeypatch.setattr(perf_bench, "_MAX_STORED_RUNS", 3)
    for _ in range(5):
        db_session.add(PerfRun(kind="client", state="succeeded"))
    db_session.commit()
    params = perf_bench.BenchParams(slide_ids=[ome.id], **FAST).normalised()
    targets = perf_bench.resolve_targets(db_session, params.slide_ids)
    row = perf_bench.create_run(db_session, "tester", params, targets)
    states = [r.state for r in db_session.query(PerfRun).all()]
    assert states.count("running") == 1 and states.count("succeeded") == 3
    perf_bench.request_cancel(db_session, row.id)
    perf_bench.execute_run(row.id, targets, params)


def test_browser_results_are_pruned_too(client, admin_headers, db_session, monkeypatch):
    monkeypatch.setattr(perf_bench, "_MAX_STORED_RUNS", 3)
    for _ in range(6):
        assert client.post("/api/admin/perf/client-runs", json={"schema": 1}, headers=admin_headers).status_code == 201
    assert db_session.query(PerfRun).count() == 3


def test_each_kind_has_its_own_history_quota(client, admin_headers, db_session, monkeypatch):
    """A run of browser tests must not push every server benchmark (the
    baseline they are compared with) out of a pooled history."""
    monkeypatch.setattr(perf_bench, "_MAX_STORED_RUNS", 3)
    servers = [PerfRun(kind="server", state="succeeded") for _ in range(2)]
    db_session.add_all(servers)
    db_session.commit()
    for _ in range(6):
        assert client.post("/api/admin/perf/client-runs", json={"schema": 1}, headers=admin_headers).status_code == 201
    kinds = [r.kind for r in db_session.query(PerfRun).all()]
    assert kinds.count("server") == 2 and kinds.count("client") == 3


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _wait(client, headers, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/admin/perf/runs/{run_id}", headers=headers).json()
        if body["state"] != "running":
            return body
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_start_poll_and_list(client, admin_headers, db_session, perf_slides):
    ome, szi = perf_slides
    r = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id, szi.id], **FAST}, headers=admin_headers)
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    running = client.get(f"/api/admin/perf/runs/{run_id}", headers=admin_headers).json()
    assert running["kind"] == "server"
    assert running["params"]["slides"][0]["id"] == ome.id
    done = _wait(client, admin_headers, run_id)
    assert done["state"] == "succeeded", done["error"]
    assert done["progress"] is None
    assert done["result"]["summary"]["peak_tiles_per_s"] > 0
    listing = client.get("/api/admin/perf/runs", headers=admin_headers).json()
    item = next(x for x in listing["runs"] if x["id"] == run_id)
    assert item["summary"]["peak_tiles_per_s"] > 0 and "result" not in item
    assert db_session.query(AuditLog).filter_by(action="perf.run", target=str(run_id)).count() == 1


def test_start_validation(client, admin_headers, perf_slides):
    ome, _ = perf_slides
    assert client.post("/api/admin/perf/runs", json={"slide_ids": [99_999_999]}, headers=admin_headers).status_code == 404
    too_many = client.post("/api/admin/perf/runs", json={"slide_ids": [1, 2, 3, 4]}, headers=admin_headers)
    assert too_many.status_code == 422
    bad = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id], "step_seconds": 60}, headers=admin_headers)
    assert bad.status_code == 422


def test_start_refused_during_backup_job(client, admin_headers, perf_slides, monkeypatch):
    from wsi_app import jobs

    ome, _ = perf_slides
    monkeypatch.setattr(jobs.store, "has_running", lambda: True)
    r = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id], **FAST}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adminPerf.err.jobRunning"


def test_cancel_and_delete_endpoints(client, admin_headers, db_session):
    row = _running_row(db_session)
    assert client.delete(f"/api/admin/perf/runs/{row.id}", headers=admin_headers).status_code == 409
    assert client.post(f"/api/admin/perf/runs/{row.id}/cancel", headers=admin_headers).status_code == 200
    db_session.expire_all()
    assert db_session.get(PerfRun, row.id).cancel_requested is True
    row.state = "cancelled"
    db_session.commit()
    r = client.post(f"/api/admin/perf/runs/{row.id}/cancel", headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adminPerf.err.notRunning"
    assert client.delete(f"/api/admin/perf/runs/{row.id}", headers=admin_headers).status_code == 200
    gone = client.get(f"/api/admin/perf/runs/{row.id}", headers=admin_headers)
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "adminPerf.err.notFound"
    assert client.post("/api/admin/perf/runs/99999999/cancel", headers=admin_headers).status_code == 404


def test_permissions(client, admin_headers, manager_headers, instructor_headers, db_session, perf_slides):
    ome, _ = perf_slides
    # Viewing follows the Server page permission ...
    assert client.get("/api/admin/perf/runs", headers=manager_headers).status_code == 403
    db_session.add(RolePermissions(id=1, manager_can_view_server=True))
    db_session.commit()
    assert client.get("/api/admin/perf/runs", headers=manager_headers).status_code == 200
    assert client.get("/api/admin/perf/runs", headers=instructor_headers).status_code == 403
    # ... but anything that loads the server is admin-only.
    for method, url, kw in (
        ("post", "/api/admin/perf/runs", {"json": {"slide_ids": [ome.id], **FAST}}),
        ("post", "/api/admin/perf/client-runs", {"json": {"schema": 1}}),
        ("get", "/api/admin/perf/ping", {}),
        ("get", "/api/admin/perf/payload", {}),
    ):
        assert getattr(client, method)(url, headers=manager_headers, **kw).status_code == 403, url
    assert client.get("/api/admin/perf/runs").status_code == 401


def test_client_run_roundtrip(client, admin_headers, db_session):
    result = {
        "schema": 1,
        "params": {"slide_id": 1, "slide_title": "x", "mode": "app", "tiles": 30, "concurrency": 6},
        "network": {"rtt_ms": {"count": 1, "p50": 2.5}, "downloads": [], "mbps": 850.5},
        "tiles": {"requested": 30, "ok": 30, "errors": 0},
        "client": {"user_agent": "pytest"},
        "summary": {"rtt_ms": 2.5, "mbps": 850.5, "tiles_per_s": 40.0, "tile_ttfb_p95_ms": 30.0, "cache_hit_pct": None},
    }
    r = client.post("/api/admin/perf/client-runs", json=result, headers=admin_headers)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    detail = client.get(f"/api/admin/perf/runs/{run_id}", headers=admin_headers).json()
    assert detail["kind"] == "client" and detail["state"] == "succeeded"
    assert detail["params"]["mode"] == "app"
    assert detail["result"]["network"]["mbps"] == 850.5
    assert detail["summary"]["tiles_per_s"] == 40.0
    assert detail["finished_at"]

    assert client.post("/api/admin/perf/client-runs", json={**result, "schema": 99}, headers=admin_headers).status_code == 422
    assert client.post("/api/admin/perf/client-runs", json={**result, "schema": True}, headers=admin_headers).status_code == 422
    # NaN parses in Python's JSON but could never be served back: refused
    # up front instead of breaking the history listing for everyone.
    nan = client.post(
        "/api/admin/perf/client-runs", content=b'{"schema": 1, "summary": {"rtt_ms": NaN}}',
        headers={**admin_headers, "Content-Type": "application/json"},
    )
    assert nan.status_code == 422
    assert client.get("/api/admin/perf/runs", headers=admin_headers).status_code == 200
    huge = {**result, "blob": "x" * (200 * 1024)}
    assert client.post("/api/admin/perf/client-runs", json=huge, headers=admin_headers).status_code == 413


def _client_result(**client) -> dict:
    return {
        "schema": 1,
        "params": {"slide_id": 7, "slide_title": "Liver", "mode": "cache", "tiles": 30, "concurrency": 6, "seed": 7},
        "slide": {"id": 7, "width": 1200, "height": 900, "tile_size": 256, "tile_format": "jpeg"},
        "network": {"rtt_ms": {"count": 1, "p50": 2.5, "p95": 4.0}, "downloads": [], "mbps": 850.5},
        "tiles": {"requested": 30, "ok": 30, "errors": 0, "ttfb_ms": {"count": 30, "p50": 3.0}},
        "client": {
            "user_agent": "pytest", "device_pixel_ratio": 2, "viewport": {"width": 1180, "height": 820},
            "max_touch_points": 5,
            "http_protocols": {"http/1.1": 28, "h2": 2}, "hidden_during_run": False, **client,
        },
        "summary": {"rtt_ms": 2.5, "mbps": 850.5, "tiles_per_s": 40.0, "tile_ttfb_p95_ms": 30.0, "cache_hit_pct": 50.0},
    }


@pytest.fixture
def rendering_settings(db_session):
    """Set the viewer's rendering settings for one test, then put them back."""
    from wsi_app.models import ViewerSettings

    vs = db_session.get(ViewerSettings, 1)
    created = vs is None
    if created:
        vs = ViewerSettings(id=1)
        db_session.add(vs)
    saved = (vs.immediate_render, vs.max_image_cache_count)

    def apply(immediate: bool, cache: int) -> None:
        vs.immediate_render, vs.max_image_cache_count = immediate, cache
        db_session.commit()

    yield apply
    if created:
        db_session.delete(vs)
    else:
        vs.immediate_render, vs.max_image_cache_count = saved
    db_session.commit()


def test_client_run_is_stamped_with_the_server_conditions(client, admin_headers, rendering_settings):
    rendering_settings(False, 350)
    forged = {**_client_result(), "server": {"environment": {"workers": 999}}}
    run_id = client.post("/api/admin/perf/client-runs", json=forged, headers=admin_headers).json()["id"]
    res = client.get(f"/api/admin/perf/runs/{run_id}", headers=admin_headers).json()["result"]
    env = res["server"]["environment"]
    assert env["workers"] != 999 and env["app_version"] and env["tile_size"] == dzi.TILE_SIZE
    vr = env["viewer_rendering"]
    assert (vr["immediate_render"], vr["max_image_cache_count"]) == (False, 350) and vr["max_zoom_pixel_ratio"] >= 1.0
    assert res["server"]["recorded_at"]
    # The browser's own report is stored as sent.
    assert res["client"]["device_pixel_ratio"] == 2


def test_export_json_and_csv(client, admin_headers, manager_headers, db_session):
    import csv
    import io
    import json as _json

    from wsi_app import perf_export

    server_row = PerfRun(
        kind="server", state="succeeded", actor_username="alice",
        params_json=_json.dumps({"cold": True, "tiles_per_band": 30, "max_concurrency": 4, "step_seconds": 3.0,
                                 "slides": [{"id": 3, "title": "A"}, {"id": 4, "title": "=B"}]}),
        result_json=_json.dumps({
            "schema": 1, "environment": {"workers": 4, "cpu_logical": 8, "timezone": "Asia/Tokyo",
                                         "data_disk": {"rotational": False, "fstype": "ext4"},
                                         "viewer_rendering": {"immediate_render": True, "max_image_cache_count": 200}},
            "system": {"cpu_busy_pct": {"avg": 55.0, "max": 97.5}, "load_avg_start": [0.5, 0.4, 0.3]},
            "slides": [
                {"id": 3, "format": "wsi", "vendor": "aperio", "width": 4000, "height": 3000, "file_bytes": 10},
                {"id": 4, "format": "szi", "vendor": "", "width": 2000, "height": 1000, "file_bytes": 20},
            ],
            "summary": {"peak_tiles_per_s": 120.5, "tile_p95_ms": 80.0}, "truncated": False, "duration_s": 42.0,
        }),
    )
    db_session.add(server_row)
    db_session.commit()
    client_id = client.post(
        "/api/admin/perf/client-runs", json=_client_result(user_agent="=HYPERLINK(1)"), headers=admin_headers,
    ).json()["id"]

    r = client.get("/api/admin/perf/export.json", headers=admin_headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith('attachment; filename="tessera-perf-')
    body = r.json()
    assert body["format"] == perf_export.FORMAT and body["format_version"] == 1
    assert body["units"] and body["source"]["app_version"]
    # Oldest first, each with its full result.
    assert [x["id"] for x in body["runs"]] == [server_row.id, client_id]
    assert body["runs"][0]["result"]["environment"]["workers"] == 4
    assert body["runs"][1]["result"]["server"]["environment"]["viewer_rendering"]

    one = client.get(f"/api/admin/perf/export.json?ids={client_id}", headers=admin_headers)
    assert one.headers["content-disposition"] == f'attachment; filename="tessera-perf-run-{client_id}.json"'
    assert [x["id"] for x in one.json()["runs"]] == [client_id]

    c = client.get(f"/api/admin/perf/export.csv?ids={server_row.id},{client_id}", headers=admin_headers)
    assert c.status_code == 200 and c.headers["content-type"].startswith("text/csv")
    text = c.content.decode("utf-8")
    assert text.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text[1:])))
    assert list(rows[0].keys()) == perf_export.CSV_HEADER
    srv, cli = rows
    assert (srv["kind"], srv["slide_ids"], srv["server.cold"], srv["peak_tiles_per_s"]) == ("server", "3;4", "true", "120.5")
    assert (srv["system.cpu_busy_max_pct"], srv["system.load_1m_start"], srv["server.data_disk"]) == ("97.5", "0.5", "SSD ext4")
    assert srv["server.workers"] == "4" and srv["viewer.max_image_cache_count"] == "200"
    assert (srv["slide.width"], srv["slide.height"], srv["slide.format"]) == ("4000;2000", "3000;1000", "wsi;szi")
    # Each ';'-joined part is defanged on its own, not just the cell's start.
    assert srv["slide_titles"] == "A;'=B" and srv["server.timezone"] == "Asia/Tokyo"
    assert (cli["slide.width"], cli["slide.format"]) == ("1200", "")
    assert srv["client.mode"] == "" and srv["client.device_pixel_ratio"] == ""
    assert (cli["kind"], cli["slide_ids"], cli["client.mode"], cli["cache_hit_pct"]) == ("client", "7", "cache", "50.0")
    assert (cli["client.device_pixel_ratio"], cli["client.viewport"]) == ("2", "1180x820")
    assert cli["client.max_touch_points"] == "5"  # what tells an iPad from a Mac
    assert cli["client.http_protocols"] == "http/1.1=28;h2=2" and cli["client.hidden_during_run"] == "false"
    # The server half of a browser run comes from the stamp made on save.
    assert cli["server.dzi_tile_size"] == str(dzi.TILE_SIZE) and cli["system.cpu_busy_max_pct"] == ""
    # Browser-reported text can't smuggle a spreadsheet formula.
    assert cli["client.user_agent"] == "'=HYPERLINK(1)"

    assert client.get("/api/admin/perf/export.csv?ids=1;2", headers=admin_headers).status_code == 422
    # Unicode digits a regex \\d would take (full-width, or newer than int() knows).
    for ids in ("%EF%BC%91", "%F0%9C%B3%B0"):
        assert client.get(f"/api/admin/perf/export.json?ids={ids}", headers=admin_headers).status_code == 422
    assert client.get("/api/admin/perf/export.json?ids=99999999", headers=admin_headers).status_code == 404
    # Reading follows the Server page permission, like the history itself.
    assert client.get("/api/admin/perf/export.csv", headers=manager_headers).status_code == 403
    db_session.add(RolePermissions(id=1, manager_can_view_server=True))
    db_session.commit()
    assert client.get("/api/admin/perf/export.csv", headers=manager_headers).status_code == 200
    assert client.get("/api/admin/perf/export.json").status_code == 401


def test_csv_row_survives_a_malformed_result():
    from wsi_app import perf_export

    run = {"id": 1, "kind": "client", "state": "succeeded",
           "result": {"client": {"http_protocols": {"h2": "x"}, "platform": "iPad"}}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(run)))
    # The broken cell is left empty; the rest of the row is still written.
    assert row["client.http_protocols"] is None
    assert (row["run_id"], row["client.platform"]) == (1, "iPad")


def test_a_stored_nan_never_breaks_the_history(client, admin_headers, db_session):
    import json as _json

    row = PerfRun(kind="server", state="succeeded", actor_username="x",
                  result_json='{"schema": 1, "summary": {"tile_p95_ms": NaN, "peak_tiles_per_s": Infinity}}')
    db_session.add(row)
    db_session.commit()
    detail = client.get(f"/api/admin/perf/runs/{row.id}", headers=admin_headers)
    assert detail.status_code == 200 and detail.json()["result"]["summary"]["tile_p95_ms"] is None
    exported = client.get(f"/api/admin/perf/export.json?ids={row.id}", headers=admin_headers).text
    _json.loads(exported, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def test_the_condition_snapshot_never_walks_the_nginx_cache(monkeypatch, tmp_path):
    from wsi_app.routers import admin_system

    monkeypatch.setattr(admin_system, "_NGINX_CACHE_STAT_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(admin_system.os, "walk", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked")))
    nc = perf_bench.environment()["nginx_cache"]
    assert nc["used_bytes"] is None and nc["file_count"] is None


def test_ping_and_payload(client, admin_headers):
    r = client.get("/api/admin/perf/ping", headers=admin_headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert isinstance(r.json()["t"], float)
    p = client.get("/api/admin/perf/payload?size=100000", headers=admin_headers)
    assert p.status_code == 200 and len(p.content) == 100000
    assert p.headers["content-type"] == "application/octet-stream"
    # Incompressible: a 1 MiB random block, not zeros.
    assert len(set(p.content[:4096])) > 200
    assert client.get("/api/admin/perf/payload?size=10", headers=admin_headers).status_code == 422
    assert client.get(f"/api/admin/perf/payload?size={1 << 30}", headers=admin_headers).status_code == 422


# ---------------------------------------------------------------------------
# Per-tile band errors, sampler start, render lane, ids, jobs, browser-run
# overlap, history summary column, labels, CSV
# ---------------------------------------------------------------------------


def test_a_failing_band_tile_is_counted_not_fatal(db_session, monkeypatch):
    """One tile that fails to render must not raise out of the band phase and
    lose every band of the slide (a one-slide run would then fail as
    noSlideMeasured); the parallel phase tolerates it too."""
    run_thread = threading.current_thread()
    calls = [0]

    def render(key):
        if threading.current_thread() is run_thread:
            calls[0] += 1
            if calls[0] % 3 == 0:
                raise RuntimeError("bad tile")
            if calls[0] == 4:
                raise dzi.BoundedRenderBusy("queue full")
        return 1.0, 0.0, 10

    row = _run_stub(db_session, monkeypatch, _StubHandle(render))
    assert row.state == "succeeded", row.error
    res = _result(row)
    slide = res["slides"][0]
    assert "error" not in slide
    assert [b["band"] for b in slide["bands"]] == ["high", "mid", "low"]
    assert all(b["total_ms"]["count"] > 0 for b in slide["bands"])
    assert sum(b["errors"] for b in slide["bands"]) == 5
    # A tile refused by a full block-read queue is a skip, not a failure.
    assert sum(b["skipped"] for b in slide["bands"]) == 1
    assert res["summary"]["tile_p95_ms"] == 1.0
    # Failed renders are no clean bill of health.
    assert "noBottleneck" not in [a["code"] for a in res["advice"]]


def test_a_latched_band_reader_keeps_what_it_measured(db_session, monkeypatch):
    """A reader that latches its error ends that slide's band phase (every
    later tile could only fail the same way) and stays out of the parallel
    phase, but the tiles measured before it count."""

    class Latched(Exception):
        pass

    calls = [0]

    def render(key):
        calls[0] += 1
        if calls[0] >= 3:
            raise Latched("sticky")
        return 1.0, 0.0, 10

    h = _StubHandle(render)
    h.latched = lambda exc: isinstance(exc, Latched)
    row = _run_stub(db_session, monkeypatch, h)
    res = _result(row)
    slide = res["slides"][0]
    assert slide["error"].startswith("measurement failed") and slide["reader_latched"] is True
    assert [(b["band"], b["total_ms"]["count"], b["errors"]) for b in slide["bands"]] == [("high", 2, 1)]
    # Never asked again once latched, by the bands or the parallel phase.
    assert calls[0] == 3 and res["scaling"] == []
    # Not "nothing measured": the run keeps its partial band and says it
    # was cut short.
    assert row.state == "succeeded" and res["truncated"] is True
    assert res["summary"]["tile_p95_ms"] == 1.0
    assert "noBottleneck" not in [a["code"] for a in res["advice"]]


def test_a_sampler_that_cannot_start_still_records_the_run(db_session, monkeypatch):
    """start() refused at the thread limit would leave a sampler thread that never
    ran; the finally's join() on it would raise, skipping the final write, so
    the row would stay 'running' until the orphan age limit."""
    real = threading.Thread

    class NoSampler(real):
        def start(self):
            if self.name == "perf-sampler":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(perf_bench.threading, "Thread", NoSampler)
    row = _run_stub(db_session, monkeypatch, _StubHandle())
    assert row.state == "failed" and "can't start new thread" in row.error
    assert row.finished_at is not None and _result(row)["system"]["samples"] == 0
    # And stop() on a sampler that never started simply returns.
    assert perf_bench._SystemSampler().stop()["samples"] == 0


def test_failed_bookkeeping_still_ends_the_run(db_session, monkeypatch):
    def broken(*a, **k):
        raise ValueError("summary broke")

    monkeypatch.setattr(perf_bench, "summarise", broken)
    row = _run_stub(db_session, monkeypatch, _StubHandle())
    assert row.state == "failed" and "summary broke" in row.error
    res = _result(row)
    assert res["truncated"] is True and res["slides"][0]["bands"]


def test_benchmark_renders_use_their_own_block_read_lane(monkeypatch, db_session, perf_slides):
    """On production's process-wide slot and eight-tile queue, the
    benchmark's parallel phase could fill the queue (a viewer's block-read
    tile would then get a 503) and its timings would wait on a class's
    renders. It has a lane of its own."""
    import math

    from tests.test_openslide_utils import _BigFlatSlide

    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 640 * 640)
    lane = dzi.BoundedRenderLane(max_queued=1)
    g = dzi.BoundedDeepZoomGenerator(_BigFlatSlide(10000, 7500), tile_size=254, overlap=1, limit_bounds=False, lane=lane)
    lv = next(
        lv for lv in range(g.level_count)
        if math.prod(g.get_tile_coordinates(lv, (0, 0))[2]) > dzi._TILE_READ_BUDGET_PX
    )
    # Production's queue full and its slot taken: the lane renders anyway,
    # and leaves production's count alone.
    monkeypatch.setattr(dzi, "_bounded_queued", dzi._BOUNDED_MAX_QUEUED)
    assert dzi._bounded_render_slots.acquire(timeout=5)
    try:
        g.get_tile(lv, (0, 0)).close()
    finally:
        dzi._bounded_render_slots.release()
    assert lane.queued == 0 and dzi._bounded_queued == dzi._BOUNDED_MAX_QUEUED
    # Its own queue limit still applies.
    lane.queued = 1
    with pytest.raises(dzi.BoundedRenderBusy):
        g.get_tile(lv, (0, 0))
    lane.queued = 0

    from wsi_app.config import settings

    ome, _ = perf_slides
    run_lane = perf_bench._new_bench_lane()
    h = perf_bench._DzHandle(os.path.join(settings.data_dir, "wsi", ome.wsi_filename), run_lane)
    try:
        assert h.dz._bounded_lane is run_lane
        assert run_lane.max_queued > perf_bench.MAX_CONCURRENCY
    finally:
        h.close()


def test_a_stuck_earlier_run_never_blocks_the_next_ones_renders(db_session, perf_slides, monkeypatch):
    """With one lane per process, a run that ended with a parallel thread
    stuck in a read would leave it holding the lane's serial slot, and the
    next run's first block-read band tile would wait on it inside get_tile —
    no deadline, no cancel — until the orphan reaper failed its row. Each run
    has its own."""
    ome, _ = perf_slides
    lanes: list = []
    real_open = perf_bench._open_handle

    def spying_open(target, lane=None):
        h = real_open(target, lane)
        lanes.append(h.dz._bounded_lane)
        return h

    monkeypatch.setattr(perf_bench, "_open_handle", spying_open)
    first = _run_sync(db_session, [ome.id], max_concurrency=1)
    assert first.state == "succeeded", first.error
    # Every handle of a run shares its lane.
    assert lanes and all(lane is lanes[0] for lane in lanes)
    stuck_lane, lanes[:] = lanes[0], []
    # That run's render thread "stuck in a read": its lane's slot is taken,
    # and every tile of the next run is a block-read one.
    assert stuck_lane.slots.acquire(timeout=5)
    monkeypatch.setattr(dzi, "_TILE_READ_BUDGET_PX", 64 * 64)
    try:
        done: dict = {}
        t = threading.Thread(
            target=lambda: done.setdefault("row", _run_sync(db_session, [ome.id], max_concurrency=1)), daemon=True,
        )
        t.start()
        t.join(60)
        assert not t.is_alive(), "the second run waited on the first run's render slot"
    finally:
        stuck_lane.slots.release()
    row = done["row"]
    assert row.state == "succeeded", row.error
    assert lanes and lanes[0] is not stuck_lane
    bands = _result(row)["slides"][0]["bands"]
    assert all(b["total_ms"]["count"] > 0 and b["skipped"] == 0 for b in bands)


def test_ids_beyond_sqlite_integers_are_refused(client, admin_headers):
    """Ids are bounded to what SQLite can bind (1 .. 2**63 - 1) where they
    are validated. The driver's OverflowError on a 2**64 id is a 422 by
    main.py's global handler anyway; what the bounds add is that 0 and -1
    are refused as invalid (422) up front instead of being
    looked up and answered 404, and that every id endpoint answers the same
    way without relying on the handler."""
    big = 2**64
    for ids in ([big], [0], [-1]):
        r = client.post("/api/admin/perf/runs", json={"slide_ids": ids}, headers=admin_headers)
        assert r.status_code == 422, (ids, r.text)
    for method, url in (
        ("get", f"/api/admin/perf/runs/{big}"),
        ("post", f"/api/admin/perf/runs/{big}/cancel"),
        ("delete", f"/api/admin/perf/runs/{big}"),
        ("get", "/api/admin/perf/runs/0"),
    ):
        assert getattr(client, method)(url, headers=admin_headers).status_code == 422, url
    assert client.get(f"/api/admin/perf/runs/{2**63 - 1}", headers=admin_headers).status_code == 404


def test_job_claim_is_refused_while_a_benchmark_runs(client, admin_headers, db_session, _private_job_store):
    from fastapi import HTTPException

    from wsi_app import jobs

    run = _running_row(db_session)
    with pytest.raises(HTTPException) as e:
        _private_job_store.try_claim_running("backup")
    assert e.value.status_code == 409 and e.value.detail["code"] == jobs.PERF_RUN_ACTIVE_CODE
    assert list(_private_job_store._iter_disk_jobs()) == []
    # The backup endpoint answers it with the code the page translates.
    r = client.post("/api/admin/backup", json={"include_wsi": False}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == jobs.PERF_RUN_ACTIVE_CODE
    # A run row whose worker is gone holds nothing.
    run.pid = _dead_pid()
    db_session.commit()
    job = _private_job_store.try_claim_running("backup")
    assert job is not None
    _private_job_store.update(job.id, state="succeeded")


def test_job_overlap_reads_the_job_records(_private_job_store):
    now = time.time()
    assert perf_bench.job_overlap(now - 60, now) is False
    job = _private_job_store.new("backup")
    # Pending under a live pid: running now.
    assert perf_bench.job_overlap(now - 60, now + 1) is True
    _private_job_store.update(job.id, state="succeeded")
    path = _private_job_store._job_path(job.id)
    # Finished (its last write) before the window opened: no overlap.
    os.utime(path, (now - 120, now - 120))
    assert perf_bench.job_overlap(now - 60, now + 1) is False
    os.utime(path, (now - 30, now - 30))
    assert perf_bench.job_overlap(now - 60, now + 1) is True
    # Created after the window closed: no overlap either.
    assert perf_bench.job_overlap(now - 600, now - 300) is False


def test_server_run_records_a_job_that_overlapped_it(db_session, monkeypatch, _private_job_store):
    """A job that started mid-run (won the race with the start check, or
    started where the check could not read the database) is recorded."""
    started = []

    def render(key):
        if not started:
            started.append(_private_job_store.new("slide_export"))
        return 1.0, 0.0, 10

    try:
        row = _run_stub(db_session, monkeypatch, _StubHandle(render))
    finally:
        for job in started:
            _private_job_store.update(job.id, state="succeeded")
    assert _result(row)["summary"]["job_overlap"] is True
    assert '"job_overlap": true' in row.summary_json


def test_client_run_records_what_else_loaded_the_server(client, admin_headers, db_session, _private_job_store):
    import json as _json

    def save(**result):
        body = _client_result(**result.pop("client", {}))
        body["summary"].update(result.pop("summary", {}))
        body.update(result)
        r = client.post("/api/admin/perf/client-runs", json=body, headers=admin_headers)
        assert r.status_code == 201, r.text
        return client.get(f"/api/admin/perf/runs/{r.json()['id']}", headers=admin_headers).json()

    stamps = {"started_at": "2026-01-01T00:00:00.000Z", "finished_at": "2026-01-01T00:01:00.000Z"}
    # The browser's summary fields are stored as sent.
    run = save(client=stamps, summary={"tile_errors": 2, "hidden_during_run": True})
    assert run["summary"]["tile_errors"] == 2 and run["summary"]["hidden_during_run"] is True
    assert run["summary"]["server_overlap"] is False and run["summary"]["job_overlap"] is False

    # A server benchmark that ran within the last minute (the browser's own
    # clock says the test took one; its offset from the server's is moot).
    now = datetime.now(timezone.utc)
    server = PerfRun(kind="server", state="succeeded", created_at=now - timedelta(seconds=40),
                     finished_at=now - timedelta(seconds=20))
    db_session.add(server)
    db_session.commit()
    assert save(client=stamps)["summary"]["server_overlap"] is True
    short = {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:05Z"}
    assert save(client=short)["summary"]["server_overlap"] is False
    # Still running counts too.
    server.state, server.finished_at, server.pid = "running", None, os.getpid()
    db_session.commit()
    assert save(client=short)["summary"]["server_overlap"] is True
    server.state, server.finished_at = "succeeded", now - timedelta(seconds=20)
    db_session.commit()

    # A job meanwhile.
    job = _private_job_store.new("backup")
    assert save(client=short)["summary"]["job_overlap"] is True
    _private_job_store.update(job.id, state="succeeded")

    # No usable time from the browser (a page that sends no times): unknown, whatever
    # the browser claims about the server.
    run = save(client={"started_at": None, "finished_at": None},
               summary={"server_overlap": False, "job_overlap": False})
    assert run["summary"]["server_overlap"] is None and run["summary"]["job_overlap"] is None
    # ...but a browser that itself saw a server run during its test is
    # believed: that can only add the warning, never clear it.
    run = save(client={"started_at": None, "finished_at": None},
               summary={"server_overlap": True, "job_overlap": True})
    assert run["summary"]["server_overlap"] is True and run["summary"]["job_overlap"] is None
    assert save(client=short, summary={"server_overlap": True})["summary"]["server_overlap"] is True
    # duration_s alone, or a top-level started_at, also give the window.
    assert save(client={"duration_s": 60})["summary"]["server_overlap"] is True
    top = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    assert save(started_at=top)["summary"]["server_overlap"] is True
    # A summary that is not an object is refused.
    bad = {**_client_result(), "summary": [1, 2]}
    assert client.post("/api/admin/perf/client-runs", json=bad, headers=admin_headers).status_code == 422
    assert _json.loads(db_session.query(PerfRun).order_by(PerfRun.id.desc()).first().summary_json)["job_overlap"] is True


def test_history_list_reads_the_summary_column(client, admin_headers, db_session):
    """The list reads the summary column rather than parsing every stored
    result (up to 128 KB each), with the same response shape."""
    import json as _json

    new = PerfRun(kind="server", state="succeeded", summary_json=_json.dumps({"peak_tiles_per_s": 5.0}),
                  result_json=_json.dumps({"summary": {"peak_tiles_per_s": 999.0}}))
    # A row with a NULL summary column: its result's.
    legacy = PerfRun(kind="client", state="succeeded", summary_json=None,
                     result_json=_json.dumps({"summary": {"tiles_per_s": 7.0}}))
    nan = PerfRun(kind="server", state="succeeded", summary_json='{"tile_p95_ms": NaN}')
    empty = PerfRun(kind="server", state="failed", summary_json="null")
    db_session.add_all([new, legacy, nan, empty])
    db_session.commit()
    body = client.get("/api/admin/perf/runs", headers=admin_headers).json()
    by_id = {r["id"]: r for r in body["runs"]}
    assert by_id[new.id]["summary"] == {"peak_tiles_per_s": 5.0}
    assert by_id[legacy.id]["summary"] == {"tiles_per_s": 7.0}
    assert by_id[nan.id]["summary"] == {"tile_p95_ms": None}
    assert by_id[empty.id]["summary"] is None
    assert set(by_id[new.id]) == {
        "id", "kind", "state", "created_at", "finished_at", "actor", "params", "progress",
        "cancel_requested", "error", "summary",
    }


def test_history_list_backfills_missing_summaries(client, admin_headers, db_session, monkeypatch):
    """Rows with a NULL summary column get it filled by the first list that
    meets them — no numbered migration (schema_version stays put)."""
    import json as _json

    from wsi_app.routers import admin_perf

    legacy = PerfRun(kind="client", state="succeeded", result_json=_json.dumps({"summary": {"tiles_per_s": 7.0}}))
    bare = PerfRun(kind="server", state="failed", result_json="")
    running = _running_row(db_session)
    db_session.add_all([legacy, bare])
    db_session.commit()
    assert legacy.summary_json is None and running.summary_json is None

    # A database that refuses the write never fails the GET; it is retried
    # by the next list.
    real_session = admin_perf.SessionLocal

    class Refusing:
        def __init__(self):
            self.real = real_session()

        def __getattr__(self, name):
            return getattr(self.real, name)

        def commit(self):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(admin_perf, "SessionLocal", Refusing)
    r = client.get("/api/admin/perf/runs", headers=admin_headers)
    assert r.status_code == 200
    assert next(x for x in r.json()["runs"] if x["id"] == legacy.id)["summary"] == {"tiles_per_s": 7.0}
    db_session.expire_all()
    assert db_session.get(PerfRun, legacy.id).summary_json is None
    monkeypatch.undo()

    body = client.get("/api/admin/perf/runs", headers=admin_headers).json()
    assert next(x for x in body["runs"] if x["id"] == legacy.id)["summary"] == {"tiles_per_s": 7.0}
    db_session.expire_all()
    assert _json.loads(db_session.get(PerfRun, legacy.id).summary_json) == {"tiles_per_s": 7.0}
    assert db_session.get(PerfRun, bare.id).summary_json == "null"
    # A running row has no result yet: left for the run's own final write.
    assert db_session.get(PerfRun, running.id).summary_json is None


def test_summary_backfill_never_overwrites_a_stored_one(db_session):
    """The run's final write (or a save) that lands between the list's read
    and its backfill keeps its own summary: the UPDATE is conditional."""
    from wsi_app.routers import admin_perf

    row = PerfRun(kind="server", state="succeeded", summary_json='{"peak_tiles_per_s": 9.0}')
    db_session.add(row)
    db_session.commit()
    admin_perf._backfill_summaries([(row.id, {"stale": True})])
    db_session.expire_all()
    assert db_session.get(PerfRun, row.id).summary_json == '{"peak_tiles_per_s": 9.0}'


def test_a_cancel_or_slow_cleanup_after_the_work_does_not_relabel_the_run(db_session, monkeypatch):
    """A cancel arriving once the measurement is over (picked up by the final
    forced flush) must not relabel a complete run 'cancelled' / truncated,
    nor a deadline timed from after the cleanup call it timed out."""
    from wsi_app.db import SessionLocal

    real_close = perf_bench._close_handles
    monkeypatch.setattr(perf_bench, "MAX_RUN_SECONDS", 3)

    def late(run_id, handles):
        own = SessionLocal()
        try:
            perf_bench.request_cancel(own, run_id)
        finally:
            own.close()
        time.sleep(3.2)  # past MAX_RUN_SECONDS, but only in the cleanup
        real_close(run_id, handles)

    monkeypatch.setattr(perf_bench, "_close_handles", late)
    row = _run_stub(db_session, monkeypatch, _StubHandle(), step_seconds=0.2)
    res = _result(row)
    assert row.state == "succeeded" and res["truncated"] is False, (row.state, row.error)
    assert res["duration_s"] >= 3.0 and row.cancel_requested is True


def test_deadline_and_cancel_that_stop_a_phase_are_labelled(db_session, monkeypatch):
    monkeypatch.setattr(perf_bench, "MAX_RUN_SECONDS", 0.5)
    row = _run_stub(db_session, monkeypatch, _StubHandle(), step_seconds=2.0)
    assert row.state == "succeeded" and _result(row)["truncated"] is True


def test_scaling_reports_the_share_of_fresh_tiles():
    """The parallel steps are never cold and warm each other's caches; each
    reports how many of its renders were of a tile new to the run."""

    class Huge(_StubHandle):
        def band_levels(self):
            return {band: (0, 10**6, 10**6) for band, _ in perf_bench.BANDS}

    steps, workers = [], []
    params = perf_bench.BenchParams(max_concurrency=2, step_seconds=0.2)
    perf_bench._measure_scaling([Huge()], params, 1, lambda f: None, _StubReporter(), steps, workers)
    assert [s["fresh_tile_pct"] for s in steps] == [100.0, 100.0]
    # A 4x4 level whose tiles the band phase (here: `seen`) already rendered.
    small = _StubHandle()
    seen = {(id(small), (0, c, r)) for c in range(4) for r in range(4)}
    steps = []
    perf_bench._measure_scaling([small], params, 1, lambda f: None, _StubReporter(), steps, [], seen=seen)
    assert all(s["fresh_tile_pct"] == 0.0 for s in steps)


def test_band_phase_records_its_tiles_in_the_run_wide_seen_set():
    """_measure_bands fills the caller's ``seen`` (a local de-dupe set
    shadowing it would make the scaling phase count band-rendered tiles as
    fresh)."""
    handle = _StubHandle()
    seen: set = set()
    bands, latched = perf_bench._measure_bands(
        handle, 3, random.Random(1), lambda f: None, _StubReporter(), seen,
    )
    assert latched is None and len(bands) == len(perf_bench.BANDS)
    assert seen
    assert all(k[0] == id(handle) and len(k[1]) == 3 for k in seen)

def test_uvicorn_worker_count_from_either_spelling(monkeypatch):
    assert perf_bench._workers_arg(["uvicorn", "app", "--workers", "4"]) == 4
    assert perf_bench._workers_arg(["uvicorn", "--workers=3", "app"]) == 3
    assert perf_bench._workers_arg(["uvicorn", "--workers"]) is None
    assert perf_bench._workers_arg(["uvicorn", "--workers", "x"]) is None
    assert perf_bench._workers_arg(["python", "-c", "spawn"]) is None

    class Proc:
        def __init__(self, cmd, parent=None):
            self._cmd, self._parent = cmd, parent

        def cmdline(self):
            return self._cmd

        def parent(self):
            return self._parent

    # --workers 1: uvicorn serves in-process; its parent is the start script.
    single = Proc(["uvicorn", "wsi_app.main:app", "--workers", "1"], Proc(["bash", "start-wsi-app.sh"]))
    monkeypatch.setattr(perf_bench.psutil, "Process", lambda: single)
    assert perf_bench._uvicorn_workers() == 1
    # A spawned worker of a multi-worker server: the supervisor's argv.
    worker = Proc(["python", "-c", "from multiprocessing.spawn import spawn_main"], Proc(["uvicorn", "--workers=6"]))
    monkeypatch.setattr(perf_bench.psutil, "Process", lambda: worker)
    assert perf_bench._uvicorn_workers() == 6


def test_csv_defangs_a_formula_hidden_behind_a_semicolon():
    from wsi_app import perf_export

    run = {
        "id": 1, "kind": "server", "state": "succeeded",
        "params": {"slides": [{"id": 1, "title": "A;=HYPERLINK(1)"}, {"id": 2, "title": "+B"}]},
    }
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(run)))
    assert row["slide_titles"] == "A;'=HYPERLINK(1);'+B"
    client = {"id": 2, "kind": "client", "state": "succeeded",
              "result": {"client": {"http_protocols": {"h2;=cmd": 1}}}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(client)))
    assert row["client.http_protocols"] == "h2;'=cmd=1"


def test_csv_carries_the_browser_and_overlap_fields():
    from wsi_app import perf_export

    run = {"id": 3, "kind": "client", "state": "succeeded", "result": {
        "client": {"hidden_during_run": False},
        "summary": {"tile_errors": 4, "hidden_during_run": True, "server_overlap": True, "job_overlap": False},
    }}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(run)))
    assert (row["client.tile_errors"], row["client.hidden_during_run"]) == (4, "true")
    assert (row["server_overlap"], row["job_overlap"]) == ("true", "false")
    # Runs stored without these fields: unknown, i.e. empty.
    old = {"id": 4, "kind": "client", "state": "succeeded", "result": {"summary": {}}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(old)))
    assert row["client.tile_errors"] is None and row["server_overlap"] is None and row["job_overlap"] is None
    assert row["client.hidden_during_run"] is None


# ---------------------------------------------------------------------------
# List race, CSV text, band-phase `seen`, run caveats in the summary,
# restore-pending refusal, slot lock, job-claim connection order
# ---------------------------------------------------------------------------


def test_history_list_survives_a_summaryless_row_deleted_mid_request(client, admin_headers, db_session):
    """The list defers result_json; were a row without a summary column to
    lazy-load it in _summary_of, a row pruned or deleted between the list's
    SELECT and that load would raise ObjectDeletedError — a 500."""
    import json as _json

    from sqlalchemy import event

    from wsi_app.db import SessionLocal, engine

    kept = PerfRun(kind="client", state="succeeded", result_json=_json.dumps({"summary": {"tiles_per_s": 7.0}}))
    doomed = PerfRun(kind="client", state="succeeded", result_json=_json.dumps({"summary": {"tiles_per_s": 3.0}}))
    db_session.add_all([kept, doomed])
    db_session.commit()
    doomed_id = doomed.id
    fired = []

    def delete_after_the_list_select(conn, cursor, statement, params, context, executemany):
        if fired or "FROM perf_runs ORDER BY perf_runs.id DESC" not in statement:
            return
        fired.append(True)
        other = SessionLocal()
        try:
            other.execute(delete(PerfRun).where(PerfRun.id == doomed_id))
            other.commit()
        finally:
            other.close()

    event.listen(engine, "after_cursor_execute", delete_after_the_list_select)
    try:
        r = client.get("/api/admin/perf/runs", headers=admin_headers)
    finally:
        event.remove(engine, "after_cursor_execute", delete_after_the_list_select)
    assert fired and r.status_code == 200, r.text
    by_id = {x["id"]: x for x in r.json()["runs"]}
    assert by_id[kept.id]["summary"] == {"tiles_per_s": 7.0}
    # Listed as the SELECT saw it, without a summary to dig out.
    assert by_id[doomed_id]["summary"] is None
    db_session.expire_all()
    assert db_session.get(PerfRun, doomed_id) is None
    assert _json.loads(db_session.get(PerfRun, kept.id).summary_json) == {"tiles_per_s": 7.0}


def test_csv_defangs_every_part_of_browser_reported_text():
    """A browser run's slide title, user agent, page origin etc. are defanged
    in every part, not only at the cell's start: a part after a ';' (a user
    agent has several) would otherwise reach a ';'-splitting spreadsheet as a
    formula."""
    from wsi_app import perf_export

    run = {"id": 5, "kind": "client", "state": "succeeded",
           "params": {"slide_id": 1, "slide_title": "Liver;=HYPERLINK(1)"},
           "result": {"client": {
               "user_agent": "Mozilla/5.0 (X11;=cmd|' /C calc'!A0; Linux x86_64)",
               "page_origin": "https://x;+SUM(1)", "platform": "Linux;@x", "language": "ja;-1",
               "timezone": "Asia/Tokyo;=1", "connection_type": "wifi;=1", "effective_type": "4g;=1",
           }}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(run)))
    assert row["slide_titles"] == "Liver;'=HYPERLINK(1)"
    assert row["client.user_agent"] == "Mozilla/5.0 (X11;'=cmd|' /C calc'!A0; Linux x86_64)"
    assert row["client.page_origin"] == "https://x;'+SUM(1)"
    assert (row["client.platform"], row["client.language"], row["client.timezone"]) == ("Linux;'@x", "ja;'-1", "Asia/Tokyo;'=1")
    assert (row["client.connection_type"], row["client.effective_type"]) == ("wifi;'=1", "4g;'=1")
    # A benign user agent comes out as it went in.
    plain = {"id": 6, "kind": "client", "state": "succeeded",
             "result": {"client": {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(plain)))
    assert row["client.user_agent"] == "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    # Any other field a page posts can be text too (a timestamp, a tile
    # format, a headline number, a list): no part of any cell may start a
    # formula.
    evil = "x;=1"
    forged = {"id": 7, "kind": "client", "state": "succeeded",
              "params": {"slide_id": evil, "slide_title": evil, "mode": evil, "tiles": evil},
              "result": {
                  "slide": {"id": evil, "tile_size": evil, "tile_format": evil, "width": evil},
                  "client": {"started_at": evil, "finished_at": evil, "mobile": evil,
                             "secure_context": evil, "http_protocols": {evil: evil}},
                  "summary": {"tiles_per_s": evil, "mbps": evil, "rtt_ms": evil},
              }}
    cells = [c for c in perf_export.csv_row(forged) if isinstance(c, str)]
    assert any(evil.split(";")[0] in c for c in cells)
    for c in cells:
        assert not any(part[:1] in "=+-@" and part for part in c.split(";")), c


def test_a_failed_or_skipped_band_tile_is_not_counted_as_rendered():
    """Were ``seen`` (the parallel phase's "already rendered" set) to take
    every band tile before its render, failed and skipped ones would lower the
    next steps' fresh-tile share though they warmed nothing."""
    calls = [0]

    def render(key):
        calls[0] += 1
        if calls[0] % 3 == 1:
            raise RuntimeError("bad tile")
        if calls[0] % 3 == 2:
            raise dzi.BoundedRenderBusy("queue full")
        return 1.0, 0.0, 10

    handle = _StubHandle(render)
    seen: set = set()
    bands, latched = perf_bench._measure_bands(handle, 3, random.Random(1), lambda f: None, _StubReporter(), seen)
    assert latched is None
    rendered = sum(b["total_ms"]["count"] for b in bands)
    assert sum(b["errors"] for b in bands) == 3 and sum(b["skipped"] for b in bands) == 3
    assert rendered == 3 and len(seen) == rendered


def test_band_errors_alone_rule_out_no_bottleneck():
    slides = [{"open": {"first_ms": 5.0}, "bands": [{"band": "high", "total_ms": {"count": 5, "p95": 20.0}, "errors": 0}]}]
    calm = [{"concurrency": 1, "tiles": 100, "errors": 0, "tiles_per_s": 30.0, "cpu_busy_pct": 30.0, "iowait_pct": 0.0}]
    codes = lambda advice: [a["code"] for a in advice]  # noqa: E731
    assert codes(perf_bench.summarise(slides, calm, {})[1]) == ["noBottleneck"]
    slides[0]["bands"][0]["errors"] = 1
    # No finding of their own, and no clean bill of health either.
    assert codes(perf_bench.summarise(slides, calm, {})[1]) == []


def test_server_summary_carries_the_runs_caveats(db_session, monkeypatch):
    """The history list and the comparison read only the summary: without
    its caveats, a server run cut short, or one that lost renders, would look
    like a clean baseline."""
    clean = _run_stub(db_session, monkeypatch, _StubHandle())
    s = _result(clean)["summary"]
    assert (s["truncated"], s["band_errors"], s["tile_errors"]) == (False, 0, 0)
    import json as _json
    assert _json.loads(clean.summary_json)["truncated"] is False

    run_thread = threading.current_thread()
    calls = [0]

    def render(key):
        if threading.current_thread() is run_thread:
            calls[0] += 1
            if calls[0] in (2, 5):
                raise RuntimeError("bad band tile")
            return 1.0, 0.0, 10
        raise RuntimeError("bad parallel tile")

    lossy = _run_stub(db_session, monkeypatch, _StubHandle(render))
    res = _result(lossy)
    assert res["summary"]["band_errors"] == 2
    assert res["summary"]["tile_errors"] == sum(st["errors"] for st in res["scaling"]) > 0
    monkeypatch.setattr(perf_bench, "MAX_RUN_SECONDS", 0.5)
    short = _run_stub(db_session, monkeypatch, _StubHandle(), step_seconds=2.0)
    assert _result(short)["summary"]["truncated"] is True

    from wsi_app import perf_export

    run = {"id": 1, "kind": "server", "state": "succeeded", "result": res}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(run)))
    assert (row["server.band_errors"], row["server.tile_errors"]) == (2, res["summary"]["tile_errors"])
    # A browser run's tile_errors stays in its own column.
    cli = {"id": 2, "kind": "client", "state": "succeeded", "result": {"summary": {"tile_errors": 4}}}
    row = dict(zip(perf_export.CSV_HEADER, perf_export.csv_row(cli)))
    assert (row["client.tile_errors"], row["server.tile_errors"], row["server.band_errors"]) == (4, None, None)


def test_start_refused_while_a_restore_apply_awaits_its_restart(client, admin_headers, db_session, perf_slides, monkeypatch):
    """A restore / reset apply that scheduled its restart has already freed
    its job slot; a benchmark started then is killed by the restart (and
    reported interrupted). Every job start refuses in that window; so does
    the benchmark."""
    from wsi_app.routers import admin_backup

    ome, _ = perf_slides
    monkeypatch.setattr(admin_backup, "_restore_apply_pending", lambda: True)
    r = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id], **FAST}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adminPerf.err.jobRunning"
    assert db_session.query(PerfRun).count() == 0


def test_start_checks_for_jobs_under_the_job_slot_lock(client, admin_headers, db_session, perf_slides, monkeypatch):
    """The job check and the row that claims the run slot happen inside
    jobs.store.slot_guard(), which a job claim takes too."""
    import contextlib

    from wsi_app import jobs

    ome, _ = perf_slides
    events: list = []

    @contextlib.contextmanager
    def recording_guard():
        events.append("lock")
        yield
        events.append("unlock")

    real_create = perf_bench.create_run

    def recording_create(*a, **k):
        events.append("create")
        return real_create(*a, **k)

    monkeypatch.setattr(jobs.store, "slot_guard", recording_guard)
    monkeypatch.setattr(jobs.store, "has_running", lambda: events.append("check") or False)
    monkeypatch.setattr(perf_bench, "create_run", recording_create)
    monkeypatch.setattr(perf_bench, "start_in_background", lambda *a: None)
    r = client.post("/api/admin/perf/runs", json={"slide_ids": [ome.id], **FAST}, headers=admin_headers)
    assert r.status_code == 202, r.text
    assert events == ["lock", "check", "create", "unlock"]
    perf_bench.request_cancel(db_session, r.json()["id"])
    db_session.execute(delete(PerfRun))
    db_session.commit()


def test_a_job_claim_takes_its_benchmark_check_connection_before_the_lock(_private_job_store, monkeypatch):
    """The claim must check out a pooled connection for the benchmark check
    before taking the job-slot flock: holding the flock with the pool
    exhausted, it would wait up to pool_timeout there, and every other
    worker's claim would wait on the lock."""
    from sqlalchemy import event

    from wsi_app import jobs
    from wsi_app.db import engine

    events: list = []
    real_flock = jobs.fcntl.flock

    def flock(fd, op):
        events.append("unlock" if op == jobs.fcntl.LOCK_UN else "lock")
        return real_flock(fd, op)

    def on_checkout(*_a):
        events.append("checkout")

    monkeypatch.setattr(jobs.fcntl, "flock", flock)
    event.listen(engine, "checkout", on_checkout)
    try:
        job = _private_job_store.try_claim_running("backup")
    finally:
        event.remove(engine, "checkout", on_checkout)
    assert job is not None
    _private_job_store.update(job.id, state="succeeded")
    locked = events.index("lock")
    # The connection is in hand before the lock; none is checked out under it.
    assert "checkout" in events[:locked] and "checkout" not in events[locked:]
