import React, { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import {
  adminListSlides,
  adminMe,
  adminPerfExport,
  adminPerfGetRun,
  adminPerfListRuns,
  clearAdminSession,
  isAbortError,
  isApiCodeError,
  isUnauthorizedError,
  type PerfRunDetail,
  type PerfRunSummary,
  type Slide,
} from '../lib/api'
import { Button } from '../components/ui'
import { logger } from '../lib/logger'
import { apiErrorMessageOr } from '../lib/admin/apiError'
import { canViewServer } from '../lib/admin/permissions'
import { useCsvExport } from '../lib/admin/usePagedReport'
import { formatBytes } from '../lib/admin/validation'
import { useI18n } from '../lib/i18n'
import LanguageSwitcher from '../components/LanguageSwitcher'
import PerfServerSection from '../components/admin/perf/PerfServerSection'
import PerfClientSection from '../components/admin/perf/PerfClientSection'
import PerfHistorySection from '../components/admin/perf/PerfHistorySection'
import { RunResultView, formatWhen } from '../components/admin/perf/PerfResults'

// How often a running server benchmark is polled. The /api/admin/ nginx zone
// allows 120 requests/min; this stays well inside it alongside the page's
// other calls.
const POLL_MS = 1500
// While the server keeps failing (a restart, an outage) the poll backs off,
// doubling up to this, instead of retrying every few seconds for as long as
// the tab stays open.
const POLL_MAX_MS = 30_000

// The server's answer for a run id it no longer has (deleted meanwhile).
const isRunNotFound = (err: unknown) => isApiCodeError(err) && err.code === 'adminPerf.err.notFound'

const ALERT: React.CSSProperties = {
  fontSize: 12, color: 'var(--color-danger-fg)', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 4,
}

export default function AdminPerformancePage() {
  const nav = useNavigate()
  const { t, locale } = useI18n()
  // null until /api/admin/me answers, so an admin never sees the
  // "administrators only" line flash first.
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null)
  // Each load has its own failure flag and retry: with one flag shared by
  // all of them, a later success elsewhere (the history refresh) would clear a
  // failed permission check, leaving a page with no controls and nothing to
  // say why.
  const [meError, setMeError] = useState(false)
  const [meReloadKey, setMeReloadKey] = useState(0)
  const [slides, setSlides] = useState<Slide[]>([])
  const [slidesError, setSlidesError] = useState(false)
  const [slidesReloadKey, setSlidesReloadKey] = useState(0)
  const [runs, setRuns] = useState<PerfRunSummary[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [active, setActive] = useState<PerfRunDetail | null>(null)
  const [selected, setSelected] = useState<PerfRunDetail | null>(null)
  const [clientRunning, setClientRunning] = useState(false)
  // The history list failed to load (its Retry reloads the list).
  const [loadError, setLoadError] = useState(false)
  // A run's detail failed to load: which run, and whether the server said it
  // is gone. Kept apart from `loadError` for the same reason as meError /
  // slidesError: with one shared flag the history refresh that follows
  // nearly every action (and the poll's own one right after a run vanished)
  // would erase the failure a frame later, and its Retry only reload the list.
  // This one's Retry asks for the same run again; a run that is gone offers
  // only a dismiss, as retrying cannot bring it back.
  const [detailError, setDetailError] = useState<{ id: number; gone: boolean } | null>(null)
  // The running benchmark's poll is failing and being retried.
  const [pollError, setPollError] = useState(false)
  // Tied to the run it is about, so a failure that lands after the admin
  // opened another run is never shown above that one.
  const runExport = useCsvExport((err) => apiErrorMessageOr(err, t, 'adminPerf.export.failed'))
  const [exportFor, setExportFor] = useState<number | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The run on screen and whether a select() is loading one, for the poll:
  // its callback outlives renders, so it reads them here rather than from a
  // stale closure.
  const selectedRef = useRef<PerfRunDetail | null>(null)
  selectedRef.current = selected
  const selectLoading = useRef(false)
  const selectSeq = useRef(0)
  const selectCtrl = useRef<AbortController | null>(null)
  const runsSeq = useRef(0)
  const resultRef = useRef<HTMLHeadingElement | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    setMeError(false)
    adminMe({ signal: ctrl.signal })
      .then((me) => {
        if (!canViewServer(me)) { nav('/admin/dashboard', { replace: true }); return }
        setIsAdmin(me.role === 'admin')
      })
      .catch((err) => {
        if (isAbortError(err)) return
        // Mirrors AdminServerPage: only a genuine auth rejection returns to
        // the login screen; a transient failure keeps the admin here, with
        // a retry.
        if (isUnauthorizedError(err)) { nav('/admin', { replace: true }); return }
        logger.error('adminMe failed:', err)
        setMeError(true)
      })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meReloadKey])

  useEffect(() => {
    const ctrl = new AbortController()
    setSlidesError(false)
    adminListSlides({ signal: ctrl.signal })
      .then(setSlides)
      .catch((err) => {
        if (isAbortError(err)) return
        logger.error('slides failed:', err)
        setSlidesError(true)
      })
    return () => ctrl.abort()
  }, [slidesReloadKey])

  // Only the latest request's answer is applied: an older one landing after
  // it would bring back a run just deleted, or a running_id for a run that
  // has since finished (and restart its poll).
  const refreshRuns = useCallback(async () => {
    const seq = ++runsSeq.current
    try {
      const body = await adminPerfListRuns()
      if (seq !== runsSeq.current) return
      setRuns(body.runs)
      setLoadError(false)
      if (body.running_id != null) setActiveId((cur) => cur ?? body.running_id)
    } catch (err) {
      if (seq !== runsSeq.current) return
      if (!isAbortError(err)) { setLoadError(true); logger.error('perf runs failed:', err) }
    }
  }, [])

  useEffect(() => { refreshRuns() }, [refreshRuns])

  // Show a run's result and bring it into view. Only the latest request
  // counts, so clicking A then B never ends on A; the superseded request is
  // aborted rather than left to finish unread.
  const select = useCallback(async (id: number) => {
    const seq = ++selectSeq.current
    selectCtrl.current?.abort()
    const ctrl = new AbortController()
    selectCtrl.current = ctrl
    // A new selection (a Retry included) supersedes the last one's failure.
    setDetailError(null)
    selectLoading.current = true
    try {
      const run = await adminPerfGetRun(id, { signal: ctrl.signal })
      if (seq !== selectSeq.current) return
      selectLoading.current = false
      selectedRef.current = run
      setSelected(run)
      requestAnimationFrame(() => {
        resultRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
        resultRef.current?.focus({ preventScroll: true })
      })
    } catch (err) {
      if (seq !== selectSeq.current || isAbortError(err)) return
      selectLoading.current = false
      logger.error('perf run failed:', err)
      setDetailError({ id, gone: isRunNotFound(err) })
    }
  }, [])

  useEffect(() => () => selectCtrl.current?.abort(), [])

  // Poll the running server benchmark until it finishes, then show its result.
  useEffect(() => {
    if (activeId == null) return
    let stopped = false
    const ctrl = new AbortController()
    let failures = 0
    const tick = async () => {
      try {
        const run = await adminPerfGetRun(activeId, { signal: ctrl.signal })
        if (stopped) return
        failures = 0
        setPollError(false)
        if (run.state === 'running') {
          setActive(run)
          pollRef.current = setTimeout(tick, POLL_MS)
          return
        }
        setActive(null)
        setActiveId(null)
        // A run the admin has open (or is opening) is their choice: the
        // finished result then only shows up in the history instead of
        // replacing it. What they did meanwhile does not count — a run
        // opened and closed again, or one that failed to load, must not
        // suppress the result too (as a "was anything selected since the
        // start" test would), leaving nothing to say the run had finished.
        const current = selectedRef.current
        const elsewhere = selectLoading.current || (current != null && current.id !== run.id)
        if (!elsewhere) {
          // Showing it is a selection like any other: it moves the sequence
          // on, as select() does.
          selectSeq.current += 1
          selectCtrl.current?.abort()
          setDetailError(null)
          selectedRef.current = run
          setSelected(run)
        }
        refreshRuns()
      } catch (err) {
        if (stopped || isAbortError(err)) return
        logger.error('perf poll failed:', err)
        // A lost session / role is the page's, not the run's — "Failed to
        // load measurement #N" with a Retry that can only fail again would say
        // nothing useful. A 401 (the token is gone or expired) returns to the
        // login screen, as the adminMe check does; a 403 means the token is
        // still good but its role no longer reaches this page (revoked
        // meanwhile), so it goes where the adminMe check sends a role without
        // the permission — the dashboard — rather than to a login the admin
        // would only pass to be refused again.
        if (isUnauthorizedError(err)) {
          nav(err.status === 403 ? '/admin/dashboard' : '/admin', { replace: true })
          return
        }
        // A deleted run won't come back by retrying either: stop polling
        // instead of freezing the page on "running". The failure is the
        // run's own, so it goes to `detailError`: the history refresh below
        // would clear `loadError` as soon as it lands.
        if (isRunNotFound(err)) {
          setDetailError({ id: activeId, gone: true })
          setActive(null)
          setActiveId(null)
          refreshRuns()
          return
        }
        // Anything else (a 5xx, the network) is retried, further apart each
        // time, and said so until a poll gets through again.
        failures += 1
        setPollError(true)
        pollRef.current = setTimeout(tick, Math.min(POLL_MAX_MS, POLL_MS * 2 ** failures))
      }
    }
    tick()
    return () => {
      stopped = true
      ctrl.abort()
      if (pollRef.current) clearTimeout(pollRef.current)
      setPollError(false)
    }
    // `nav` only leaves the page; a new one must not restart the poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, refreshRuns])

  // A run export is under way for a run other than the one on screen (or
  // with no run on screen at all).
  const otherExport = runExport.exporting && exportFor != null && selected?.id !== exportFor
  const exportStatusId = useId()

  function exportSelected(id: number) {
    if (runExport.exporting) return
    setExportFor(id)
    void runExport.start((opts) => adminPerfExport('json', [id], opts))
  }

  function onLogout() {
    if (!window.confirm(t('admin.logoutConfirm'))) return
    clearAdminSession()
    nav('/admin')
  }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-bg)', padding: '0 var(--space-4) var(--space-5)' }}>
      <div style={{ maxWidth: 1400, margin: '0 auto' }}>
        <div style={{
          position: 'sticky',
          top: 0,
          zIndex: 1,
          background: 'var(--color-bg)',
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--space-3)',
          paddingTop: 'var(--space-5)',
          paddingBottom: 'var(--space-4)',
        }}>
          <h2 style={{ margin: 0, fontSize: 'var(--font-size-2xl)', fontWeight: 700, letterSpacing: '-0.01em' }}>
            {t('adminPerf.title')}
          </h2>
          <div style={{ flex: 1 }} />
          <Link to="/admin/server" style={{ fontSize: 'var(--font-size-sm)' }}>{t('adminDash.tabServer')}</Link>
          <Link to="/admin/dashboard" style={{ fontSize: 'var(--font-size-sm)' }}>{t('admin.dashboard')}</Link>
          <LanguageSwitcher />
          <Button size="sm" variant="ghost" onClick={onLogout}>{t('admin.logout')}</Button>
        </div>

        <p style={{ margin: '0 0 4px', fontSize: 13, color: 'var(--color-text-muted)', maxWidth: 900 }}>{t('adminPerf.intro')}</p>
        {meError && (
          <div role="alert" style={ALERT}>
            <span>{t('adminDash.accessCheckFailed')}</span>
            <Button size="sm" variant="secondary" onClick={() => setMeReloadKey((k) => k + 1)}>{t('adminDash.retry')}</Button>
          </div>
        )}
        {slidesError && (
          <div role="alert" style={ALERT}>
            <span>{t('adminPerf.slidesFailed')}</span>
            <Button size="sm" variant="secondary" onClick={() => setSlidesReloadKey((k) => k + 1)}>{t('adminDash.retry')}</Button>
          </div>
        )}
        {loadError && (
          <div role="alert" style={ALERT}>
            <span>{t('adminPerf.loadFailed')}</span>
            <Button size="sm" variant="secondary" onClick={() => { refreshRuns() }}>{t('adminDash.retry')}</Button>
          </div>
        )}
        {detailError && (
          <div role="alert" style={ALERT}>
            <span>{detailError.gone ? t('adminPerf.err.notFound') : t('adminPerf.runLoadFailed', { id: detailError.id })}</span>
            {detailError.gone
              ? <Button size="sm" variant="secondary" onClick={() => setDetailError(null)}>{t('adminDash.dismiss')}</Button>
              : <Button size="sm" variant="secondary" onClick={() => { void select(detailError.id) }}>{t('adminDash.retry')}</Button>}
          </div>
        )}
        {/* Always mounted, only its text changes: a live region inserted
            together with its message is not announced by every screen
            reader, and this one appears with no action of the admin's. */}
        <div role="status" style={pollError ? ALERT : undefined}>{pollError ? t('adminPerf.pollFailed') : ''}</div>

        <PerfServerSection
          isAdmin={isAdmin}
          slides={slides}
          running={active}
          pending={activeId != null && active == null}
          disabled={clientRunning}
          onStarted={(id) => { setActiveId(id); refreshRuns() }}
          onConflict={refreshRuns}
        />
        <PerfClientSection
          isAdmin={isAdmin}
          slides={slides}
          disabled={activeId != null}
          onRunningChange={setClientRunning}
          onFinished={(id) => { refreshRuns(); select(id) }}
          onServerRunFound={(id) => { setActiveId((cur) => cur ?? id); refreshRuns() }}
        />

        {/* A run's export carries on when another run is opened (or the
            result is closed), but its progress and Cancel live in that run's
            panel — so the panel on screen would show a disabled Export with
            nothing to say why, and no way to stop the download short of
            reopening the run. This line stands in for them whenever the run
            being exported is not the one on screen. */}
        {otherExport && (
          <div id={exportStatusId} style={{ fontSize: 12, color: 'var(--color-text-subtle)', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 16, fontVariantNumeric: 'tabular-nums' }}>
            <span>
              {runExport.received > 0
                ? t('adminPerf.export.otherProgress', { id: exportFor!, size: formatBytes(runExport.received) })
                : t('adminPerf.export.other', { id: exportFor! })}
            </span>
            <Button size="sm" variant="ghost" onClick={runExport.cancel} aria-label={t('adminPerf.export.cancelRun', { id: exportFor! })}>
              {t('adminPerf.export.cancel')}
            </Button>
          </div>
        )}

        {selected && (
          <section style={{ marginTop: 24, padding: '12px 14px', border: '1px solid var(--color-border)', borderRadius: 8, background: 'var(--color-surface)' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
              <h3 ref={resultRef} tabIndex={-1} style={{ margin: 0, fontSize: 'var(--font-size-lg)', scrollMarginTop: 80, outline: 'none' }}>
                {t('adminPerf.result.title', { kind: t(`adminPerf.kind.${selected.kind}`), id: selected.id })}
              </h3>
              <span style={{ fontSize: 12, color: 'var(--color-text-subtle)' }}>
                {formatWhen(selected.created_at, locale)} · {t(`adminPerf.state.${selected.state}`)}
              </span>
              <div style={{ flex: 1 }} />
              <Button
                size="sm"
                variant="secondary"
                onClick={() => exportSelected(selected.id)}
                disabled={runExport.exporting}
                // Disabled by another run's export, it says why — and points
                // at the line above that names that run and can cancel it.
                title={otherExport ? t('adminPerf.export.busyOther', { id: exportFor! }) : t('adminPerf.export.runHint')}
                aria-describedby={otherExport ? exportStatusId : undefined}
              >
                {runExport.exporting && exportFor === selected.id
                  ? (runExport.received > 0 ? t('adminPerf.export.progress', { size: formatBytes(runExport.received) }) : t('adminPerf.export.exporting'))
                  : t('adminPerf.export.run')}
              </Button>
              {/* Only for this run's own export: one of a run opened before
                  (still downloading) is not this run's to cancel — the line
                  above the panel offers that one's. Named after the run,
                  apart from the history export's Cancel. */}
              {runExport.exporting && exportFor === selected.id && (
                <Button size="sm" variant="ghost" onClick={runExport.cancel} aria-label={t('adminPerf.export.cancelRun', { id: selected.id })}>
                  {t('adminPerf.export.cancel')}
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>{t('adminPerf.result.close')}</Button>
            </div>
            {runExport.error && exportFor === selected.id && (
              <div role="alert" style={{ fontSize: 12, padding: '4px 6px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)', marginBottom: 8 }}>
                {runExport.error}
              </div>
            )}
            <RunResultView run={selected} />
          </section>
        )}

        <PerfHistorySection
          runs={runs}
          isAdmin={isAdmin}
          selectedId={selected?.id ?? null}
          onSelect={select}
          onDeleted={(id) => {
            refreshRuns()
            setSelected((cur) => (cur?.id === id ? null : cur))
            setDetailError((cur) => (cur?.id === id ? null : cur))
          }}
        />
      </div>
    </div>
  )
}
