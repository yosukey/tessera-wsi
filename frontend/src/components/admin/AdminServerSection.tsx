import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  AdminStats,
  AppVersionSource,
  ServerInfo,
  adminGetServerInfo,
  adminGetStats,
  isAbortError,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { displayFilename, formatBytes, formatUptime } from '../../lib/admin/validation'
import { formatInEmbeddedOffset } from '../../lib/admin/datetime'

function formatMHz(mhz: number | null | undefined): string {
  if (mhz == null) return '—'
  if (mhz >= 1000) return `${(mhz / 1000).toFixed(2)} GHz`
  return `${Math.round(mhz)} MHz`
}

function formatPct(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return '—'
  return `${n.toFixed(digits)}%`
}

function formatNumber(n: number | null | undefined): string {
  if (n == null) return '—'
  return n.toLocaleString()
}

function formatKb(kb: number | null | undefined): string {
  if (kb == null) return '—'
  if (kb >= 1024) return `${(kb / 1024).toFixed(0)} MB`
  return `${kb} KB`
}

function formatMb(mb: number | null | undefined): string {
  if (mb == null) return '—'
  return `${mb.toLocaleString()} MB`
}

function formatGb(gb: number | null | undefined): string {
  return gb == null ? '—' : `${gb} GB`
}

function pct(n: number, total: number): string {
  if (!total) return '0%'
  return `${Math.round((n / total) * 100)}%`
}

function StatRow({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
      <td style={{ padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)', width: '52%' }}>{label}</td>
      <td style={{ padding: '4px 6px', fontFamily: 'monospace', wordBreak: 'break-all' }}>
        {value}
        {sub && <span style={{ marginLeft: 6, fontSize: 10, opacity: 0.6 }}>{sub}</span>}
      </td>
    </tr>
  )
}

function SubHeading({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)', margin: '14px 0 4px' }}>
      {children}
    </div>
  )
}

// A real <button>, not a clickable <div>: these headings are the only way to
// open the six detail tables (CPU, hugepages, mounts, disk I/O, OS, net I/O),
// and a div carries no role, no tab stop and no Enter/Space handling, so a
// keyboard or screen-reader user could not reach them at all. The button's
// own chrome is reset so the visual result is unchanged.
function CollapsibleHeading({ children, expanded, onToggle }: { children: React.ReactNode; expanded: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      style={{ appearance: 'none', background: 'none', border: 0, padding: 0, font: 'inherit', textAlign: 'left', width: '100%', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)', margin: '14px 0 4px', cursor: 'pointer', userSelect: 'none', display: 'flex', alignItems: 'center', gap: 4 }}
    >
      <span aria-hidden="true" style={{ fontSize: 9, opacity: 0.7 }}>{expanded ? '▼' : '▶'}</span>
      {children}
    </button>
  )
}

function SummaryCard({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div style={{ flex: '1 1 160px', minWidth: 160, padding: '10px 12px', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-surface)' }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)' }}>{label}</div>
      <div style={{ fontSize: 18, fontFamily: 'monospace', marginTop: 2 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, opacity: 0.6, marginTop: 1 }}>{sub}</div>}
    </div>
  )
}

function ProgressBar({ pct: p, color }: { pct: number; color?: string }) {
  const w = Math.max(0, Math.min(100, p))
  return (
    <div style={{ display: 'inline-block', width: 50, height: 7, background: 'var(--color-surface-alt, #eee)', borderRadius: 2, overflow: 'hidden', verticalAlign: 'middle', marginRight: 6 }}>
      <div style={{ width: `${w}%`, height: '100%', background: color || '#88c' }} />
    </div>
  )
}

const TABLE_STYLE: React.CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 12 }

const VERSION_SOURCE_KEYS: Record<AppVersionSource, string> = {
  'VERSION': 'adminDash.versionSourceFile',
  'git describe': 'adminDash.versionSourceGit',
  'package.json': 'adminDash.versionSourcePackageJson',
  'unknown': 'adminDash.versionSourceUnknown',
}

// Where the running version was read from, next to it: a package.json
// fallback may lag the code actually running, so it says so on hover.
function VersionSourceNote({ source }: { source: AppVersionSource | undefined }) {
  const { t } = useI18n()
  if (!source) return null
  const key = VERSION_SOURCE_KEYS[source]
  const fallback = source === 'package.json'
  return (
    <span
      title={fallback ? t('adminDash.versionSourcePackageJsonHint') : undefined}
      style={{ marginLeft: 6, fontSize: 12, lineHeight: 1, color: fallback ? '#c66' : 'var(--color-text-faint)' }}
    >
      {key ? t(key) : `(${source})`}
    </span>
  )
}

