import React from 'react'
import { useI18n } from '../lib/i18n'

// Bottom-left switch for OpenSeadragon's debug overlay (tile boundaries and
// per-tile metadata), shown in the viewers' admin previews (?from=admin) and
// in the tour editor.
export default function DebugToggle({ enabled, onToggle }: { enabled: boolean; onToggle: () => void }) {
  const { t } = useI18n()
  return (
    <button
      type="button"
      onClick={onToggle}
      title={enabled ? t('viewer.debugOn') : t('viewer.debugOff')}
      style={{
        position: 'absolute', bottom: 'calc(52px + env(safe-area-inset-bottom, 0px))', left: 'calc(12px + env(safe-area-inset-left, 0px))', zIndex: 10,
        background: enabled ? 'var(--color-warning-bg, #fef3c7)' : 'rgba(255, 255, 255, 0.92)',
        color: enabled ? 'var(--color-warning-fg, #92400e)' : 'var(--color-text, #111)',
        border: `1px solid ${enabled ? 'var(--color-warning-fg, #92400e)' : 'var(--color-border, #ccc)'}`,
        borderRadius: 'var(--radius-md, 6px)',
        padding: '4px 10px',
        fontSize: 12,
        fontWeight: 600,
        cursor: 'pointer',
        boxShadow: 'var(--shadow-sm)',
        letterSpacing: '0.02em',
      }}
    >
      {t('viewer.debugLabel', { enabled: enabled ? 1 : 0 })}
    </button>
  )
}
