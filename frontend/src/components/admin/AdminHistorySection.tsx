import React, { useCallback, useMemo, useState } from 'react'
import {
  adminDownloadAuditLogsCsv,
  adminGetAuditLogs,
  type AuditLogItem,
  type AuditLogResponse,
} from '../../lib/api'
import { Button, Input, Select } from '../ui'
import { useI18n } from '../../lib/i18n'
import { formatInServerTz, serverTzLabel } from '../../lib/admin/datetime'
import { apiErrorMessageOr } from '../../lib/admin/apiError'
import { formatBytes } from '../../lib/admin/validation'
import { useCsvExport, usePagedReport, type PageQuery } from '../../lib/admin/usePagedReport'

const PAGE_SIZE = 50

// First segment of the action string ("slide.upload" → "slide"). The
// category list is fixed and translated; individual action strings are
// rendered raw (monospace) so new audit() call sites show up without
// needing an i18n entry per action.
const CATEGORIES = [
  'slide', 'tour', 'metadata', 'viewer', 'network', 'auth', 'user',
  'role_permissions', 'admin', 'app', 'backup', 'restore', 'reset', 'sync',
  'analytics', 'perf', 'system',
] as const

const CATEGORY_COLORS: Record<string, { bg: string; fg: string }> = {
  slide: { bg: 'var(--color-brand-50)', fg: 'var(--color-brand-700)' },
  tour: { bg: 'var(--color-info-bg)', fg: 'var(--color-info-fg)' },
  backup: { bg: 'var(--color-success-bg)', fg: 'var(--color-success-fg)' },
  restore: { bg: 'var(--color-warning-bg)', fg: 'var(--color-warning-fg)' },
  reset: { bg: 'var(--color-warning-bg)', fg: 'var(--color-warning-fg)' },
  system: { bg: 'var(--color-warning-bg)', fg: 'var(--color-warning-fg)' },
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

function categoryOf(action: string): string {
  const dot = action.indexOf('.')
  return dot > 0 ? action.slice(0, dot) : action
}

function CategoryChip({ action }: { action: string }) {
  const { t } = useI18n()
  const cat = categoryOf(action)
  const c = CATEGORY_COLORS[cat] ?? { bg: 'var(--color-surface-2, var(--color-surface))', fg: 'var(--color-text-faint)' }
  const known = (CATEGORIES as readonly string[]).includes(cat)
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 10,
      background: c.bg,
      color: c.fg,
      fontSize: 10,
      fontWeight: 600,
      whiteSpace: 'nowrap',
      border: '1px solid var(--color-border)',
    }}>{known ? t(`adminHistory.cat.${cat}`) : cat}</span>
  )
}

function DetailsCell({ item }: { item: AuditLogItem }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const keys = Object.keys(item.details)
  if (keys.length === 0) return <span style={{ opacity: 0.4 }}>—</span>
  return (
    <div>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
          color: 'var(--color-brand-700)', fontSize: 11, textDecoration: 'underline',
        }}
      >
        {open ? t('adminHistory.hideDetails') : t('adminHistory.showDetails', { count: keys.length })}
      </button>
      {open && (
        <pre style={{
          margin: '4px 0 0', padding: '6px 8px',
          background: 'var(--color-surface)', border: '1px solid var(--color-border)',
          borderRadius: 4, fontSize: 10, maxWidth: 480,
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>{JSON.stringify(item.details, null, 2)}</pre>
      )}
    </div>
  )
}

