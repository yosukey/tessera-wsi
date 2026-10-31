"""Exports of the admin Performance page's history.

A measurement is only useful later — to compare against the next one, or to
hand to whoever tunes the server — when the numbers travel with the
conditions they were taken under. Two shapes:

* **JSON** — every stored field of each run (params, full result, the
  server / browser / network conditions recorded with it), wrapped in a
  self-describing envelope with the unit conventions.
* **CSV** — one row per run: its settings, its headline numbers and the
  conditions flattened into columns, so runs line up in a spreadsheet.

A run stored without a condition (one the browser did not report, or a
hand-edited row) leaves that field empty.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .csv_export import csv_safe

FORMAT = "tessera-perf-export"
FORMAT_VERSION = 1

# How the numbers in a run are expressed. Shipped inside every JSON export so
# a reader needs nothing else to interpret them.
UNITS = {
    "*_ms": "milliseconds",
    "*_s / seconds / duration_s / wall_s": "seconds",
    "*_mb_s / mb_per_s": "megabytes (10^6 bytes) per second",
    "mbps / *_mbps": "megabits (10^6 bits) per second",
    "*_pct": "percent (0-100)",
    "*_fraction": "fraction (0-1)",
    "scope": "below_http: a server run's latencies exclude the HTTP request around a tile "
             "(slide lookup, stat, response); see the browser test for the full path",
    "*_bytes / bytes": "bytes",
    "*_mb": "mebibytes (2^20 bytes)",
    "tiles_per_s": "tiles per second",
    "stats objects": "{count, mean, min, p50, p90, p95, p99, max} of millisecond samples; "
                     "percentiles linearly interpolated",
    "timestamps": "ISO 8601; created_at / finished_at in the server's local offset, "
                  "client.started_at / finished_at from the measuring browser's clock",
    "tiles.samples": "a browser test's tile requests, one row per request in issue order; "
                     "start_ms counts from the start of the tile phase, which follows the "
                     "round-trip and download probes",
}


def finite(value: Any) -> Any:
    """``value`` with every NaN / ±Infinity replaced by None.

    Python's json writes them as bare NaN / Infinity, which is not JSON: a run
    stored with one (a hand-edited or script-written row) would otherwise make
    every response and export that carries it fail or be unreadable.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def export_envelope(runs: list[dict], exported_by: str, source: dict) -> dict:
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "exported_by": exported_by,
        "source": source,
        "units": UNITS,
        "runs": runs,
    }


def dumps(envelope: dict) -> str:
    return json.dumps(finite(envelope), ensure_ascii=False, indent=2, allow_nan=False)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _get(d: Any, *path: Any) -> Any:
    for key in path:
        if isinstance(d, dict):
            d = d.get(key)
        elif isinstance(d, list) and isinstance(key, int) and -len(d) <= key < len(d):
            d = d[key]
        else:
            return None
    return d


def _result(run: dict) -> dict:
    res = run.get("result")
    return res if isinstance(res, dict) else {}


def _env(run: dict) -> dict:
    """Server-side conditions: measured with a server run, stamped on save for
    a browser run."""
    res = _result(run)
    env = res.get("environment") if run.get("kind") == "server" else _get(res, "server", "environment")
    return env if isinstance(env, dict) else {}


