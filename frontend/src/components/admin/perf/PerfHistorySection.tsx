import React, { useState } from 'react'
import { adminPerfDeleteRun, adminPerfExport, type PerfRunSummary } from '../../../lib/api'
import { apiErrorMessageOr } from '../../../lib/admin/apiError'
import { formatBytes } from '../../../lib/admin/validation'
import { useCsvExport } from '../../../lib/admin/usePagedReport'
import { useI18n } from '../../../lib/i18n'
import { useInFlight } from '../../../hooks/useInFlight'
import { Button } from '../../ui'
import { CompareView, SR_ONLY, SubHeading, TD, TH, fmtMs, fmtNum, formatWhen, runCaveats, serverRenderErrors } from './PerfResults'

function headline(run: PerfRunSummary, t: (k: string, p?: Record<string, string | number>) => string): string {
  const s = run.summary
  if (!s) return '—'
  if (run.kind === 'server') {
    return t('adminPerf.history.serverLine', {
      peak: fmtNum(s.peak_tiles_per_s),
      p95: fmtMs(s.tile_p95_ms),
      mbps: fmtNum(s.seq_read_mb_s),
    })
  }
  return t('adminPerf.history.clientLine', {
    rtt: fmtMs(s.rtt_ms),
    mbps: fmtNum(s.mbps),
    tps: fmtNum(s.tiles_per_s),
  })
}

function target(run: PerfRunSummary, t: (k: string) => string): string {
  const p = run.params || {}
  if (run.kind === 'server') {
    const slides: Array<{ id: number; title: string }> = Array.isArray(p.slides) ? p.slides : []
    return slides.map((s) => `#${s.id}`).join(', ') || '—'
  }
  const mode = p.mode === 'cache' ? t('adminPerf.client.modeCacheShort') : t('adminPerf.client.modeAppShort')
  return `#${p.slide_id ?? '—'} · ${mode}`
}

