// Admin Performance page API client (/api/admin/perf/*). See
// services/app/wsi_app/routers/admin_perf.py and perf_bench.py for the
// server side of every shape below.
import { UnauthorizedError, adminFetch, adminGet, adminSend, authHeaders, downloadAttachment, throwApi } from './shared'

// Millisecond sample summary (perf_bench.stats_ms / perfStats on the client).
export type PerfStats = {
  count: number
  mean?: number
  min?: number
  p50?: number
  p90?: number
  p95?: number
  p99?: number
  max?: number
}

export type PerfRunState = 'running' | 'succeeded' | 'failed' | 'cancelled'
export type PerfRunKind = 'server' | 'client'

export type PerfProgress = {
  phase: 'starting' | 'open' | 'disk' | 'tiles' | 'scaling' | 'done'
  percent: number
  slide?: string
  slide_index?: number
  slide_count?: number
}

// Headline numbers the history list shows without loading the full result.
export type PerfSummary = {
  // server
  peak_tiles_per_s?: number
  peak_concurrency?: number
  single_tiles_per_s?: number
  tile_p95_ms?: number
  seq_read_mb_s?: number
  random_read_p95_ms?: number
  open_first_ms?: number
  // client
  rtt_ms?: number | null
  mbps?: number | null
  tiles_per_s?: number | null
  tile_ttfb_p95_ms?: number | null
  cache_hit_pct?: number | null
  // What makes a browser run's numbers questionable, absent when not
  // recorded (unknown, not "clean"): tile requests that
  // failed (left out of the figures above), a tab sent to the background
  // (throttled timers), and — stamped by the server — a server benchmark or
  // a background job (backup, restore, transfer) running while the browser
  // measured, sharing the machine (null: the server could not tell).
  // job_overlap is stamped on server runs too: a job that won the race
  // against the benchmark's start check ran beside it.
  tile_errors?: number
  hidden_during_run?: boolean
  server_overlap?: boolean | null
  job_overlap?: boolean | null
  // Server runs (may be absent): the run ended early (cancelled,
  // timed out, threads stalled, a reader failed for good), and its failed
  // single-tile (band phase) renders. tile_errors above is, for a server
  // run, the parallel phase's failed renders. Each is left out of the
  // figures, so such a run is no clean baseline.
  truncated?: boolean
  band_errors?: number
  // Browser runs: how they were timed. Present = the download timed from the
  // body alone, and the tile times from Resource Timing (the request's own
  // timeline) for every tile that succeeded (2) or not for all of them (1:
  // some or all kept fetch() around the request, which includes the wait for
  // a browser connection). Absent = an unmarked run, both timed from fetch()
  // around each request (the download speed including the round trip, tile
  // times the connection wait). Two runs timed differently are not like for
  // like (lib/admin/perfClientTest.ts, CLIENT_TIMING_RESOURCE).
  timing?: number
}

export type PerfRunSummary = {
  id: number
  kind: PerfRunKind
  state: PerfRunState
  created_at: string
  finished_at: string
  actor: string
  params: Record<string, any>
  progress: PerfProgress | null
  cancel_requested: boolean
  error: string | null
  summary: PerfSummary | null
}

export type PerfBand = {
  band: 'high' | 'mid' | 'low'
  level: number
  generate_ms: PerfStats
  encode_ms: PerfStats
  total_ms: PerfStats
  avg_bytes: number | null
  // Renders that failed, and renders refused only because the benchmark's
  // block-read queue was full (not a reader failure). May be absent.
  errors?: number
  skipped?: number
}

export type PerfSlideResult = {
  id: number
  title: string
  format: 'wsi' | 'szi'
  vendor: string
  width: number
  height: number
  file_bytes: number
  error?: string
  // The reader failed for good part-way through the bands: those measured
  // until then are kept (`error` says why), and the slide was left out of
  // the parallel phase.
  reader_latched?: boolean
  // Warm mode: bytes of the file the sequential read covered before the
  // band phase, and that as a share of the file (< 1: only partly warm).
  warm_bytes?: number
  warm_fraction?: number | null
  open?: { first_ms: number; repeat_ms: number }
  disk?: {
    file: string
    file_bytes: number
    sequential: { bytes: number; seconds: number; mb_per_s: number | null }
    random_64k_ms: PerfStats
  }
  bands?: PerfBand[]
}

