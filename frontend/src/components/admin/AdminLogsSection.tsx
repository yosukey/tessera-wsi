import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  adminClearErrorLogs,
  adminDownloadErrorLogsCsv,
  adminGetErrorLog,
  adminGetErrorLogs,
  isAbortError,
  type ErrorLogItem,
  type ErrorLogResponse,
} from '../../lib/api'
import { Button, Input, Select } from '../ui'
import { useI18n } from '../../lib/i18n'
import { formatInServerTz, serverTzLabel } from '../../lib/admin/datetime'
import { apiErrorMessageOr } from '../../lib/admin/apiError'
import { formatBytes } from '../../lib/admin/validation'
import { useCsvExport, usePagedReport, type PageQuery } from '../../lib/admin/usePagedReport'

const PAGE_SIZE = 50

// Levels offered in the filter dropdown. The stored level is rendered raw
// (uppercased by the backend) so an unexpected level still shows through.
const LEVELS = ['ERROR', 'WARNING', 'CRITICAL', 'INFO', 'DEBUG'] as const

// The newest log id this browser has listed: rows above it get a NEW badge on
// the next visit. Per-browser convenience only, so storage may be missing.
const LAST_SEEN_KEY = 'wsi.adminLogs.lastSeenId'

function readLastSeen(): number | null {
  try {
    const v = Number(window.localStorage.getItem(LAST_SEEN_KEY))
    return Number.isFinite(v) && v > 0 ? v : null
  } catch {
    return null
  }
}

function storeLastSeen(id: number) {
  try {
    window.localStorage.setItem(LAST_SEEN_KEY, String(id))
  } catch { /* storage unavailable */ }
}

const LEVEL_COLORS: Record<string, { bg: string; fg: string }> = {
  ERROR: { bg: 'var(--color-danger-bg, #fee2e2)', fg: 'var(--color-danger-fg, #b91c1c)' },
  CRITICAL: { bg: 'var(--color-danger-bg, #fee2e2)', fg: 'var(--color-danger-fg, #b91c1c)' },
  WARNING: { bg: 'var(--color-warning-bg)', fg: 'var(--color-warning-fg)' },
}

const TH: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: 'var(--color-text-faint)',
}
const TD: React.CSSProperties = {
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 12,
  verticalAlign: 'top',
}

function LevelChip({ level }: { level: string }) {
  const c = LEVEL_COLORS[level] ?? { bg: 'var(--color-surface-2, var(--color-surface))', fg: 'var(--color-text-faint)' }
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 10,
      background: c.bg,
      color: c.fg,
      fontSize: 10,
      fontWeight: 700,
      whiteSpace: 'nowrap',
      border: '1px solid var(--color-border)',
    }}>{level}</span>
  )
}

