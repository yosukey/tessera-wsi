import React from 'react'
import { useI18n } from '../../lib/i18n'

// Layout pieces shared by the Network tab's two panels (AdminNetworkPanel,
// AdminAuthPanel): their blocks (Top page URL, IP filter, mDNS, ...), block
// titles and the small labels above text fields.
export const SECTION_STYLE: React.CSSProperties = { borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }
export const SECTION_TITLE_STYLE: React.CSSProperties = { fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'inherit', marginBottom: 8 }
export const FIELD_LABEL_STYLE: React.CSSProperties = { fontSize: 10, color: 'var(--color-text-muted)', fontWeight: 600 }
// A Save button while its form has unsaved edits.
export const DIRTY_SAVE_STYLE: React.CSSProperties = {
  background: 'var(--color-brand-600)',
  color: 'var(--color-on-brand, #fff)',
  borderColor: 'var(--color-brand-600)',
}

// The chip beside a Save button while its form has unsaved edits.
export function UnsavedChangesChip() {
  const { t } = useI18n()
  return (
    <span style={{
      fontSize: 11,
      fontWeight: 600,
      padding: '2px 8px',
      borderRadius: 4,
      background: 'var(--color-warning-bg, #fef3c7)',
      color: 'var(--color-warning-fg, #92400e)',
    }}>
      {t('adminDash.unsavedChanges')}
    </span>
  )
}

// The outcome line under a Save button.
export function SaveResult({ text, isError }: { text: string; isError: boolean }) {
  if (!text) return null
  return (
    <div style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, whiteSpace: 'pre-wrap', background: isError ? 'var(--color-danger-bg)' : 'var(--color-success-bg)' }}>
      {text}
    </div>
  )
}