export type PerfScalingStep = {
  concurrency: number
  tiles: number
  errors: number
  seconds: number
  tiles_per_s: number | null
  latency_ms: PerfStats
  cpu_busy_pct: number
  iowait_pct: number
  // May be absent. skipped: renders refused because the block-read
  // queue was full. fresh_tile_pct: share of the step's renders whose tile
  // this run had not rendered before (low = mostly warm caches).
  skipped?: number
  fresh_tile_pct?: number | null
  // The step ended early: threads stuck in a read, or no reader left.
  stalled_threads?: number
  readers_failed?: boolean
}

export type PerfAdvice = { code: string; params: Record<string, string | number> }

export type PerfServerResult = {
  schema: number
  // What the latencies include: "below_http" = reader, generator and
  // encoder, not the request around them (may be absent).
  scope?: string
  environment: PerfServerEnvironment
  params: Record<string, any>
  slides: PerfSlideResult[]
  scaling: PerfScalingStep[]
  system: {
    cpu_busy_pct: { avg: number | null; max: number | null }
    iowait_pct: { avg: number | null; max: number | null }
    mem_available_mb_min: number | null
    // 1 / 5 / 15-minute load when the run began (may be absent).
    load_avg_start?: number[] | null
    load_avg_end: number[] | null
    samples: number
    // Where "CPU busy" was read from (perf_bench._CpuClock) and the CPUs it
    // is a share of; absent = the host's.
    cpu_basis?: 'host' | 'cgroup' | 'process'
    cpu_effective?: number
  }
  summary: PerfSummary
  advice: PerfAdvice[]
  truncated: boolean
  duration_s: number
}

export type PerfClientBand = {
  band: 'high' | 'mid' | 'low'
  level: number
  count: number
  ttfb_ms: PerfStats
  total_ms: PerfStats
}

// The server half of a run's conditions (perf_bench.conditions_snapshot):
// measured with a server run, stamped by the server when a browser run is
// saved. Every key is best-effort and may be missing.
export type PerfServerEnvironment = {
  hostname?: string
  cpu_model?: string | null
  cpu_logical?: number
  cpu_physical?: number | null
  cpu_freq_mhz?: number | null
  // CPUs the app can actually use: affinity mask and cgroup quota, possibly
  // fractional (perf_bench.effective_cpus).
  cpu_effective?: number
  mem_total_mb?: number
  workers?: number | null
  app_version?: string
  python?: string
  openslide?: string | null
  tile_size?: number
  overlap?: number
  jpeg_quality?: number
  dzi_cache_size?: number
  dzi_cache_max_bytes?: number
  ome_tile_cache_bytes?: number
  data_disk?: { fstype?: string | null; rotational?: boolean | null } | null
  os?: string
  kernel?: string
  machine?: string
  virtualization?: string
  timezone?: string | null
  cgroup?: { memory_max_mb: number | null; cpu_limit: number | null } | null
  nginx_cache?: { max_size: string | null; inactive: string | null; used_bytes: number | null; file_count: number | null } | null
  viewer_rendering?: { immediate_render: boolean; max_image_cache_count: number; max_zoom_pixel_ratio: number } | null
  [key: string]: unknown
}

