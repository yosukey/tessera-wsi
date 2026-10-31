import React from 'react'

interface Props {
  label: string
  // null = the global default could not be loaded. The row is then read-only:
  // toggling decides between "explicit override" and "inherit" by comparing
  // against the global value, and deciding that against a guess can store
  // inherit where the operator meant an explicit value (or vice versa).
  globalValue: boolean | null
  perSlide: boolean | null
  onChange: (next: boolean | null) => void
  overrideLabel: string
  overrideTitleFn: (globalValue: boolean, effectiveValue: boolean) => string
  resetTitle: string
}

export default function OverlayOverrideRow({
  label, globalValue, perSlide, onChange, overrideLabel, overrideTitleFn, resetTitle,
}: Props) {
  const unknownGlobal = globalValue === null
  const effective = perSlide ?? globalValue
  const overridden = !unknownGlobal && perSlide !== null && perSlide !== globalValue
  // Show reset button whenever there is any explicit per-slide value, even
  // when it currently matches the global default — the override is still
  // stored in the DB and will diverge if the global default changes later.
  const hasOverride = perSlide !== null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: unknownGlobal ? 'not-allowed' : 'pointer', flex: 1, opacity: unknownGlobal ? 0.6 : undefined }}>
        <input
          type="checkbox"
          checked={effective === true}
          disabled={unknownGlobal}
          // Inheriting an unknown default: neither checked nor unchecked is true.
          ref={(el) => { if (el) el.indeterminate = effective === null }}
          onChange={(e) => {
            if (globalValue === null) return
            const next = e.target.checked
            // Toggling to the global value clears the override (null).
            // Toggling away from global creates an explicit override.
            // The × reset button handles the corner case where an existing
            // explicit override happens to match the global default — e.g.,
            // after the admin changes the global setting.
            onChange(next === globalValue ? null : next)
          }}
        />
        {label}
      </label>
      {overridden && (
        <span
          title={overrideTitleFn(globalValue as boolean, effective as boolean)}
          style={{
            fontSize: 10,
            fontWeight: 600,
            padding: '1px 6px',
            borderRadius: 4,
            background: 'var(--color-warning-bg, #fef3c7)',
            color: 'var(--color-warning-fg, #92400e)',
            border: '1px solid var(--color-warning-fg, #92400e)',
            flexShrink: 0,
          }}
        >
          {overrideLabel}
        </span>
      )}
      {hasOverride && (
        <button
          type="button"
          onClick={() => onChange(null)}
          disabled={unknownGlobal}
          title={resetTitle}
          style={{
            cursor: unknownGlobal ? 'not-allowed' : 'pointer',
            background: 'none',
            border: '1px solid var(--color-border-strong, #aaa)',
            borderRadius: 4,
            fontSize: 11,
            lineHeight: 1,
            padding: '1px 5px',
            color: 'var(--color-text-muted)',
            flexShrink: 0,
          }}
        >
          ×
        </button>
      )}
    </div>
  )
}
