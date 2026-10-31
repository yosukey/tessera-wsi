import React, { useState } from 'react'
import type {
  PerfClientResult,
  PerfRunDetail,
  PerfRunSummary,
  PerfScalingStep,
  PerfServerEnvironment,
  PerfServerResult,
  PerfStats,
  PerfSummary,
} from '../../../lib/api'
import { useI18n } from '../../../lib/i18n'
import { formatBytes } from '../../../lib/admin/validation'

// Byte sizes in decimal units, like the MB/s the benchmark reports.
const DECIMAL_UNITS = { decimal: true }

// Screen-reader-only text: a column without a visible heading, and the live
// regions that announce a running test's phase.
export const SR_ONLY: React.CSSProperties = {
  position: 'absolute', width: 1, height: 1, padding: 0, margin: -1,
  overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0,
}

export type RunCaveat = 'errors' | 'hidden' | 'serverOverlap' | 'jobOverlap' | 'truncated' | 'renderErrors'

// What makes a stored run's numbers questionable even though it
// "succeeded" (the server stores every browser run that way), as far as its
// summary — all the history list has — tells: for a browser run, failed tile
// requests (left out of its figures), a background tab, or a server
// benchmark / background job sharing the machine; for a server run, a run
// cut short (cancelled, timed out, stalled threads, a reader that failed for
// good), failed renders — single-tile or parallel, left out of its figures —
// or a background job that won the race against the benchmark's start
// check. Only what the run says it had: a run that didn't record these
// is unknown and flags nothing (for a server run, its full result still
// shows them in ServerResultView).
export function runCaveats(run: Pick<PerfRunSummary, 'kind' | 'summary'>): RunCaveat[] {
  const s = run.summary
  if (!s) return []
  if (run.kind === 'server') {
    const out: RunCaveat[] = []
    if (s.truncated === true) out.push('truncated')
    if (serverRenderErrors(s) > 0) out.push('renderErrors')
    if (s.job_overlap === true) out.push('jobOverlap')
    return out
  }
  const out: RunCaveat[] = []
  if ((s.tile_errors ?? 0) > 0) out.push('errors')
  if (s.hidden_during_run === true) out.push('hidden')
  if (s.server_overlap === true) out.push('serverOverlap')
  if (s.job_overlap === true) out.push('jobOverlap')
  return out
}

// A server run's failed renders as its summary counts them: the band
// phase's single-tile ones plus the parallel phase's (0 when not recorded).
export function serverRenderErrors(s: PerfSummary): number {
  return (s.band_errors ?? 0) + (s.tile_errors ?? 0)
}

// The timing method of a browser run (PerfSummary.timing), as a key two runs
// compare by: 'legacy' for one with no marker (download and tiles both timed
// around fetch()), else the marker's value. 'legacy' is kept apart from 1
// although both take the tile times around fetch(): a run with the marker
// times its download from the body alone, one without it with the round trip
// in it.
const timingOf = (run: Pick<PerfRunSummary, 'summary'>): string => {
  const m = run.summary?.timing
  return m == null ? 'legacy' : String(m)
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 })
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  // Page-cache reads can finish in microseconds; "0 ms" would read as "not measured".
  if (v > 0 && v < 0.005) return '< 0.01 ms'
  return `${fmtNum(v, v < 10 ? 2 : v < 100 ? 1 : 0)} ms`
}

export function formatWhen(iso: string, locale: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(locale === 'ja' ? 'ja-JP' : 'en-US', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

export const TH: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: 'var(--color-text-faint)',
  whiteSpace: 'nowrap',
}
export const TD: React.CSSProperties = {
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 12,
  verticalAlign: 'middle',
}
// Numeric table cells align on the digits.
export const TDN: React.CSSProperties = { ...TD, textAlign: 'right', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }
const THN: React.CSSProperties = { ...TH, textAlign: 'right' }

export function SubHeading({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)', margin: '16px 0 6px' }}>
      {children}
    </div>
  )
}

// Stat tile: label, value (proportional figures, sans semibold), optional sub.
export function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div style={{ flex: '1 1 150px', minWidth: 150, padding: '10px 12px', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-surface)' }}>
      <div style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 2, color: 'var(--color-text)' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', marginTop: 1 }}>{sub}</div>}
    </div>
  )
}

export function Meter({ percent, label }: { percent: number; label: string }) {
  const p = Math.max(0, Math.min(100, percent))
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(p)}
      aria-label={label}
      style={{ height: 8, borderRadius: 4, background: 'var(--color-brand-100)', overflow: 'hidden' }}
    >
      <div style={{ width: `${p}%`, height: '100%', background: 'var(--color-brand-600)', borderRadius: 4, transition: 'width 300ms ease-out' }} />
    </div>
  )
}

