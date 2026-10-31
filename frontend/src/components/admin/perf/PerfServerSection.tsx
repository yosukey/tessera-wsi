import React, { useEffect, useState } from 'react'
import {
  adminPerfCancelRun,
  adminPerfStartRun,
  isApiCodeError,
  type PerfRunDetail,
  type Slide,
} from '../../../lib/api'
import { apiErrorMessageOr } from '../../../lib/admin/apiError'
import { useI18n } from '../../../lib/i18n'
import { Button, Input, LockFieldset, Select } from '../../ui'
import { Meter, SR_ONLY } from './PerfResults'

const SLOTS = 3

function slideLabel(s: Slide): string {
  const name = s.title || s.catalog_id || s.wsi_filename
  return `#${s.id} ${name}`
}

// The inputs are not in a <form>, so their min/max aren't enforced: clamp
// here rather than let the API answer with a raw validation message.
function clampInt(raw: string, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(raw))
  return Number.isFinite(n) && raw.trim() !== '' ? Math.max(lo, Math.min(hi, n)) : fallback
}

export default function PerfServerSection({
  isAdmin,
  slides,
  running,
  pending,
  disabled,
  onStarted,
  onConflict,
}: {
  // null until /api/admin/me has answered.
  isAdmin: boolean | null
  slides: Slide[]
  running: PerfRunDetail | null
  // A run is known to be active but its first poll hasn't answered yet.
  pending: boolean
  disabled: boolean
  onStarted: (id: number) => void
  // The server already has a run going (started elsewhere): pick it up.
  onConflict: () => void
}) {
  const { t } = useI18n()
  const [picks, setPicks] = useState<string[]>(Array(SLOTS).fill(''))
  const [cold, setCold] = useState(false)
  const [tilesPerBand, setTilesPerBand] = useState('30')
  const [maxConcurrency, setMaxConcurrency] = useState('0')
  const [stepSeconds, setStepSeconds] = useState('3')
  const [starting, setStarting] = useState(false)
  // Between the Cancel click and the poll that reports cancel_requested: the
  // button is off, so a second click can't answer with a stale "not running".
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState('')

  // A run's end settles whatever was said about it (a cancel that failed, a
  // start that found it already going): the message would otherwise stay
  // under the form it no longer applies to.
  const hasRun = running != null
  useEffect(() => {
    if (hasRun) return
    setCancelling(false)
    setError('')
  }, [hasRun])

  const withFiles = slides.filter((s) => !!s.wsi_filename)

  async function start() {
    setError('')
    setStarting(true)
    try {
      const ids = Array.from(new Set(picks.filter(Boolean).map(Number)))
      const step = Number(stepSeconds)
      const { id } = await adminPerfStartRun({
        slide_ids: ids,
        cold,
        tiles_per_band: clampInt(tilesPerBand, 5, 200, 30),
        max_concurrency: clampInt(maxConcurrency, 0, 32, 0),
        step_seconds: Number.isFinite(step) && stepSeconds.trim() !== '' ? Math.max(1, Math.min(10, step)) : 3,
      })
      onStarted(id)
    } catch (err) {
      if (isApiCodeError(err) && err.code === 'adminPerf.err.alreadyRunning') onConflict()
      setError(apiErrorMessageOr(err, t, 'adminPerf.startFailed'))
    } finally {
      setStarting(false)
    }
  }

  async function cancel() {
    if (!running || cancelling) return
    setError('')
    setCancelling(true)
    try {
      await adminPerfCancelRun(running.id)
    } catch (err) {
      setCancelling(false)
      // The run ended (or was cancelled from elsewhere) before this click
      // reached the server: what the admin asked for has happened, and the
      // next poll shows it.
      if (isApiCodeError(err) && err.code === 'adminPerf.err.notRunning') return
      setError(apiErrorMessageOr(err, t, 'adminPerf.server.cancelFailed'))
    }
  }

  const progress = running?.progress
  const phase = t(`adminPerf.phase.${progress?.phase || 'starting'}`)
  const anyPicked = picks.some(Boolean)
  const stopping = !!running?.cancel_requested || cancelling

  return (
    <section style={{ marginTop: 16 }}>
      {/* Only the phase is announced; the percentage (updated on every poll)
          lives in the progress bar. Always mounted, only the text changes:
          a region inserted with its first text is not reliably announced. */}
      <div aria-live="polite" style={SR_ONLY}>{running || pending ? phase : ''}</div>
      <h3 style={{ margin: '0 0 4px', fontSize: 'var(--font-size-lg)' }}>{t('adminPerf.server.title')}</h3>
      <p style={{ margin: '0 0 6px', fontSize: 13, color: 'var(--color-text-muted)' }}>{t('adminPerf.server.desc')}</p>
      <div style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-warning-bg)', color: 'var(--color-warning-fg)', marginBottom: 10 }}>
        ⚠ {t('adminPerf.server.warning')}
      </div>

      {isAdmin === true && !running && !pending && (
        // Locked while the start request is out: what it sends is what the
        // form held at the click, and its answer replaces the form.
        <LockFieldset locked={starting} style={{ display: 'grid', gap: 8, maxWidth: 720 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('adminPerf.server.slides')}</div>
            <div style={{ display: 'grid', gap: 4 }}>
              {picks.map((v, i) => (
                <Select
                  key={i}
                  size="sm"
                  value={v}
                  aria-label={t('adminPerf.server.slideN', { n: i + 1 })}
                  onChange={(e) => setPicks((p) => p.map((x, j) => (j === i ? e.target.value : x)))}
                >
                  {/* "Automatic" only applies while no slide is picked at all. */}
                  <option value="">{i === 0 && !anyPicked ? t('adminPerf.server.slideAuto') : t('adminPerf.server.slideNone')}</option>
                  {withFiles.map((s) => (
                    <option key={s.id} value={String(s.id)}>{slideLabel(s)}</option>
                  ))}
                </Select>
              ))}
            </div>
          </div>
          <label style={{ fontSize: 13, display: 'flex', gap: 6, alignItems: 'flex-start' }}>
            <input type="checkbox" checked={cold} onChange={(e) => setCold(e.target.checked)} style={{ marginTop: 3 }} />
            <span>{t('adminPerf.server.cold')}</span>
          </label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
            <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
              {t('adminPerf.server.tilesPerBand')}
              <Input size="sm" type="number" min={5} max={200} value={tilesPerBand} onChange={(e) => setTilesPerBand(e.target.value)} style={{ width: 100 }} />
            </label>
            <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
              {t('adminPerf.server.maxConcurrency')}
              <Input size="sm" type="number" min={0} max={32} value={maxConcurrency} onChange={(e) => setMaxConcurrency(e.target.value)} style={{ width: 100 }} />
            </label>
            <label style={{ fontSize: 12, display: 'grid', gap: 2 }}>
              {t('adminPerf.server.stepSeconds')}
              <Input size="sm" type="number" min={1} max={10} step={0.5} value={stepSeconds} onChange={(e) => setStepSeconds(e.target.value)} style={{ width: 100 }} />
            </label>
          </div>
          <div>
            <Button variant="primary" size="sm" onClick={start} loading={starting} disabled={starting || disabled}>
              {t('adminPerf.server.start')}
            </Button>
            {disabled && <span style={{ fontSize: 12, marginLeft: 8, color: 'var(--color-text-subtle)' }}>{t('adminPerf.busyOther')}</span>}
          </div>
        </LockFieldset>
      )}
      {isAdmin === false && !running && !pending && (
        <div style={{ fontSize: 12, color: 'var(--color-text-subtle)' }}>{t('adminPerf.adminOnly')}</div>
      )}

      {pending && !running && (
        <div style={{ fontSize: 13, maxWidth: 720 }}><strong>{phase}</strong></div>
      )}
      {running && (
        <div style={{ display: 'grid', gap: 6, maxWidth: 720 }}>
          <div style={{ fontSize: 13 }}>
            <strong>{phase}</strong>
            {progress?.slide && progress.slide_index != null && (
              <span style={{ color: 'var(--color-text-muted)' }}>
                {' · '}{t('adminPerf.server.progressSlide', { i: progress.slide_index, n: progress.slide_count ?? 1, title: progress.slide })}
              </span>
            )}
            <span style={{ float: 'right', fontVariantNumeric: 'tabular-nums' }}>{Math.round(progress?.percent ?? 0)}%</span>
          </div>
          <Meter percent={progress?.percent ?? 0} label={t('adminPerf.server.title')} />
          <div style={{ fontSize: 12, color: 'var(--color-text-subtle)' }}>
            {t('adminPerf.server.runningBy', { actor: running.actor || '—' })}
          </div>
          {isAdmin === true && (
            <div>
              <Button size="sm" variant="secondary" onClick={cancel} disabled={stopping}>
                {stopping ? t('adminPerf.server.cancelling') : t('adminPerf.server.cancel')}
              </Button>
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
