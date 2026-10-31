import React from 'react'
import { metaFieldLabel } from '../../lib/metaLabel'

// Checkbox list with up/down ordering. Selected items appear at the top
// in user-defined order; unselected items follow in their natural order
// so admins can always find them. Shared between the three metadata
// display settings (viewer panel / top-page card / top-page filter).
type Props = {
  selected: string[]
  allKeys: readonly string[]   // built-in keys to show as candidates
  customKeys: string[]         // admin-added custom category keys
  t: (k: string, p?: Record<string, string | number>) => string
  onChange: (next: string[]) => void
}

export default function OrderedAttrList({ selected, allKeys, customKeys, t, onChange }: Props) {
  const selectedSet = new Set(selected)
  // Natural order of unselected candidates: built-ins first, then customs.
  const unselected = [...allKeys, ...customKeys].filter(
    (k, i, a) => a.indexOf(k) === i && !selectedSet.has(k)
  )
  const toggle = (key: string) => {
    if (selectedSet.has(key)) onChange(selected.filter(k => k !== key))
    else onChange([...selected, key])
  }
  const move = (idx: number, delta: number) => {
    const target = idx + delta
    if (target < 0 || target >= selected.length) return
    const next = selected.slice()
    const [item] = next.splice(idx, 1)
    next.splice(target, 0, item)
    onChange(next)
  }
  const rowStyle: React.CSSProperties = {
    fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer',
  }
  const btnStyle: React.CSSProperties = {
    padding: '0 6px', fontSize: 11, lineHeight: 1.4,
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {selected.map((key, idx) => (
        <label key={key} style={rowStyle}>
          <input type="checkbox" checked onChange={() => toggle(key)} />
          {metaFieldLabel(t, key)}
          <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 2 }}>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); move(idx, -1) }}
              disabled={idx === 0}
              style={btnStyle}
              aria-label={t('adminDash.moveUp')}
              title={t('adminDash.moveUp')}
            >↑</button>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); move(idx, 1) }}
              disabled={idx === selected.length - 1}
              style={btnStyle}
              aria-label={t('adminDash.moveDown')}
              title={t('adminDash.moveDown')}
            >↓</button>
          </span>
        </label>
      ))}
      {unselected.map((key) => (
        <label key={key} style={rowStyle}>
          <input type="checkbox" checked={false} onChange={() => toggle(key)} />
          {metaFieldLabel(t, key)}
        </label>
      ))}
    </div>
  )
}