function statPair(s: PerfStats | undefined): string {
  if (!s || !s.count) return '—'
  return `${fmtMs(s.p50)} / ${fmtMs(s.p95)}`
}

function bandLabel(t: (k: string) => string, band: string): string {
  return t(`adminPerf.band.${band}`)
}

// ---------------------------------------------------------------------------
// Server result
// ---------------------------------------------------------------------------

// Throughput by parallelism: one series, so one colour and no legend; the
// value sits in a fixed-width column after the bar (so every bar is drawn
// against the same track width) and the rest of the row is the table view.
function ThroughputBars({ steps }: { steps: PerfScalingStep[] }) {
  const { t } = useI18n()
  const max = Math.max(1, ...steps.map((s) => s.tiles_per_s || 0))
  // A run stored without it has no "new tiles" share at all.
  const hasFresh = steps.some((s) => s.fresh_tile_pct != null)
  return (
    <>
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={THN}>{t('adminPerf.col.concurrency')}</th>
          <th style={{ ...TH, width: '42%' }}>{t('adminPerf.col.tilesPerSec')}</th>
          <th style={THN}>{t('adminPerf.col.latency')}</th>
          <th style={THN}>{t('adminPerf.col.cpu')}</th>
          <th style={THN}>{t('adminPerf.col.iowait')}</th>
          {hasFresh && <th style={THN} title={t('adminPerf.scaling.freshHint')}>{t('adminPerf.col.fresh')}</th>}
          <th style={THN}>{t('adminPerf.col.tiles')}</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((s) => {
          const pct = ((s.tiles_per_s || 0) / max) * 100
          return (
            <tr key={s.concurrency}>
              <td style={TDN}>{s.concurrency}</td>
              <td style={TD}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      title={`${s.concurrency}: ${fmtNum(s.tiles_per_s)} ${t('adminPerf.unit.tilesPerSec')}`}
                      style={{
                        width: `${Math.max(0.5, pct)}%`,
                        height: 14,
                        background: 'var(--color-brand-600)',
                        borderRadius: '0 4px 4px 0',
                      }}
                    />
                  </div>
                  <span style={{ flex: '0 0 4.5em', fontVariantNumeric: 'tabular-nums', color: 'var(--color-text)' }}>{fmtNum(s.tiles_per_s)}</span>
                </div>
              </td>
              <td style={TDN}>{statPair(s.latency_ms)}</td>
              <td style={TDN}>{fmtNum(s.cpu_busy_pct)}%</td>
              <td style={TDN}>{fmtNum(s.iowait_pct)}%</td>
              {hasFresh && <td style={TDN}>{s.fresh_tile_pct != null ? `${fmtNum(s.fresh_tile_pct)}%` : '—'}</td>}
              <td style={TDN}>
                {s.tiles.toLocaleString()}{s.errors ? ` (${s.errors} ✕)` : ''}
                {s.skipped ? ` (${t('adminPerf.scaling.skipped', { n: s.skipped })})` : ''}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
    {hasFresh && <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', marginTop: 4 }}>{t('adminPerf.scaling.freshHint')}</div>}
    </>
  )
}

// Failed single-tile renders across a server run's band phase. The run's
// summary doesn't carry them, so only the full result can tell.
export function bandErrorCount(result: PerfServerResult): number {
  return (result.slides || []).reduce(
    (n, sl) => n + (sl.bands || []).reduce((m, b) => m + (b.errors || 0), 0),
    0,
  )
}

function Advice({ run, result }: { run: PerfRunSummary; result: PerfServerResult }) {
  const { t } = useI18n()
  // "No bottleneck" only means something for a run that measured everything
  // it set out to, without failed parallel renders or failed band renders
  // (the server's own rules, applied again here to the stored advice).
  const complete = run.state === 'succeeded' && !result.truncated
    && !(result.scaling || []).some((s) => s.errors > 0) && bandErrorCount(result) === 0
  const advice = (result.advice || []).filter((a) => complete || a.code !== 'noBottleneck')
  if (!advice.length) return null
  return (
    <ul style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }}>
      {advice.map((a, i) => (
        <li key={i}>{t(`adminPerf.advice.${a.code}`, a.params)}</li>
      ))}
    </ul>
  )
}

type Translate = (k: string, p?: Record<string, string | number>) => string
type KeyValueRows = Array<[string, React.ReactNode]>

function KeyValueTable({ rows }: { rows: KeyValueRows }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td style={{ ...TD, width: '30%', color: 'var(--color-text-muted)', fontWeight: 600 }}>{k}</td>
            <td style={{ ...TD, wordBreak: 'break-word' }}>{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// A heading that opens and closes the table under it.
function Disclosure({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{ appearance: 'none', background: 'none', border: 0, padding: 0, font: 'inherit', cursor: 'pointer', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)', margin: '16px 0 6px', display: 'flex', gap: 4 }}
      >
        <span aria-hidden="true">{open ? '▼' : '▶'}</span>{title}
      </button>
      {open && children}
    </>
  )
}

const dash = (v: unknown): string => (v == null || v === '' ? '—' : String(v))

// The server's side of the conditions, shared by both result views (a browser
// run carries the snapshot the server stamped when the run was saved).
function serverEnvironmentRows(e: PerfServerEnvironment, t: Translate): KeyValueRows {
  const disk = e.data_disk?.rotational == null ? '—' : e.data_disk.rotational ? 'HDD' : 'SSD'
  const rows: KeyValueRows = [
    [t('adminPerf.env.host'), dash(e.hostname)],
    [t('adminPerf.env.os'), [e.os, e.kernel, e.machine, e.virtualization].filter(Boolean).join(' · ') || '—'],
    // The usable CPUs only when they are fewer than the host's: an affinity
    // mask or a cgroup quota, which is what the app's throughput is bound by.
    [t('adminPerf.env.cpu'), `${e.cpu_model || '—'} (${e.cpu_physical ?? '—'} / ${e.cpu_logical ?? '—'})${
      e.cpu_effective != null && (e.cpu_logical == null || e.cpu_effective < e.cpu_logical)
        ? ` · ${t('adminPerf.env.cpuEffective', { n: fmtNum(e.cpu_effective, 2) })}` : ''
    }${e.cpu_freq_mhz ? ` · ${e.cpu_freq_mhz.toLocaleString()} MHz` : ''}`],
    // The server reports memory in MiB (bytes // 1024²), so it says MiB.
    [t('adminPerf.env.memory'), e.mem_total_mb ? `${Number(e.mem_total_mb).toLocaleString()} MiB` : '—'],
  ]
  if (e.cgroup && (e.cgroup.cpu_limit != null || e.cgroup.memory_max_mb != null)) {
    rows.push([t('adminPerf.env.limits'), t('adminPerf.env.limitsValue', {
      cpu: dash(e.cgroup.cpu_limit),
      mem: e.cgroup.memory_max_mb != null ? `${e.cgroup.memory_max_mb.toLocaleString()} MiB` : '—',
    })])
  }
  rows.push(
    [t('adminPerf.env.workers'), dash(e.workers)],
    [t('adminPerf.env.disk'), `${disk}${e.data_disk?.fstype ? ` (${e.data_disk.fstype})` : ''}`],
    [t('adminPerf.env.tiles'), t('adminPerf.env.tilesValue', { size: dash(e.tile_size), overlap: dash(e.overlap), quality: dash(e.jpeg_quality) })],
    [t('adminPerf.env.caches'), `DZI ${e.dzi_cache_size ?? '—'} / ${formatBytes(e.dzi_cache_max_bytes, DECIMAL_UNITS)} · OME ${formatBytes(e.ome_tile_cache_bytes, DECIMAL_UNITS)}`],
  )
  // Unreadable without the installed nginx config (dev, or a custom layout):
  // every field null, which would read as "— used of —".
  if (e.nginx_cache && (e.nginx_cache.max_size != null || e.nginx_cache.used_bytes != null)) {
    rows.push([t('adminPerf.env.nginxCache'), t('adminPerf.env.nginxCacheValue', {
      used: formatBytes(e.nginx_cache.used_bytes, DECIMAL_UNITS),
      max: dash(e.nginx_cache.max_size),
      files: e.nginx_cache.file_count != null ? e.nginx_cache.file_count.toLocaleString() : '—',
    })])
  }
  if (e.viewer_rendering) {
    rows.push([t('adminPerf.env.rendering'), t('adminPerf.env.renderingValue', {
      immediate: t(e.viewer_rendering.immediate_render ? 'adminPerf.env.on' : 'adminPerf.env.off'),
      cache: e.viewer_rendering.max_image_cache_count,
      zoom: fmtNum(e.viewer_rendering.max_zoom_pixel_ratio),
    })])
  }
  rows.push([t('adminPerf.env.versions'), `Tessera ${e.app_version || '—'} · OpenSlide ${e.openslide || '—'} · Python ${e.python || '—'}`])
  return rows
}

function EnvironmentTable({ result }: { result: PerfServerResult }) {
  const { t } = useI18n()
  const p = result.params || {}
  const load = result.system?.load_avg_start
  const rows: KeyValueRows = [
    ...serverEnvironmentRows(result.environment || {}, t),
    [t('adminPerf.env.params'), t('adminPerf.env.paramsValue', {
      cold: p.cold ? t('adminPerf.env.cold') : t('adminPerf.env.warm'),
      tiles: p.tiles_per_band ?? '—',
      conc: p.max_concurrency ?? '—',
      step: p.step_seconds ?? '—',
    })],
  ]
  const basis = result.system?.cpu_basis
  if (basis) {
    rows.push([t('adminPerf.env.cpuBasis'), t(`adminPerf.env.cpuBasis.${basis}`, {
      n: result.system.cpu_effective != null ? fmtNum(result.system.cpu_effective, 2) : '—',
    })])
  }
  if (load) rows.push([t('adminPerf.env.loadStart'), load.map((x) => fmtNum(x, 2)).join(' / ')])
  rows.push([t('adminPerf.env.duration'), `${fmtNum(result.duration_s)} s`])
  return <KeyValueTable rows={rows} />
}

// The measuring browser, device and network, the slide, and the server it
// was measured against. A run missing some of these shows what it has.
function ClientConditionsTable({ result }: { result: PerfClientResult }) {
  const { t, locale } = useI18n()
  const c = result.client || { user_agent: '' }
  const sl = result.slide
  const rows: KeyValueRows = []
  if (c.started_at) {
    rows.push([t('adminPerf.cond.when'), t('adminPerf.cond.whenValue', {
      start: formatWhen(c.started_at, locale),
      duration: c.duration_s != null ? fmtNum(c.duration_s) : '—',
    })])
  }
  rows.push([t('adminPerf.cond.browser'), dash(c.user_agent)])
  if (c.platform !== undefined) {
    rows.push([t('adminPerf.cond.platform'), `${dash(c.platform)}${c.mobile ? ` · ${t('adminPerf.cond.mobile')}` : ''}${c.max_touch_points ? ` · ${t('adminPerf.cond.touch', { n: c.max_touch_points })}` : ''}`])
  }
  if (c.viewport) {
    rows.push([t('adminPerf.cond.display'), t('adminPerf.cond.displayValue', {
      vw: c.viewport.width, vh: c.viewport.height,
      sw: c.screen?.width ?? '—', sh: c.screen?.height ?? '—',
      dpr: fmtNum(c.device_pixel_ratio ?? null, 2),
    })])
  }
  if (c.hardware_concurrency !== undefined) {
    rows.push([t('adminPerf.cond.device'), t('adminPerf.cond.deviceValue', {
      cores: dash(c.hardware_concurrency),
      mem: c.device_memory_gb != null ? `${c.device_memory_gb} GB` : '—',
    })])
  }
  const net = [
    c.connection_type, c.effective_type,
    c.downlink_mbps != null ? `${c.downlink_mbps} Mbps` : null,
    c.connection_rtt_ms != null ? `RTT ${c.connection_rtt_ms} ms` : null,
    c.save_data ? 'save-data' : null,
  ].filter(Boolean)
  rows.push([t('adminPerf.cond.network'), net.length ? net.join(' · ') : '—'])
  if (c.http_protocols) {
    const p = Object.entries(c.http_protocols).sort((a, b) => b[1] - a[1])
    rows.push([t('adminPerf.cond.protocol'), p.length ? p.map(([k, n]) => `${k} (${n})`).join(', ') : '—'])
  }
  const timing = result.tiles?.timing
  if (timing) {
    rows.push([t('adminPerf.cond.timing'), t('adminPerf.cond.timingValue', { resource: timing.resource, fetch: timing.fetch })])
  }
  if (c.page_origin) {
    rows.push([t('adminPerf.cond.page'), `${c.page_origin}${c.secure_context === false ? ` · ${t('adminPerf.cond.insecure')}` : ''}`])
  }
  if (sl) {
    rows.push([t('adminPerf.cond.slide'), t('adminPerf.cond.slideValue', {
      w: sl.width.toLocaleString(), h: sl.height.toLocaleString(),
      tile: sl.tile_size, overlap: sl.overlap, format: sl.tile_format, max: sl.max_level,
    })])
  }
  const env = result.server?.environment
  return (
    <>
      <KeyValueTable rows={rows} />
      {env && (
        <>
          <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', margin: '8px 0 2px' }}>
            {t('adminPerf.cond.serverNote', { when: formatWhen(result.server?.recorded_at || '', locale) })}
          </div>
          <KeyValueTable rows={serverEnvironmentRows(env, t)} />
        </>
      )}
    </>
  )
}

export function ServerResultView({ run, result }: { run: PerfRunSummary; result: PerfServerResult }) {
  const { t } = useI18n()
  const s: PerfSummary = result.summary || {}
  const bandErrors = bandErrorCount(result)
  const anySkipped = (result.slides || []).some((sl) => (sl.bands || []).some((b) => (b.skipped || 0) > 0))
  const warn: React.CSSProperties = { fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-warning-bg)', color: 'var(--color-warning-fg)', marginBottom: 8 }
  return (
    <div>
      {result.truncated && <div style={warn}>⚠ {t('adminPerf.truncated')}</div>}
      {(s.job_overlap === true || run.summary?.job_overlap === true) && <div style={warn}>⚠ {t('adminPerf.client.jobOverlap')}</div>}
      {bandErrors > 0 && <div style={warn}>⚠ {t('adminPerf.slides.bandErrors', { n: bandErrors })}</div>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <StatTile
          label={t('adminPerf.kpi.peak')}
          value={`${fmtNum(s.peak_tiles_per_s)} ${t('adminPerf.unit.tilesPerSec')}`}
          sub={s.peak_concurrency != null ? t('adminPerf.kpi.peakSub', { n: s.peak_concurrency }) : undefined}
        />
        <StatTile label={t('adminPerf.kpi.single')} value={`${fmtNum(s.single_tiles_per_s)} ${t('adminPerf.unit.tilesPerSec')}`} />
        <StatTile label={t('adminPerf.kpi.tileP95')} value={fmtMs(s.tile_p95_ms)} sub={t('adminPerf.kpi.tileP95Sub')} />
        <StatTile label={t('adminPerf.kpi.seqRead')} value={s.seq_read_mb_s != null ? `${fmtNum(s.seq_read_mb_s)} MB/s` : '—'} />
        <StatTile label={t('adminPerf.kpi.randomRead')} value={fmtMs(s.random_read_p95_ms)} />
        <StatTile label={t('adminPerf.kpi.open')} value={fmtMs(s.open_first_ms)} />
      </div>
      {result.scope === 'below_http' && (
        <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', marginTop: 6 }}>{t('adminPerf.scope.belowHttp')}</div>
      )}

      <SubHeading>{t('adminPerf.advice.title')}</SubHeading>
      <Advice run={run} result={result} />

      {result.scaling?.length > 0 && (
        <>
          <SubHeading>{t('adminPerf.scaling.title')}</SubHeading>
          <div style={{ fontSize: 12, color: 'var(--color-text-subtle)', marginBottom: 4 }}>{t('adminPerf.scaling.caption')}</div>
          <ThroughputBars steps={result.scaling} />
        </>
      )}

      <SubHeading>{t('adminPerf.slides.title')}</SubHeading>
      {result.slides.map((sl) => (
        <div key={sl.id} style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>
            {sl.title} <span style={{ fontWeight: 400, color: 'var(--color-text-subtle)' }}>
              (#{sl.id} · {sl.format === 'szi' ? 'SZI' : sl.vendor || '—'} · {sl.width && sl.height ? `${sl.width.toLocaleString()}×${sl.height.toLocaleString()}` : '—'} · {formatBytes(sl.file_bytes, DECIMAL_UNITS)})
            </span>
          </div>
          {/* A reader that failed part-way keeps the bands it measured, so
              an error does not hide the rest of the slide. */}
          {sl.error && (
            <div style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>✕ {t('adminPerf.slides.error')} {sl.error}</div>
          )}
          {sl.reader_latched && (
            <div style={{ fontSize: 12, color: 'var(--color-warning-fg)' }}>⚠ {t('adminPerf.slides.readerLatched')}</div>
          )}
          {sl.warm_fraction != null && sl.warm_fraction < 1 && (
            <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
              {t('adminPerf.slides.partlyWarm', {
                read: formatBytes(sl.warm_bytes ?? null, DECIMAL_UNITS),
                pct: fmtNum(sl.warm_fraction * 100),
              })}
            </div>
          )}
          {(sl.open || sl.disk || sl.bands) && (
            <>
              <div style={{ fontSize: 12, color: 'var(--color-text-muted)', margin: '2px 0 4px' }}>
                {sl.open && t('adminPerf.slides.open', { first: fmtMs(sl.open.first_ms), repeat: fmtMs(sl.open.repeat_ms) })}
                {sl.disk && ` · ${t('adminPerf.slides.disk', {
                  mbps: sl.disk.sequential.mb_per_s != null ? fmtNum(sl.disk.sequential.mb_per_s) : '—',
                  p95: fmtMs(sl.disk.random_64k_ms.p95),
                  file: sl.disk.file,
                })}`}
              </div>
              {sl.bands && (
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={TH}>{t('adminPerf.col.band')}</th>
                      <th style={THN}>{t('adminPerf.col.level')}</th>
                      <th style={THN}>{sl.format === 'szi' ? t('adminPerf.col.read') : t('adminPerf.col.generate')}</th>
                      {sl.format !== 'szi' && <th style={THN}>{t('adminPerf.col.encode')}</th>}
                      <th style={THN}>{t('adminPerf.col.total')}</th>
                      <th style={THN}>{t('adminPerf.col.max')}</th>
                      <th style={THN}>{t('adminPerf.col.avgSize')}</th>
                      {sl.bands.some((b) => b.errors || b.skipped) && <th style={THN}>{t('adminPerf.col.failed')}</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {sl.bands.map((b) => (
                      <tr key={b.band}>
                        <td style={TD}>{bandLabel(t, b.band)}</td>
                        <td style={TDN}>{b.level}</td>
                        <td style={TDN}>{statPair(b.generate_ms)}</td>
                        {sl.format !== 'szi' && <td style={TDN}>{statPair(b.encode_ms)}</td>}
                        <td style={TDN}>{statPair(b.total_ms)}</td>
                        <td style={TDN}>{fmtMs(b.total_ms.max)}</td>
                        <td style={TDN}>{formatBytes(b.avg_bytes, DECIMAL_UNITS)}</td>
                        {sl.bands!.some((x) => x.errors || x.skipped) && (
                          <td style={TDN}>
                            {[b.errors ? `${b.errors} ✕` : '', b.skipped ? t('adminPerf.scaling.skipped', { n: b.skipped }) : '']
                              .filter(Boolean).join(' · ') || '—'}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      ))}
      <div style={{ fontSize: 11, color: 'var(--color-text-subtle)' }}>{t('adminPerf.slides.pairNote')}</div>
      {anySkipped && <div style={{ fontSize: 11, color: 'var(--color-text-subtle)' }}>{t('adminPerf.slides.skippedNote')}</div>}

      <Disclosure title={t('adminPerf.env.title')}>
        <EnvironmentTable result={result} />
      </Disclosure>
      <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', marginTop: 8 }}>
        {t('adminPerf.runMeta', { id: run.id, actor: run.actor || '—' })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Client (browser) result
// ---------------------------------------------------------------------------

export function ClientResultView({ run, result }: { run: PerfRunSummary; result: PerfClientResult }) {
  const { t } = useI18n()
  const s = result.summary || {}
  const tiles = result.tiles
  const client = result.client || { user_agent: '' }
  const cacheKeys = Object.keys(tiles.cache || {}).sort()
  const errorMs = tiles.error_ms
  // The server stamps the overlap flags on the stored run; either copy will do.
  const serverOverlap = s.server_overlap === true || run.summary?.server_overlap === true
  const jobOverlap = s.job_overlap === true || run.summary?.job_overlap === true
  const warn: React.CSSProperties = { fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-warning-bg)', color: 'var(--color-warning-fg)', marginTop: 8 }
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 8 }}>
        {t('adminPerf.client.resultHead', {
          title: result.params.slide_title,
          mode: t(result.params.mode === 'app' ? 'adminPerf.client.modeAppShort' : 'adminPerf.client.modeCacheShort'),
          tiles: result.params.tiles,
          conc: result.params.concurrency,
        })}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <StatTile label={t('adminPerf.kpi.rtt')} value={fmtMs(s.rtt_ms)} />
        <StatTile label={t('adminPerf.kpi.download')} value={s.mbps != null ? `${fmtNum(s.mbps)} Mbps` : '—'} />
        <StatTile label={t('adminPerf.kpi.tilesPerSec')} value={`${fmtNum(s.tiles_per_s)} ${t('adminPerf.unit.tilesPerSec')}`}
          sub={tiles.mb_per_s != null ? `${fmtNum(tiles.mb_per_s * 8)} Mbps` : undefined} />
        <StatTile label={t('adminPerf.kpi.ttfbP95')} value={fmtMs(s.tile_ttfb_p95_ms)} />
        {s.cache_hit_pct != null && <StatTile label={t('adminPerf.kpi.cacheHit')} value={`${fmtNum(s.cache_hit_pct)}%`} />}
      </div>
      {tiles.errors > 0 && (
        <div style={{ fontSize: 12, color: 'var(--color-danger-fg)', marginTop: 6 }}>
          ✕ {t('adminPerf.client.errors', { n: tiles.errors })}
          {errorMs && errorMs.count > 0 && ` ${t('adminPerf.client.errorTimes', { p50: fmtMs(errorMs.p50), max: fmtMs(errorMs.max) })}`}
        </div>
      )}
      {client.hidden_during_run && <div style={warn}>⚠ {t('adminPerf.cond.hidden')}</div>}
      {serverOverlap && <div style={warn}>⚠ {t('adminPerf.client.serverOverlap')}</div>}
      {jobOverlap && <div style={warn}>⚠ {t('adminPerf.client.jobOverlap')}</div>}

      <SubHeading>{t('adminPerf.client.bandsTitle')}</SubHeading>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={TH}>{t('adminPerf.col.band')}</th>
            <th style={THN}>{t('adminPerf.col.level')}</th>
            <th style={THN}>{t('adminPerf.col.tiles')}</th>
            <th style={THN}>{t('adminPerf.col.ttfb')}</th>
            <th style={THN}>{t('adminPerf.col.total')}</th>
            <th style={THN}>{t('adminPerf.col.max')}</th>
          </tr>
        </thead>
        <tbody>
          {tiles.by_band.map((b) => (
            <tr key={b.band}>
              <td style={TD}>{bandLabel(t, b.band)}</td>
              <td style={TDN}>{b.level}</td>
              <td style={TDN}>{b.count}</td>
              <td style={TDN}>{statPair(b.ttfb_ms)}</td>
              <td style={TDN}>{statPair(b.total_ms)}</td>
              <td style={TDN}>{fmtMs(b.total_ms.max)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {cacheKeys.length > 0 && (
        <>
          <SubHeading>{t('adminPerf.client.cacheTitle')}</SubHeading>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={TH}>X-Cache-Status</th>
                <th style={THN}>{t('adminPerf.col.tiles')}</th>
                <th style={THN}>{t('adminPerf.col.ttfb')}</th>
              </tr>
            </thead>
            <tbody>
              {cacheKeys.map((k) => (
                <tr key={k}>
                  <td style={TD}>{k === 'NONE' ? t('adminPerf.client.cacheNone') : k}</td>
                  <td style={TDN}>{tiles.cache[k]}</td>
                  <td style={TDN}>{statPair(tiles.ttfb_by_cache?.[k])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <SubHeading>{t('adminPerf.client.networkTitle')}</SubHeading>
      <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
        {t('adminPerf.client.networkLine', {
          min: fmtMs(result.network.rtt_ms.min),
          p50: fmtMs(result.network.rtt_ms.p50),
          p95: fmtMs(result.network.rtt_ms.p95),
          size: formatBytes(result.network.downloads[result.network.downloads.length - 1]?.bytes, DECIMAL_UNITS),
        })}
      </div>
      <Disclosure title={t('adminPerf.cond.title')}>
        <ClientConditionsTable result={result} />
      </Disclosure>
      <div style={{ fontSize: 11, color: 'var(--color-text-subtle)', marginTop: 8, wordBreak: 'break-word' }}>
        {t('adminPerf.runMeta', { id: run.id, actor: run.actor || '—' })} · {client.user_agent || '—'}
        {client.effective_type ? ` · ${client.effective_type}` : ''}
      </div>
    </div>
  )
}

// The server stores a short code for the failures it names itself
// (perf_bench.RUN_ERROR_*); anything else is an exception text shown as is.
function runErrorText(t: (k: string) => string, error: string): string {
  const key = `adminPerf.runError.${error}`
  const text = t(key)
  return text === key ? error : text
}

export function RunResultView({ run }: { run: PerfRunDetail }) {
  const { t } = useI18n()
  // A failed run can still carry a partial result; the failure comes first
  // so the numbers below are never read as a healthy run.
  const failure = run.state === 'failed' && (
    <div role="alert" style={{ fontSize: 13, padding: '6px 8px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)', marginBottom: 8 }}>
      ✕ {t('adminPerf.state.failed')}{run.error ? `: ${runErrorText(t, run.error)}` : ''}
    </div>
  )
  if (!run.result) return failure || null
  return (
    <>
      {failure}
      {run.kind === 'server'
        ? <ServerResultView run={run} result={run.result as PerfServerResult} />
        : <ClientResultView run={run} result={run.result as PerfClientResult} />}
    </>
  )
}

// ---------------------------------------------------------------------------
// Comparison
// ---------------------------------------------------------------------------

// unit 'tiles' is translated; the others read the same in every language.
type MetricDef = { key: keyof PerfSummary; unit: string; higherIsBetter: boolean }

const SERVER_METRICS: MetricDef[] = [
  { key: 'peak_tiles_per_s', unit: 'tiles', higherIsBetter: true },
  { key: 'single_tiles_per_s', unit: 'tiles', higherIsBetter: true },
  { key: 'tile_p95_ms', unit: 'ms', higherIsBetter: false },
  { key: 'seq_read_mb_s', unit: 'MB/s', higherIsBetter: true },
  { key: 'random_read_p95_ms', unit: 'ms', higherIsBetter: false },
  { key: 'open_first_ms', unit: 'ms', higherIsBetter: false },
]
const CLIENT_METRICS: MetricDef[] = [
  { key: 'rtt_ms', unit: 'ms', higherIsBetter: false },
  { key: 'mbps', unit: 'Mbps', higherIsBetter: true },
  { key: 'tiles_per_s', unit: 'tiles', higherIsBetter: true },
  { key: 'tile_ttfb_p95_ms', unit: 'ms', higherIsBetter: false },
  { key: 'cache_hit_pct', unit: '%', higherIsBetter: true },
]

function withUnit(v: number | null | undefined, unit: string, t: (k: string) => string): string {
  if (v == null) return '—'
  if (unit === '%') return `${fmtNum(v)}%`
  return `${fmtNum(v)} ${unit === 'tiles' ? t('adminPerf.unit.tilesPerSec') : unit}`
}

// The settings that make two runs measure the same thing.
function comparableKey(run: PerfRunSummary): string {
  const p = run.params || {}
  if (run.kind === 'server') {
    const slides: Array<{ id: number }> = Array.isArray(p.slides) ? p.slides : []
    return JSON.stringify([slides.map((s) => s.id), !!p.cold, p.tiles_per_band, p.max_concurrency, p.step_seconds])
  }
  return JSON.stringify([p.slide_id, p.mode, p.tiles, p.concurrency])
}

export function CompareView({ a, b }: { a: PerfRunSummary; b: PerfRunSummary }) {
  const { t, locale } = useI18n()
  const defs = a.kind === 'server' ? SERVER_METRICS : CLIENT_METRICS
  const rows = defs.filter((d) => a.summary?.[d.key] != null || b.summary?.[d.key] != null)
  const unlike = comparableKey(a) !== comparableKey(b)
  const partial = a.state !== 'succeeded' || b.state !== 'succeeded'
  // A browser run is always stored as succeeded, so the state alone never
  // flags one that lost tiles, ran in a background tab or shared the machine
  // (nor a server run a background job raced).
  const caveats = runCaveats(a).length > 0 || runCaveats(b).length > 0
  // Browser runs timed by different methods: a download speed timed with
  // the round trip in it, or tile response times with the browser's
  // connection queue in them, make one run look slower on those figures
  // with nothing changed. Any difference counts — an unmarked run against a
  // marked one, or two marked ones of which only one had Resource Timing for
  // every tile. A caveat rather than a refusal: the other figures still
  // compare, and unmarked runs stay usable as baselines.
  const methods = a.kind === 'client' && timingOf(a) !== timingOf(b)
  return (
    <>
    {(unlike || partial || caveats || methods) && (
      <div style={{ fontSize: 12, color: 'var(--color-warning-fg)', marginBottom: 6 }}>
        {unlike && <div>⚠ {t('adminPerf.compare.unlike')}</div>}
        {methods && <div>⚠ {t('adminPerf.compare.timingMethod')}</div>}
        {partial && <div>⚠ {t('adminPerf.compare.partial')}</div>}
        {caveats && <div>⚠ {t(a.kind === 'server' ? 'adminPerf.compare.caveatsServer' : 'adminPerf.compare.caveats')}</div>}
      </div>
    )}
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={TH}>{t('adminPerf.compare.metric')}</th>
          <th style={THN}>#{a.id} · {formatWhen(a.created_at, locale)}</th>
          <th style={THN}>#{b.id} · {formatWhen(b.created_at, locale)}</th>
          <th style={THN}>{t('adminPerf.compare.change')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => {
          const va = a.summary?.[d.key] as number | null | undefined
          const vb = b.summary?.[d.key] as number | null | undefined
          let change: React.ReactNode = '—'
          if (va != null && vb != null) {
            // A rate already in % changes by points, and a zero baseline has
            // no relative change: both show the plain difference instead.
            const points = d.unit === '%'
            const absolute = points || va === 0
            const delta = absolute ? vb - va : ((vb - va) / Math.abs(va)) * 100
            if (absolute ? Math.abs(delta) < (points ? 0.5 : 1e-9) : Math.abs(delta) < 0.5) {
              change = <span style={{ color: 'var(--color-text-muted)' }}>= {t('adminPerf.compare.same')}</span>
            } else {
              const better = d.higherIsBetter ? delta > 0 : delta < 0
              const amount = points
                ? t('adminPerf.compare.points', { n: fmtNum(Math.abs(delta)) })
                : absolute
                  ? withUnit(Math.abs(delta), d.unit, t)
                  : `${fmtNum(Math.abs(delta), 0)}%`
              change = (
                <span style={{ color: better ? 'var(--color-success-fg)' : 'var(--color-danger-fg)' }}>
                  {delta > 0 ? '▲' : '▼'} {amount} {t(better ? 'adminPerf.compare.better' : 'adminPerf.compare.worse')}
                </span>
              )
            }
          }
          return (
            <tr key={d.key}>
              <td style={TD}>{t(`adminPerf.metric.${d.key}`)}</td>
              <td style={TDN}>{withUnit(va, d.unit, t)}</td>
              <td style={TDN}>{withUnit(vb, d.unit, t)}</td>
              <td style={TDN}>{change}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
    </>
  )
}
