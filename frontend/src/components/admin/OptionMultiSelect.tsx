import React from 'react'
import type { TFn } from '../../lib/admin/validation'

interface Props {
  label: string
  k: string
  options: string[]
  selected: string[]
  onChange: (next: string[]) => void
  t: TFn
  // Tooltip of a selected value that is no longer registered. Defaults to the
  // slide editor's wording.
  orphanTitle?: (value: string) => string
}

export default function OptionMultiSelect({ label, k, options, selected, onChange, t, orphanTitle }: Props) {
  // Include any values on the slide that are no longer in the master list so the
  // admin can still see and clear them.
  const merged = Array.from(new Set([...options, ...selected]))
  const toggle = (v: string) => {
    if (selected.includes(v)) onChange(selected.filter((x) => x !== v))
    else onChange([...selected, v])
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{label}</span>
      {merged.length === 0 ? (
        <div style={{ fontSize: 11, opacity: 0.6, fontStyle: 'italic' }}>{t('adminDash.metaNoValuesHint')}</div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {merged.map((v) => {
            const isOrphan = !options.includes(v)
            const isSelected = selected.includes(v)
            return (
              <label
                key={v}
                title={isOrphan ? (orphanTitle ? orphanTitle(v) : t('adminDash.metaOrphanValue', { key: k, value: v })) : ''}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                  fontSize: 12, padding: '2px 8px', borderRadius: 12, cursor: 'pointer',
                  border: `1px solid ${isSelected ? 'var(--color-brand-600)' : 'var(--color-border)'}`,
                  background: isSelected ? 'var(--color-brand-50)' : 'var(--color-surface)',
                  color: isSelected ? 'var(--color-brand-700)' : 'inherit',
                  opacity: isOrphan ? 0.7 : 1,
                }}
              >
                <input type="checkbox" checked={isSelected} onChange={() => toggle(v)} style={{ margin: 0 }} />
                {v}{isOrphan ? ' *' : ''}
              </label>
            )
          })}
        </div>
      )}
    </div>
  )
}
