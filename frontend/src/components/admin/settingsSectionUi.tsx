import React, { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../../lib/i18n'

// Layout and status-line pieces shared by the viewer-settings sections
// (AdminViewerSettingsSection, AdminSeasonalBackgroundsSection).

export const subHeading: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: 'inherit',
  marginBottom: 8,
}

export const itemRow: React.CSSProperties = {
  border: '1px solid var(--color-border)',
  borderRadius: 5,
  padding: '8px 10px',
}

export type MsgTone = 'success' | 'error'
export type SectionMsg = { text: string; tone: MsgTone }

// The tone travels with the message so the renderer and the auto-dismiss can
// treat success and error differently without parsing localized text. Only
// success messages auto-dismiss (after 6 s); errors carry the server's
// failure reason and stay until the admin dismisses them. The next setMsg()
// restarts the timer.
export function useSectionMessage() {
  const [msg, setMsgState] = useState<SectionMsg | null>(null)
  const setMsg = useCallback((text: string, tone: MsgTone) => setMsgState(text ? { text, tone } : null), [])
  const clearMsg = useCallback(() => setMsgState(null), [])
  useEffect(() => {
    if (!msg || msg.tone !== 'success') return
    const id = setTimeout(() => setMsgState(null), 6000)
    return () => clearTimeout(id)
  }, [msg])
  return { msg, setMsg, clearMsg }
}

export function SectionMessage({ msg, onDismiss, style }: {
  msg: SectionMsg | null
  onDismiss: () => void
  style?: React.CSSProperties
}) {
  const { t } = useI18n()
  if (!msg) return null
  return (
    <div
      role={msg.tone === 'error' ? 'alert' : 'status'}
      style={{
        fontSize: 12,
        padding: '4px 6px',
        borderRadius: 4,
        whiteSpace: 'pre-line',
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        background: msg.tone === 'success' ? 'var(--color-success-bg)' : 'var(--color-danger-bg)',
        color: msg.tone === 'success' ? 'var(--color-success-fg, inherit)' : 'var(--color-danger-fg, inherit)',
        ...style,
      }}
    >
      <span style={{ flex: 1 }}>{msg.text}</span>
      {msg.tone === 'error' && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={t('adminDash.dismiss')}
          title={t('adminDash.dismiss')}
          style={{
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            color: 'inherit',
            padding: '0 2px',
            fontSize: 14,
            lineHeight: 1,
          }}
        >
          ×
        </button>
      )}
    </div>
  )
}
