/**
 * SyncIdleToast — presenter-facing toast for the live-sync inactivity timeout.
 *
 * Two shapes, driven by useSyncPresenterSession's ``idleNotice``:
 *   - 'warning': the server will force-end the session for inactivity in
 *     ``seconds``. Amber, with a local countdown; it clears the instant the
 *     presenter interacts (the hook drops it on the next view/cursor frame).
 *   - 'ended':   the session was just force-ended (inactivity, or the absolute
 *     session-age cap). Dark, dismissable, and auto-dismisses after a short delay.
 *   - 'maxAgeWarning': the age cap ends the session in a few minutes; activity
 *     does not postpone it, so it is dismissable like 'ended'.
 *   - 'unavailable': the presenter moved to an unpublished slide; followers
 *     stay on the previous one and the pointer is withheld.
 *   - 'unpublished': the slide being presented was unpublished meanwhile, so
 *     followers can no longer load it until the presenter moves on.
 *   - 'unlistedAnnounced': the session was announced on the index page and is
 *     now on a link-only slide, which anyone who saw the banner can follow.
 *
 * Followers are notified separately (the ended overlay), so this is rendered
 * only on the presenter's screen.
 */
import React, { useEffect, useState } from 'react'
import type { IdleNotice } from '../hooks/useSyncPresenterSession'
import { useI18n } from '../lib/i18n'

const ENDED_AUTO_DISMISS_MS = 12000
const MAX_AGE_WARNING_AUTO_DISMISS_MS = 45000

export default function SyncIdleToast({
  notice,
  onDismiss,
}: {
  notice: IdleNotice | null
  onDismiss: () => void
}) {
  const { t } = useI18n()

  // Local countdown for the warning, seeded from the server's seconds and
  // ticked down once a second so the presenter sees time running out. Keyed on
  // the `notice` object (a fresh object per warning), NOT on `notice.seconds`:
  // the server always warns at the same value (e.g. 60), so keying on the
  // primitive would skip re-seeding when a SECOND warning arrives with the same
  // seconds, leaving the toast resuming a stale (possibly already-0) countdown.
  const warnSeconds = notice?.kind === 'warning' ? notice.seconds : null
  const [remaining, setRemaining] = useState(warnSeconds ?? 0)
  useEffect(() => {
    if (notice?.kind !== 'warning') return
    setRemaining(notice.seconds)
    const id = window.setInterval(() => {
      setRemaining((r) => (r <= 1 ? 0 : r - 1))
    }, 1000)
    return () => window.clearInterval(id)
  }, [notice])

  // Everything but the idle countdown is dismissable and auto-dismisses so it
  // doesn't sit over the launcher forever once the presenter has seen it.
  const dismissable = !!notice && notice.kind !== 'warning'
  const autoDismissMs = notice?.kind === 'maxAgeWarning' ? MAX_AGE_WARNING_AUTO_DISMISS_MS : ENDED_AUTO_DISMISS_MS
  useEffect(() => {
    if (!notice || !dismissable) return
    const id = window.setTimeout(onDismiss, autoDismissMs)
    return () => window.clearTimeout(id)
  }, [notice, dismissable, autoDismissMs, onDismiss])

  if (!notice) return null

  const warning = notice.kind !== 'ended'
  const body =
    notice.kind === 'warning'
      ? (t('sync.idleWarning', { seconds: remaining }) as string)
      : notice.kind === 'maxAgeWarning'
        ? (t('sync.maxAgeWarning', { minutes: Math.max(1, Math.round(notice.seconds / 60)) }) as string)
        : notice.kind === 'unavailable'
          ? (t('sync.slideUnavailable') as string)
          : notice.kind === 'unpublished'
            ? (t('sync.slideUnpublished') as string)
            : notice.kind === 'unlistedAnnounced'
              ? (t('sync.unlistedAnnounced') as string)
              : notice.reason === 'max_age'
                ? (t('sync.maxAgeEnded') as string)
                : (t('sync.idleEnded') as string)

  return (
    <div
      role={warning ? 'status' : 'alert'}
      aria-live={warning ? 'polite' : 'assertive'}
      style={{
        position: 'absolute',
        top: 'calc(12px + env(safe-area-inset-top, 0px))',
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 60,
        maxWidth: 'min(92%, 420px)',
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
        background: warning ? 'rgba(180, 83, 9, 0.97)' : 'rgba(17, 24, 39, 0.97)',
        color: '#fff',
        borderRadius: 'var(--radius-lg, 8px)',
        boxShadow: '0 10px 25px rgba(0, 0, 0, 0.3)',
        padding: '12px 16px',
        fontSize: 'var(--font-size-sm, 13px)',
        lineHeight: 1.45,
      }}
    >
      <span aria-hidden style={{ fontSize: 16, lineHeight: 1.2 }}>
        {warning ? '⏳' : '⏹'}
      </span>
      <div style={{ flex: 1 }}>{body}</div>
      {/* The idle warning has no dismiss: closing it wouldn't stop the
          server-side countdown, and interacting (which DOES stop it) clears the
          toast on its own. Every other notice is dismissable. */}
      {dismissable && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={t('common.close') as string}
          style={{
            background: 'transparent',
            border: 'none',
            color: '#d1d5db',
            fontSize: 18,
            lineHeight: 1,
            cursor: 'pointer',
            padding: 2,
            marginTop: -2,
          }}
        >
          ×
        </button>
      )}
    </div>
  )
}
