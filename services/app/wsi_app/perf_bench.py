"""Server-side performance benchmark behind the admin Performance page.

A run measures, on this host and inside the worker that received the start
request:

* **Slide open time** — the reader (OpenSlide, the OME-TIFF reader or the SZI
  archive) plus the Deep Zoom generator; the first open (cold when requested)
  and the median of the repeats.
* **Raw read speed** of the slide file — sequential MB/s and the latency of
  random 64 KiB reads.
* **Single-tile latency** at three zoom bands (full resolution, 1/4, 1/16),
  split into generation (read + composite + resample) and JPEG encoding —
  the work ``/dzi/.../<col>_<row>.jpeg`` does on an nginx cache miss. For SZI
  it is the archive read a Range request costs.
* **Throughput vs parallelism** — random full-resolution tiles rendered by
  1, 2, 4, … threads for a fixed time each: what a class opening unseen areas
  at the same moment looks like to one worker.
* **System load** while it runs — CPU busy / iowait and available memory.

Everything is measured below nginx: a tile nginx already caches never reaches
this code, so these are cache-miss costs. The browser test on the same page
covers the network + nginx side. It is below the app's HTTP layer too: a
real tile request also pays for the slide lookup (the resolve cache or a DB
SELECT), the file stat behind the ETag and the response itself
(``FileResponse`` for an SZI Range read) — small next to a generated tile,
but most of an SZI tile's cost. A result says so (``scope: "below_http"``);
no HTTP layer is imitated, which would measure the imitation.

The benchmark never touches the production handle cache (``dzi._dz_cache``).
It opens its own handles and closes them when done, so a run can neither
close a slide handle a class is using nor keep a file open after it ends.
Two caches are shared with production, though: the OME-TIFF reader's
per-process segment cache (a run's reads push out segments of slides being
viewed on this worker; they are decoded again on demand) and, in cold mode,
the OS page cache of the measured slide files. It does load the server: one
run at a time (enforced by a partial unique index on ``perf_runs``), a time
limit, and a cancel flag the run polls.

The run executes in a daemon thread of the worker that accepted it; its
``perf_runs`` row is the cross-worker record (progress, cancel flag, result),
the same pattern as the backup job store. A worker that dies mid-run leaves a
``running`` row whose pid is dead; :func:`reap_orphans` marks such rows failed
so the single-run slot frees up.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import platform
import random
import socket
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import psutil
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import PerfRun, Slide

_logger = logging.getLogger("wsi_app.perf_bench")

SCHEMA = 1
# What a server result's latencies include (see the module docstring): the
# reader, the generator and the JPEG encoder, not the request around them.
RESULT_SCOPE = "below_http"
MAX_SLIDES = 3
# Most render threads a parallel step may use (see concurrency_cap).
MAX_CONCURRENCY = 32
# Ceiling on one run. The phases check it between tiles, so a run ends within
# about one tile of this even on a very slow disk; a parallel step whose
# threads are stuck in a read gets _JOIN_GRACE_SECONDS more, then is given up.
MAX_RUN_SECONDS = 240
_JOIN_GRACE_SECONDS = 30
# A 'running' row older than this is treated as orphaned even if its pid is
# alive (the pid may have been reused by an unrelated process).
_ORPHAN_AGE_SECONDS = MAX_RUN_SECONDS * 2 + 60
_MAX_STORED_RUNS = 100
_SEQ_READ_BYTES = 256 * 1024 * 1024
_SEQ_CHUNK = 4 * 1024 * 1024
_RANDOM_READS = 200
_RANDOM_READ_SIZE = 64 * 1024
_PROGRESS_FLUSH_SECONDS = 0.5
_SAMPLE_INTERVAL_SECONDS = 1.0
BANDS = (("high", 0), ("mid", 2), ("low", 4))

# Failures the engine names itself are stored as these codes in
# ``perf_runs.error`` (the page translates them, adminPerf.runError.*);
# anything else there is an exception text.
RUN_ERROR_INTERRUPTED = "interrupted"
RUN_ERROR_NO_SLIDE = "noSlideMeasured"
RUN_ERROR_PARALLEL_FAILED = "parallelFailed"


@dataclass
class BenchParams:
    slide_ids: list[int] = field(default_factory=list)
    cold: bool = False
    tiles_per_band: int = 30
    max_concurrency: int = 0
    step_seconds: float = 3.0

    def normalised(self) -> "BenchParams":
        # The CPUs this process may actually run on (affinity, cgroup quota),
        # not the host's: a container held to 2 CPUs on a 64-core host gets
        # 2-CPU defaults. Automatic is one render thread per such CPU, at
        # most 8.
        cpus = effective_cpus()
        conc = self.max_concurrency if self.max_concurrency > 0 else min(math.ceil(cpus), 8)
        return BenchParams(
            slide_ids=[int(s) for s in self.slide_ids][:MAX_SLIDES],
            cold=bool(self.cold),
            tiles_per_band=max(5, min(200, int(self.tiles_per_band))),
            max_concurrency=max(1, min(concurrency_cap(cpus), int(conc))),
            step_seconds=max(1.0, min(10.0, float(self.step_seconds))),
        )


def concurrency_cap(cpus: Optional[float] = None) -> int:
    """Most render threads a run may start: twice the effective CPUs (at
    least 4, at most MAX_CONCURRENCY). Twice is enough to see where
    throughput levels off and whether storage latency rather than the CPU
    sets the ceiling (threads waiting on a read leave a CPU free); beyond it
    the threads only contend, inside a worker that is serving viewers at the
    same time."""
    cpus = effective_cpus() if cpus is None else cpus
    return max(4, min(MAX_CONCURRENCY, math.ceil(2 * cpus)))


@dataclass
class Target:
    slide_id: int
    title: str
    path: str
    fmt: str  # "szi" | "wsi"
    vendor: str
    width: int
    height: int
    file_size: int


class BenchError(RuntimeError):
    """A start request that can't be honoured (maps to a 4xx)."""

    def __init__(self, code: str, status: int = 409, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.params = params


# ---------------------------------------------------------------------------
# Statistics / system helpers
# ---------------------------------------------------------------------------


def stats_ms(values: list[float]) -> dict:
    """Summary of millisecond samples: count, mean, min, p50/p90/p95/p99, max."""
    if not values:
        return {"count": 0}
    arr = sorted(values)
    n = len(arr)

    def pct(p: float) -> float:
        k = (n - 1) * p / 100.0
        lo = math.floor(k)
        hi = min(lo + 1, n - 1)
        return arr[lo] + (arr[hi] - arr[lo]) * (k - lo)

    return {
        "count": n,
        "mean": round(sum(arr) / n, 2),
        "min": round(arr[0], 2),
        "p50": round(pct(50), 2),
        "p90": round(pct(90), 2),
        "p95": round(pct(95), 2),
        "p99": round(pct(99), 2),
        "max": round(arr[-1], 2),
    }


def _cpu_split(a, b) -> tuple[float, float]:
    """(busy %, iowait %) between two ``psutil.cpu_times()`` snapshots.

    Computed from deltas rather than ``psutil.cpu_percent()``, whose hidden
    per-process "last call" state would be shared (and reset) between the
    sampler thread and the per-step measurement.
    """
    def total(t) -> float:
        s = sum(t)
        # guest / guest_nice are already counted inside user / nice on Linux.
        s -= getattr(t, "guest", 0.0) + getattr(t, "guest_nice", 0.0)
        return s

    dt = total(b) - total(a)
    if dt <= 0:
        return 0.0, 0.0
    # Linux can step iowait (and in rare cases idle) backwards between two
    # reads; clamp the way psutil.cpu_percent does.
    idle = max(0.0, b.idle - a.idle)
    iowait = max(0.0, getattr(b, "iowait", 0.0) - getattr(a, "iowait", 0.0))
    busy = max(0.0, dt - idle - iowait)
    return round(100.0 * busy / dt, 1), round(100.0 * iowait / dt, 1)


def _cgroup_cpu_limit() -> Optional[float]:
    """CPUs' worth of time the cgroup quota allows (cpu.max / CFS quota on
    this process's cgroup or an ancestor), or None when unlimited."""
    try:
        from .routers.admin_system import _cgroup_info

        cg = _cgroup_info()
    except Exception:
        return None
    quota, period = cg.get("cpu_quota_us"), cg.get("cpu_period_us")
    return quota / period if quota and period and quota > 0 and period > 0 else None


def effective_cpus() -> float:
    """CPUs this process can actually use: the affinity mask (a cpuset or
    ``taskset``), further limited by a cgroup CPU quota — which may be
    fractional (a 1.5-CPU container). ``os.cpu_count()`` counts the host's,
    which on a limited container would make the automatic concurrency, the CPU
    busy figures and the advice describe a machine the app doesn't have."""
    try:
        n: float = len(os.sched_getaffinity(0)) or (os.cpu_count() or 1)
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    if limit is not None:
        n = min(n, limit)
    return round(max(n, 0.01), 2)


def _cgroup_cpu_usage_s() -> Optional[float]:
    """CPU seconds used so far by this process's cgroup v2 (every process in
    it: all uvicorn workers of a service unit, or a whole container), from
    ``cpu.stat``; None when not readable (cgroup v1, no cpu controller)."""
    try:
        from .routers.admin_system import _CGROUP_ROOT, _own_cgroup_v2_path

        path = _own_cgroup_v2_path()
        if path is None:
            return None
        with open(_CGROUP_ROOT + ("" if path == "/" else path) + "/cpu.stat", encoding="ascii") as f:
            for line in f:
                key, _, value = line.partition(" ")
                if key == "usage_usec":
                    return int(value) / 1e6
    except Exception:  # noqa: BLE001 — best-effort; the caller falls back
        return None
    return None


def _parse_cpu_list(text: str) -> Optional[int]:
    """Number of CPUs in a kernel CPU list ("0-3,8,10-11" -> 7); None when
    empty or malformed."""
    n = 0
    for part in text.strip().split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        try:
            a, b = int(lo), int(hi or lo)
        except ValueError:
            return None
        if b < a:
            return None
        n += b - a + 1
    return n or None


def _cgroup_cpuset_count() -> Optional[int]:
    """CPUs this process's cgroup v2 may run on (``cpuset.cpus.effective``):
    the confinement every process in the cgroup shares — Docker's
    ``--cpuset-cpus``, systemd's ``AllowedCPUs=``. None when not readable."""
    try:
        from .routers.admin_system import _CGROUP_ROOT, _own_cgroup_v2_path

        path = _own_cgroup_v2_path()
        if path is None:
            return None
        with open(_CGROUP_ROOT + ("" if path == "/" else path) + "/cpuset.cpus.effective", encoding="ascii") as f:
            return _parse_cpu_list(f.read())
    except Exception:  # noqa: BLE001 — best-effort; the caller falls back
        return None


def _affinity_count() -> Optional[int]:
    try:
        return len(os.sched_getaffinity(0)) or None
    except (AttributeError, OSError):
        return None


class _CpuClock:
    """Where "CPU busy" is read from, chosen once per run.

    * ``host`` — the host-wide /proc/stat split, when this process may use
      every CPU of the host: other processes compete for the same CPUs, so
      their load is part of the answer.
    * ``cgroup`` — the cgroup's CPU time (cpu.stat) against the effective
      CPUs, when a cgroup CPU quota is what limits the process: host-wide
      percentages on a 2-CPU container of a 64-core host could never show
      the container saturated (and the advice would say threadBound where
      the truth is cpuBound). The quota bounds the whole cgroup, so its usage
      over the quota's CPUs is a share that tops out at 100 %.
      Likewise when the cgroup's own cpuset (``cpuset.cpus.effective``) is
      what limits it — a Docker ``--cpuset-cpus`` / systemd ``AllowedCPUs=``
      confines every process in the cgroup to those CPUs, so the other
      workers' load on them is part of the answer.
    * ``process`` — this process's own CPU time against the effective CPUs,
      when limited otherwise: by an affinity mask narrower than its cgroup's
      (``taskset``), or by a quota / cpuset whose cgroup usage cannot be
      read. An affinity mask binds only this process, while the
      cgroup's usage counts every process in it (the other uvicorn workers,
      whatever else the service runs, unpinned): divided by this process's
      CPUs it would overstate busy — past 100 %, clamped — and claim cpuBound
      for a run that left its CPUs idle.

    iowait stays host-wide in every mode: the kernel keeps no per-cgroup
    iowait, and a disk shared with the host is a shared wait.
    """

    def __init__(self, cpus: Optional[float] = None) -> None:
        self.cpus = effective_cpus() if cpus is None else cpus
        host = psutil.cpu_count(logical=True) or 1
        limit = _cgroup_cpu_limit()
        # The quota set the effective count when it is no larger than it
        # (effective_cpus() is the smaller of mask and quota, rounded).
        quota_bound = limit is not None and round(limit, 2) <= self.cpus
        # The cgroup's cpuset set it when the mask is exactly the cgroup's
        # CPUs (not a taskset narrower than them).
        mask = _affinity_count()
        cpuset = _cgroup_cpuset_count()
        cpuset_bound = cpuset is not None and mask is not None and cpuset == mask
        if self.cpus >= host:
            self.basis = "host"
        elif (quota_bound or cpuset_bound) and _cgroup_cpu_usage_s() is not None:
            self.basis = "cgroup"
        else:
            self.basis = "process"

    def _used(self) -> Optional[float]:
        if self.basis == "cgroup":
            return _cgroup_cpu_usage_s()
        if self.basis == "process":
            try:
                t = psutil.Process().cpu_times()
                return t.user + t.system
            except Exception:
                return None
        return None

    def snap(self) -> tuple:
        return psutil.cpu_times(), self._used(), time.monotonic()

    def split(self, a: tuple, b: tuple) -> tuple[float, float]:
        """(busy %, iowait %) between two ``snap()`` results."""
        busy, iowait = _cpu_split(a[0], b[0])
        if self.basis == "host":
            return busy, iowait
        wall = b[2] - a[2]
        if a[1] is None or b[1] is None or wall <= 0:
            # The usage read failed this once: the host figure is all there is.
            return busy, iowait
        used = max(0.0, b[1] - a[1])
        return round(min(100.0, 100.0 * used / (wall * self.cpus)), 1), iowait


class _SystemSampler:
    """Samples CPU busy / iowait and available memory once a second."""

    def __init__(self, clock: Optional[_CpuClock] = None) -> None:
        self.clock = clock or _CpuClock()
        self.busy: list[float] = []
        self.iowait: list[float] = []
        self.mem_available_mb: list[int] = []
        # What the host was already doing when the run began: a busy server
        # (a class viewing slides, a backup) skews every number that follows.
        self.load_avg_start: Optional[list[float]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="perf-sampler", daemon=True)

    def start(self) -> None:
        self.load_avg_start = _load_avg()
        self._thread.start()

    def _run(self) -> None:
        prev = self.clock.snap()
        while not self._stop.wait(_SAMPLE_INTERVAL_SECONDS):
            cur = self.clock.snap()
            busy, iowait = self.clock.split(prev, cur)
            prev = cur
            self.busy.append(busy)
            self.iowait.append(iowait)
            try:
                self.mem_available_mb.append(psutil.virtual_memory().available // (1024 * 1024))
            except Exception:
                pass

    def stop(self) -> dict:
        self._stop.set()
        # Only a thread that started can be joined: start() refused at the
        # thread limit left one that never ran, and join() on it raises —
        # which would escape execute_run's finally and skip the final
        # write, leaving the row 'running' until the orphan age limit.
        if self._thread.ident is not None:
            self._thread.join(timeout=5)

        def avg_max(xs: list[float]) -> dict:
            if not xs:
                return {"avg": None, "max": None}
            return {"avg": round(sum(xs) / len(xs), 1), "max": round(max(xs), 1)}

        return {
            "cpu_busy_pct": avg_max(self.busy),
            # What cpu_busy_pct is a percentage of (see _CpuClock): the host's
            # CPUs, or the effective ones by the cgroup's / process's time.
            "cpu_basis": self.clock.basis,
            "cpu_effective": self.clock.cpus,
            "iowait_pct": avg_max(self.iowait),
            "mem_available_mb_min": min(self.mem_available_mb) if self.mem_available_mb else None,
            "load_avg_start": self.load_avg_start,
            "load_avg_end": _load_avg(),
            "samples": len(self.busy),
        }


def _load_avg() -> Optional[list[float]]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


def _cpu_model() -> Optional[str]:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _workers_arg(cmd: list[str]) -> Optional[int]:
    """The ``--workers N`` / ``--workers=N`` value of an argv, if any."""
    for i, arg in enumerate(cmd):
        if arg == "--workers":
            value = cmd[i + 1] if i + 1 < len(cmd) else ""
        elif arg.startswith("--workers="):
            value = arg.split("=", 1)[1]
        else:
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _uvicorn_workers() -> Optional[int]:
    """Worker count of the uvicorn server this process belongs to, if any.

    The start script computes the count and passes it only on the command
    line, so read it back from an argv: this process's own first — with
    ``--workers 1`` uvicorn serves in-process, without a supervisor, so the
    parent is the start script or systemd — then the parent's, the
    supervisor of a multi-worker server (whose workers are spawned with a
    multiprocessing argv of their own).
    """
    try:
        proc = psutil.Process()
        own = _workers_arg(proc.cmdline())
        if own is not None:
            return own
        parent = proc.parent()
        return _workers_arg(parent.cmdline()) if parent else None
    except Exception:
        return None


def environment() -> dict:
    from . import dzi, ome_tiff
    from .config import settings
    from .version import read_version

    env: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "cpu_model": _cpu_model(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        # What this process can use of them: affinity and cgroup quota.
        "cpu_effective": effective_cpus(),
        "mem_total_mb": psutil.virtual_memory().total // (1024 * 1024),
        "workers": _uvicorn_workers(),
        "pid": os.getpid(),
        "app_version": read_version(),
        "python": platform.python_version(),
        "tile_size": dzi.TILE_SIZE,
        "overlap": dzi.OVERLAP,
        "jpeg_quality": dzi._JPEG_QUALITY,
        "dzi_cache_size": dzi._CACHE_MAX,
        "dzi_cache_max_bytes": dzi._CACHE_MAX_BYTES,
        "ome_tile_cache_bytes": ome_tiff.segment_cache_stats()["max_bytes"],
    }
    try:
        import openslide

        env["openslide"] = openslide.__library_version__
    except Exception:
        env["openslide"] = None
    try:
        from .routers.admin_system import _disk_data_dir_full

        disk = _disk_data_dir_full(settings.data_dir)
        env["data_disk"] = {"fstype": disk.get("fstype"), "rotational": disk.get("rotational")}
    except Exception:
        env["data_disk"] = None
    # The rest is what an exported result needs to be read without access to
    # this host: what it runs on, what limits it, and what sits in front of
    # the app. Each part is best-effort, so one unreadable source leaves only
    # its own keys empty.
    try:
        from .routers.admin_system import _detect_virtualization, _get_iana_timezone, _read_os_release

        env["os"] = _read_os_release().get("PRETTY_NAME") or platform.system()
        env["kernel"] = platform.release()
        env["machine"] = platform.machine()
        env["virtualization"] = _detect_virtualization()
        env["timezone"] = _get_iana_timezone()
    except Exception:
        _logger.debug("perf: os snapshot failed", exc_info=True)
    try:
        freq = psutil.cpu_freq()
        # Some VMs and ARM boards report zeros: unknown, not 0 MHz.
        env["cpu_freq_mhz"] = round(freq.max or freq.current) or None if freq else None
    except Exception:
        env["cpu_freq_mhz"] = None
    try:
        from .routers.admin_system import _cgroup_info

        cg = _cgroup_info()
        quota, period = cg.get("cpu_quota_us"), cg.get("cpu_period_us")
        env["cgroup"] = {
            "memory_max_mb": cg["memory_max_bytes"] // (1024 * 1024) if cg.get("memory_max_bytes") else None,
            "cpu_limit": round(quota / period, 2) if quota and period else None,
        } if cg.get("in_container") or cg.get("memory_max_bytes") or cg.get("cpu_quota_us") else None
    except Exception:
        env["cgroup"] = None
    try:
        from .routers.admin_system import _nginx_cache_info_compute

        # The stat file only, never the directory walk (see its docstring):
        # this snapshot runs inside the request that saves a browser result.
        nc = _nginx_cache_info_compute(walk=False)
        env["nginx_cache"] = {k: nc.get(k) for k in ("max_size", "inactive", "used_bytes", "file_count")}
    except Exception:
        env["nginx_cache"] = None
    return env


def viewer_rendering(db: Session) -> dict:
    """The viewer's OpenSeadragon settings as stored right now.

    Not inputs of either test, but they decide how many tiles every viewer
    asks for, so an exported run carries them as part of its conditions.
    """
    from ._serializers import normalize_max_image_cache_count, normalize_max_zoom_pixel_ratio
    from .models import ViewerSettings

    # Only these columns: the row also holds the logo / header image blobs.
    row = db.execute(
        select(
            ViewerSettings.immediate_render,
            ViewerSettings.max_image_cache_count,
            ViewerSettings.max_zoom_pixel_ratio,
        ).where(ViewerSettings.id == 1)
    ).first()
    immediate, cache, zoom = row if row is not None else (True, None, None)
    return {
        "immediate_render": bool(immediate),
        "max_image_cache_count": normalize_max_image_cache_count(cache),
        "max_zoom_pixel_ratio": normalize_max_zoom_pixel_ratio(zoom),
    }


def conditions_snapshot(db: Optional[Session] = None) -> dict:
    """``environment()`` plus the viewer rendering settings, never raising."""
    env = _safe(environment)
    try:
        if db is not None:
            env["viewer_rendering"] = viewer_rendering(db)
        else:
            with SessionLocal() as own:
                env["viewer_rendering"] = viewer_rendering(own)
    except Exception:
        _logger.warning("perf: viewer settings snapshot failed", exc_info=True)
        env["viewer_rendering"] = None
    return env


# ---------------------------------------------------------------------------
# Slide handles
# ---------------------------------------------------------------------------


class _DzHandle:
    """OpenSlide / OME-TIFF slide + DeepZoomGenerator, rendering tiles the
    way ``dzi.get_tile`` does."""

    def __init__(self, path: str, lane=None) -> None:
        from . import dzi
        from .openslide_utils import open_wsi

        self._quality = dzi._JPEG_QUALITY
        self.osr = open_wsi(path)
        try:
            # The run's own block-read slot and queue (_new_bench_lane), not
            # production's: sharing them would let a run's parallel phase fill
            # the worker's queue — a viewer's block-read tile would then get a 503 —
            # and put waits behind a class's renders into the timings.
            self.dz = dzi.BoundedDeepZoomGenerator(
                self.osr, tile_size=dzi.TILE_SIZE, overlap=dzi.OVERLAP, limit_bounds=False,
                lane=lane if lane is not None else _new_bench_lane(),
            )
        except Exception:
            self.osr.close()
            raise
        self.vendor = self.osr.properties.get("openslide.vendor", "") or ""

    def band_levels(self) -> dict[str, Any]:
        top = self.dz.level_count - 1
        out = {}
        for band, drop in BANDS:
            level = max(0, top - drop)
            cols, rows = self.dz.level_tiles[level]
            out[band] = (level, cols, rows)
        return out

    @staticmethod
    def level_number(ref) -> int:
        return ref[0]

    @staticmethod
    def random_tile(ref, rng: random.Random):
        level, cols, rows = ref
        return (level, rng.randrange(cols), rng.randrange(rows))

    def render(self, key) -> tuple[float, float, int]:
        level, col, row = key
        t0 = time.perf_counter()
        tile = self.dz.get_tile(level, (col, row))
        t1 = time.perf_counter()
        buf = io.BytesIO()
        try:
            rgb = tile.convert("RGB")
        finally:
            tile.close()
        try:
            rgb.save(buf, "JPEG", quality=self._quality)
        finally:
            rgb.close()
        t2 = time.perf_counter()
        return (t1 - t0) * 1000.0, (t2 - t1) * 1000.0, buf.tell()

    def latched(self, exc: BaseException) -> bool:
        """Whether ``exc`` left this handle failing every later call.

        OpenSlide latches its first read error: from then on every call on
        the handle raises it again (dzi.get_tile retires such a handle from
        the production cache for the same reason). The OME-TIFF reader raises
        the same exception class for one bad segment but does not latch."""
        from openslide import OpenSlide, OpenSlideError

        return isinstance(exc, OpenSlideError) and isinstance(self.osr, OpenSlide)

    def close(self) -> None:
        self.osr.close()


class _SziHandle:
    """SZI archive: a tile costs one read of a stored ZIP member — what the
    Range request szi-tile-source sends amounts to on the server."""

    def __init__(self, path: str) -> None:
        from . import szi_utils as su

        self.zf = su._open_szi(path)
        try:
            dzi_name = su._find_dzi_member(self.zf)
            meta = su._parse_dzi_xml(
                su._read_member_capped(self.zf, dzi_name, su._MAX_DZI_DESCRIPTOR_BYTES, what="descriptor")
            )
            prefix = su._stem_from_dzi_name(dzi_name) + "_files/"
            max_dim = max(meta["width"], meta["height"])
            max_level = math.ceil(math.log2(max_dim)) if max_dim > 1 else 0
            levels: dict[int, list[str]] = {}
            for name in self.zf.namelist():
                if not name.startswith(prefix):
                    continue
                head, _, tail = name[len(prefix):].partition("/")
                if not tail:
                    continue
                # Same level-name rule as the thumbnail builder: a negative or
                # non-canonical directory ("-5", "08") is not a level the
                # viewer could ever request, so it must not be measured either.
                lvl = su._level_number(head, max_level)
                if lvl is not None:
                    levels.setdefault(lvl, []).append(name)
            if not levels:
                raise ValueError("SZI archive contains no tile levels")
            self._levels = levels
        except Exception:
            self.zf.close()
            raise
        self.vendor = "szi"

    def band_levels(self) -> dict[str, Any]:
        present = sorted(self._levels)
        top = present[-1]
        out = {}
        for band, drop in BANDS:
            want = top - drop
            level = max((lv for lv in present if lv <= want), default=present[0])
            out[band] = (level, self._levels[level])
        return out

    @staticmethod
    def level_number(ref) -> int:
        return ref[0]

    @staticmethod
    def random_tile(ref, rng: random.Random):
        return rng.choice(ref[1])

    def render(self, key) -> tuple[float, float, int]:
        t0 = time.perf_counter()
        data = self.zf.read(key)
        return (time.perf_counter() - t0) * 1000.0, 0.0, len(data)

    @staticmethod
    def latched(exc: BaseException) -> bool:
        # A member that fails to read leaves the archive readable for the rest.
        return False

    def close(self) -> None:
        self.zf.close()


def _new_bench_lane():
    """A block-read render lane (dzi.BoundedRenderLane) for one run: every
    handle of the run shares it, and the next run gets a new one.

    Not one per process, although runs never overlap (the single-run index):
    a run that ends with a parallel thread stuck in a read (a network or
    failing disk; see _measure_scaling) leaves that thread holding the lane's
    slot, maybe for good, and the next run's first block-read band tile would
    then wait on it inside get_tile — where no deadline or cancel is checked —
    so the run would hang until the orphan age limit reaped its row, and its
    thread would never end. The price of a fresh lane is memory: each stuck run keeps
    at most one block-read render in hand (the slot is serial; its queued
    threads hold nothing yet), so the worker may hold one such render per
    stalled run plus the current run's, beside production's own one. Its
    queue admits every render thread a run can start, so the parallel phase
    waits for the serial slot the way queued viewers do, rather than being
    refused."""
    from . import dzi

    return dzi.BoundedRenderLane(max_queued=MAX_CONCURRENCY + 1)


def _render_busy(exc: BaseException) -> bool:
    """Whether a render failed only because its block-read queue was full
    (dzi.BoundedRenderBusy): a skipped tile, not a fault of the reader."""
    from .dzi import BoundedRenderBusy

    return isinstance(exc, BoundedRenderBusy)


def _open_handle(target: Target, lane=None):
    return _SziHandle(target.path) if target.fmt == "szi" else _DzHandle(target.path, lane)


def _drop_page_cache(path: str) -> None:
    """Evict ``path`` from the OS page cache (best effort, no root needed:
    POSIX_FADV_DONTNEED drops the clean pages of a file the caller can read)."""
    fadvise = getattr(os, "posix_fadvise", None)
    if fadvise is None:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Run bookkeeping (DB)
# ---------------------------------------------------------------------------


def _utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _pid_alive(pid: Optional[int]) -> bool:
    from .jobs import _pid_alive as alive

    return alive(pid)


def reap_orphans(db: Session) -> int:
    """Mark 'running' rows whose worker is gone (or that are implausibly old)
    as failed, freeing the single-run slot. Returns the number reaped."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = db.execute(
        select(PerfRun.id, PerfRun.pid, PerfRun.created_at).where(PerfRun.state == "running")
    ).all()
    reaped = 0
    for row in rows:
        created = _utc_naive(row.created_at)
        too_old = created is not None and now - created > timedelta(seconds=_ORPHAN_AGE_SECONDS)
        if _pid_alive(row.pid) and not too_old:
            continue
        # Conditional, like the run's own final write: a run that finished
        # between the SELECT above and this UPDATE keeps its outcome.
        res = db.execute(
            update(PerfRun).where(PerfRun.id == row.id, PerfRun.state == "running").values(
                state="failed", error=RUN_ERROR_INTERRUPTED, finished_at=datetime.now(timezone.utc),
            )
        )
        reaped += res.rowcount or 0
    db.commit()
    return reaped


RUN_KINDS = ("server", "client")


def prune(db: Session) -> None:
    """Keep the newest _MAX_STORED_RUNS finished runs of each kind.

    A quota per kind, not one pool: browser tests are quick to repeat (from
    several devices, one after another), and a pooled limit would let an
    afternoon of them push every server benchmark — the baseline they are
    compared with — out of the history."""
    ids: list[int] = []
    for kind in RUN_KINDS:
        ids += db.execute(
            select(PerfRun.id).where(PerfRun.state != "running", PerfRun.kind == kind)
            .order_by(PerfRun.id.desc()).offset(_MAX_STORED_RUNS)
        ).scalars().all()
    # A row of any other kind (none is written today) has no quota to fall
    # under; it is removed rather than kept forever.
    ids += db.execute(
        select(PerfRun.id).where(PerfRun.state != "running", PerfRun.kind.not_in(RUN_KINDS))
    ).scalars().all()
    if ids:
        db.execute(delete(PerfRun).where(PerfRun.id.in_(ids)))
        db.commit()


def server_run_active(db: Optional[Session] = None) -> bool:
    """Whether a server benchmark is running (after reaping orphans, so a
    row left by a dead worker never blocks anything)."""
    own = db is None
    db = db or SessionLocal()
    try:
        reap_orphans(db)
        return db.execute(
            select(PerfRun.id).where(PerfRun.state == "running").limit(1)
        ).first() is not None
    finally:
        if own:
            db.close()


def _epoch(dt: Optional[datetime]) -> Optional[float]:
    """A stored timestamp (naive = UTC, as SQLite hands them back) as epoch."""
    dt = _utc_naive(dt)
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt is not None else None


def server_run_overlap(db: Session, start: float, end: float, exclude_id: Optional[int] = None) -> bool:
    """Whether a server benchmark ran at any moment of [start, end] (epoch
    seconds): a browser test taken meanwhile measured a loaded server."""
    rows = db.execute(
        select(PerfRun.id, PerfRun.state, PerfRun.created_at, PerfRun.finished_at)
        .where(PerfRun.kind == "server").order_by(PerfRun.id.desc()).limit(_MAX_STORED_RUNS + 1)
    ).all()
    for r in rows:
        if r.id == exclude_id:
            continue
        began = _epoch(r.created_at)
        ended = time.time() if r.state == "running" else _epoch(r.finished_at)
        if began is None or ended is None:
            continue
        if began <= end and ended >= start:
            return True
    return False


def job_overlap(start: float, end: float) -> Optional[bool]:
    """Whether a backup / restore / slide-transfer job (jobs.store) ran at
    any moment of [start, end] (epoch seconds); None when the job records
    cannot be read.

    A record holds its creation time; its file's mtime is its last write,
    which for a finished job is the terminal update. Records live a week
    (prune_terminal), far longer than any window asked about here."""
    from .jobs import _TERMINAL_STATES, _created_at_epoch, _pid_alive as job_pid_alive, store

    try:
        for job in store._iter_disk_jobs():
            began = _created_at_epoch(job.created_at)
            if began is None or began > end:
                continue
            if job.state not in _TERMINAL_STATES:
                if job_pid_alive(job.pid):
                    return True
                # Its worker died mid-job: the record's last write is the
                # last moment it is known to have been working.
            path = store._job_path(job.id)
            try:
                ended = os.stat(path).st_mtime if path else None
            except OSError:
                continue
            if ended is not None and ended >= start:
                return True
    except Exception:
        _logger.warning("perf: reading the job records failed", exc_info=True)
        return None
    return False


def resolve_targets(db: Session, slide_ids: list[int]) -> list[Target]:
    """Slides to measure: the requested ids, or the largest slides with a file
    on disk when none were given."""
    from .dzi import _slide_path

    columns = (
        Slide.id, Slide.title, Slide.catalog_id, Slide.wsi_filename, Slide.vendor,
        Slide.width, Slide.height, Slide.file_size_bytes,
    )
    if slide_ids:
        rows = db.execute(select(*columns).where(Slide.id.in_(slide_ids))).all()
        found = {r.id for r in rows}
        missing = [i for i in slide_ids if i not in found]
        if missing:
            raise BenchError("adminPerf.err.unknownSlide", status=404, ids=", ".join(map(str, missing)))
        order = {sid: i for i, sid in enumerate(slide_ids)}
        rows = sorted(rows, key=lambda r: order[r.id])
    else:
        rows = db.execute(
            select(*columns).where(Slide.wsi_filename.is_not(None), Slide.wsi_filename != "")
            .order_by(Slide.file_size_bytes.desc(), Slide.id.asc())
        ).all()

    targets: list[Target] = []
    for r in rows:
        if not r.wsi_filename:
            if slide_ids:
                raise BenchError("adminPerf.err.slideFileMissing", status=422, id=r.id)
            continue
        try:
            path = _slide_path(Slide(wsi_filename=r.wsi_filename))
        except FileNotFoundError:
            path = None
        if not path or not os.path.isfile(path):
            if slide_ids:
                raise BenchError("adminPerf.err.slideFileMissing", status=422, id=r.id)
            continue
        targets.append(Target(
            slide_id=r.id,
            title=(r.title or r.catalog_id or r.wsi_filename or "")[:200],
            path=path,
            fmt="szi" if r.wsi_filename.lower().endswith(".szi") else "wsi",
            vendor=r.vendor or "",
            width=r.width or 0,
            height=r.height or 0,
            file_size=os.path.getsize(path),
        ))
        if len(targets) >= MAX_SLIDES:
            break
    if not targets:
        raise BenchError("adminPerf.err.noSlides", status=422)
    return targets


def create_run(db: Session, actor_username: str, params: BenchParams, targets: list[Target]) -> PerfRun:
    """Insert the 'running' row that claims the single-run slot."""
    reap_orphans(db)
    row = PerfRun(
        kind="server",
        state="running",
        actor_username=actor_username or "",
        pid=os.getpid(),
        params_json=json.dumps({
            **asdict(params),
            "slides": [{"id": t.slide_id, "title": t.title} for t in targets],
        }, ensure_ascii=False),
        progress_json=json.dumps({"phase": "starting", "percent": 0}),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise BenchError("adminPerf.err.alreadyRunning", status=409)
    db.refresh(row)
    prune(db)
    return row


def request_cancel(db: Session, run_id: int) -> bool:
    res = db.execute(
        update(PerfRun).where(PerfRun.id == run_id, PerfRun.state == "running").values(cancel_requested=True)
    )
    db.commit()
    return bool(res.rowcount)


class _Reporter:
    """Throttled progress writes + cancel polling for one run."""

    def __init__(self, run_id: int, deadline: float) -> None:
        self.run_id = run_id
        self.deadline = deadline
        self.cancelled = False
        self.gone = False
        # Why should_stop() first answered True — "deadline", "cancel" or
        # "gone" — or None while nothing has stopped the run. The run's
        # labels (cancelled, timed out) come from this, i.e. from what
        # actually cut a phase short, not from flags seen after the work.
        self.stopped_by: Optional[str] = None
        self._progress: dict = {"phase": "starting", "percent": 0}
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def update(self, phase: str, percent: float, **detail: Any) -> None:
        with self._lock:
            self._progress = {"phase": phase, "percent": round(max(0.0, min(100.0, percent)), 1), **detail}
        self.flush()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_flush < _PROGRESS_FLUSH_SECONDS:
                return
            self._last_flush = now
            payload = json.dumps(self._progress, ensure_ascii=False)
        db = SessionLocal()
        try:
            res = db.execute(
                update(PerfRun).where(PerfRun.id == self.run_id, PerfRun.state == "running")
                .values(progress_json=payload)
            )
            flag = db.execute(select(PerfRun.cancel_requested).where(PerfRun.id == self.run_id)).scalar_one_or_none()
            db.commit()
            if not res.rowcount:
                # The row was reaped, deleted or reset out from under the run.
                self.gone = True
            if flag:
                self.cancelled = True
        except Exception:
            db.rollback()
            _logger.warning("perf run %s: progress write failed", self.run_id, exc_info=True)
        finally:
            db.close()

    def should_stop(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.stopped_by = self.stopped_by or "deadline"
            return True
        self.flush()
        if self.cancelled or self.gone:
            self.stopped_by = self.stopped_by or ("cancel" if self.cancelled else "gone")
            return True
        return False


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def _measure_open(target: Target, cold: bool, lane=None) -> tuple[Any, dict]:
    """Open the slide three times; return the last handle (kept open for the
    tile phases) and the timings. The first open is cold when requested.
    ``lane`` is the run's block-read lane (_new_bench_lane)."""
    if cold:
        _drop_page_cache(target.path)
    samples = []
    handle = None
    for i in range(3):
        if handle is not None:
            handle.close()
        t0 = time.perf_counter()
        handle = _open_handle(target, lane)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return handle, {
        "first_ms": round(samples[0], 2),
        "repeat_ms": round(statistics.median(samples[1:]), 2),
    }


def _measure_disk(target: Target, cold: bool, rng: random.Random, reporter: _Reporter) -> dict:
    path = target.path
    size = os.path.getsize(path)
    if cold:
        _drop_page_cache(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        want = min(size, _SEQ_READ_BYTES)
        done = 0
        # Only the reads are timed: should_stop() may write progress to the
        # database, which must not count against the disk.
        seq_s = 0.0
        while done < want:
            t0 = time.perf_counter()
            chunk = os.read(fd, min(_SEQ_CHUNK, want - done))
            seq_s += time.perf_counter() - t0
            if not chunk:
                break
            done += len(chunk)
            if reporter.should_stop():
                break
        if cold:
            _drop_page_cache(path)
        latencies = []
        chunk = min(_RANDOM_READ_SIZE, size)
        if chunk > 0:
            for _ in range(_RANDOM_READS):
                off = rng.randrange(0, size - chunk + 1)
                t = time.perf_counter()
                os.pread(fd, chunk, off)
                latencies.append((time.perf_counter() - t) * 1000.0)
                if len(latencies) % 20 == 0 and reporter.should_stop():
                    break
    finally:
        os.close(fd)
    return {
        "file": os.path.basename(path),
        "file_bytes": size,
        "sequential": {
            "bytes": done,
            "seconds": round(seq_s, 3),
            "mb_per_s": round(done / seq_s / 1e6, 1) if seq_s > 0 and done else None,
        },
        "random_64k_ms": stats_ms(latencies),
    }


def _measure_bands(handle, tiles_per_band: int, rng: random.Random,
                   progress: Callable[[float], None], reporter: _Reporter,
                   seen: Optional[set] = None) -> tuple[list[dict], Optional[BaseException]]:
    """Single-tile latency at each band; returns (bands, latched error).

    A tile that fails to render is counted in its band's ``errors`` and the
    phase goes on, as the parallel phase does: one bad tile raising out of
    here would lose every band of the slide (a one-slide run would then fail
    as noSlideMeasured). A tile refused only because the block-read queue was
    full is counted as ``skipped``. Only an error the reader latches
    (``handle.latched``) ends the slide — every later tile could only fail
    the same way — and is returned along with the bands measured up to it,
    the current one partial. ``seen`` collects the (handle, tile) keys
    rendered successfully, for the parallel phase's fresh-tile share.
    """
    bands = handle.band_levels()
    out = []
    total = len(BANDS) * tiles_per_band
    done = 0
    latched: Optional[BaseException] = None
    logged = False
    for band, _drop in BANDS:
        if latched is not None:
            break
        ref = bands[band]
        keys = []
        # Not ``seen``: that is the caller's run-wide set of rendered tiles.
        picked = set()
        for _ in range(tiles_per_band * 4):
            k = handle.random_tile(ref, rng)
            if k not in picked:
                picked.add(k)
                keys.append(k)
            if len(keys) >= tiles_per_band:
                break
        gen, enc, tot, sizes = [], [], [], []
        errors = skipped = 0
        for key in keys:
            if reporter.should_stop():
                break
            try:
                g, e, n = handle.render(key)
            except Exception as exc:
                if _render_busy(exc):
                    skipped += 1
                else:
                    errors += 1
                    if not logged:
                        # Once per slide: the result keeps only counts.
                        logged = True
                        _logger.warning("perf: a band tile render failed", exc_info=True)
                    if handle.latched(exc):
                        latched = exc
                        break
            else:
                gen.append(g)
                enc.append(e)
                tot.append(g + e)
                sizes.append(n)
                # Only a tile that came back warmed anything: one that failed
                # or was skipped (refused by a full queue) is still fresh to
                # the parallel phase, as its own worker loop counts it.
                if seen is not None:
                    seen.add((id(handle), key))
            done += 1
            progress(done / total)
        out.append({
            "band": band,
            "level": handle.level_number(ref),
            "generate_ms": stats_ms(gen),
            "encode_ms": stats_ms(enc),
            "total_ms": stats_ms(tot),
            "avg_bytes": int(sum(sizes) / len(sizes)) if sizes else None,
            "errors": errors,
            "skipped": skipped,
        })
    return out, latched


def _concurrency_steps(max_concurrency: int) -> list[int]:
    steps = []
    c = 1
    while c < max_concurrency:
        steps.append(c)
        c *= 2
    steps.append(max_concurrency)
    return steps


def _measure_scaling(handles: list, params: BenchParams, seed: int,
                     progress: Callable[[float], None], reporter: _Reporter,
                     results: list[dict], workers: list[threading.Thread],
                     clock: Optional[_CpuClock] = None, seen: Optional[set] = None) -> None:
    """Full-resolution tiles from the measured slides, rendered by N threads
    for ``step_seconds`` each, for N = 1, 2, 4, ... max_concurrency.

    Appends one entry per step to ``results`` and every worker thread it
    starts to ``workers``. Both lists belong to the caller so that they
    survive this raising part-way through a step (a ``Thread.start()``
    refused at the thread limit, a raising reporter or progress callback):
    the caller must still learn which threads it started. Whatever the exit,
    a step's threads are first told to stop and given the join grace; one
    still alive after that is stuck in a read (a network or failing disk),
    and the handles it renders from must stay open while it lives: closing
    an OpenSlide handle under a thread inside it is a use-after-free (see
    dzi._DZEntry).

    A reader that latches an error (``latched``) is dropped from the phase
    rather than asked, until the step ends, for tiles it can only fail; once
    no reader is left the phase stops, marking the step ``readers_failed``.

    SZI archives are left out when any other slide was measured: an SZI tile
    is a ~0.02 ms archive read with no generation, so mixing them in would
    make the latency percentiles bimodal and inflate the throughput with work
    that never loads the CPU. An SZI-only run still measures SZI reads.

    The steps are never cold, and each warms caches (OpenSlide's, the
    OME-TIFF segment cache, the page cache) for the next: a later step
    re-renders tiles an earlier one — or the band phase — already did. Each
    step therefore reports ``fresh_tile_pct``, the share of its renders
    whose tile this run had not rendered before (``seen``, shared with the
    band phase, holds the keys rendered so far). Drawing a fresh tile set
    per step instead would run out of distinct tiles on a small slide and
    change what is measured as the run goes on.
    """
    clock = clock or _CpuClock()
    seen = set() if seen is None else seen
    generated = [h for h in handles if not isinstance(h, _SziHandle)]
    refs = [(h, h.band_levels()["high"]) for h in (generated or handles)]
    steps = _concurrency_steps(params.max_concurrency)
    # Readers that latched an error; a latched handle stays broken, so the
    # set carries over from one step to the next.
    dead: set = set()
    for i, n in enumerate(steps):
        if reporter.should_stop():
            break
        stop = threading.Event()
        lock = threading.Lock()
        latencies: list[float] = []
        errors = [0]
        skipped = [0]
        keys: list = []

        def worker(worker_seed: int) -> None:
            rng = random.Random(worker_seed)
            local: list[float] = []
            rendered: list = []
            while not stop.is_set():
                live = [r for r in refs if r[0] not in dead] if dead else refs
                if not live:
                    break
                h, ref = live[rng.randrange(len(live))]
                try:
                    key = h.random_tile(ref, rng)
                    g, e, _ = h.render(key)
                    local.append(g + e)
                    rendered.append((id(h), key))
                except Exception as exc:
                    if _render_busy(exc):
                        # The block-read queue was full: not a reader fault.
                        with lock:
                            skipped[0] += 1
                        # A beat before the next try, not a spin on the lock.
                        stop.wait(0.05)
                        continue
                    with lock:
                        errors[0] += 1
                        first = errors[0] == 1
                        if h.latched(exc):
                            dead.add(h)
                    if first:
                        # Once per step: the result keeps only a count, and
                        # the page points the admin here for the cause.
                        _logger.warning("perf: a parallel tile render failed", exc_info=True)
            with lock:
                latencies.extend(local)
                keys.extend(rendered)

        threads: list[threading.Thread] = []
        cpu0 = clock.snap()
        t0 = time.perf_counter()
        try:
            for k in range(n):
                t = threading.Thread(target=worker, args=(seed * 1000 + n * 100 + k,), daemon=True)
                t.start()
                # Listed only once running: a thread whose start() raised
                # never ran, holds no handle and cannot be joined.
                threads.append(t)
                workers.append(t)
            end = time.monotonic() + params.step_seconds
            # Ends early when no worker is left, i.e. every reader latched.
            while time.monotonic() < end and any(t.is_alive() for t in threads):
                if reporter.should_stop():
                    break
                time.sleep(0.1)
        finally:
            # On every way out of the step — its time is up, a later start()
            # failed, should_stop() raised — the threads already running are
            # told to stop and waited for. Skipped on a raise, they would go
            # on rendering with no stop request while the caller, never told
            # they existed, closed the handles under them.
            stop.set()
            # One shared deadline for the whole step, not one per thread.
            join_deadline = time.monotonic() + _JOIN_GRACE_SECONDS
            for t in threads:
                t.join(timeout=max(0.0, join_deadline - time.monotonic()))
        stuck = [t for t in threads if t.is_alive()]
        elapsed = time.perf_counter() - t0
        busy, iowait = clock.split(cpu0, clock.snap())
        # Read under the lock: a stuck thread may still append on its way out.
        with lock:
            step_keys = list(keys)
        fresh = set(step_keys) - seen
        seen.update(step_keys)
        results.append({
            "concurrency": n,
            "tiles": len(latencies),
            "errors": errors[0],
            "skipped": skipped[0],
            "seconds": round(elapsed, 2),
            "tiles_per_s": round(len(latencies) / elapsed, 1) if elapsed > 0 else None,
            "latency_ms": stats_ms(latencies),
            "cpu_busy_pct": busy,
            "iowait_pct": iowait,
            # Renders of a tile new to this run (see the docstring); the rest
            # repeated one and found it at least partly cached.
            "fresh_tile_pct": round(100.0 * len(fresh) / len(step_keys), 1) if step_keys else None,
        })
        if stuck:
            # Their tiles never reached `latencies`; more steps would only
            # pile further threads onto the same stalled storage.
            _logger.warning("perf: %d tile thread(s) still busy after the join grace; stopping", len(stuck))
            results[-1]["stalled_threads"] = len(stuck)
            break
        if len(dead) == len(refs):
            # Every reader is latched: further steps could only count the
            # same failure again, faster.
            _logger.warning("perf: every slide reader failed; stopping the parallel phase")
            results[-1]["readers_failed"] = True
            break
        progress((i + 1) / len(steps))


# ---------------------------------------------------------------------------
# Summary / advice
# ---------------------------------------------------------------------------


def _iowait_cpus(iowait_pct: Optional[float], cores: int) -> float:
    """Host-wide iowait % as the number of CPUs' worth of time spent waiting
    on I/O — comparable across core counts, unlike the percentage (two
    threads blocked on a disk are 50 % of a 4-core host but 12.5 % of a
    16-core one)."""
    return (iowait_pct or 0.0) * cores / 100.0


def _tile_errors(scaling: list[dict]) -> int:
    """Failed renders of the parallel phase, over all its steps."""
    return sum(r.get("errors") or 0 for r in scaling)


def _band_errors(slides: list[dict]) -> int:
    """Failed single-tile renders of the band phase, over all slides."""
    return sum(b.get("errors") or 0 for s in slides for b in s.get("bands", []))


def summarise(slides: list[dict], scaling: list[dict], system: dict,
              workers: Optional[int] = None, cores: Optional[float] = None) -> tuple[dict, list[dict]]:
    """Headline numbers and advice. ``cores`` is the effective CPU count
    (effective_cpus(), the default); iowait, a host-wide percentage, is
    still converted with the host's count."""
    ok = [s for s in slides if not s.get("error")]
    summary: dict[str, Any] = {}
    # Every slide's measured phases count, including one that failed part-way
    # (its reader latched mid-bands): what it measured before is still real.
    high_p95 = [b["total_ms"].get("p95") for s in slides for b in s.get("bands", []) if b["band"] == "high" and b["total_ms"].get("count")]
    if high_p95:
        summary["tile_p95_ms"] = max(high_p95)
    seq = [s["disk"]["sequential"]["mb_per_s"] for s in slides if s.get("disk") and s["disk"]["sequential"].get("mb_per_s")]
    if seq:
        summary["seq_read_mb_s"] = min(seq)
    rnd = [s["disk"]["random_64k_ms"].get("p95") for s in slides if s.get("disk") and s["disk"]["random_64k_ms"].get("count")]
    if rnd:
        summary["random_read_p95_ms"] = max(rnd)
    opens = [s["open"]["first_ms"] for s in slides if s.get("open")]
    if opens:
        summary["open_first_ms"] = max(opens)
    rated = [r for r in scaling if r.get("tiles_per_s")]
    if rated:
        peak = max(rated, key=lambda r: r["tiles_per_s"])
        single = next((r for r in rated if r["concurrency"] == 1), None)
        summary["peak_tiles_per_s"] = peak["tiles_per_s"]
        summary["peak_concurrency"] = peak["concurrency"]
        if single:
            summary["single_tiles_per_s"] = single["tiles_per_s"]

    advice: list[dict] = []
    # Failed parallel renders first: the rates only count the tiles that came
    # back, so a step that lost tiles — or all of them, when it drops out of
    # `rated` — measured a failing reader, not the server's capacity. Being a
    # finding of its own, it also rules out "no bottleneck" below.
    tile_errors = _tile_errors(scaling)
    if tile_errors:
        attempts = tile_errors + sum(r.get("tiles") or 0 for r in scaling)
        advice.append({"code": "tileErrors", "params": {"errors": tile_errors, "attempts": attempts}})
    # Failed single-tile renders have no finding of their own (the per-band
    # ``errors`` show them), but they too rule out "no bottleneck".
    band_errors = _band_errors(slides)
    if cores is None:
        cores = effective_cpus()
    host_cores = psutil.cpu_count(logical=True) or 1
    disk_slow = (
        (summary.get("seq_read_mb_s") is not None and summary["seq_read_mb_s"] < 100)
        or (summary.get("random_read_p95_ms") is not None and summary["random_read_p95_ms"] > 20)
    )
    if rated:
        peak_rate = summary["peak_tiles_per_s"]
        peak = next(r for r in rated if r["tiles_per_s"] == peak_rate)
        single_rate = summary.get("single_tiles_per_s") or 0
        # Scaling can only be judged when more than one thread was measured.
        parallel_tested = any(r["concurrency"] > 1 for r in rated)
        if parallel_tested:
            knee = next(r for r in rated if r["tiles_per_s"] >= 0.9 * peak_rate)
            advice.append({"code": "saturation", "params": {"concurrency": knee["concurrency"], "tilesPerSec": peak_rate}})
        if peak["cpu_busy_pct"] >= 85:
            advice.append({"code": "cpuBound", "params": {"busy": peak["cpu_busy_pct"]}})
        elif (
            parallel_tested and cores >= 2 and single_rate
            and peak_rate < 1.5 * single_rate
            and peak["cpu_busy_pct"] < 60
            # Fewer than a fifth of the peak step's threads' worth of CPUs
            # waited on I/O, and the disk probes look healthy ...
            and _iowait_cpus(peak["iowait_pct"], host_cores) < 0.2 * peak["concurrency"]
            and not disk_slow
            # ... and there is room for more worker processes at all.
            and (workers is None or workers < cores)
        ):
            # Parallel requests barely beat one, yet the CPU sits idle and the
            # disk isn't the wait: the ceiling is inside the process (the
            # GIL), which more worker processes get around.
            advice.append({"code": "threadBound", "params": {"busy": peak["cpu_busy_pct"]}})
    if summary.get("seq_read_mb_s") is not None and summary["seq_read_mb_s"] < 100:
        advice.append({"code": "diskSlow", "params": {"mbps": summary["seq_read_mb_s"]}})
    if summary.get("random_read_p95_ms") is not None and summary["random_read_p95_ms"] > 20:
        advice.append({"code": "diskSeekSlow", "params": {"p95": summary["random_read_p95_ms"]}})
    iowait_avg = (system.get("iowait_pct") or {}).get("avg")
    # 20 % of the host, or on average at least one whole CPU waiting on I/O
    # (the percentage alone understates the wait on a many-core host).
    if iowait_avg is not None and (iowait_avg > 20 or _iowait_cpus(iowait_avg, host_cores) >= 1.0):
        advice.append({"code": "ioWaitHigh", "params": {"iowait": iowait_avg}})
    if summary.get("tile_p95_ms") is not None and summary["tile_p95_ms"] > 500:
        advice.append({"code": "tileSlow", "params": {"p95": summary["tile_p95_ms"]}})
    # "No bottleneck" needs something to have been measured.
    if (ok or rated) and not band_errors and not [a for a in advice if a["code"] != "saturation"]:
        advice.append({"code": "noBottleneck", "params": {}})
    return summary, advice


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_run(run_id: int, targets: list[Target], params: BenchParams) -> None:
    """Run the benchmark and record the outcome on the ``perf_runs`` row.

    Never raises: any failure lands in the row as state 'failed'. Callable
    synchronously (tests) or as a thread target (the router).
    """
    started = time.monotonic()
    wall_started = time.time()
    reporter = _Reporter(run_id, started + MAX_RUN_SECONDS)
    clock = _CpuClock()
    sampler = _SystemSampler(clock)
    seed = run_id
    rng = random.Random(seed)
    slides: list[dict] = []
    handles: list = []
    # The handles the parallel phase may use: all but those whose reader
    # latched an error in the band phase (they could only fail there).
    scaling_handles: list = []
    scaling: list[dict] = []
    workers: list[threading.Thread] = []
    # (handle, tile) keys rendered so far, for each step's fresh-tile share.
    seen: set = set()
    # This run's block-read slot and queue, shared by all of its handles and
    # never by another run's (see _new_bench_lane).
    lane = _new_bench_lane()
    error = ""
    stuck: list[threading.Thread] = []
    system: dict = {}
    wall_work_end = wall_started
    n = len(targets)
    try:
        sampler.start()
        for i, target in enumerate(targets):
            if reporter.should_stop():
                break
            base = 60.0 * i / n
            span = 60.0 / n
            where = {"slide": target.title, "slide_index": i + 1, "slide_count": n}
            entry: dict[str, Any] = {
                "id": target.slide_id, "title": target.title, "format": target.fmt,
                "vendor": target.vendor, "width": target.width, "height": target.height,
                "file_bytes": target.file_size,
            }
            slides.append(entry)
            try:
                reporter.update("open", base, **where)
                handle, entry["open"] = _measure_open(target, params.cold, lane)
            except Exception as exc:
                _logger.warning("perf run %s: slide %s failed to open", run_id, target.slide_id, exc_info=True)
                entry["error"] = f"open failed: {exc}"[:300]
                continue
            handles.append(handle)
            scaling_handles.append(handle)
            entry["vendor"] = handle.vendor or target.vendor
            try:
                reporter.update("disk", base + span * 0.1, **where)
                entry["disk"] = _measure_disk(target, params.cold, rng, reporter)
                if params.cold:
                    _drop_page_cache(target.path)
                else:
                    # Warm mode warms nothing on purpose: the band latencies
                    # follow the disk phase, whose sequential read covers only
                    # the first _SEQ_READ_BYTES. A larger slide's tiles are
                    # therefore partly warm at best (what the read covered,
                    # plus whatever the page cache already held), and the
                    # fraction says how much of the file that read was.
                    warm = entry["disk"]["sequential"]["bytes"]
                    entry["warm_bytes"] = warm
                    entry["warm_fraction"] = round(warm / target.file_size, 3) if target.file_size else None
                entry["bands"], latched = _measure_bands(
                    handle, params.tiles_per_band, rng,
                    lambda f, b=base, s=span, w=where: reporter.update("tiles", b + s * (0.25 + 0.75 * f), **w),
                    reporter, seen,
                )
                if latched is not None:
                    # The bands measured until then are kept; the reader is
                    # broken for the rest of the run.
                    entry["error"] = f"measurement failed: {latched}"[:300]
                    entry["reader_latched"] = True
                    scaling_handles.remove(handle)
            except Exception as exc:
                _logger.warning("perf run %s: slide %s measurement failed", run_id, target.slide_id, exc_info=True)
                entry["error"] = f"measurement failed: {exc}"[:300]
        if scaling_handles and not reporter.should_stop():
            _measure_scaling(
                scaling_handles, params, seed,
                lambda f: reporter.update("scaling", 60.0 + 40.0 * f),
                reporter, scaling, workers, clock=clock, seen=seen,
            )
    except Exception as exc:  # noqa: BLE001 — recorded on the row, never raised
        _logger.exception("perf run %s failed", run_id)
        error = str(exc)[:500] or exc.__class__.__name__
    finally:
        # Where the measurement ended: the window job_overlap checks.
        wall_work_end = time.time()
        # Read off the threads themselves rather than a return value, which
        # never arrives when _measure_scaling raises: by now each has been
        # told to stop and given the join grace, so one still alive is inside
        # render() and its handles must outlive it.
        stuck = [t for t in workers if t.is_alive()]
        if stuck:
            # Hand the close to a thread that first waits the stuck readers
            # out, so the run can still finish and record its result.
            try:
                threading.Thread(
                    target=_close_after, args=(run_id, stuck, list(handles)),
                    name=f"perf-run-{run_id}-close", daemon=True,
                ).start()
            except Exception:
                # No thread for the closer either (the thread limit may be
                # what failed the run). Leave the handles open rather than
                # close them under a live reader: the stuck threads hold the
                # last references, and the readers release their files when
                # collected, after those threads return.
                _logger.warning("perf run %s: no closer thread; leaving the slide handles to be collected", run_id, exc_info=True)
        else:
            _close_handles(run_id, handles)
        try:
            system = sampler.stop()
        except Exception:
            # Never let the sampler cost the run its final write.
            _logger.warning("perf run %s: stopping the system sampler failed", run_id, exc_info=True)

    elapsed = time.monotonic() - started
    try:
        state, error, result = _outcome(
            params, slides, scaling, system, error, reporter, bool(stuck),
            elapsed, clock.cpus, (wall_started, wall_work_end),
        )
    except Exception as exc:  # noqa: BLE001 — the final write must still happen
        # Whatever went wrong in the bookkeeping, the row must leave
        # 'running': a skipped write held the single-run slot until the
        # orphan age limit. What was measured is kept as it stands.
        _logger.exception("perf run %s: summarising the result failed", run_id)
        state = "failed"
        error = error or str(exc)[:500] or exc.__class__.__name__
        result = {
            "schema": SCHEMA, "scope": RESULT_SCOPE, "params": asdict(params),
            "slides": slides, "scaling": scaling, "system": system,
            # Only the caveat: the counts come from the same bookkeeping
            # that just failed.
            "summary": {"truncated": True}, "advice": [], "truncated": True, "duration_s": round(elapsed, 1),
        }
    try:
        encoded = json.dumps(result, ensure_ascii=False)
        summary_json = json.dumps(result.get("summary"), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — as above
        _logger.exception("perf run %s: encoding the result failed", run_id)
        state, error = "failed", error or str(exc)[:500] or exc.__class__.__name__
        encoded, summary_json = "", "null"
    values = dict(
        state=state,
        finished_at=datetime.now(timezone.utc),
        result_json=encoded,
        summary_json=summary_json,
        progress_json=json.dumps({"phase": "done", "percent": 100}),
        error=error,
    )
    # A write lost to a briefly locked database would leave the row
    # 'running' (and the single-run slot taken) until the orphan age limit,
    # so it is retried a few times before giving up.
    for attempt in range(3):
        db = SessionLocal()
        try:
            db.execute(update(PerfRun).where(PerfRun.id == run_id, PerfRun.state == "running").values(**values))
            db.commit()
            break
        except Exception:
            db.rollback()
            _logger.exception("perf run %s: storing the result failed (attempt %d)", run_id, attempt + 1)
        finally:
            db.close()
        time.sleep(1.0)


def _outcome(params: BenchParams, slides: list[dict], scaling: list[dict], system: dict,
             error: str, reporter: _Reporter, stuck: bool, elapsed: float,
             cpus: float, window: tuple[float, float]) -> tuple[str, str, dict]:
    """(state, error, result) of a run whose measurement has ended."""
    # From what stopped the measurement (should_stop), not from flags read
    # afterwards: a cancel that arrived once the work was done would
    # relabel a complete run 'cancelled', and timing the deadline from after the cleanup (the join
    # grace, the sampler's stop) could call a run that finished in time
    # timed out.
    cancelled = reporter.stopped_by == "cancel"
    timed_out = reporter.stopped_by == "deadline"
    if error:
        state = "failed"
    elif cancelled:
        state = "cancelled"
    elif slides and all(s.get("error") for s in slides) and not any(
        b["total_ms"].get("count") for s in slides for b in s.get("bands", [])
    ):
        # Nothing measured a tile. A slide whose reader latched part-way
        # through its bands carries an error but still measured some.
        state = "failed"
        error = RUN_ERROR_NO_SLIDE
    elif scaling and not any(s["tiles"] for s in scaling) and any(s["errors"] for s in scaling):
        # Every parallel render failed: the phase the throughput figures come
        # from measured nothing, however well the per-slide phases went.
        state = "failed"
        error = RUN_ERROR_PARALLEL_FAILED
    else:
        state = "succeeded"
    # The parallel phase also ends before its last step when its threads
    # stall in a read or no reader is left that can render; a slide's band
    # phase ends early when its reader latches an error.
    cut_short = any(s.get("stalled_threads") or s.get("readers_failed") for s in scaling) or any(
        s.get("reader_latched") for s in slides
    )
    truncated = bool(timed_out or cancelled or stuck or cut_short)
    env = conditions_snapshot()
    summary, advice = summarise(slides, scaling, system, workers=env.get("workers"), cores=cpus)
    # A backup / restore / slide transfer refuses to start during a run
    # (jobs.try_claim_running), but one that won the race, or one started
    # where that check could not read the database, competed for the same
    # disk and CPU: the page warns that the numbers are suspect.
    summary["job_overlap"] = job_overlap(*window)
    # The caveats of the run itself, in the summary because the history list
    # and the comparison read nothing else: a run that ended early, or lost
    # renders (left out of every figure above), is no like-for-like baseline
    # however well its numbers look. The per-band / per-step counts stay in
    # the full result.
    summary["truncated"] = truncated
    summary["band_errors"] = _band_errors(slides)
    summary["tile_errors"] = _tile_errors(scaling)
    if state != "succeeded" or truncated:
        # "No bottleneck" is a statement about a complete run.
        advice = [a for a in advice if a["code"] != "noBottleneck"]
    result = {
        "schema": SCHEMA,
        "scope": RESULT_SCOPE,
        "environment": env,
        "params": asdict(params),
        "slides": slides,
        "scaling": scaling,
        "system": system,
        "summary": summary,
        "advice": advice,
        "truncated": truncated,
        "duration_s": round(elapsed, 1),
    }
    return state, error, result


def _close_handles(run_id: int, handles: list) -> None:
    for h in handles:
        try:
            h.close()
        except Exception:
            _logger.warning("perf run %s: closing a slide handle failed", run_id, exc_info=True)


def _close_after(run_id: int, threads: list[threading.Thread], handles: list) -> None:
    for t in threads:
        t.join()
    _close_handles(run_id, handles)


def _safe(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except Exception:
        _logger.warning("perf: environment snapshot failed", exc_info=True)
        return {}


def start_in_background(run_id: int, targets: list[Target], params: BenchParams) -> threading.Thread:
    thread = threading.Thread(
        target=execute_run, args=(run_id, targets, params), name=f"perf-run-{run_id}", daemon=True,
    )
    try:
        thread.start()
    except Exception:
        # The row already claims the single-run slot; release it rather than
        # leave it 'running' behind a live pid until the orphan age limit.
        db = SessionLocal()
        try:
            db.execute(update(PerfRun).where(PerfRun.id == run_id, PerfRun.state == "running").values(
                state="failed", error=RUN_ERROR_INTERRUPTED, finished_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
        raise
    return thread
