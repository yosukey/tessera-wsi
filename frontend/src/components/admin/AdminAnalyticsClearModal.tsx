import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import type { AnalyticsClearScope } from '../../lib/api'
import { useI18n } from '../../lib/i18n'

type Props = {
  onCancel: () => void
  // `{ all: true }` for the wipe-everything choice (both dates blank), else the
  // range. The scope is explicit so the server can refuse a scope-less request.
  onSubmit: (scope: AnalyticsClearScope) => Promise<void> | void
  // The analytics timezone the dates are read in (serverTzLabel), shown next
  // to the inputs; '' before the dashboard has reported it.
  tzLabel?: string
}

type Phase = 'form' | 'confirm'

// The word a wipe-everything must be confirmed with by typing it. Fixed ASCII
// in every locale: it is shown in the dialog, and an IME-composed word would
// make the confirmation itself the hard part.
export const WIPE_CONFIRM_WORD = 'DELETE'

const WARNING_ID = 'analytics-clear-warning'

function pickConfirmKey(fromDate: string, toDate: string): string {
  if (fromDate && toDate) return 'analytics.clearRangeConfirm'
  if (fromDate) return 'analytics.clearFromConfirm'
  if (toDate) return 'analytics.clearToConfirm'
  return 'analytics.clearAllConfirm'
}