// What the measuring browser reports about itself. Only user_agent is
// guaranteed; the rest is optional and depends on the browser.
export type PerfClientConditions = {
  user_agent: string
  effective_type?: string | null
  downlink_mbps?: number | null
  started_at?: string
  finished_at?: string
  duration_s?: number
  platform?: string | null
  mobile?: boolean | null
  language?: string | null
  timezone?: string | null
  hardware_concurrency?: number | null
  device_memory_gb?: number | null
  device_pixel_ratio?: number
  viewport?: { width: number; height: number }
  screen?: { width: number; height: number }
  max_touch_points?: number | null
  connection_type?: string | null
  connection_rtt_ms?: number | null
  save_data?: boolean | null
  page_origin?: string
  secure_context?: boolean
  // Negotiated protocol per successful tile response (Resource Timing
  // nextHopProtocol), e.g. {"http/1.1": 60}; tiles the browser reported
  // nothing for count as "unknown".
  http_protocols?: Record<string, number>
  // The tab was hidden at some point: browsers throttle background tabs, so
  // such a run's timings are not comparable.
  hidden_during_run?: boolean
}

export type PerfClientResult = {
  schema: number
  params: {
    slide_id: number
    slide_title: string
    mode: 'app' | 'cache'
    tiles: number
    concurrency: number
    // Seed of the tile sample: fixed per slide in 'cache' mode, fresh per
    // run in 'app' mode (absent on results saved without it).
    seed?: number
  }
  network: {
    rtt_ms: PerfStats
    // mbps null: a body timed too short to be a reading (buffered, or a
    // coarse clock), left out of every figure (perfClientTest measureNetwork).
    downloads: { bytes: number; seconds: number; mbps: number | null }[]
    mbps: number | null
  }
  tiles: {
    requested: number
    ok: number
    errors: number
    wall_s: number
    tiles_per_s: number | null
    mb_per_s: number | null
    ttfb_ms: PerfStats
    total_ms: PerfStats
    by_band: PerfClientBand[]
    // X-Cache-Status value -> count ("none" when the header was absent).
    cache: Record<string, number>
    ttfb_by_cache: Record<string, PerfStats>
    // Time to the HTTP error of the failed tiles that got one (may be
    // absent).
    error_ms?: PerfStats
    // How many tile timings come from Resource Timing (the request's own
    // timeline) and how many from fetch() around it, which includes waiting
    // for a browser connection (absent: all fetch()).
    timing?: { resource: number; fetch: number }
    // Every tile request, in the order issued (may be absent);
    // start_ms counts from the start of the tile phase.
    samples?: { columns: string[]; rows: Array<Array<string | number | boolean>> }
  }
  // The slide as its Deep Zoom descriptor describes it (may be absent).
  slide?: {
    id: number
    public_id: string
    width: number
    height: number
    tile_size: number
    overlap: number
    tile_format: string
    max_level: number
    tile_version: number | null
    published: boolean
  }
  client: PerfClientConditions
  // Stamped by the server on save; the browser never sends it.
  server?: { recorded_at: string; environment: PerfServerEnvironment }
  summary: PerfSummary
}

export type PerfRunDetail = PerfRunSummary & {
  result: PerfServerResult | PerfClientResult | null
}

export type PerfRunStartParams = {
  slide_ids: number[]
  cold: boolean
  tiles_per_band: number
  max_concurrency: number
  step_seconds: number
}

export async function adminPerfListRuns(
  opts: { signal?: AbortSignal } = {},
): Promise<{ runs: PerfRunSummary[]; running_id: number | null }> {
  return adminGet('/api/admin/perf/runs', 'perf runs failed', { signal: opts.signal, cache: 'no-store' })
}

export async function adminPerfGetRun(id: number, opts: { signal?: AbortSignal } = {}): Promise<PerfRunDetail> {
  const r = await fetch(`/api/admin/perf/runs/${id}`, {
    headers: authHeaders(),
    signal: opts.signal,
    cache: 'no-store',
  })
  // A lost session or role is final for the page's poll loop, so it gets
  // its own error type instead of the generic message.
  if (r.status === 401 || r.status === 403) throw new UnauthorizedError(r.status)
  if (!r.ok) await throwApi(r, 'perf run failed')
  return r.json()
}

export async function adminPerfStartRun(params: PerfRunStartParams): Promise<{ id: number }> {
  return adminSend('POST', '/api/admin/perf/runs', params, 'perf start failed')
}

