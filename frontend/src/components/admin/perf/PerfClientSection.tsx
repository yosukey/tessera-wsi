import React, { useEffect, useRef, useState } from 'react'
import {
  adminGetSlide,
  adminPerfListRuns,
  adminPerfSaveClientRun,
  isAbortError,
  isSziSlide,
  type Slide,
} from '../../../lib/api'
import { apiErrorMessageOr } from '../../../lib/admin/apiError'
import {
  MAX_CLIENT_CONCURRENCY,
  runClientTest,
  type ClientTestMode,
  type ClientTestProgress,
} from '../../../lib/admin/perfClientTest'
import { useI18n } from '../../../lib/i18n'
import { Button, Input, Select } from '../../ui'
import { Meter, SR_ONLY } from './PerfResults'

export default function PerfClientSection({
  isAdmin,
  slides,
  disabled,
  onRunningChange,
  onFinished,
  onServerRunFound,
}: {
  // null until /api/admin/me has answered.
  isAdmin: boolean | null
  slides: Slide[]
  disabled: boolean
  onRunningChange: (running: boolean) => void
  onFinished: (runId: number) => void
  // A server benchmark started elsewhere (another admin or tab) is running.
  onServerRunFound: (runId: number) => void
}) {
  const { t } = useI18n()
  const candidates = slides.filter((s) => !!s.wsi_filename && !isSziSlide(s))
  const [slideId, setSlideId] = useState('')
  const [mode, setMode] = useState<ClientTestMode>('app')
  const [tiles, setTiles] = useState('60')
  const [concurrency, setConcurrency] = useState('6')
  const [progress, setProgress] = useState<ClientTestProgress | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const ctrlRef = useRef<AbortController | null>(null)

  useEffect(() => () => ctrlRef.current?.abort(), [])

  const slide = candidates.find((s) => String(s.id) === slideId) ?? candidates[0]
  // The normal (cached) path serves published slides only: without the admin
  // token a draft's tiles are 404 — exactly what a viewer would get.
  const cacheModeBlocked = mode === 'cache' && !!slide && !slide.is_published
  const running = progress !== null

  async function start() {
    if (!slide) return
    setError('')
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    setProgress({ phase: 'rtt', done: 0, total: 1 })
    onRunningChange(true)
    try {
      // The page only learns of server runs it started itself; one started
      // from another tab would share the machine and skew both results.
      const { running_id: serverRun } = await adminPerfListRuns({ signal: ctrl.signal })
      if (serverRun != null) {
        onServerRunFound(serverRun)
        setError(t('adminPerf.err.alreadyRunning'))
        return
      }
      // The slide as it is now, not as the page listed it when it loaded: a
      // re-upload or re-conversion since bumps tile_version, and the stale
      // one names a descriptor that is gone (404). Its publish state and file
      // are checked again for the same reason.
      const fresh = await adminGetSlide(slide.id, { signal: ctrl.signal })
      if (!fresh.wsi_filename || isSziSlide(fresh)) {
        setError(t('adminPerf.client.err.slideChanged'))
        return
      }
      if (mode === 'cache' && !fresh.is_published) {
        setError(t('adminPerf.client.cacheNeedsPublished'))
        return
      }
      const result = await runClientTest(
        {
          slide: fresh,
          mode,
          tiles: Math.max(10, Math.min(500, Math.round(Number(tiles)) || 60)),
          concurrency: Math.max(1, Math.min(MAX_CLIENT_CONCURRENCY, Math.round(Number(concurrency)) || MAX_CLIENT_CONCURRENCY)),
        },
        (p) => { if (!ctrl.signal.aborted) setProgress(p) },
        ctrl.signal,
      )
      if (ctrl.signal.aborted) return
      // The check above only covers the start: a server benchmark started
      // from elsewhere during the test shared the machine with it. One still
      // running now is picked up by the page and flags the result. On save the
      // server works out its own verdict over the run's whole window
      // (summary.server_overlap / job_overlap) and ORs this flag into it: a
      // `true` sent here is kept even where the server's window check found
      // nothing (e.g. a run it can't place in time), while an absent / false
      // flag leaves the server's verdict as it is. A failed check flags
      // nothing rather than losing the measurement.
      try {
        const { running_id: lateRun } = await adminPerfListRuns({ signal: ctrl.signal })
        if (lateRun != null) {
          result.summary.server_overlap = true
          onServerRunFound(lateRun)
        }
      } catch (err) {
        if (isAbortError(err)) throw err
      }
      if (ctrl.signal.aborted) return
      // Past this point Cancel can no longer take the run back.
      setSaving(true)
      const { id } = await adminPerfSaveClientRun(result)
      onFinished(id)
    } catch (err) {
      // Only errors with a message of their own are shown as such; the rest
      // ("Failed to fetch", a server's English fallback) get the generic one.
      if (!isAbortError(err)) setError(apiErrorMessageOr(err, t, 'adminPerf.client.failed'))
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null
      setSaving(false)
      setProgress(null)
      onRunningChange(false)
    }
  }

  const percent = progress
    ? progress.phase === 'rtt'
      ? (progress.done / Math.max(1, progress.total)) * 10
      : progress.phase === 'download'
        ? 10 + (progress.done / Math.max(1, progress.total)) * 20
        : 30 + (progress.done / Math.max(1, progress.total)) * 70
    : 0
  const phaseText = progress
    ? t(saving ? 'adminPerf.client.phase.save' : `adminPerf.client.phase.${progress.phase}`)
    : ''

  return (
    <section style={{ marginTop: 24 }}>
      {/* Only the phase is announced; the running counts would flood a screen
          reader, and the progress bar carries the percentage. The region is
          always mounted and only its text changes: one inserted together with
          its first text is not reliably announced. */}
      <div aria-live="polite" style={SR_ONLY}>{phaseText}</div>
      <h3 style={{ margin: '0 0 4px', fontSize: 'var(--font-size-lg)' }}>{t('adminPerf.client.title')}</h3>
      <p style={{ margin: '0 0 8px', fontSize: 13, color: 'var(--color-text-muted)' }}>{t('adminPerf.client.desc')}</p>

      {isAdmin === false && <div style={{ fontSize: 12, color: 'var(--color-text-subtle)' }}>{t('adminPerf.adminOnly')}</div>}

      {isAdmin === true && !running && (
        <div style={{ display: 'grid', gap: 8, maxWidth: 720 }}>
          <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
            {t('adminPerf.client.slide')}
            <Select size="sm" value={slide ? String(slide.id) : ''} onChange={(e) => setSlideId(e.target.value)}>
              {candidates.map((s) => (
                <option key={s.id} value={String(s.id)}>
                  #{s.id} {s.title || s.catalog_id || s.wsi_filename}{s.is_published ? '' : ` (${t('adminPerf.client.draft')})`}
                </option>
              ))}
            </Select>
          </label>
          <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 4 }}>
            <legend style={{ fontSize: 12, fontWeight: 600, marginBottom: 2 }}>{t('adminPerf.client.mode')}</legend>
            <label style={{ fontSize: 13, display: 'flex', gap: 6 }}>
              <input type="radio" name="perf-client-mode" checked={mode === 'app'} onChange={() => setMode('app')} />
              {t('adminPerf.client.modeApp')}
            </label>
            <label style={{ fontSize: 13, display: 'flex', gap: 6 }}>
              <input type="radio" name="perf-client-mode" checked={mode === 'cache'} onChange={() => setMode('cache')} />
              {t('adminPerf.client.modeCache')}
            </label>
          </fieldset>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
            <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
              {t('adminPerf.client.tiles')}
              <Input size="sm" type="number" min={10} max={500} value={tiles} onChange={(e) => setTiles(e.target.value)} style={{ width: 100 }} />
            </label>
            <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
              {t('adminPerf.client.concurrency')}
              <Input size="sm" type="number" min={1} max={MAX_CLIENT_CONCURRENCY} value={concurrency} onChange={(e) => setConcurrency(e.target.value)} style={{ width: 100 }} />
            </label>
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-subtle)' }}>{t('adminPerf.client.concurrencyNote', { n: MAX_CLIENT_CONCURRENCY })}</div>
          {mode === 'app' && (
            <div style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-warning-bg)', color: 'var(--color-warning-fg)' }}>
              ⚠ {t('adminPerf.client.appLoadWarning')}
            </div>
          )}
          {cacheModeBlocked && (
            <div style={{ fontSize: 12, color: 'var(--color-warning-fg)' }}>⚠ {t('adminPerf.client.cacheNeedsPublished')}</div>
          )}
          <div>
            <Button variant="primary" size="sm" onClick={start} disabled={!slide || disabled || cacheModeBlocked}>
              {t('adminPerf.client.start')}
            </Button>
            {disabled && <span style={{ fontSize: 12, marginLeft: 8, color: 'var(--color-text-subtle)' }}>{t('adminPerf.busyOther')}</span>}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-subtle)' }}>{t('adminPerf.client.sziNote')}</div>
        </div>
      )}

      {running && progress && (
        <div style={{ display: 'grid', gap: 6, maxWidth: 720 }}>
          <div style={{ fontSize: 13 }}>
            <strong>{phaseText}</strong>
            {progress.phase === 'tiles' && (
              <span style={{ color: 'var(--color-text-muted)' }}> · {progress.done} / {progress.total}</span>
            )}
            <span style={{ float: 'right', fontVariantNumeric: 'tabular-nums' }}>{Math.round(percent)}%</span>
          </div>
          <Meter percent={percent} label={t('adminPerf.client.title')} />
          {!saving && (
            <div>
              <Button size="sm" variant="secondary" onClick={() => ctrlRef.current?.abort()}>{t('adminPerf.server.cancel')}</Button>
            </div>
          )}
        </div>
      )}

      {error && (
        <div role="alert" style={{ fontSize: 12, padding: '4px 6px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)', marginTop: 8 }}>
          {error}
        </div>
      )}
    </section>
  )
}
