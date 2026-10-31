import React, { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { getViewerSettings, type AnalyticsMode } from '../lib/api'
import { STORAGE_KEYS } from '../lib/storageKeys'
import { safeGetItem, safeSetItem } from '../lib/safeStorage'
import { useI18n } from '../lib/i18n'

// One-time per-mode notice that the viewer's activity is being recorded.
// Shown after the page first loads (and after the viewer settings fetch
// resolves) when:
//   * analytics_mode != 'off', AND
//   * analytics_notice_toast_enabled is true, AND
//   * the same {mode} pair has not already been dismissed by this user.
// A change of mode (off -> hashed, hashed -> plain) re-shows the toast
// even within the same session because the dismissal envelope keys on
// mode — that way an admin who tightens or loosens the policy doesn't
// leave existing users in the dark about the new contract.

type DismissEnvelope = { mode: AnalyticsMode; at: number }

// Unreadable storage or a malformed entry reads as "never dismissed", so the
// toast can still inform the user; an unwritable one just means it may
// re-appear on the next visit.
function readDismissed(): DismissEnvelope | null {
  const raw = safeGetItem(STORAGE_KEYS.analyticsNoticeDismissed)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<DismissEnvelope>
    if (parsed && typeof parsed.mode === 'string' && typeof parsed.at === 'number') {
      return { mode: parsed.mode as AnalyticsMode, at: parsed.at }
    }
  } catch { /* malformed JSON */ }
  return null
}

function persistDismissed(mode: AnalyticsMode): void {
  const envelope: DismissEnvelope = { mode, at: Date.now() }
  safeSetItem(STORAGE_KEYS.analyticsNoticeDismissed, JSON.stringify(envelope))
}

// Auto-dismiss after 8 seconds. Long enough to read a two-line notice
// without being so long that the toast lingers over the slide content.
const AUTO_DISMISS_MS = 8000

// Routes where the notice never makes sense. Mirrors useAnalyticsBeacon's
// skip list — admin/login pages don't record analytics rows so the toast
// would be a lie there.
function isPublicAnalyticsRoute(pathname: string): boolean {
  if (pathname === '/login' || pathname.startsWith('/login/')) return false
  if (pathname === '/admin' || pathname.startsWith('/admin/')) return false
  return true
}

export default function AnalyticsNoticeToast() {
  const { t } = useI18n()
  const location = useLocation()
  const [mode, setMode] = useState<AnalyticsMode | null>(null)
  const [visible, setVisible] = useState(false)
  // Whether the browser tab itself is showing. A tab opened in the background
  // (middle-click, "open in new tab") would otherwise run the countdown out
  // unseen and persist the dismissal, so the notice would never be read.
  const [pageVisible, setPageVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  )
  // The pointer is over the toast or focus is inside it: the reader is on it,
  // so the countdown holds (WCAG 2.2.1) instead of pulling the text away
  // mid-sentence — and persisting a dismissal for a notice not finished.
  // Hover means a mouse only: after a tap, touch browsers fire emulated
  // mouseenter with no mouseleave until the next tap elsewhere, which would
  // hold the countdown for good. Pointer events say which device it was.
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const paused = hovered || focused
  const showing = visible && !!mode && isPublicAnalyticsRoute(location.pathname)

  // The toast's box unmounts while hidden (admin/login routes) without a
  // pointerleave / blur, which would otherwise leave the countdown held for
  // good once it is shown again.
  useEffect(() => {
    if (showing) return
    setHovered(false)
    setFocused(false)
  }, [showing])

  useEffect(() => {
    const onChange = () => setPageVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])

  useEffect(() => {
    let cancelled = false
    const ctrl = new AbortController()
    getViewerSettings({ signal: ctrl.signal })
      .then((vs) => {
        if (cancelled) return
        const m: AnalyticsMode = vs.analytics_mode || 'off'
        if (m === 'off' || !vs.analytics_notice_toast_enabled) return
        const prior = readDismissed()
        if (prior && prior.mode === m) return
        setMode(m)
        setVisible(true)
      })
      .catch(() => {
        // Quietly: failing to fetch viewer settings already breaks the
        // rest of the app and surfaces its own banner; the toast is
        // strictly additive UX.
      })
    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [])

  useEffect(() => {
    if (!visible) return
    // Only run the auto-dismiss timer while the toast is actually
    // showing. On admin/login routes the toast is rendered as null, so
    // counting down there would silently consume the notice without the
    // user ever seeing it; the timer (re)starts when the user navigates
    // back to a public route.
    if (!isPublicAnalyticsRoute(location.pathname)) return
    // Same reasoning for a hidden tab: (re)start the full countdown once the
    // user actually brings the tab to the front.
    if (!pageVisible) return
    // And while the reader is on it; leaving restarts the full countdown,
    // as a return to the tab does.
    if (paused) return
    const id = window.setTimeout(() => {
      setVisible(false)
      if (mode) persistDismissed(mode)
    }, AUTO_DISMISS_MS)
    return () => window.clearTimeout(id)
  }, [visible, mode, location.pathname, pageVisible, paused])

  // The live region is always mounted and the notice inserted into it: a
  // role="status" element mounted together with its text is often not
  // announced at all, as screen readers only watch regions that already
  // exist for changes. Hiding on admin/login routes leaves the component
  // (and so the region) mounted, so a later navigation back to a public
  // route doesn't force a fresh settings fetch.
  if (!showing || !mode) return <div role="status" aria-live="polite" />

  const headline = t('analyticsNotice.title')
  const body =
    mode === 'hashed'
      ? t('analyticsNotice.bodyHashed')
      : t('analyticsNotice.bodyPlain')

  const handleClose = () => {
    setVisible(false)
    persistDismissed(mode)
  }

  return (
    <div role="status" aria-live="polite">
      <div
        onPointerEnter={e => { if (e.pointerType === 'mouse') setHovered(true) }}
        onPointerLeave={e => { if (e.pointerType === 'mouse') setHovered(false) }}
        onFocus={() => setFocused(true)}
        onBlur={e => {
          // Focus moving between controls inside the toast keeps it held.
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false)
        }}
        style={{
          position: 'fixed',
          bottom: 'calc(20px + env(safe-area-inset-bottom, 0px))',
          right: 'calc(20px + env(safe-area-inset-right, 0px))',
          zIndex: 1000,
          maxWidth: 360,
          background: 'rgba(17, 24, 39, 0.96)',
          color: '#f9fafb',
          borderRadius: 'var(--radius-lg, 8px)',
          boxShadow: '0 10px 25px rgba(0, 0, 0, 0.25)',
          padding: '14px 18px',
          display: 'flex',
          gap: 12,
          alignItems: 'flex-start',
          fontSize: 'var(--font-size-sm, 13px)',
          lineHeight: 1.45,
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{headline}</div>
          <div style={{ color: '#d1d5db' }}>{body}</div>
        </div>
        <button
          type="button"
          onClick={handleClose}
          aria-label={t('analyticsNotice.close')}
          style={{
            background: 'transparent',
            border: 'none',
            color: '#d1d5db',
            fontSize: 18,
            lineHeight: 1,
            cursor: 'pointer',
            padding: 4,
            marginTop: -4,
          }}
        >
          ×
        </button>
      </div>
    </div>
  )
}
