import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../../lib/i18n'
import { useInFlightKey } from '../../hooks/useInFlight'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { formatBytes } from '../../lib/admin/validation'
import { Button } from '../ui'
import {
  ExportListItem,
  ExportSlideSummary,
  adminDeleteExport,
  adminDownloadExport,
  adminListExports,
} from '../../lib/api'

// Admin-only export history. Exports are started from the slide list's bulk
// "Export selected" action; the generated bundles live under backup_dir/.staging
// and are reclaimed by the 24h TTL sweep. This panel surfaces the bundles still
// on disk so an admin can re-download one (no re-generation / re-read of the WSI
// bytes) or delete it early to reclaim disk before the sweep would.
type Props = {
  // Bumped by the parent after a bulk export completes so a freshly-generated
  // bundle appears without a manual refresh.
  reloadSignal?: number
}

function formatDateTime(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

function slideLabel(s: ExportSlideSummary): string {
  return s.title || s.public_id || '—'
}

export default function AdminExportHistorySection({ reloadSignal }: Props) {
  const { t } = useI18n()
  const [items, setItems] = useState<ExportListItem[] | null>(null)
  const [error, setError] = useState('')
  // One row action (download, delete) at a time; every row's buttons wait
  // for it, so another row's finish can't re-enable a delete still out.
  const rowRequest = useInFlightKey<string>()
  // Which bundles have their slide list expanded. Multiple can be open at once
  // so an admin can compare two same-count exports side by side.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  function toggleExpanded(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // A reload (reloadSignal) and the reload after a delete can be in flight at
  // once; only the latest one may apply its result, or an older list that
  // still has the deleted bundle (or lacks a new one) could land last.
  const loadSeqRef = useRef(0)

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current
    setError('')
    try {
      const list = await adminListExports()
      if (seq !== loadSeqRef.current) return
      setItems(list)
    } catch (err) {
      if (seq !== loadSeqRef.current) return
      setError(apiErrorMessage(err, t))
      setItems([])
    }
  }, [t])

  useEffect(() => { void load() }, [load, reloadSignal])

  async function onDownload(item: ExportListItem) {
    await rowRequest.run(item.export_id, async () => {
      setError('')
      try {
        await adminDownloadExport(item.export_id, item.archive_format)
      } catch (err) {
        setError(apiErrorMessage(err, t))
      }
    })
  }

  async function onDelete(item: ExportListItem) {
    if (!window.confirm(t('adminExport.deleteConfirm'))) return
    await rowRequest.run(item.export_id, async () => {
      setError('')
      try {
        await adminDeleteExport(item.export_id)
        await load()
      } catch (err) {
        setError(apiErrorMessage(err, t))
      }
    })
  }

  const th: React.CSSProperties = { padding: '4px 8px', textAlign: 'left', color: 'var(--color-text-muted)', whiteSpace: 'nowrap' }
  const td: React.CSSProperties = { padding: '4px 8px', verticalAlign: 'top' }

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div>
        <h3 style={{ margin: '0 0 4px' }}>{t('adminExport.title')}</h3>
        <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
          {t('adminExport.intro')}
        </p>
      </div>

      {error && (
        <div
          role="alert"
          style={{
            padding: '8px 10px', borderRadius: 'var(--radius-md)',
            background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)',
            border: '1px solid var(--color-danger-border)', fontSize: 'var(--font-size-sm)',
          }}
        >
          {error}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button variant="secondary" size="sm" onClick={() => void load()}>
          {t('adminExport.refresh')}
        </Button>
      </div>

      {items != null && items.length === 0 && (
        <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
          {t('adminExport.empty')}
        </p>
      )}

      {items != null && items.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 'var(--font-size-sm)' }}>
            <thead>
              <tr>
                <th style={th}>{t('adminExport.colCreated')}</th>
                <th style={th}>{t('adminExport.colSlides')}</th>
                <th style={th}>{t('adminExport.colTours')}</th>
                <th style={th}>{t('adminExport.colSize')}</th>
                <th style={th}>{t('adminExport.colNote')}</th>
                <th style={th}>{t('adminExport.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const slides = item.slides
                const hasSlides = slides.length > 0
                const isOpen = expanded.has(item.export_id)
                return (
                  <React.Fragment key={item.export_id}>
                    <tr style={{ borderTop: '1px solid var(--color-border)' }}>
                      <td style={td} title={item.export_id}>{formatDateTime(item.created_at)}</td>
                      <td style={td}>
                        {hasSlides ? (
                          <button
                            type="button"
                            onClick={() => toggleExpanded(item.export_id)}
                            aria-expanded={isOpen}
                            title={t('adminExport.slidesToggle')}
                            style={{
                              display: 'inline-flex', alignItems: 'baseline', gap: 4,
                              maxWidth: 200, background: 'none', border: 'none', padding: 0,
                              margin: 0, font: 'inherit', color: 'var(--color-text)',
                              cursor: 'pointer', textAlign: 'left',
                            }}
                          >
                            <span aria-hidden style={{ color: 'var(--color-text-muted)' }}>
                              {isOpen ? '▾' : '▸'}
                            </span>
                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {slideLabel(slides[0])}
                            </span>
                            {slides.length > 1 && (
                              <span style={{ color: 'var(--color-text-muted)', whiteSpace: 'nowrap' }}>
                                {t('adminExport.slidesMore', { count: slides.length - 1 })}
                              </span>
                            )}
                          </button>
                        ) : (
                          item.slide_count || '—'
                        )}
                      </td>
                      <td style={td}>{item.tour_count || '—'}</td>
                      <td style={td}>{formatBytes(item.size_bytes)}</td>
                      <td style={{ ...td, maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={item.note || ''}>
                        {item.note || '—'}
                      </td>
                      <td style={{ ...td, whiteSpace: 'nowrap' }}>
                        <div style={{ display: 'flex', gap: 6 }}>
                          <Button
                            size="sm"
                            variant="secondary"
                            disabled={rowRequest.key !== null}
                            onClick={() => onDownload(item)}
                          >
                            {t('adminExport.download')}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            disabled={rowRequest.key !== null}
                            onClick={() => onDelete(item)}
                          >
                            {t('adminExport.delete')}
                          </Button>
                        </div>
                      </td>
                    </tr>
                    {hasSlides && isOpen && (
                      <tr style={{ background: 'var(--color-surface-muted)' }}>
                        <td aria-hidden />
                        <td colSpan={5} style={{ ...td, paddingTop: 0 }}>
                          <ul
                            style={{
                              margin: '4px 0', paddingLeft: 16, maxHeight: 180,
                              overflowY: 'auto', display: 'grid', gap: 2,
                            }}
                          >
                            {slides.map((s, i) => (
                              <li key={s.public_id || i} title={s.public_id || ''}>
                                {slideLabel(s)}
                                {s.catalog_id && (
                                  <span style={{ color: 'var(--color-text-muted)', marginLeft: 6 }}>
                                    {s.catalog_id}
                                  </span>
                                )}
                              </li>
                            ))}
                          </ul>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