// Visible state for the upstream version check. The backend never blocks on
// GitHub, so the dashboard sees four distinct states: disabled, not-yet-checked,
// check-failed, and checked. Without surfacing them, "no marker" silently
// covers all of them and operators can't tell "up to date" from "couldn't
// reach GitHub" or "haven't asked yet".
function UpdateCheckBadge({ update }: { update: ServerInfo['app_update'] | undefined }) {
  const { t } = useI18n()
  if (!update) return null
  const baseStyle: React.CSSProperties = { marginLeft: 6, fontSize: 12, lineHeight: 1 }
  if (!update.enabled) {
    return (
      <span title={t('adminDash.updateCheckDisabled')} style={{ ...baseStyle, color: 'var(--color-text-faint)' }}>
        {t('adminDash.updateCheckDisabled')}
      </span>
    )
  }
  // `last_attempt_at` is set on every fetch (success OR failure);
  // `checked_at` is set only on success. Using `last_attempt_at` for the
  // "pending" decision is what stops the badge from getting stuck on
  // "Checking…" forever when the GitHub fetch failed (in that state
  // checked_at stays null but an attempt has been recorded).
  if (!update.last_attempt_at) {
    return (
      <span title={t('adminDash.updateCheckPending')} style={{ ...baseStyle, color: 'var(--color-text-faint)' }}>
        {t('adminDash.updateCheckPending')}
      </span>
    )
  }
  if (!update.latest) {
    return (
      <span title={t('adminDash.updateCheckFailed')} style={{ ...baseStyle, color: '#c66' }}>
        ⚠ {t('adminDash.updateCheckFailed')}
      </span>
    )
  }
  if (update.update_available) {
    return (
      <span
        title={`${t('adminDash.updateAvailable')}: ${update.latest}`}
        style={{ ...baseStyle, color: '#f59e0b', fontSize: 14 }}
      >
        ● {t('adminDash.updateAvailable')}: v{update.latest.replace(/^v/, '')}
      </span>
    )
  }
  // A successful upstream fetch isn't enough to claim "up to date" — the
  // running version also has to be parseable as semver, otherwise the
  // backend can't actually compare it (dev builds with git-describe
  // hashes, untagged commits) and a green checkmark would be misleading.
  if (!update.comparable) {
    return (
      <span title={t('adminDash.updateCheckIncomparable')} style={{ ...baseStyle, color: 'var(--color-text-faint)' }}>
        {t('adminDash.updateCheckIncomparable')}
      </span>
    )
  }
  return (
    <span title={`${t('adminDash.upToDate')} (${update.latest})`} style={{ ...baseStyle, color: 'var(--color-success-fg, #393)' }}>
      ✓ {t('adminDash.upToDate')}
    </span>
  )
}