// The traceback is not in the listing (up to 16 KB a row): it is fetched the
// first time the row is expanded, and kept for later toggles.
function MessageCell({ item }: { item: ErrorLogItem }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [traceback, setTraceback] = useState<string | null>(item.traceback ?? null)
  const [tbLoading, setTbLoading] = useState(false)
  const [tbError, setTbError] = useState('')
  const ctrlRef = useRef<AbortController | null>(null)
  useEffect(() => () => ctrlRef.current?.abort(), [])
  const where = [item.module, item.func].filter(Boolean).join('.')

  function toggle() {
    const next = !open
    setOpen(next)
    if (!next || traceback !== null || tbLoading) return
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    setTbLoading(true)
    setTbError('')
    adminGetErrorLog(item.id, ctrl.signal)
      .then(full => setTraceback(full.traceback ?? ''))
      .catch(err => {
        if (!isAbortError(err)) setTbError(apiErrorMessageOr(err, t, 'adminLogs.tracebackFailed'))
      })
      .finally(() => { if (!ctrl.signal.aborted) setTbLoading(false) })
  }

  return (
    <div>
      <div style={{ fontFamily: 'monospace', fontSize: 11, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {item.message || <span style={{ opacity: 0.4 }}>—</span>}
      </div>
      {(where || item.line != null || item.request_method) && (
        <div style={{ fontSize: 10, opacity: 0.55, marginTop: 2 }}>
          {where}{item.line != null ? `:${item.line}` : ''}
          {item.request_method && (
            <span style={{ marginLeft: where || item.line != null ? 8 : 0 }}>
              {item.request_method} {item.request_path}
            </span>
          )}
        </div>
      )}
      {item.has_traceback && (
        <div style={{ marginTop: 2 }}>
          <button
            type="button"
            onClick={toggle}
            aria-expanded={open}
            style={{
              background: 'none', border: 'none', padding: 0, cursor: 'pointer',
              color: 'var(--color-brand-700)', fontSize: 11, textDecoration: 'underline',
            }}
          >
            {open ? t('adminLogs.hideTraceback') : t('adminLogs.showTraceback')}
          </button>
          {open && tbLoading && (
            <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>{t('adminLogs.tracebackLoading')}</div>
          )}
          {open && tbError && (
            <div style={{ fontSize: 11, color: 'var(--color-danger-fg, #b91c1c)', marginTop: 4 }}>{tbError}</div>
          )}
          {open && traceback !== null && (
            <pre style={{
              margin: '4px 0 0', padding: '6px 8px',
              background: 'var(--color-surface)', border: '1px solid var(--color-border)',
              borderRadius: 4, fontSize: 10, maxWidth: 640,
              whiteSpace: 'pre-wrap', wordBreak: 'break-all',
            }}>{traceback}</pre>
          )}
        </div>
      )}
    </div>
  )
}

export default function AdminLogsSection() {
  const { t, locale } = useI18n()

  // Draft filter inputs; applied (= fetch-triggering) state is separate so
  // half-typed dates don't fire requests (same pattern as the history view).
  const [draftLevel, setDraftLevel] = useState('')
  const [draftLogger, setDraftLogger] = useState('')
  const [draftSearch, setDraftSearch] = useState('')
  const [draftSince, setDraftSince] = useState('')
  const [draftUntil, setDraftUntil] = useState('')
  const [applied, setApplied] = useState({ level: '', logger: '', search: '', since: '', until: '' })
  const [includeTraceback, setIncludeTraceback] = useState(true)
  const [clearing, setClearing] = useState(false)
  const [notice, setNotice] = useState('')
  // Read once: the badge marks what is new since the previous visit, even
  // though this visit's newest id is stored as soon as the list loads.
  const [lastSeen, setLastSeen] = useState(readLastSeen)

  const fetchPage = useCallback((q: PageQuery, signal: AbortSignal) => adminGetErrorLogs({
    ...q,
    level: applied.level || undefined,
    logger: applied.logger || undefined,
    search: applied.search || undefined,
    since: applied.since || undefined,
    until: applied.until || undefined,
  }, signal), [applied])
  const report = usePagedReport<ErrorLogItem, ErrorLogResponse>(
    fetchPage,
    JSON.stringify(applied),
    err => apiErrorMessageOr(err, t, 'adminLogs.loadFailed'),
    PAGE_SIZE,
  )
  const { items, total, newer, snapshotId, serverTz, offset, setOffset, loading, error, reload } = report
  const levelCounts = report.response?.level_counts ?? {}

  useEffect(() => {
    if (!snapshotId) return
    if (lastSeen !== null && snapshotId < lastSeen) {
      // Ids went backwards: the database was replaced (a restore) or reset
      // since the last visit, so the stored id says nothing about these rows.
      setLastSeen(null)
      storeLastSeen(snapshotId)
    } else if (lastSeen === null || snapshotId > (readLastSeen() ?? 0)) {
      storeLastSeen(snapshotId)
    }
  }, [snapshotId, lastSeen])

  const csv = useCsvExport(err => apiErrorMessageOr(err, t, 'adminLogs.exportFailed'))

  function applyFilters() {
    setNotice('')
    setApplied({
      level: draftLevel,
      logger: draftLogger.trim(),
      search: draftSearch.trim(),
      since: draftSince,
      until: draftUntil,
    })
    // The same filters again still mean "fetch now".
    reload()
  }

  function applyLevel(level: string) {
    setDraftLevel(level)
    setNotice('')
    setApplied(a => ({ ...a, level }))
  }

  function exportCsv() {
    // Export the applied filters, not the half-typed drafts, so the
    // download matches what the table currently shows.
    void csv.start(opts => adminDownloadErrorLogsCsv({
      level: applied.level || undefined,
      logger: applied.logger || undefined,
      search: applied.search || undefined,
      since: applied.since || undefined,
      until: applied.until || undefined,
      include_traceback: includeTraceback,
    }, opts))
  }

  async function clearLogs() {
    if (snapshotId == null || clearing) return
    if (!window.confirm(t('adminLogs.clearConfirm'))) return
    setClearing(true)
    setNotice('')
    try {
      const res = await adminClearErrorLogs(snapshotId)
      setNotice(t('adminLogs.cleared', { count: res.deleted }))
      reload()
    } catch (err) {
      report.setError(apiErrorMessageOr(err, t, 'adminLogs.clearFailed'))
    } finally {
      setClearing(false)
    }
  }

  function clearFilters() {
    setDraftLevel('')
    setDraftLogger('')
    setDraftSearch('')
    setDraftSince('')
    setDraftUntil('')
    setNotice('')
    setApplied({ level: '', logger: '', search: '', since: '', until: '' })
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilter = useMemo(
    () => Boolean(applied.level || applied.logger || applied.search || applied.since || applied.until),
    [applied],
  )
  const tzLabel = serverTzLabel(serverTz)
  const levelCountEntries = Object.entries(levelCounts).sort((a, b) => b[1] - a[1])
  const shownError = error || csv.error

  const labelStyle: React.CSSProperties = {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.05em', color: 'var(--color-text-faint)',
    display: 'block', marginBottom: 2,
  }

  return (
    <div>
      <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 'var(--space-3)' }}>
        {t('adminLogs.description')}
      </div>

      {/* Filter bar */}
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 'var(--space-3)', alignItems: 'flex-end',
        padding: 'var(--space-3)', border: '1px solid var(--color-border)',
        borderRadius: 6, background: 'var(--color-surface)', marginBottom: 'var(--space-4)',
      }}>
        <div>
          <label style={labelStyle}>{t('adminLogs.filterLevel')}</label>
          <Select value={draftLevel} onChange={e => setDraftLevel(e.target.value)}>
            <option value="">{t('adminLogs.allLevels')}</option>
            {LEVELS.map(l => (
              <option key={l} value={l}>{l}</option>
            ))}
          </Select>
        </div>
        <div>
          <label style={labelStyle}>{t('adminLogs.filterLogger')}</label>
          <Input
            value={draftLogger}
            onChange={e => setDraftLogger(e.target.value)}
            placeholder={t('adminLogs.filterLoggerPlaceholder')}
            style={{ width: 200 }}
          />
        </div>
        <div>
          <label style={labelStyle}>{t('adminLogs.filterSearch')}</label>
          <Input
            value={draftSearch}
            onChange={e => setDraftSearch(e.target.value)}
            placeholder={t('adminLogs.filterSearchPlaceholder')}
            style={{ width: 220 }}
          />
        </div>
        <div>
          <label style={labelStyle}>{t('adminLogs.filterFrom')}</label>
          <Input type="date" value={draftSince} onChange={e => setDraftSince(e.target.value)} />
        </div>
        <div>
          <label style={labelStyle}>{t('adminLogs.filterTo')}</label>
          <Input type="date" value={draftUntil} onChange={e => setDraftUntil(e.target.value)} />
        </div>
        <Button size="sm" onClick={applyFilters}>{t('adminLogs.applyFilters')}</Button>
        {hasFilter && (
          <Button size="sm" variant="ghost" onClick={clearFilters}>{t('adminLogs.clearFilters')}</Button>
        )}
        <Button size="sm" variant="ghost" onClick={reload} disabled={loading}>{t('adminLogs.refresh')}</Button>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2, 8px)', alignItems: 'center', flexWrap: 'wrap' }}>
          <label style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
            <input
              type="checkbox"
              checked={includeTraceback}
              onChange={e => setIncludeTraceback(e.target.checked)}
              disabled={csv.exporting}
            />
            {t('adminLogs.includeTraceback')}
          </label>
          <Button
            size="sm"
            variant="secondary"
            onClick={exportCsv}
            disabled={csv.exporting}
            title={t('adminLogs.exportCsvHint')}
          >
            {csv.exporting
              ? (csv.received > 0 ? t('adminLogs.exportProgress', { size: formatBytes(csv.received) }) : t('adminLogs.exporting'))
              : t('adminLogs.exportCsv')}
          </Button>
          {csv.exporting && (
            <Button size="sm" variant="ghost" onClick={csv.cancel}>{t('adminLogs.cancelExport')}</Button>
          )}
          <Button
            size="sm"
            variant="danger"
            onClick={clearLogs}
            // Not gated on the filtered total: the clear ignores the filters,
            // and the server answers 0 when there is nothing left to clear.
            disabled={clearing || !snapshotId}
            title={t('adminLogs.clearHint')}
          >
            {clearing ? t('adminLogs.clearing') : t('adminLogs.clear')}
          </Button>
        </div>
      </div>

      {shownError && (
        <div style={{ color: 'var(--color-danger-fg, #b91c1c)', fontSize: 12, marginBottom: 'var(--space-3)' }}>
          {shownError}
        </div>
      )}
      {notice && (
        <div role="status" style={{ fontSize: 12, marginBottom: 'var(--space-3)' }}>{notice}</div>
      )}
      {newer > 0 && (
        <div role="status" style={{
          display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
          padding: '4px 8px', marginBottom: 'var(--space-3)',
          border: '1px solid var(--color-border)', borderRadius: 4, background: 'var(--color-surface)',
        }}>
          {t('adminLogs.newerEntries', { count: newer })}
          <Button size="sm" variant="ghost" onClick={reload}>{t('adminLogs.showNewer')}</Button>
        </div>
      )}

      {loading && items.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminLogs.loading')}</div>
      ) : items.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminLogs.empty')}</div>
      ) : (
        <>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, fontSize: 11, marginBottom: 4 }}>
            <span style={{ opacity: 0.65 }}>{t('adminLogs.totalCount', { count: total })}</span>
            {levelCountEntries.length > 0 && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
                <span style={{ opacity: 0.65 }}>{t('adminLogs.levelCountsLabel')}:</span>
                {levelCountEntries.map(([lvl, n]) => (
                  <button
                    key={lvl}
                    type="button"
                    onClick={() => applyLevel(applied.level === lvl ? '' : lvl)}
                    aria-pressed={applied.level === lvl}
                    style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 2 }}
                  >
                    <LevelChip level={lvl} />
                    <span style={{ fontFamily: 'monospace', fontWeight: applied.level === lvl ? 700 : 400 }}>{n.toLocaleString()}</span>
                  </button>
                ))}
              </span>
            )}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={{ ...TH, width: 150 }}>{t('adminLogs.colTime')}{tzLabel && (
                    // The heading style uppercases; a zone name must keep its case.
                    <>{' '}<span style={{ textTransform: 'none' }}>({tzLabel})</span></>
                  )}</th>
                  <th style={{ ...TH, width: 80 }}>{t('adminLogs.colLevel')}</th>
                  <th style={{ ...TH, width: 180 }}>{t('adminLogs.colLogger')}</th>
                  <th style={TH}>{t('adminLogs.colMessage')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.id}>
                    <td style={{ ...TD, whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: 11 }}>
                      {formatInServerTz(item.occurred_at, serverTz, locale, { weekday: false })}
                      {lastSeen !== null && item.id > lastSeen && (
                        <span
                          title={t('adminLogs.newBadgeHint')}
                          style={{ marginLeft: 4, fontSize: 9, fontWeight: 700, color: 'var(--color-brand-700)' }}
                        >
                          {t('adminLogs.newBadge')}
                        </span>
                      )}
                    </td>
                    <td style={TD}><LevelChip level={item.level} /></td>
                    <td style={{ ...TD, fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>
                      {item.logger}
                    </td>
                    <td style={TD}><MessageCell item={item} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Pagination */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginTop: 'var(--space-3)' }}>
            <Button
              size="sm"
              variant="secondary"
              disabled={offset === 0 || loading}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              {t('adminLogs.prevPage')}
            </Button>
            <span style={{ fontSize: 12, opacity: 0.7 }}>
              {t('adminLogs.pageOf', { page, pageCount })}
            </span>
            <Button
              size="sm"
              variant="secondary"
              disabled={offset + PAGE_SIZE >= total || loading}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t('adminLogs.nextPage')}
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