export default function AdminAnalyticsClearModal({ onCancel, onSubmit, tzLabel = '' }: Props) {
  const { t } = useI18n()
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [phase, setPhase] = useState<Phase>('form')
  const [typedWord, setTypedWord] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const submittingRef = useRef(false)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const proceedRef = useRef<HTMLButtonElement | null>(null)
  const backRef = useRef<HTMLButtonElement | null>(null)
  const typedRef = useRef<HTMLInputElement | null>(null)
  useDialogFocus(panelRef)

  const trimmedFrom = fromDate.trim()
  const trimmedTo = toDate.trim()
  const rangeInverted = !!(trimmedFrom && trimmedTo && trimmedFrom > trimmedTo)
  const wipeAll = !trimmedFrom && !trimmedTo
  // NFKC first: with a Japanese IME left in full-width mode the field reads
  // ＤＥＬＥＴＥ, which is the word the dialog asked for, not a typo — the
  // same "the confirmation must not be the hard part" reason the word is ASCII.
  const wordOk = typedWord.normalize('NFKC').trim() === WIPE_CONFIRM_WORD

  // A phase switch unmounts the button that had focus, which would drop focus
  // to <body> — outside the dialog, so a screen reader announces nothing and
  // Tab escapes the trap. Move it to the new phase's natural control instead:
  // the typed confirmation for a wipe-everything, Back (the safe choice) for a
  // ranged clear, and the Delete button again on returning to the form.
  // Focus moves only when the phase differs from the one last acted on, so the
  // mount run (and StrictMode's second mount run, which a "skip the first run"
  // flag would let through — landing the initial focus on Delete instead of
  // where useDialogFocus put it) leaves the initial focus alone.
  const lastPhase = useRef<Phase>(phase)
  useEffect(() => {
    if (lastPhase.current === phase) return
    lastPhase.current = phase
    if (phase === 'confirm') (wipeAll ? typedRef.current : backRef.current)?.focus()
    else proceedRef.current?.focus()
    // Only a phase change moves focus; wipeAll is fixed while confirming.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  const requestCancel = () => { if (!submittingRef.current) onCancel() }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // An Escape during IME composition (in the typed confirmation, or a
      // date typed through an IME) cancels the conversion, not the dialog;
      // keyCode 229 as in the Enter guard below.
      if (e.isComposing || e.keyCode === 229) return
      if (e.key === 'Escape') requestCancel()
    }
    window.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onProceed = () => {
    if (rangeInverted) {
      setError(t('analytics.clearRangeInvalid'))
      return
    }
    setError('')
    setPhase('confirm')
  }

  const onBack = () => {
    setTypedWord('')
    setPhase('form')
  }

  const onConfirm = async () => {
    if (submittingRef.current) return
    if (wipeAll && !wordOk) return
    submittingRef.current = true
    setSubmitting(true)
    try {
      await onSubmit(wipeAll
        ? { all: true }
        : { fromDate: trimmedFrom || undefined, toDate: trimmedTo || undefined })
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  const confirmMsg = t(pickConfirmKey(trimmedFrom, trimmedTo), {
    from: trimmedFrom,
    to: trimmedTo,
  })

  return createPortal((
    <div
      className="overscroll-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="analytics-clear-title"
      aria-describedby={phase === 'confirm' ? WARNING_ID : undefined}
      onClick={requestCancel}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0, 0, 0, 0.45)',
        zIndex: 'var(--z-modal, 1000)' as unknown as number,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: 20,
        overflowY: 'auto',
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--color-surface, #fff)',
          border: '1px solid var(--color-border)',
          borderRadius: 12,
          padding: 16,
          width: '100%',
          maxWidth: 480,
          boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))',
          margin: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
        }}
      >
        <div id="analytics-clear-title" style={{ fontWeight: 800 }}>
          {t('analytics.clearTitle')}
        </div>

        {phase === 'form' ? (
          <>
            <div style={{ fontSize: 12, opacity: 0.75 }}>
              {t('analytics.clearHelp')}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>
                  {t('analytics.clearFromLabel')}
                </span>
                <input
                  type="date"
                  value={fromDate}
                  onChange={e => setFromDate(e.target.value)}
                  style={{ boxSizing: 'border-box', width: '100%' }}
                />
              </label>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>
                  {t('analytics.clearToLabel')}
                </span>
                <input
                  type="date"
                  value={toDate}
                  onChange={e => setToDate(e.target.value)}
                  style={{ boxSizing: 'border-box', width: '100%' }}
                />
              </label>
            </div>
            {tzLabel && (
              <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginTop: -6 }}>
                {t('analytics.clearTzNote', { tz: tzLabel })}
              </div>
            )}
            {error && (
              <div style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>{error}</div>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" onClick={requestCancel}>
                {t('analytics.clearCancel')}
              </button>
              <button
                ref={proceedRef}
                type="button"
                onClick={onProceed}
                disabled={rangeInverted}
                style={{
                  color: 'var(--color-danger-fg)',
                  borderColor: 'var(--color-danger-fg)',
                }}
              >
                {t('analytics.clearSubmit')}
              </button>
            </div>
          </>
        ) : (
          <>
            {/* role="alert": the irreversible-delete warning is announced as
                it appears, not only if the user happens to read back to it;
                aria-describedby on the dialog and its controls ties it to
                them for a screen reader that re-reads on focus. */}
            <div id={WARNING_ID} role="alert" style={{ fontSize: 13 }}>{confirmMsg}</div>
            {wipeAll && (
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>
                  {t('analytics.clearTypeToConfirm', { word: WIPE_CONFIRM_WORD })}
                </span>
                <input
                  ref={typedRef}
                  type="text"
                  value={typedWord}
                  onChange={e => setTypedWord(e.target.value)}
                  onKeyDown={e => {
                    // The Enter that commits an IME composition is not a
                    // submit: with a Japanese IME the field already reads
                    // DELETE while the word is still being composed, so that
                    // Enter would fire the wipe. (keyCode 229 is how browsers
                    // that leave isComposing unset mark the same key.)
                    if (e.nativeEvent.isComposing || e.keyCode === 229) return
                    if (e.key === 'Enter' && wordOk) void onConfirm()
                  }}
                  disabled={submitting}
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  aria-describedby={WARNING_ID}
                  style={{ boxSizing: 'border-box', width: '100%', fontFamily: 'monospace' }}
                />
              </label>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                ref={backRef}
                type="button"
                onClick={onBack}
                disabled={submitting}
                aria-describedby={WARNING_ID}
              >
                {t('analytics.clearConfirmBack')}
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={submitting || (wipeAll && !wordOk)}
                style={{
                  color: 'var(--color-danger-fg)',
                  borderColor: 'var(--color-danger-fg)',
                }}
              >
                {submitting ? t('analytics.loading') : t('analytics.clearConfirmSubmit')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  ), document.body)
}
