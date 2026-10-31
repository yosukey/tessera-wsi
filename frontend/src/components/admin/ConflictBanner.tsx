import React from 'react'

interface Props {
  message: string
  reloadLabel: string
  overwriteLabel: string
  onReload: () => void
  onOverwrite: () => void
  // True while a save is in flight: both ways out wait for it to land.
  disabled?: boolean
}

// Shown when a save was refused (409) because the thing being edited changed
// in another window or by another user since the editor loaded it. The
// editor keeps the user's unsaved edits; this offers the two ways out: drop
// them for the server's version, or save them over it.
export default function ConflictBanner({ message, reloadLabel, overwriteLabel, onReload, onOverwrite, disabled }: Props) {
  return (
    <div
      role="alert"
      style={{
        marginTop: 10,
        padding: '8px 10px',
        borderRadius: 8,
        border: '1px solid var(--color-warning-border)',
        background: 'var(--color-warning-bg)',
        color: 'var(--color-warning-fg)',
        fontSize: 12,
        display: 'grid',
        gap: 6,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div>{message}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button type="button" onClick={onReload} disabled={disabled}>{reloadLabel}</button>
        <button type="button" onClick={onOverwrite} disabled={disabled}>{overwriteLabel}</button>
      </div>
    </div>
  )
}
