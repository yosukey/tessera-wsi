import React from 'react'
import { useI18n } from '../../lib/i18n'
import { metaFieldLabel } from '../../lib/metaLabel'
import { Chip } from '../ui'
import type { ToursFilter } from '../../lib/admin/validation'

type Props = {
  attrFacets: Record<string, string[]>
  vendorFacet: string[]
  attrFilters: Record<string, Set<string>>
  vendorFilter: Set<string>
  toursFilter: ToursFilter
  activeFilterCount: number
  onAttrFilterToggle: (key: string, value: string) => void
  onVendorFilterToggle: (value: string) => void
  onToursFilterChange: (v: ToursFilter) => void
  onClearAll: () => void
}

export default function SlideFilterPanel({
  attrFacets, vendorFacet,
  attrFilters, vendorFilter, toursFilter,
  activeFilterCount,
  onAttrFilterToggle, onVendorFilterToggle, onToursFilterChange, onClearAll,
}: Props) {
  const { t } = useI18n()
  const labelStyle: React.CSSProperties = {
    fontSize: 10, fontWeight: 700, color: 'var(--color-text-faint)',
    textTransform: 'uppercase', letterSpacing: '0.08em',
    minWidth: 92, textAlign: 'right', flexShrink: 0, paddingTop: 6,
  }
  // Bottom spacing is owned by the sticky wrapper in AdminDashboard so the
  // opaque background fully covers the gap while the panel is pinned (a
  // transparent margin would let cards show through during scroll).
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '10px 12px', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)', background: 'var(--color-surface)' }}>
      {Object.entries(attrFacets).map(([key, values]) => {
        const picked = attrFilters[key] || new Set<string>()
        return (
          <div key={key} style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <span style={labelStyle}>{metaFieldLabel(t, key)}</span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {values.map(v => (
                <Chip key={v} active={picked.has(v)} onClick={() => onAttrFilterToggle(key, v)}>{v}</Chip>
              ))}
            </div>
          </div>
        )
      })}

      {vendorFacet.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <span style={labelStyle}>{t('adminDash.filterVendor')}</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {vendorFacet.map(v => (
              <Chip key={v} active={vendorFilter.has(v)} onClick={() => onVendorFilterToggle(v)}>{v}</Chip>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={labelStyle}>{t('adminDash.filterTours')}</span>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          <Chip active={toursFilter === 'all'} onClick={() => onToursFilterChange('all')}>{t('adminDash.toursAll')}</Chip>
          <Chip active={toursFilter === 'has'} onClick={() => onToursFilterChange('has')}>{t('adminDash.toursHas')}</Chip>
          <Chip active={toursFilter === 'none'} onClick={() => onToursFilterChange('none')}>{t('adminDash.toursNone')}</Chip>
        </div>
      </div>

      {activeFilterCount > 0 && (
        <div style={{ paddingLeft: 104 }}>
          <button
            type="button"
            className="btn-reset"
            onClick={onClearAll}
            style={{
              padding: '3px 12px',
              border: '1px solid var(--color-danger-border)',
              borderRadius: 'var(--radius-pill)',
              background: 'var(--color-danger-bg)',
              color: 'var(--color-danger-fg)',
              fontSize: 'var(--font-size-xs)',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            {t('adminDash.clearFilters', { count: activeFilterCount })}
          </button>
        </div>
      )}
    </div>
  )
}
