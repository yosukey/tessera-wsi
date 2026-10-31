import React from 'react'
import { useI18n } from '../../lib/i18n'
import { Button } from '../ui'

// The frame shared by the slide and tour bulk-action bars: a select-all
// checkbox (indeterminate while only some visible items are selected), the
// selected count, the bar's own actions, and Clear last.
type Props = {
  selectedCount: number
  allVisibleSelected: boolean
  selectAllLabel: string
  selectedLabel: string
  busy: boolean
  onSelectAll: () => void
  onClear: () => void
  // Names the bar as a group, when the page shows more than one.
  ariaLabel?: string
  children: React.ReactNode
}

export default function BulkSelectionBar({
  selectedCount, allVisibleSelected, selectAllLabel, selectedLabel, busy,
  onSelectAll, onClear, ariaLabel, children,
}: Props) {
  const { t } = useI18n()
  return (
    <div
      role={ariaLabel ? 'group' : undefined}
      aria-label={ariaLabel}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
        // Bottom spacing is owned by the sticky wrapper in AdminDashboard so the
        // opaque background fully covers the gap while the bar is pinned (a
        // transparent margin would let cards show through during scroll).
        padding: '8px 12px',
        border: '1px solid var(--color-brand-300)',
        borderRadius: 'var(--radius-md)',
        background: 'var(--color-brand-50)',
      }}
    >
      <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--font-size-sm)', cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={allVisibleSelected}
          // Like the list itself: a bulk action clears the selection it was
          // sent with, so a change made meanwhile would be undone unseen.
          disabled={busy}
          ref={el => { if (el) el.indeterminate = selectedCount > 0 && !allVisibleSelected }}
          onChange={() => (allVisibleSelected ? onClear() : onSelectAll())}
        />
        {selectAllLabel}
      </label>

      <span style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600, color: 'var(--color-brand-700)' }}>
        {selectedLabel}
      </span>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginLeft: 'auto', alignItems: 'center' }}>
        {children}
        <Button size="sm" variant="ghost" disabled={busy} onClick={onClear}>
          {t('adminDash.bulkClearSelection')}
        </Button>
      </div>
    </div>
  )
}
