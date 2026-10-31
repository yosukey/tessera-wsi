import React from 'react'
import { useI18n } from '../../lib/i18n'
import { Badge, ClearButton, IconButton, Input, Select } from '../ui'
import { SORT_OPTIONS, formatBytes } from '../../lib/admin/validation'
import type { SortField, SortOrder } from '../../lib/admin/validation'

type Props = {
  searchQ: string
  onSearchChange: (v: string) => void
  sortField: SortField
  onSortFieldChange: (v: SortField) => void
  sortOrder: SortOrder
  onSortOrderToggle: () => void
  filtersOpen: boolean
  onFiltersToggle: () => void
  hasFilterableData: boolean
  activeFilterCount: number
  visibleCount: number
  totalCount: number
  totalFileBytes: number
}

export default function SlideListToolbar({
  searchQ, onSearchChange,
  sortField, onSortFieldChange,
  sortOrder, onSortOrderToggle,
  filtersOpen, onFiltersToggle,
  hasFilterableData, activeFilterCount,
  visibleCount, totalCount, totalFileBytes,
}: Props) {
  const { t } = useI18n()
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
      <div style={{ flex: 1, minWidth: 220, maxWidth: 420 }}>
        <Input
          size="sm"
          value={searchQ}
          onChange={e => onSearchChange(e.target.value)}
          placeholder={t('adminDash.searchPlaceholder')}
          leftAdornment={
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
              <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" strokeWidth="2" />
              <path d="M13 13L18 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          }
          rightAdornment={searchQ ? (
            <ClearButton onClick={() => onSearchChange('')} label={t('adminDash.clearSearch')} />
          ) : undefined}
        />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>{t('adminDash.sort')}</span>
        <Select size="sm" value={sortField} onChange={e => onSortFieldChange(e.target.value as SortField)}>
          {SORT_OPTIONS.map(o => <option key={o} value={o}>{t(`adminDash.sort.${o}`)}</option>)}
        </Select>
        <IconButton
          size="sm"
          onClick={onSortOrderToggle}
          // Describe the action the next click will perform, not the
          // current sort direction — for a toggle button the latter is
          // already conveyed by the ↑/↓ glyph and the actual ordering
          // of the list.
          title={sortOrder === 'asc' ? t('adminDash.order.toggleToDesc') : t('adminDash.order.toggleToAsc')}
          aria-label={sortOrder === 'asc' ? t('adminDash.order.toggleToDesc') : t('adminDash.order.toggleToAsc')}
        >
          {sortOrder === 'asc' ? '↑' : '↓'}
        </IconButton>
      </div>

      {hasFilterableData && (
        <button
          type="button"
          className="btn-reset"
          onClick={onFiltersToggle}
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '5px 10px',
            border: '1px solid',
            borderColor: activeFilterCount > 0 ? 'var(--color-brand-300)' : 'var(--color-border)',
            borderRadius: 'var(--radius-md)',
            background: activeFilterCount > 0 ? 'var(--color-brand-50)' : 'var(--color-surface)',
            color: activeFilterCount > 0 ? 'var(--color-brand-700)' : 'var(--color-text-muted)',
            fontSize: 'var(--font-size-sm)',
            fontWeight: activeFilterCount > 0 ? 600 : 500,
            cursor: 'pointer',
            minHeight: 28,
          }}
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <path d="M2 4h12M4 8h8M6 12h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
          {t('adminDash.filter')}
          {activeFilterCount > 0 && (
            <Badge tone="brand" pill>{activeFilterCount}</Badge>
          )}
          <span style={{ opacity: 0.6, fontSize: 9 }}>{filtersOpen ? '▲' : '▼'}</span>
        </button>
      )}

      <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)', marginLeft: 'auto' }}>
        {t('adminDash.slidesShown', { shown: visibleCount, total: totalCount })}
        {totalFileBytes > 0 && (
          <span style={{ marginLeft: 8, opacity: 0.7 }}>
            ({t('adminDash.totalFileSize', { size: formatBytes(totalFileBytes || null) })})
          </span>
        )}
      </span>
    </div>
  )
}