def _server_only(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
    return lambda run: fn(run) if run.get("kind") == "server" else None


def _client_only(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
    return lambda run: fn(run) if run.get("kind") == "client" else None


def _defang_parts(text: str) -> str:
    """``text`` with every ';'-separated part defanged on its own: a
    spreadsheet that splits cells on ';' would otherwise see a later part
    starting with "=" as a formula. Split on the joined text, not per value,
    so a value that itself holds ';' (a slide title "A;=B") cannot carry a
    formula past it."""
    return ";".join(str(csv_safe(part)) for part in text.split(";"))


def _join(values: Iterable[Any]) -> str:
    return _defang_parts(";".join(str(v) for v in values if v is not None))


def _slides(run: dict) -> list[dict]:
    slides = _get(run, "params", "slides")
    return [s for s in slides if isinstance(s, dict)] if isinstance(slides, list) else []


def _disk(env: dict) -> Optional[str]:
    disk = env.get("data_disk")
    if not isinstance(disk, dict):
        return None
    kind = {True: "HDD", False: "SSD"}.get(disk.get("rotational"))
    return " ".join(x for x in (kind, disk.get("fstype")) if x) or None


def _server_slides(run: dict, key: str) -> Optional[str]:
    """A per-slide value of a server run's measured slides, ';'-joined."""
    slides = _result(run).get("slides")
    if not isinstance(slides, list):
        return None
    return _join(s.get(key) for s in slides if isinstance(s, dict)) or None


def _wxh(d: Any) -> Optional[str]:
    if isinstance(d, dict) and d.get("width") is not None and d.get("height") is not None:
        return f"{d['width']}x{d['height']}"
    return None


def _protocols(run: dict) -> Optional[str]:
    seen = _get(run, "result", "client", "http_protocols")
    if not isinstance(seen, dict) or not seen:
        return None
    ordered = sorted(seen.items(), key=lambda kv: -int(kv[1] or 0))
    return _defang_parts(";".join(f"{k}={v}" for k, v in ordered))


def _summary(key: str) -> Callable[[dict], Any]:
    return lambda run: _get(_result(run), "summary", key)


def _sys(*path: Any) -> Callable[[dict], Any]:
    return _server_only(lambda run: _get(_result(run), "system", *path))


def _cl(*path: Any) -> Callable[[dict], Any]:
    return _client_only(lambda run: _get(_result(run), *path))


def _e(*path: Any) -> Callable[[dict], Any]:
    return lambda run: _get(_env(run), *path)


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


COLUMNS: list[tuple[str, Callable[[dict], Any]]] = [
    # --- the run ---
    ("run_id", lambda r: r.get("id")),
    ("kind", lambda r: r.get("kind")),
    ("state", lambda r: r.get("state")),
    ("created_at", lambda r: r.get("created_at")),
    ("finished_at", lambda r: r.get("finished_at")),
    ("actor", lambda r: r.get("actor")),
    ("error", lambda r: r.get("error")),
    ("truncated", _server_only(lambda r: _result(r).get("truncated"))),
    ("duration_s", lambda r: _result(r).get("duration_s") if r.get("kind") == "server" else _get(_result(r), "client", "duration_s")),
    # --- what was measured ---
    ("slide_ids", lambda r: _join(s.get("id") for s in _slides(r)) if r.get("kind") == "server" else _get(r, "params", "slide_id")),
    ("slide_titles", lambda r: _join(s.get("title") for s in _slides(r)) if r.get("kind") == "server" else _get(r, "params", "slide_title")),
    ("server.cold", _server_only(lambda r: _get(r, "params", "cold"))),
    ("server.tiles_per_band", _server_only(lambda r: _get(r, "params", "tiles_per_band"))),
    ("server.max_concurrency", _server_only(lambda r: _get(r, "params", "max_concurrency"))),
    ("server.step_seconds", _server_only(lambda r: _get(r, "params", "step_seconds"))),
    ("client.mode", _client_only(lambda r: _get(r, "params", "mode"))),
    ("client.tiles", _client_only(lambda r: _get(r, "params", "tiles"))),
    ("client.concurrency", _client_only(lambda r: _get(r, "params", "concurrency"))),
    ("client.seed", _client_only(lambda r: _get(r, "params", "seed"))),
    # A browser run measures one slide; a server run up to three (';'-joined).
    ("slide.width", lambda r: _get(_result(r), "slide", "width") if r.get("kind") == "client" else _server_slides(r, "width")),
    ("slide.height", lambda r: _get(_result(r), "slide", "height") if r.get("kind") == "client" else _server_slides(r, "height")),
    ("slide.format", _server_only(lambda r: _server_slides(r, "format"))),
    ("slide.vendor", _server_only(lambda r: _server_slides(r, "vendor"))),
    ("slide.file_bytes", _server_only(lambda r: _server_slides(r, "file_bytes"))),
    ("slide.tile_size", _cl("slide", "tile_size")),
    ("slide.tile_format", _cl("slide", "tile_format")),
    # --- headline numbers ---
    ("peak_tiles_per_s", _summary("peak_tiles_per_s")),
    ("peak_concurrency", _summary("peak_concurrency")),
    ("single_tiles_per_s", _summary("single_tiles_per_s")),
    ("tile_p95_ms", _summary("tile_p95_ms")),
    ("seq_read_mb_s", _summary("seq_read_mb_s")),
    ("random_read_p95_ms", _summary("random_read_p95_ms")),
    ("open_first_ms", _summary("open_first_ms")),
    ("rtt_ms", _summary("rtt_ms")),
    ("mbps", _summary("mbps")),
    ("tiles_per_s", _summary("tiles_per_s")),
    ("tile_ttfb_p95_ms", _summary("tile_ttfb_p95_ms")),
    ("cache_hit_pct", _summary("cache_hit_pct")),
    ("client.rtt_p95_ms", _cl("network", "rtt_ms", "p95")),
    ("client.tiles_requested", _cl("tiles", "requested")),
    ("client.tiles_ok", _cl("tiles", "ok")),
    ("client.tiles_errors", _cl("tiles", "errors")),
    ("client.tile_ttfb_p50_ms", _cl("tiles", "ttfb_ms", "p50")),
    ("client.tile_total_p50_ms", _cl("tiles", "total_ms", "p50")),
    ("client.tile_total_p95_ms", _cl("tiles", "total_ms", "p95")),
    ("client.tiles_wall_s", _cl("tiles", "wall_s")),
    ("client.tiles_mb_per_s", _cl("tiles", "mb_per_s")),
    # Failed tile requests as the browser's summary counts them (empty when
    # the summary does not carry them).
    ("client.tile_errors", _client_only(_summary("tile_errors"))),
    # A server run's failed renders, from its summary (perf_bench._outcome):
    # single-tile (band phase) ones and parallel-phase ones. "truncated" above
    # says whether it ended early. Empty when the summary does not carry
    # them (the per-band / per-step counts are in the JSON).
    ("server.band_errors", _server_only(_summary("band_errors"))),
    ("server.tile_errors", _server_only(_summary("tile_errors"))),
    # Whether something else loaded the server while the run measured it,
    # recorded by the server (a browser run: a server benchmark or a job; a
    # server run: a job). Empty when the summary does not carry it.
    ("server_overlap", _client_only(_summary("server_overlap"))),
    ("job_overlap", _summary("job_overlap")),
    # --- the server while it ran (server test) ---
    ("system.cpu_busy_avg_pct", _sys("cpu_busy_pct", "avg")),
    ("system.cpu_busy_max_pct", _sys("cpu_busy_pct", "max")),
    # host, cgroup or process: what the busy percentages are of (perf_bench._CpuClock).
    ("system.cpu_basis", _sys("cpu_basis")),
    ("system.iowait_avg_pct", _sys("iowait_pct", "avg")),
    ("system.iowait_max_pct", _sys("iowait_pct", "max")),
    ("system.mem_available_min_mb", _sys("mem_available_mb_min")),
    ("system.load_1m_start", _sys("load_avg_start", 0)),
    ("system.load_1m_end", _sys("load_avg_end", 0)),
    # --- server conditions ---
    ("server.app_version", _e("app_version")),
    ("server.hostname", _e("hostname")),
    ("server.os", _e("os")),
    ("server.kernel", _e("kernel")),
    ("server.machine", _e("machine")),
    ("server.virtualization", _e("virtualization")),
    ("server.cpu_model", _e("cpu_model")),
    ("server.cpu_physical", _e("cpu_physical")),
    ("server.cpu_logical", _e("cpu_logical")),
    ("server.cpu_freq_mhz", _e("cpu_freq_mhz")),
    ("server.cgroup_cpu_limit", _e("cgroup", "cpu_limit")),
    ("server.cpu_effective", _e("cpu_effective")),
    ("server.mem_total_mb", _e("mem_total_mb")),
    ("server.cgroup_memory_max_mb", _e("cgroup", "memory_max_mb")),
    ("server.workers", _e("workers")),
    ("server.data_disk", lambda r: _disk(_env(r))),
    ("server.dzi_tile_size", _e("tile_size")),
    ("server.dzi_overlap", _e("overlap")),
    ("server.jpeg_quality", _e("jpeg_quality")),
    ("server.dzi_cache_size", _e("dzi_cache_size")),
    ("server.dzi_cache_max_bytes", _e("dzi_cache_max_bytes")),
    ("server.ome_tile_cache_bytes", _e("ome_tile_cache_bytes")),
    ("server.nginx_cache_max_size", _e("nginx_cache", "max_size")),
    ("server.nginx_cache_used_bytes", _e("nginx_cache", "used_bytes")),
    ("server.nginx_cache_file_count", _e("nginx_cache", "file_count")),
    ("server.openslide", _e("openslide")),
    ("server.python", _e("python")),
    ("server.timezone", _e("timezone")),
    ("viewer.immediate_render", _e("viewer_rendering", "immediate_render")),
    ("viewer.max_image_cache_count", _e("viewer_rendering", "max_image_cache_count")),
    ("viewer.max_zoom_pixel_ratio", _e("viewer_rendering", "max_zoom_pixel_ratio")),
    # --- browser / device / network conditions (browser test) ---
    ("client.started_at", _cl("client", "started_at")),
    ("client.finished_at", _cl("client", "finished_at")),
    # Browser-reported text (every text cell is ';'-part defanged in _cell).
    ("client.user_agent", _cl("client", "user_agent")),
    ("client.platform", _cl("client", "platform")),
    ("client.mobile", _cl("client", "mobile")),
    # iPadOS Safari reports a Mac user agent and platform; touch points are
    # what tell an iPad from a Mac.
    ("client.max_touch_points", _cl("client", "max_touch_points")),
    ("client.language", _cl("client", "language")),
    ("client.device_pixel_ratio", _cl("client", "device_pixel_ratio")),
    ("client.viewport", _client_only(lambda r: _wxh(_get(_result(r), "client", "viewport")))),
    ("client.screen", _client_only(lambda r: _wxh(_get(_result(r), "client", "screen")))),
    ("client.hardware_concurrency", _cl("client", "hardware_concurrency")),
    ("client.device_memory_gb", _cl("client", "device_memory_gb")),
    ("client.timezone", _cl("client", "timezone")),
    ("client.connection_type", _cl("client", "connection_type")),
    ("client.effective_type", _cl("client", "effective_type")),
    ("client.downlink_mbps", _cl("client", "downlink_mbps")),
    ("client.connection_rtt_ms", _cl("client", "connection_rtt_ms")),
    ("client.save_data", _cl("client", "save_data")),
    ("client.page_origin", _cl("client", "page_origin")),
    ("client.secure_context", _cl("client", "secure_context")),
    ("client.http_protocols", _client_only(_protocols)),
    # From the browser's summary, else from the "client" block.
    ("client.hidden_during_run", _client_only(lambda r: _first(
        _get(_result(r), "summary", "hidden_during_run"), _get(_result(r), "client", "hidden_during_run"),
    ))),
]

CSV_HEADER = [name for name, _ in COLUMNS]


def _cell(v: Any) -> Any:
    """One CSV cell. Every text cell has each ';'-part defanged
    (_defang_parts), not just the ones expected to hold free text: a browser
    run is stored as the JSON its page posted, so any field of it — a
    timestamp, a tile format, a headline number — can arrive as a string
    such as "x;=1", and csv_response only looks at a cell's start. A benign
    value comes out unchanged (no part of it starts with a formula
    character)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        return _defang_parts(v)
    return v


def csv_row(run: dict) -> list:
    out = []
    for _, fn in COLUMNS:
        try:
            out.append(_cell(fn(run)))
        except Exception:
            # One malformed stored result must not abort the whole download.
            out.append(None)
    return out