export default function AdminHistorySection() {
  const { t, locale } = useI18n()

  // Draft filter inputs; applied (= fetch-triggering) state is separate so
  // half-typed dates don't fire requests (same pattern as the analytics
  // custom range).
  const [draftCategory, setDraftCategory] = useState('')
  const [draftActor, setDraftActor] = useState('')
  const [draftTarget, setDraftTarget] = useState('')
  const [draftSystem, setDraftSystem] = useState(false)
  const [draftSince, setDraftSince] = useState('')
  const [draftUntil, setDraftUntil] = useState('')
  const [applied, setApplied] = useState({
    category: '', actor: '', target: '', system: false, since: '', until: '',
  })

  const filterParams = useCallback(() => ({
    action: applied.category ? `${applied.category}.` : undefined,
    actor: applied.actor || undefined,
    target: applied.target || undefined,
    system: applied.system || undefined,
    since: applied.since || undefined,
    until: applied.until || undefined,
  }), [applied])
  const fetchPage = useCallback(
    (q: PageQuery, signal: AbortSignal) => adminGetAuditLogs({ ...q, ...filterParams() }, signal),
    [filterParams],
  )
  const { items, total, newer, serverTz, offset, setOffset, loading, error, reload } =
    usePagedReport<AuditLogItem, AuditLogResponse>(
      fetchPage,
      JSON.stringify(applied),
      err => apiErrorMessageOr(err, t, 'adminHistory.loadFailed'),
      PAGE_SIZE,
    )
  const csv = useCsvExport(err => apiErrorMessageOr(err, t, 'adminHistory.exportFailed'))

  function applyFilters() {
    setApplied({
      category: draftCategory,
      actor: draftActor.trim(),
      target: draftTarget.trim(),
      system: draftSystem,
      since: draftSince,
      until: draftUntil,
    })
    // The same filters again still mean "fetch now".
    reload()
  }

  function exportCsv() {
    // Export the applied filters, not the half-typed drafts, so the
    // download matches what the table currently shows.
    void csv.start(opts => adminDownloadAuditLogsCsv(filterParams(), opts))
  }

  function clearFilters() {
    setDraftCategory('')
    setDraftActor('')
    setDraftTarget('')
    setDraftSystem(false)
    setDraftSince('')
    setDraftUntil('')
    setApplied({ category: '', actor: '', target: '', system: false, since: '', until: '' })
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilter = useMemo(
    () => Boolean(applied.category || applied.actor || applied.target || applied.system || applied.since || applied.until),
    [applied],
  )
  const tzLabel = serverTzLabel(serverTz)
  const shownError = error || csv.error

  const labelStyle: React.CSSProperties = {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.05em', color: 'var(--color-text-faint)',
    display: 'block', marginBottom: 2,
  }

  return (
    <div>
      <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 'var(--space-3)' }}>
        {t('adminHistory.description')}
      </div>

      {/* Filter bar */}
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 'var(--space-3)', alignItems: 'flex-end',
        padding: 'var(--space-3)', border: '1px solid var(--color-border)',
        borderRadius: 6, background: 'var(--color-surface)', marginBottom: 'var(--space-4)',
      }}>
        <div>
          <label style={labelStyle}>{t('adminHistory.filterCategory')}</label>
          <Select value={draftCategory} onChange={e => setDraftCategory(e.target.value)}>
            <option value="">{t('adminHistory.allCategories')}</option>
            {CATEGORIES.map(c => (
              <option key={c} value={c}>{t(`adminHistory.cat.${c}`)}</option>
            ))}
          </Select>
        </div>
        <div>
          <label style={labelStyle}>{t('adminHistory.filterActor')}</label>
          <Input
            value={draftActor}
            onChange={e => setDraftActor(e.target.value)}
            placeholder={t('adminHistory.filterActorPlaceholder')}
            style={{ width: 160 }}
          />
        </div>
        <div>
          <label style={labelStyle}>{t('adminHistory.filterTarget')}</label>
          <Input
            value={draftTarget}
            onChange={e => setDraftTarget(e.target.value)}
            placeholder={t('adminHistory.filterTargetPlaceholder')}
            style={{ width: 140 }}
          />
        </div>
        <label style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 4, paddingBottom: 6 }}>
          <input type="checkbox" checked={draftSystem} onChange={e => setDraftSystem(e.target.checked)} />
          {t('adminHistory.filterSystem')}
        </label>
        <div>
          <label style={labelStyle}>{t('adminHistory.filterFrom')}</label>
          <Input type="date" value={draftSince} onChange={e => setDraftSince(e.target.value)} />
        </div>
        <div>
          <label style={labelStyle}>{t('adminHistory.filterTo')}</label>
          <Input type="date" value={draftUntil} onChange={e => setDraftUntil(e.target.value)} />
        </div>
        <Button size="sm" onClick={applyFilters}>{t('adminHistory.applyFilters')}</Button>
        {hasFilter && (
          <Button size="sm" variant="ghost" onClick={clearFilters}>{t('adminHistory.clearFilters')}</Button>
        )}
        <Button size="sm" variant="ghost" onClick={reload} disabled={loading}>{t('adminHistory.refresh')}</Button>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2, 8px)', alignItems: 'center' }}>
          <Button
            size="sm"
            variant="secondary"
            onClick={exportCsv}
            disabled={csv.exporting}
            title={t('adminHistory.exportCsvHint')}
          >
            {csv.exporting
              ? (csv.received > 0 ? t('adminHistory.exportProgress', { size: formatBytes(csv.received) }) : t('adminHistory.exporting'))
              : t('adminHistory.exportCsv')}
          </Button>
          {csv.exporting && (
            <Button size="sm" variant="ghost" onClick={csv.cancel}>{t('adminHistory.cancelExport')}</Button>
          )}
        </div>
      </div>

      {shownError && (
        <div style={{ color: 'var(--color-danger-fg, #b91c1c)', fontSize: 12, marginBottom: 'var(--space-3)' }}>
          {shownError}
        </div>
      )}
      {newer > 0 && (
        <div role="status" style={{
          display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
          padding: '4px 8px', marginBottom: 'var(--space-3)',
          border: '1px solid var(--color-border)', borderRadius: 4, background: 'var(--color-surface)',
        }}>
          {t('adminHistory.newerEntries', { count: newer })}
          <Button size="sm" variant="ghost" onClick={reload}>{t('adminHistory.showNewer')}</Button>
        </div>
      )}

      {loading && items.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminHistory.loading')}</div>
      ) : items.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminHistory.empty')}</div>
      ) : (
        <>
          <div style={{ fontSize: 11, opacity: 0.65, marginBottom: 4 }}>
            {t('adminHistory.totalCount', { count: total })}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={{ ...TH, width: 150 }}>{t('adminHistory.colTime')}{tzLabel && (
                    // The heading style uppercases; a zone name must keep its case.
                    <>{' '}<span style={{ textTransform: 'none' }}>({tzLabel})</span></>
                  )}</th>
                  <th style={{ ...TH, width: 160 }}>{t('adminHistory.colActor')}</th>
                  <th style={TH}>{t('adminHistory.colAction')}</th>
                  <th style={{ ...TH, width: 140 }}>{t('adminHistory.colTarget')}</th>
                  <th style={TH}>{t('adminHistory.colDetails')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.id}>
                    <td style={{ ...TD, whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: 11 }}>
                      {formatInServerTz(item.occurred_at, serverTz, locale, { weekday: false })}
                    </td>
                    <td style={TD}>
                      {item.actor_username
                        ? (
                          <>
                            {item.actor_username}
                            {item.actor_role && (
                              <span style={{ marginLeft: 4, fontSize: 10, opacity: 0.6 }}>
                                ({item.actor_role})
                              </span>
                            )}
                          </>
                        )
                        : <span style={{ opacity: 0.6, fontStyle: 'italic' }}>{t('adminHistory.system')}</span>}
                    </td>
                    <td style={TD}>
                      <CategoryChip action={item.action} />
                      <span style={{ marginLeft: 6, fontFamily: 'monospace', fontSize: 11 }}>
                        {item.action}
                      </span>
                    </td>
                    <td style={{ ...TD, fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>
                      {item.target ?? <span style={{ opacity: 0.4 }}>—</span>}
                    </td>
                    <td style={TD}><DetailsCell item={item} /></td>
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
              {t('adminHistory.prevPage')}
            </Button>
            <span style={{ fontSize: 12, opacity: 0.7 }}>
              {t('adminHistory.pageOf', { page, pageCount })}
            </span>
            <Button
              size="sm"
              variant="secondary"
              disabled={offset + PAGE_SIZE >= total || loading}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t('adminHistory.nextPage')}
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