export default function PerfHistorySection({
  runs,
  isAdmin,
  selectedId,
  onSelect,
  onDeleted,
}: {
  runs: PerfRunSummary[]
  isAdmin: boolean | null
  selectedId: number | null
  onSelect: (id: number) => void
  onDeleted: (id: number) => void
}) {
  const { t, locale } = useI18n()
  const [checked, setChecked] = useState<number[]>([])
  const [error, setError] = useState('')
  // Cancellable, with the bytes received so far: a JSON export carries every
  // tile sample of every browser run and can take a while.
  const download = useCsvExport((err) => apiErrorMessageOr(err, t, 'adminPerf.export.failed'))

  const picked = checked.map((id) => runs.find((r) => r.id === id)).filter(Boolean) as PerfRunSummary[]
  const comparable = picked.length === 2 && picked[0].kind === picked[1].kind
  // Oldest first, so "change" reads as "from A to B".
  const [a, b] = comparable ? [...picked].sort((x, y) => x.id - y.id) : [undefined, undefined]

  function toggle(id: number) {
    setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c.slice(-1), id]))
  }

  function exportAll(format: 'csv' | 'json') {
    setError('')
    void download.start((opts) => adminPerfExport(format, undefined, opts))
  }

  // One delete at a time; every row's Delete waits for it, so a second click
  // can't send a second DELETE for the run just removed.
  const deleteRequest = useInFlight()
  async function remove(run: PerfRunSummary) {
    if (!window.confirm(t('adminPerf.history.deleteConfirm', { id: run.id }))) return
    await deleteRequest.run(async () => {
      setError('')
      try {
        await adminPerfDeleteRun(run.id)
        setChecked((c) => c.filter((x) => x !== run.id))
        onDeleted(run.id)
      } catch (err) {
        setError(apiErrorMessageOr(err, t, 'adminPerf.history.deleteFailed'))
      }
    })
  }

  return (
    <section style={{ marginTop: 24 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', flexWrap: 'wrap', gap: 8, marginBottom: 6 }}>
        <h3 style={{ margin: 0, fontSize: 'var(--font-size-lg)' }}>{t('adminPerf.history.title')}</h3>
        <div style={{ flex: 1 }} />
        {runs.length > 0 && (
          <>
            <span style={{ fontSize: 12, color: 'var(--color-text-subtle)' }}>{t('adminPerf.export.all')}</span>
            <Button size="sm" variant="secondary" onClick={() => exportAll('csv')} disabled={download.exporting} aria-label={t('adminPerf.export.allCsv')}>CSV</Button>
            <Button size="sm" variant="secondary" onClick={() => exportAll('json')} disabled={download.exporting} aria-label={t('adminPerf.export.allJson')}>JSON</Button>
            {download.exporting && (
              <>
                <span style={{ fontSize: 12, color: 'var(--color-text-subtle)', fontVariantNumeric: 'tabular-nums' }}>
                  {download.received > 0 ? t('adminPerf.export.progress', { size: formatBytes(download.received) }) : t('adminPerf.export.exporting')}
                </span>
                {/* Named apart from the open run's export Cancel and the
                    server test's own Cancel, which can be on screen too. */}
                <Button size="sm" variant="ghost" onClick={download.cancel} aria-label={t('adminPerf.export.cancelAll')}>{t('adminPerf.export.cancel')}</Button>
              </>
            )}
          </>
        )}
      </div>
      {runs.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--color-text-subtle)', marginBottom: 4 }}>{t('adminPerf.export.hint')}</div>
      )}
      {runs.length === 0 ? (
        <div style={{ fontSize: 13, color: 'var(--color-text-subtle)' }}>{t('adminPerf.history.empty')}</div>
      ) : (
        <>
          <div style={{ fontSize: 12, color: 'var(--color-text-subtle)', marginBottom: 4 }}>{t('adminPerf.history.compareHint')}</div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={TH}><span style={SR_ONLY}>{t('adminPerf.history.select')}</span></th>
                  <th style={TH}>{t('adminPerf.col.when')}</th>
                  <th style={TH}>{t('adminPerf.col.kind')}</th>
                  <th style={TH}>{t('adminPerf.col.target')}</th>
                  <th style={TH}>{t('adminPerf.col.result')}</th>
                  <th style={TH}>{t('adminPerf.col.state')}</th>
                  <th style={TH}>{t('adminPerf.col.by')}</th>
                  <th style={TH} />
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} style={r.id === selectedId ? { background: 'var(--color-brand-50)' } : undefined}>
                    <td style={TD}>
                      <input
                        type="checkbox"
                        checked={checked.includes(r.id)}
                        onChange={() => toggle(r.id)}
                        disabled={r.state !== 'succeeded' && r.state !== 'cancelled'}
                        aria-label={t('adminPerf.history.selectRun', { id: r.id })}
                      />
                    </td>
                    <td style={{ ...TD, whiteSpace: 'nowrap' }}>{formatWhen(r.created_at, locale)}</td>
                    <td style={TD}>{t(`adminPerf.kind.${r.kind}`)}</td>
                    <td style={TD}>{target(r, t)}</td>
                    <td style={{ ...TD, fontVariantNumeric: 'tabular-nums' }}>
                      {headline(r, t)}
                      {/* Why this run's numbers may not be comparable, where
                          the history list would otherwise read as clean. */}
                      {runCaveats(r).map((c) => (
                        <div key={c} style={{ fontSize: 11, color: 'var(--color-warning-fg)' }}>
                          ⚠ {c === 'errors'
                            ? t('adminPerf.history.caveat.errors', { n: r.summary?.tile_errors ?? 0 })
                            : c === 'renderErrors'
                              ? t('adminPerf.history.caveat.renderErrors', { n: serverRenderErrors(r.summary ?? {}) })
                              : t(`adminPerf.history.caveat.${c}`)}
                        </div>
                      ))}
                    </td>
                    <td style={TD}>
                      <span style={{ color: r.state === 'failed' ? 'var(--color-danger-fg)' : r.state === 'running' ? 'var(--color-info-fg)' : undefined }}>
                        {r.state === 'failed' ? '✕ ' : r.state === 'running' ? '● ' : ''}{t(`adminPerf.state.${r.state}`)}
                      </span>
                    </td>
                    <td style={TD}>{r.actor || '—'}</td>
                    <td style={{ ...TD, whiteSpace: 'nowrap', textAlign: 'right' }}>
                      {/* Up to a hundred rows of identical "View" / "Delete":
                          the accessible name says which run each is for. */}
                      <Button size="sm" variant="ghost" onClick={() => onSelect(r.id)} disabled={r.state === 'running'} aria-label={t('adminPerf.history.viewRun', { id: r.id })}>
                        {t('adminPerf.history.view')}
                      </Button>
                      {isAdmin === true && (
                        <Button size="sm" variant="ghost" onClick={() => remove(r)} disabled={r.state === 'running' || deleteRequest.busy} aria-label={t('adminPerf.history.deleteRun', { id: r.id })}>
                          {t('adminPerf.history.delete')}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {picked.length === 2 && !comparable && (
        <div style={{ fontSize: 12, color: 'var(--color-warning-fg)', marginTop: 6 }}>⚠ {t('adminPerf.history.compareKinds')}</div>
      )}
      {comparable && a && b && (
        <>
          <SubHeading>{t('adminPerf.compare.title')}</SubHeading>
          <CompareView a={a} b={b} />
        </>
      )}
      {(error || download.error) && (
        <div role="alert" style={{ fontSize: 12, padding: '4px 6px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)', marginTop: 8 }}>
          {error || download.error}
        </div>
      )}
    </section>
  )
}