export default function AdminServerSection() {
  const { t, locale } = useI18n()
  // In the server's own offset (embedded in the string), in the UI language.
  const formatServerDateTime = (iso: string | null | undefined): string =>
    iso ? formatInEmbeddedOffset(iso, { locale }) : '—'
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null)
  const [serverInfoError, setServerInfoError] = useState(false)
  const [serverRefreshing, setServerRefreshing] = useState(false)

  const [stats, setStats] = useState<AdminStats | null>(null)
  const [statsError, setStatsError] = useState(false)
  const [statsLoading, setStatsLoading] = useState(false)

  // Collapse state for "deep" details
  const [cpuDetailsExpanded, setCpuDetailsExpanded] = useState(false)
  const [hugepagesExpanded, setHugepagesExpanded] = useState(false)
  const [osDetailsExpanded, setOsDetailsExpanded] = useState(false)
  const [otherMountsExpanded, setOtherMountsExpanded] = useState(false)
  const [diskIoExpanded, setDiskIoExpanded] = useState(false)
  const [netIoExpanded, setNetIoExpanded] = useState(false)

  // In-flight controllers — aborted on unmount and when a new fetch supersedes
  // the previous one, so stale responses can't overwrite fresh state.
  const statsCtrlRef = useRef<AbortController | null>(null)
  const serverInfoCtrlRef = useRef<AbortController | null>(null)
  // Caps the "first-load update-check" auto-poll so a wedged backend (or a
  // GitHub thread that never records last_attempt_at) can't drive an
  // endless refetch loop. The async GitHub fetch has a 4s timeout
  // server-side, so a handful of 2s polls covers both success and failure.
  const updatePollCountRef = useRef(0)

  const loadStats = useCallback(() => {
    statsCtrlRef.current?.abort()
    const ctrl = new AbortController()
    statsCtrlRef.current = ctrl
    setStatsError(false)
    setStatsLoading(true)
    adminGetStats({ signal: ctrl.signal })
      .then((s) => { if (!ctrl.signal.aborted) setStats(s) })
      .catch((err) => { if (!isAbortError(err)) setStatsError(true) })
      .finally(() => {
        if (statsCtrlRef.current === ctrl) statsCtrlRef.current = null
        if (!ctrl.signal.aborted) setStatsLoading(false)
      })
  }, [])

  const loadServerInfo = useCallback(() => {
    serverInfoCtrlRef.current?.abort()
    const ctrl = new AbortController()
    serverInfoCtrlRef.current = ctrl
    setServerInfoError(false)
    setServerRefreshing(true)
    adminGetServerInfo({ signal: ctrl.signal })
      .then((info) => {
        if (ctrl.signal.aborted) return
        setServerInfo(info)
      })
      .catch((err) => { if (!isAbortError(err)) setServerInfoError(true) })
      .finally(() => {
        if (serverInfoCtrlRef.current === ctrl) serverInfoCtrlRef.current = null
        if (!ctrl.signal.aborted) setServerRefreshing(false)
      })
  }, [])

  const refreshing = serverRefreshing || statsLoading

  const refreshAll = () => {
    loadServerInfo()
    loadStats()
  }

  useEffect(() => {
    loadServerInfo()
    loadStats()
    return () => {
      serverInfoCtrlRef.current?.abort()
      statsCtrlRef.current?.abort()
    }
  }, [loadServerInfo, loadStats])

  // The backend kicks off the GitHub version check in a background thread
  // and the first /server/info response returns before that thread has
  // recorded an attempt. Without polling, the badge would sit on
  // "Checking…" until the user clicks Refresh. Poll briefly until an
  // attempt is recorded; cap the count so a stuck thread can't loop forever.
  //
  // Depend on the whole `serverInfo` object, not just the extracted
  // `enabled`/`last_attempt_at` primitives: while the check is pending those
  // two stay `(true, null)` across consecutive polls, so a primitive-only
  // dependency list would compare equal and the effect would never re-run —
  // scheduling exactly one poll and leaving the 5-poll cap dead. Re-running on
  // every new response is what lets the loop continue until an attempt lands.
  useEffect(() => {
    const upd = serverInfo?.app_update
    if (!upd?.enabled) return
    if (upd.last_attempt_at) {
      updatePollCountRef.current = 0
      return
    }
    if (updatePollCountRef.current >= 5) return
    const t = setTimeout(() => {
      updatePollCountRef.current += 1
      loadServerInfo()
    }, 2000)
    return () => clearTimeout(t)
  }, [serverInfo, loadServerInfo])

  const slidesStats = stats?.slides
  const toursStats = stats?.tours
  const backupsStats = stats?.backups
  const disk = serverInfo?.disk_data_dir
  const memory = serverInfo?.memory
  const cpu = serverInfo?.cpu
  const memoryUsedPct = memory && memory.total_mb && memory.used_mb != null ? Math.round((memory.used_mb / memory.total_mb) * 100) : null
  // Summary card label is "available", so its subtitle must be a percentage
  // of available memory. used+available do NOT sum to total on Linux
  // (buffers/cached sit in between), so reusing memoryUsedPct here makes
  // the headline number and the % disagree on the same card.
  const memoryAvailPct = memory && memory.total_mb && memory.available_mb != null ? Math.round((memory.available_mb / memory.total_mb) * 100) : null
  const cgroup = serverInfo?.cgroup
  const showCgroup = Boolean(cgroup && (cgroup.in_container || cgroup.memory_max_bytes != null || cgroup.cpu_quota_us != null))
  const workers = serverInfo?.app_process.workers
  const nginx = serverInfo?.nginx_cache

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
        {/* When the snapshot below was taken: a failed refresh leaves the
            previous one on screen, and this says how old it is. */}
        {serverInfo?.os.server_time && (
          <span style={{ fontSize: 11, opacity: 0.65 }}>
            {t('adminDash.serverFetchedAt')}: {formatServerDateTime(serverInfo.os.server_time)}
          </span>
        )}
        <button onClick={refreshAll} disabled={refreshing} style={{ fontSize: 12 }}>
          {refreshing ? t('adminDash.serverRefreshing') : t('adminDash.serverRefresh')}
        </button>
      </div>

      {(serverInfoError || statsError) && (
        <div style={{ marginTop: 8, fontSize: 13, color: 'var(--color-danger-fg)' }}>
          {serverInfoError && <div>{t('adminDash.serverLoadFailed')}</div>}
          {statsError && <div>{t('adminDash.statsLoadFailed')}</div>}
        </div>
      )}

      {/* ── Summary cards ── */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginTop: 12 }}>
        <SummaryCard
          label={t('adminDash.statsSlidesSection')}
          value={slidesStats ? slidesStats.total.toLocaleString() : '—'}
          sub={slidesStats ? `${t('adminDash.statsPublished')} ${slidesStats.published.toLocaleString()} / ${t('adminDash.statsDraft')} ${slidesStats.draft.toLocaleString()}` : undefined}
        />
        <SummaryCard
          label={t('adminDash.statsTotalSize')}
          value={slidesStats ? formatBytes(slidesStats.total_file_bytes) : '—'}
          sub={slidesStats && slidesStats.total > 0 ? `${t('adminDash.statsAvgSize')} ${formatBytes(slidesStats.avg_file_bytes)}` : undefined}
        />
        <SummaryCard
          label={t('adminDash.statsToursSection')}
          value={toursStats ? toursStats.total.toLocaleString() : '—'}
          sub={toursStats ? `${t('adminDash.statsPublished')} ${toursStats.published.toLocaleString()} / ${t('adminDash.statsDraft')} ${toursStats.draft.toLocaleString()}` : undefined}
        />
        {/* The server omits backups (null) for non-admin viewers. */}
        {backupsStats !== null && (
          <SummaryCard
            label={t('adminDash.statsBackupsSection')}
            value={backupsStats ? backupsStats.count.toLocaleString() : '—'}
            sub={backupsStats ? `${t('adminDash.statsTotalSize')} ${formatBytes(backupsStats.total_size_bytes)}` : undefined}
          />
        )}
        <SummaryCard
          label={t('adminDash.serverDiskUsed')}
          value={disk && disk.total_gb != null ? `${disk.used_gb} / ${disk.total_gb} GB` : '—'}
          sub={disk && disk.total_gb != null ? `${t('adminDash.serverDiskFree')} ${disk.free_gb} GB (${disk.used_pct}%)` : undefined}
        />
        <SummaryCard
          label={t('adminDash.serverMemAvail')}
          value={memory && memory.available_mb != null && memory.total_mb != null ? `${memory.available_mb.toLocaleString()} / ${memory.total_mb.toLocaleString()} MB` : '—'}
          sub={memoryAvailPct != null ? `${memoryAvailPct}% ${t('adminDash.memAvailableSuffix')}` : undefined}
        />
        <SummaryCard
          label={t('adminDash.serverUptime')}
          value={serverInfo ? formatUptime(serverInfo.uptime_seconds) : '—'}
          sub={serverInfo ? `${t('adminDash.loadAbbrev')} ${serverInfo.load_avg.m1} / ${serverInfo.load_avg.m5} / ${serverInfo.load_avg.m15}` : undefined}
        />
      </div>

      {/* ── Two-column detail grid ── */}
      <div className="server-grid" style={{ display: 'grid', gap: 24, gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', marginTop: 8 }}>
        {/* Left: content statistics */}
        <section style={{ minWidth: 0 }}>
          {!stats && !statsError && (
            <div style={{ fontSize: 13, opacity: 0.7, marginTop: 14 }}>{t('adminDash.statsLoading')}</div>
          )}
          {slidesStats && (
            <>
              <SubHeading>{t('adminDash.statsSlidesSection')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.statsTotal')} value={slidesStats.total.toLocaleString()} />
                  <StatRow label={t('adminDash.statsTotalSize')} value={formatBytes(slidesStats.total_file_bytes)} />
                  <StatRow label={t('adminDash.statsAvgSize')} value={slidesStats.total > 0 ? formatBytes(slidesStats.avg_file_bytes) : '—'} />
                  <StatRow
                    label={t('adminDash.statsLargestFile')}
                    value={slidesStats.largest_file_bytes > 0 ? formatBytes(slidesStats.largest_file_bytes) : '—'}
                    sub={slidesStats.largest_file_name ? displayFilename(slidesStats.largest_file_name) : undefined}
                  />
                  <StatRow label={t('adminDash.statsWithMpp')}         value={slidesStats.with_mpp.toLocaleString()}         sub={`/ ${slidesStats.total} (${pct(slidesStats.with_mpp, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsWithoutMpp')}      value={slidesStats.without_mpp.toLocaleString()}      sub={`/ ${slidesStats.total} (${pct(slidesStats.without_mpp, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsWithCatalogId')}    value={slidesStats.with_catalog_id.toLocaleString()}    sub={`/ ${slidesStats.total} (${pct(slidesStats.with_catalog_id, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsWithoutCatalogId')} value={slidesStats.without_catalog_id.toLocaleString()} sub={`/ ${slidesStats.total} (${pct(slidesStats.without_catalog_id, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsPublished')}        value={slidesStats.published.toLocaleString()}        sub={`/ ${slidesStats.total} (${pct(slidesStats.published, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsDraft')}            value={slidesStats.draft.toLocaleString()}            sub={`/ ${slidesStats.total} (${pct(slidesStats.draft, slidesStats.total)})`} />
                </tbody>
              </table>

              {slidesStats.by_vendor.length > 0 && (
                <>
                  <SubHeading>{t('adminDash.statsByVendor')}</SubHeading>
                  <table style={TABLE_STYLE}>
                    <tbody>
                      {slidesStats.by_vendor.map(({ label, count }) => (
                        <StatRow key={label} label={label} value={count.toLocaleString()} sub={`(${pct(count, slidesStats.total)})`} />
                      ))}
                    </tbody>
                  </table>
                </>
              )}

              {slidesStats.by_extension.length > 0 && (
                <>
                  <SubHeading>{t('adminDash.statsByFormat')}</SubHeading>
                  <table style={TABLE_STYLE}>
                    <tbody>
                      {slidesStats.by_extension.map(({ label, count }) => (
                        <StatRow key={label} label={label} value={count.toLocaleString()} sub={`(${pct(count, slidesStats.total)})`} />
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}

          {toursStats && slidesStats && (
            <>
              <SubHeading>{t('adminDash.statsToursSection')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.statsTotal')}              value={toursStats.total.toLocaleString()} />
                  <StatRow label={t('adminDash.statsPublished')}          value={toursStats.published.toLocaleString()}          sub={`/ ${toursStats.total} (${pct(toursStats.published, toursStats.total)})`} />
                  <StatRow label={t('adminDash.statsDraft')}              value={toursStats.draft.toLocaleString()}              sub={`/ ${toursStats.total} (${pct(toursStats.draft, toursStats.total)})`} />
                  <StatRow label={t('adminDash.statsSlidesWithTours')}    value={toursStats.slides_with_tours.toLocaleString()}    sub={`/ ${slidesStats.total} (${pct(toursStats.slides_with_tours, slidesStats.total)})`} />
                  <StatRow label={t('adminDash.statsSlidesWithoutTours')} value={toursStats.slides_without_tours.toLocaleString()} sub={`/ ${slidesStats.total} (${pct(toursStats.slides_without_tours, slidesStats.total)})`} />
                </tbody>
              </table>
            </>
          )}
        </section>

        {/* Right: server / system / cache */}
        <section style={{ minWidth: 0 }}>
          {!serverInfo && !serverInfoError && (
            <div style={{ fontSize: 13, opacity: 0.7, marginTop: 14 }}>{t('adminDash.serverLoading')}</div>
          )}
          {serverInfo && cpu && memory && disk && (
            <>
              {/* CPU */}
              <SubHeading>{t('adminDash.cpuTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.cpuModel')} value={cpu.model || '—'} sub={cpu.vendor || undefined} />
                  <StatRow
                    label={t('adminDash.cpuPhysical') + ' / ' + t('adminDash.cpuLogical') + ' / ' + t('adminDash.cpuSockets')}
                    value={`${cpu.physical ?? '—'} / ${cpu.logical} / ${cpu.sockets ?? '—'}`}
                  />
                  <StatRow
                    label={t('adminDash.cpuFreqCurrent') + ' / ' + t('adminDash.cpuFreqMax')}
                    value={`${formatMHz(cpu.freq_current_mhz)} / ${formatMHz(cpu.freq_max_mhz)}`}
                  />
                  <StatRow
                    label={t('adminDash.cpuUsage')}
                    value={
                      cpu.usage_total_pct != null ? (
                        <span><ProgressBar pct={cpu.usage_total_pct} />{formatPct(cpu.usage_total_pct)}</span>
                      ) : '—'
                    }
                  />
                  <StatRow label={t('adminDash.serverLoadAvg')} value={`${serverInfo.load_avg.m1} / ${serverInfo.load_avg.m5} / ${serverInfo.load_avg.m15}`} />
                  {cpu.temperature_c != null && (
                    <StatRow label={t('adminDash.cpuTemperature')} value={`${cpu.temperature_c} °C`} />
                  )}
                </tbody>
              </table>

              <CollapsibleHeading expanded={cpuDetailsExpanded} onToggle={() => setCpuDetailsExpanded(e => !e)}>
                {t('adminDash.cpuDetails')}
              </CollapsibleHeading>
              {cpuDetailsExpanded && (
                <>
                  {cpu.usage_per_core_pct && cpu.usage_per_core_pct.length > 0 && (
                    <>
                      <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)', margin: '6px 0 4px' }}>{t('adminDash.cpuPerCoreUsage')}</div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', gap: '2px 8px', fontSize: 11, fontFamily: 'monospace', marginBottom: 8 }}>
                        {cpu.usage_per_core_pct.map((p, i) => (
                          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <span style={{ width: 28, opacity: 0.7 }}>#{i}</span>
                            <ProgressBar pct={p} />
                            <span style={{ width: 36, textAlign: 'right' }}>{formatPct(p, 0)}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                  <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)', margin: '6px 0 4px' }}>{t('adminDash.cpuCacheTitle')}</div>
                  <table style={TABLE_STYLE}>
                    <tbody>
                      <StatRow label={t('adminDash.cpuCacheL1d')} value={formatKb(cpu.cache.l1d_kb)} />
                      <StatRow label={t('adminDash.cpuCacheL1i')} value={formatKb(cpu.cache.l1i_kb)} />
                      <StatRow label={t('adminDash.cpuCacheL2')}  value={formatKb(cpu.cache.l2_kb)} />
                      <StatRow label={t('adminDash.cpuCacheL3')}  value={formatKb(cpu.cache.l3_kb)} />
                    </tbody>
                  </table>
                  {cpu.flags_subset.length > 0 && (
                    <>
                      <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)', margin: '8px 0 4px' }}>{t('adminDash.cpuFlagsTitle')}</div>
                      <div style={{ fontSize: 11, fontFamily: 'monospace', display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                        {cpu.flags_subset.map(f => (
                          <span key={f} style={{ padding: '1px 6px', background: 'var(--color-surface-alt, #f0f0f0)', borderRadius: 3 }}>{f}</span>
                        ))}
                      </div>
                    </>
                  )}
                </>
              )}

              {/* Memory */}
              <SubHeading>{t('adminDash.memoryTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.serverMemTotal')} value={formatMb(memory.total_mb)} />
                  <StatRow
                    label={t('adminDash.memUsed')}
                    value={
                      memory.used_mb != null
                        ? <span><ProgressBar pct={memoryUsedPct ?? 0} />{formatMb(memory.used_mb)}</span>
                        : '—'
                    }
                    sub={memoryUsedPct != null ? `(${memoryUsedPct}%)` : undefined}
                  />
                  <StatRow label={t('adminDash.serverMemAvail')} value={formatMb(memory.available_mb)} />
                  <StatRow label={t('adminDash.memBuffers')}     value={formatMb(memory.buffers_mb)} />
                  <StatRow label={t('adminDash.memCached')}      value={formatMb(memory.cached_mb)} />
                  {(memory.swap_total_mb ?? 0) > 0 && (
                    <>
                      <StatRow label={t('adminDash.swapTotal')} value={formatMb(memory.swap_total_mb)} />
                      <StatRow label={t('adminDash.swapUsed')}  value={formatMb(memory.swap_used_mb)} />
                      <StatRow label={t('adminDash.swapFree')}  value={formatMb(memory.swap_free_mb)} />
                    </>
                  )}
                </tbody>
              </table>
              {(memory.hugepages_total ?? 0) > 0 && (
                <>
                  <CollapsibleHeading expanded={hugepagesExpanded} onToggle={() => setHugepagesExpanded(e => !e)}>
                    {t('adminDash.hugepagesTitle')}
                  </CollapsibleHeading>
                  {hugepagesExpanded && (
                    <table style={TABLE_STYLE}>
                      <tbody>
                        <StatRow label={t('adminDash.hugepagesTotal')} value={formatNumber(memory.hugepages_total)} />
                        <StatRow label={t('adminDash.hugepagesSize')}  value={formatKb(memory.hugepages_size_kb)} />
                      </tbody>
                    </table>
                  )}
                </>
              )}

              {/* Storage */}
              <SubHeading>{t('adminDash.serverDiskTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.serverDiskPath')}  value={disk.path} />
                  <StatRow label={t('adminDash.diskFstype')}       value={disk.fstype || '—'} sub={disk.rotational == null ? undefined : (disk.rotational ? t('adminDash.diskRotationalHDD') : t('adminDash.diskRotationalSSD'))} />
                  <StatRow label={t('adminDash.serverDiskTotal')} value={formatGb(disk.total_gb)} />
                  <StatRow
                    label={t('adminDash.serverDiskUsed')}
                    value={disk.used_pct != null
                      ? <span><ProgressBar pct={disk.used_pct} color={disk.used_pct > 90 ? '#c66' : disk.used_pct > 75 ? '#cb6' : '#88c'} />{formatGb(disk.used_gb)}</span>
                      : '—'}
                    sub={disk.used_pct != null ? `(${disk.used_pct}%)` : undefined}
                  />
                  <StatRow label={t('adminDash.serverDiskFree')}  value={formatGb(disk.free_gb)} />
                  {disk.inodes_total != null && disk.inodes_free != null && (
                    <StatRow
                      label={t('adminDash.diskInodes')}
                      value={`${(disk.inodes_total - disk.inodes_free).toLocaleString()} / ${disk.inodes_total.toLocaleString()}`}
                      sub={disk.inodes_total > 0 ? `(${Math.round(((disk.inodes_total - disk.inodes_free) / disk.inodes_total) * 100)}%)` : undefined}
                    />
                  )}
                </tbody>
              </table>

              {serverInfo.other_mounts.length > 0 && (
                <>
                  <CollapsibleHeading expanded={otherMountsExpanded} onToggle={() => setOtherMountsExpanded(e => !e)}>
                    {t('adminDash.otherMountsTitle')} ({serverInfo.other_mounts.length})
                  </CollapsibleHeading>
                  {otherMountsExpanded && (
                    <table style={TABLE_STYLE}>
                      <thead>
                        <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                          <th style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.mountpoint')} / {t('adminDash.mountDevice')}</th>
                          <th style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.diskFstype')}</th>
                          <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.serverDiskUsed')}</th>
                          <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.serverDiskTotal')}</th>
                          <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.diskUsedPct')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {serverInfo.other_mounts.map(m => (
                          <tr key={m.mountpoint} style={{ borderBottom: '1px solid var(--color-border)' }}>
                            <td style={{ padding: '4px 6px', fontFamily: 'monospace', wordBreak: 'break-all' }}>{m.mountpoint}<br /><span style={{ fontSize: 10, opacity: 0.6 }}>{m.device}</span></td>
                            <td style={{ padding: '4px 6px', fontFamily: 'monospace', opacity: 0.8 }}>{m.fstype || '—'}</td>
                            <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{m.used_gb} GB</td>
                            <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{m.total_gb} GB</td>
                            <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{m.used_pct}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              )}

              {serverInfo.disk_io_total && (
                <>
                  <CollapsibleHeading expanded={diskIoExpanded} onToggle={() => setDiskIoExpanded(e => !e)}>
                    {t('adminDash.diskIoTitle')}
                  </CollapsibleHeading>
                  {diskIoExpanded && (
                    <table style={TABLE_STYLE}>
                      <tbody>
                        <StatRow
                          label={t('adminDash.diskIoRead')}
                          value={formatBytes(serverInfo.disk_io_total.read_bytes)}
                          sub={`${formatNumber(serverInfo.disk_io_total.read_count)} ${t('adminDash.unitOps')}`}
                        />
                        <StatRow
                          label={t('adminDash.diskIoWrite')}
                          value={formatBytes(serverInfo.disk_io_total.write_bytes)}
                          sub={`${formatNumber(serverInfo.disk_io_total.write_count)} ${t('adminDash.unitOps')}`}
                        />
                      </tbody>
                    </table>
                  )}
                </>
              )}

              {/* OS / system */}
              <SubHeading>{t('adminDash.serverSpecsTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.osDistro')}        value={serverInfo.os.distro_pretty_name || `${serverInfo.os.system} ${serverInfo.os.release}`} />
                  <StatRow label={t('adminDash.serverKernel')}    value={serverInfo.os.version} />
                  <StatRow label={t('adminDash.serverArch')}      value={serverInfo.os.machine} />
                  <StatRow label={t('adminDash.serverHostname')}  value={serverInfo.hostname} />
                  {serverInfo.fqdn && serverInfo.fqdn !== serverInfo.hostname && (
                    <StatRow label={t('adminDash.osFqdn')}        value={serverInfo.fqdn} />
                  )}
                  <StatRow label={t('adminDash.osBootTime')}      value={formatServerDateTime(serverInfo.os.boot_time)} />
                  <StatRow label={t('adminDash.serverUptime')}    value={formatUptime(serverInfo.uptime_seconds)} />
                  <StatRow
                    label={t('adminDash.serverTimezone')}
                    value={serverInfo.os.tz_name || serverInfo.os.timezone || '—'}
                    sub={serverInfo.os.utc_offset ? `UTC${serverInfo.os.utc_offset}` : undefined}
                  />
                  <StatRow label={t('adminDash.osServerTime')}    value={formatServerDateTime(serverInfo.os.server_time)} />
                  <StatRow
                    label={t('adminDash.osVirtualization')}
                    value={serverInfo.os.virtualization === 'bare-metal' ? t('adminDash.virtBareMetal') : serverInfo.os.virtualization}
                  />
                </tbody>
              </table>
              <CollapsibleHeading expanded={osDetailsExpanded} onToggle={() => setOsDetailsExpanded(e => !e)}>
                {t('adminDash.osDetails')}
              </CollapsibleHeading>
              {osDetailsExpanded && (
                <table style={TABLE_STYLE}>
                  <tbody>
                    <StatRow label={t('adminDash.osGlibc')} value={serverInfo.os.glibc_version || '—'} />
                    <StatRow label={t('adminDash.dmiVendor')}  value={serverInfo.dmi.sys_vendor || '—'} />
                    <StatRow label={t('adminDash.dmiProduct')} value={serverInfo.dmi.product_name || '—'} />
                    <StatRow label={t('adminDash.dmiVersion')} value={serverInfo.dmi.product_version || '—'} />
                  </tbody>
                </table>
              )}

              {/* App process */}
              <SubHeading>{t('adminDash.appProcessTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow
                    label={t('adminDash.processVersion')}
                    value={
                      <>
                        v{serverInfo.app_process.app_version}
                        <VersionSourceNote source={serverInfo.app_process.app_version_source} />
                        <UpdateCheckBadge update={serverInfo.app_update} />
                        {serverInfo.app_process.db_app_version
                          && serverInfo.app_process.db_app_version !== serverInfo.app_process.app_version && (
                          <span
                            title={`${t('adminDash.dbVersionMismatch')} (${t('adminDash.dbAppVersion')}: v${serverInfo.app_process.db_app_version})`}
                            style={{ color: '#c66', marginLeft: 6, fontSize: 12, lineHeight: 1 }}
                          >
                            ⚠ {t('adminDash.dbAppVersion')}: v{serverInfo.app_process.db_app_version}
                          </span>
                        )}
                      </>
                    }
                    sub={
                      serverInfo.app_update?.checked_at
                        ? `${t('adminDash.lastCheckedAt')}: ${formatServerDateTime(serverInfo.app_update.checked_at)}`
                        : undefined
                    }
                  />
                  <StatRow label={t('adminDash.processPid')}        value={serverInfo.app_process.pid} />
                  <StatRow label={t('adminDash.processThreads')}    value={formatNumber(serverInfo.app_process.threads)} />
                  <StatRow label={t('adminDash.processFds')}        value={formatNumber(serverInfo.app_process.fds)} />
                  <StatRow label={t('adminDash.processVmRss')}      value={serverInfo.app_process.rss_mb != null ? `${serverInfo.app_process.rss_mb.toLocaleString()} MB` : '—'} />
                  <StatRow label={t('adminDash.processVmPeak')}     value={serverInfo.app_process.vm_peak_mb != null ? `${serverInfo.app_process.vm_peak_mb.toLocaleString()} MB` : '—'} />
                  <StatRow label={t('adminDash.processVmSize')}     value={serverInfo.app_process.vm_size_mb != null ? `${serverInfo.app_process.vm_size_mb.toLocaleString()} MB` : '—'} />
                  <StatRow label={t('adminDash.processStartedAt')}  value={formatServerDateTime(serverInfo.app_process.started_at)} />
                  <StatRow label={t('adminDash.processPython')}     value={serverInfo.app_process.python_version} />
                  <StatRow label={t('adminDash.processDb')}         value={serverInfo.db.version ? `${serverInfo.db.kind} ${serverInfo.db.version}` : serverInfo.db.kind} />
                  <StatRow label={t('adminDash.processVisibleCount')} value={formatNumber(serverInfo.system_processes.process_count)} />
                </tbody>
              </table>
              <div style={{ fontSize: 11, opacity: 0.55, marginTop: 4 }}>{t('adminDash.processWorkerNote')}</div>
              {workers && workers.items.length > 0 && (
                <>
                  <SubHeading>{t('adminDash.workersTitle', { count: workers.count })}</SubHeading>
                  <table style={TABLE_STYLE}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                        <th style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.processPid')}</th>
                        <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.processVmRss')}</th>
                        <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.processThreads')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {workers.items.map(w => (
                        <tr key={w.pid} style={{ borderBottom: '1px solid var(--color-border)' }}>
                          <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>
                            {w.pid}
                            {w.current && <span style={{ marginLeft: 6, fontSize: 10, opacity: 0.6 }}>({t('adminDash.workerThisRequest')})</span>}
                          </td>
                          <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{formatMb(w.rss_mb)}</td>
                          <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{formatNumber(w.threads)}</td>
                        </tr>
                      ))}
                      <tr>
                        <td style={{ padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.workersRssTotal')}</td>
                        <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{formatMb(workers.rss_mb_total)}</td>
                        <td />
                      </tr>
                    </tbody>
                  </table>
                </>
              )}

              {/* Network */}
              <SubHeading>{t('adminDash.serverNetworkTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                    {[t('adminDash.serverIfName'), t('adminDash.netState'), t('adminDash.netSpeed'), t('adminDash.serverIfIPv4'), t('adminDash.serverIfNetmask'), t('adminDash.serverIfIPv6')].map((h) => (
                      <th key={h} style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {serverInfo.interfaces.map((iface) => (
                    <tr key={iface.name} style={{ borderBottom: '1px solid var(--color-border)' }}>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>{iface.name}</td>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace', color: iface.link_state === 'up' ? 'var(--color-success-fg, #393)' : iface.link_state === 'down' ? 'var(--color-text-faint)' : undefined }}>{iface.link_state}</td>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>{iface.speed_mbps != null ? `${iface.speed_mbps} Mb/s` : '—'}</td>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>{iface.ipv4 ?? '—'}</td>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>{iface.netmask ?? '—'}</td>
                      <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>
                        {iface.ipv6.length === 0
                          ? '—'
                          : iface.ipv6.map((a) => <div key={a}>{a}</div>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <CollapsibleHeading expanded={netIoExpanded} onToggle={() => setNetIoExpanded(e => !e)}>
                {t('adminDash.netIoTitle')}
              </CollapsibleHeading>
              {netIoExpanded && (
                <table style={TABLE_STYLE}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                      <th style={{ textAlign: 'left', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.serverIfName')}</th>
                      <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.netIoRecv')}</th>
                      <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.netIoSent')}</th>
                      <th style={{ textAlign: 'right', padding: '4px 6px', fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.netIoErrors')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {serverInfo.interfaces.filter(i => i.io).map((iface) => (
                      <tr key={iface.name} style={{ borderBottom: '1px solid var(--color-border)' }}>
                        <td style={{ padding: '4px 6px', fontFamily: 'monospace' }}>{iface.name}</td>
                        <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{formatBytes(iface.io!.bytes_recv)}<br /><span style={{ fontSize: 10, opacity: 0.6 }}>{formatNumber(iface.io!.packets_recv)} {t('adminDash.unitPkt')}</span></td>
                        <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{formatBytes(iface.io!.bytes_sent)}<br /><span style={{ fontSize: 10, opacity: 0.6 }}>{formatNumber(iface.io!.packets_sent)} {t('adminDash.unitPkt')}</span></td>
                        <td style={{ padding: '4px 6px', fontFamily: 'monospace', textAlign: 'right' }}>{(iface.io!.errin + iface.io!.errout).toLocaleString()} / {(iface.io!.dropin + iface.io!.dropout).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {/* cgroup limits: a container's, or the service unit's own
                  (systemd MemoryMax= / CPUQuota=) on a plain host */}
              {showCgroup && cgroup && (
                <>
                  <SubHeading>{t('adminDash.cgroupTitle')}</SubHeading>
                  <table style={TABLE_STYLE}>
                    <tbody>
                      {cgroup.in_container && (
                        <StatRow label={t('adminDash.cgroupContainerKind')} value={cgroup.container_kind || '—'} />
                      )}
                      {cgroup.cgroup_path && (
                        <StatRow label={t('adminDash.cgroupPath')} value={cgroup.cgroup_path} />
                      )}
                      <StatRow
                        label={t('adminDash.cgroupMemoryMax')}
                        value={serverInfo.cgroup.memory_max_bytes != null ? formatBytes(serverInfo.cgroup.memory_max_bytes) : t('adminDash.cgroupUnlimited')}
                      />
                      <StatRow
                        label={t('adminDash.cgroupCpuQuota')}
                        value={
                          serverInfo.cgroup.cpu_quota_us != null && serverInfo.cgroup.cpu_period_us
                            ? `${(serverInfo.cgroup.cpu_quota_us / serverInfo.cgroup.cpu_period_us).toFixed(2)} CPU`
                            : t('adminDash.cgroupUnlimited')
                        }
                      />
                    </tbody>
                  </table>
                </>
              )}

              {/* nginx tile cache */}
              <SubHeading>{t('adminDash.serverNginxCacheTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.serverNginxCachePath')}     value={serverInfo.nginx_cache.path} />
                  <StatRow label={t('adminDash.serverNginxCacheMaxSize')}  value={serverInfo.nginx_cache.max_size ?? t('adminDash.serverNginxCacheNA')} />
                  <StatRow label={t('adminDash.serverNginxCacheInactive')} value={serverInfo.nginx_cache.inactive  ?? t('adminDash.serverNginxCacheNA')} />
                  <StatRow
                    label={t('adminDash.serverNginxCacheUsed')}
                    value={serverInfo.nginx_cache.used_bytes != null ? formatBytes(serverInfo.nginx_cache.used_bytes) : t('adminDash.serverNginxCacheNA')}
                    sub={nginx?.partial ? t('adminDash.serverNginxCachePartial') : undefined}
                  />
                  <StatRow
                    label={t('adminDash.serverNginxCacheFiles')}
                    value={serverInfo.nginx_cache.file_count != null ? formatNumber(serverInfo.nginx_cache.file_count) : t('adminDash.serverNginxCacheNA')}
                    sub={nginx?.partial ? t('adminDash.serverNginxCachePartial') : undefined}
                  />
                  {nginx?.stat_updated_at && (
                    <StatRow
                      label={t('adminDash.serverNginxCacheUpdated')}
                      value={formatServerDateTime(nginx.stat_updated_at)}
                      sub={nginx.stat_stale && nginx.stat_age_seconds != null ? (
                        <span style={{ color: '#c66', opacity: 1 }}>
                          ⚠ {t('adminDash.serverNginxCacheStale', { age: formatUptime(nginx.stat_age_seconds) })}
                        </span>
                      ) : undefined}
                    />
                  )}
                </tbody>
              </table>

              {/* WSI handle cache (read-only snapshot of the worker that
                  served this request — see serverCacheNote) */}
              <SubHeading>{t('adminDash.serverCacheTitle')}</SubHeading>
              <table style={TABLE_STYLE}>
                <tbody>
                  <StatRow label={t('adminDash.serverCacheEntries')} value={`${serverInfo.dzi_cache.entries} / ${serverInfo.dzi_cache.max}`} />
                  {serverInfo.dzi_cache.max_bytes > 0 && (
                    <StatRow
                      label={t('adminDash.serverCacheBytes')}
                      value={`${formatBytes(serverInfo.dzi_cache.bytes)} / ${formatBytes(serverInfo.dzi_cache.max_bytes)}`}
                      sub={`(${Math.round((serverInfo.dzi_cache.bytes / serverInfo.dzi_cache.max_bytes) * 100)}%)`}
                    />
                  )}
                </tbody>
              </table>
              <div style={{ fontSize: 11, opacity: 0.55, marginTop: 4 }}>{t('adminDash.serverCacheNote')}</div>
            </>
          )}
        </section>
      </div>

      <style>{`
        @media (max-width: 960px) {
          .server-grid {
            grid-template-columns: minmax(0, 1fr) !important;
          }
        }
      `}</style>
    </>
  )
}