export async function adminPerfCancelRun(id: number): Promise<void> {
  await adminFetch(`/api/admin/perf/runs/${id}/cancel`, 'perf cancel failed', { method: 'POST' })
}

export async function adminPerfDeleteRun(id: number): Promise<void> {
  await adminFetch(`/api/admin/perf/runs/${id}`, 'perf delete failed', { method: 'DELETE' })
}

export async function adminPerfSaveClientRun(result: PerfClientResult): Promise<{ id: number }> {
  return adminSend('POST', '/api/admin/perf/client-runs', result, 'perf save failed')
}

// One authenticated round trip; resolves to the elapsed milliseconds.
export async function adminPerfPing(signal?: AbortSignal): Promise<number> {
  const t0 = performance.now()
  const r = await adminFetch('/api/admin/perf/ping', 'perf ping failed', { signal, cache: 'no-store' })
  await r.arrayBuffer()
  return performance.now() - t0
}

// Download `size` incompressible bytes; resolves to bytes received and the
// seconds the body took to arrive. Timed from the response headers to the
// end of the body, not from the request: the round trip and the server's
// time to first byte would otherwise be counted as transfer time, which on a
// short transfer swamps it. Resource Timing's responseStart → responseEnd is
// preferred when the browser has the entry (it starts at the first byte, a
// little before fetch() resolves); its buffer can be full or the entry not
// queued yet, so the fetch() figure is the fallback. That fallback can read
// ~0 s as well — a body the browser already had buffered when fetch()
// resolved — so the caller, not this, decides whether a reading is one
// (measureNetwork drops those under a millisecond).
export async function adminPerfDownload(
  size: number,
  signal?: AbortSignal,
): Promise<{ bytes: number; seconds: number }> {
  const url = `/api/admin/perf/payload?size=${size}`
  const issued = performance.now()
  const r = await adminFetch(url, 'perf download failed', { signal, cache: 'no-store' })
  const t0 = performance.now()
  const buf = await r.arrayBuffer()
  const seconds = (performance.now() - t0) / 1000
  return { bytes: buf.byteLength, seconds: resourceBodySeconds(url, issued) ?? seconds }
}

// responseEnd - responseStart of the last entry for `url` issued at or
// after `since`, in seconds; null without a usable one. A zero span is no
// reading: some browsers stamp both ends at once (a body already buffered,
// a coarsened clock), and 0 s would be recorded as 0 Mbps — or, on the 1 MiB
// probe, as a link so fast that the measurement would be sized to its cap.
function resourceBodySeconds(url: string, since: number): number | null {
  try {
    const entries = performance.getEntriesByName(new URL(url, window.location.href).href, 'resource') as PerformanceResourceTiming[]
    const e = entries.filter((x) => x.startTime >= since).pop()
    if (!e || !(e.responseStart > 0 && e.responseEnd > e.responseStart)) return null
    return (e.responseEnd - e.responseStart) / 1000
  } catch {
    return null
  }
}

// Download stored runs with everything recorded about them: 'json' is the
// full record of each run, 'csv' one row per run for a spreadsheet. No ids
// means the whole history. `signal` cancels it (nothing is saved then) and
// `onProgress` reports the bytes received, as a long JSON export of every
// tile sample can take a while.
export async function adminPerfExport(
  format: 'json' | 'csv',
  ids?: number[],
  opts: { signal?: AbortSignal; onProgress?: (receivedBytes: number) => void } = {},
): Promise<void> {
  const qs = ids && ids.length ? `?ids=${ids.join(',')}` : ''
  await downloadAttachment(`/api/admin/perf/export.${format}${qs}`, {
    headers: authHeaders(),
    fallbackName: ids && ids.length === 1 ? `tessera-perf-run-${ids[0]}.${format}` : `tessera-perf.${format}`,
    failMessage: 'perf export failed',
    signal: opts.signal,
    onProgress: opts.onProgress,
  })
}
