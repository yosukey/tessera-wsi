import React from 'react'
import type { Facets, IndexTypeFilter, Slide, TourSummary } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { metaFieldLabel } from '../../lib/metaLabel'
import { compareValues } from '../../lib/naturalSort'
import { Badge, Chip, ClearButton, IconButton, IconButtonGroup, Input, Select } from '../ui'
import LanguageSwitcher from '../LanguageSwitcher'
import SignOutButton from '../SignOutButton'
import InfoIcon from '../icons/InfoIcon'
import { SORT_OPTIONS, TYPE_OPTIONS, THUMBNAIL_SIZE_OPTIONS, selectedValues } from './types'
import type { SortField, Order, ViewMode, ThumbnailSize } from './types'

type Props = {
  siteTitle: string
  titleBarBackgroundUrl?: string
  titleColor?: string
  inputQ: string
  onSearchChange: (v: string) => void
  sort: SortField
  order: Order
  type: IndexTypeFilter
  defaultIndexType: IndexTypeFilter
  viewMode: ViewMode
  onViewModeChange: (v: ViewMode) => void
  thumbnailSize: ThumbnailSize
  onThumbnailSizeChange: (v: ThumbnailSize) => void
  groupKeys: string[]
  facets: Facets
  // Order of the filter chips within each category: 'count' keeps the
  // backend's most-used-first order; 'alpha' sorts by value.
  filterSortOrder: 'count' | 'alpha'
  searchParams: URLSearchParams
  filtersOpen: boolean
  onFiltersToggle: () => void
  activeFilterCount: number
  displayedSlides: Slide[]
  displayedTours: TourSummary[]
  totalToursVisible: number
  onUpdateParams: (updater: (sp: URLSearchParams) => void) => void
  onToggleFilter: (key: string, value: string) => void
  onClearFilters: () => void
  onAbout?: () => void
  showAdminLink?: boolean
}

export default function IndexHeader({
  siteTitle,
  titleBarBackgroundUrl,
  titleColor,
  inputQ, onSearchChange,
  sort, order,
  type, defaultIndexType,
  viewMode, onViewModeChange,
  thumbnailSize, onThumbnailSizeChange,
  groupKeys, facets, filterSortOrder, searchParams,
  filtersOpen, onFiltersToggle,
  activeFilterCount,
  displayedSlides, displayedTours, totalToursVisible,
  onUpdateParams, onToggleFilter, onClearFilters,
  onAbout,
  showAdminLink = false,
}: Props) {
  const { t } = useI18n()

  return (
    <div style={{
      background: 'var(--color-surface)',
      boxShadow: 'var(--shadow-sm)',
    }}>
      {/* Row 1: title / lang / about / admin / sign out. Wraps on a narrow
          screen: the controls move under the title rather than being pushed
          off the right edge, and a title still too long for its own line is
          cut with an ellipsis. */}
      <div style={{
        display: 'flex', alignItems: 'center', flexWrap: 'wrap',
        columnGap: 14, rowGap: 8,
        padding: '10px 24px',
        borderBottom: '1px solid var(--color-border)',
        ...(titleBarBackgroundUrl
          ? {
              // encodeURI escapes `"`, so the URL can't break out of url("…").
              backgroundImage: `url("${encodeURI(titleBarBackgroundUrl)}")`,
              backgroundRepeat: 'repeat',
            }
          : null),
      }}>
        <span style={{
          fontSize: 'var(--font-size-lg)', fontWeight: 700,
          color: titleColor || 'var(--color-text)', whiteSpace: 'nowrap',
          letterSpacing: '-0.01em',
          minWidth: 0, maxWidth: '100%',
          overflow: 'hidden', textOverflow: 'ellipsis',
        }}>
          {siteTitle || t('index.siteTitleFallback')}
        </span>

        <div style={{
          display: 'flex', alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end',
          columnGap: 14, rowGap: 8,
          marginLeft: 'auto',
        }}>
          <LanguageSwitcher />

          {onAbout && (
            <button
              type="button"
              onClick={onAbout}
              title="About this app"
              aria-label="About this app"
              style={{
                background: 'transparent',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--color-text-faint)',
                padding: 4,
                lineHeight: 1,
                display: 'flex',
                alignItems: 'center',
                borderRadius: 4,
                flexShrink: 0,
                transition: 'color var(--duration-fast, 120ms)',
              }}
              onMouseEnter={e => (e.currentTarget.style.color = 'var(--color-text-subtle)')}
              onMouseLeave={e => (e.currentTarget.style.color = 'var(--color-text-faint)')}
            >
              <InfoIcon />
            </button>
          )}

          {showAdminLink && (
            // Full-page navigation (not <Link>) on purpose: entering /admin must
            // start a fresh document so the Google Analytics script loaded on
            // public pages is NOT carried into the admin SPA (where it could read
            // the JWT in sessionStorage). See useGoogleAnalytics in App.tsx.
            <a
              href="/admin"
              style={{
                fontSize: 'var(--font-size-xs)',
                color: 'var(--color-text-faint)',
                textDecoration: 'none',
                flexShrink: 0,
              }}
            >
              {t('common.admin')}
            </a>
          )}

          <SignOutButton />
        </div>
      </div>

      {/* Row 2: search / sort / type / count / filter / view mode */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 14,
        padding: '8px 24px',
        flexWrap: 'wrap',
        borderBottom: groupKeys.length > 0 ? '1px solid var(--color-border)' : 'none',
      }}>
        {/* Search */}
        <div style={{ flex: '1 1 240px', minWidth: 200, maxWidth: 420 }}>
          <Input
            size="sm"
            value={inputQ}
            onChange={e => onSearchChange(e.target.value)}
            placeholder={t('index.searchPlaceholder')}
            aria-label={t('index.searchLabel')}
            leftAdornment={
              <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
                <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" strokeWidth="2" />
                <path d="M13 13L18 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            }
            rightAdornment={inputQ ? (
              <ClearButton onClick={() => onSearchChange('')} label={t('index.clearSearch')} />
            ) : undefined}
          />
        </div>

        {/* Sort field + order */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>{t('index.sort')}</span>
          <Select
            size="sm"
            value={sort}
            onChange={e => onUpdateParams((sp) => sp.set('sort', e.target.value))}
          >
            {SORT_OPTIONS.map(o => <option key={o} value={o}>{t(`index.sort.${o}`)}</option>)}
          </Select>
          <IconButton
            size="sm"
            onClick={() => onUpdateParams((sp) => sp.set('order', order === 'asc' ? 'desc' : 'asc'))}
            title={order === 'asc' ? t('index.order.asc') : t('index.order.desc')}
            aria-label={order === 'asc' ? t('index.order.asc') : t('index.order.desc')}
          >
            {order === 'asc' ? '↑' : '↓'}
          </IconButton>
        </div>

        {/* Type filter (segmented) */}
        <div role="group" style={{
          display: 'inline-flex',
          border: '1px solid var(--color-border)',
          borderRadius: 'var(--radius-md)',
          overflow: 'hidden',
          background: 'var(--color-surface)',
          flexShrink: 0,
        }}>
          {TYPE_OPTIONS.map((tp, i) => {
            const active = type === tp
            return (
              <button
                key={tp}
                type="button"
                className="btn-reset"
                onClick={() => onUpdateParams((sp) => {
                  if (tp === defaultIndexType) sp.delete('type')
                  else sp.set('type', tp)
                })}
                title={t(`index.type.${tp}`)}
                aria-label={t(`index.type.${tp}`)}
                aria-pressed={active}
                style={{
                  padding: '4px 14px',
                  minHeight: 26,
                  borderRight: i < TYPE_OPTIONS.length - 1 ? '1px solid var(--color-border)' : 'none',
                  background: active ? 'var(--color-brand-50)' : 'transparent',
                  color: active ? 'var(--color-brand-700)' : 'var(--color-text-muted)',
                  fontSize: 'var(--font-size-sm)',
                  fontWeight: 600,
                  cursor: 'pointer',
                  whiteSpace: 'nowrap',
                  transition: 'background var(--duration-fast), color var(--duration-fast)',
                }}
              >
                {t(`index.type.${tp}`)}
              </button>
            )
          })}
        </div>

        {/* Count */}
        <span style={{
          fontSize: 'var(--font-size-sm)',
          color: 'var(--color-text-subtle)',
          whiteSpace: 'nowrap',
          flexShrink: 0,
        }}>
          {type === 'tours'
            ? t('index.toursCount', { count: displayedTours.length })
            : type === 'slides'
            ? t('index.slidesCount', { count: displayedSlides.length })
            : t('index.slideTourCount', { slides: displayedSlides.length, tours: totalToursVisible })}
        </span>

        <div style={{ flex: 1 }} />

        {/* Filter toggle. Also shown when no category is visible but the URL
            still carries a filter (e.g. its category was dropped from
            index_filter_attrs), so the badge and "clear" stay reachable. */}
        {(groupKeys.length > 0 || activeFilterCount > 0) && (
          <button
            type="button"
            className="btn-reset"
            onClick={onFiltersToggle}
            aria-expanded={filtersOpen}
            aria-controls={filtersOpen ? 'index-filter-panel' : undefined}
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
              cursor: 'pointer', flexShrink: 0,
              minHeight: 28,
              transition: 'border-color var(--duration-fast), background var(--duration-fast), color var(--duration-fast)',
            }}
          >
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
              <path d="M2 4h12M4 8h8M6 12h4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
            {t('index.filter')}
            {activeFilterCount > 0 && (
              <Badge tone="brand" pill>{activeFilterCount}</Badge>
            )}
            <span aria-hidden style={{ opacity: 0.6, fontSize: 9 }}>{filtersOpen ? '▲' : '▼'}</span>
          </button>
        )}

        {/* Thumbnail size selector */}
        <div role="group" style={{
          display: 'inline-flex',
          border: '1px solid var(--color-border)',
          borderRadius: 'var(--radius-md)',
          overflow: 'hidden',
          background: 'var(--color-surface)',
          flexShrink: 0,
        }}>
          {THUMBNAIL_SIZE_OPTIONS.map((sz, i) => {
            const active = thumbnailSize === sz
            const labels: Record<typeof sz, string> = { sm: 'S', md: 'M', lg: 'L' }
            return (
              <button
                key={sz}
                type="button"
                className="btn-reset"
                onClick={() => onThumbnailSizeChange(sz)}
                title={t(`index.thumbSize.${sz}`)}
                aria-label={t(`index.thumbSize.${sz}`)}
                aria-pressed={active}
                style={{
                  padding: '4px 10px',
                  minHeight: 26,
                  borderRight: i < THUMBNAIL_SIZE_OPTIONS.length - 1 ? '1px solid var(--color-border)' : 'none',
                  background: active ? 'var(--color-brand-50)' : 'transparent',
                  color: active ? 'var(--color-brand-700)' : 'var(--color-text-muted)',
                  fontSize: 'var(--font-size-xs)',
                  fontWeight: 700,
                  cursor: 'pointer',
                  transition: 'background var(--duration-fast), color var(--duration-fast)',
                }}
              >
                {labels[sz]}
              </button>
            )
          })}
        </div>

        {/* View mode toggle */}
        <IconButtonGroup>
          <IconButton
            size="sm"
            active={viewMode === 'tile'}
            onClick={() => onViewModeChange('tile')}
            title={t('index.tileView')}
            aria-label={t('index.tileView')}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
              <rect x="1" y="1" width="6" height="6" rx="1" />
              <rect x="9" y="1" width="6" height="6" rx="1" />
              <rect x="1" y="9" width="6" height="6" rx="1" />
              <rect x="9" y="9" width="6" height="6" rx="1" />
            </svg>
          </IconButton>
          <IconButton
            size="sm"
            active={viewMode === 'list'}
            onClick={() => onViewModeChange('list')}
            title={t('index.listView')}
            aria-label={t('index.listView')}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
              <rect x="1" y="2" width="14" height="2.5" rx="1" />
              <rect x="1" y="6.75" width="14" height="2.5" rx="1" />
              <rect x="1" y="11.5" width="14" height="2.5" rx="1" />
            </svg>
          </IconButton>
        </IconButtonGroup>
      </div>

      {/* Filter chip bar */}
      {filtersOpen && (groupKeys.length > 0 || activeFilterCount > 0) && (
        // Cap the open panel and let it scroll internally: the header doesn't
        // scroll with the page (only <main> does), so a tall filter list must
        // stay reachable on short viewports without pushing the layout off-screen.
        <div id="index-filter-panel" style={{ padding: '12px 24px 14px', display: 'flex', flexDirection: 'column', gap: 8, maxHeight: '40vh', overflowY: 'auto' }}>
          {groupKeys.map(key => {
            const selected = selectedValues(searchParams, key)
            const rawItems = facets.groups[key] || []
            if (rawItems.length === 0) return null
            // The backend returns each category's values most-used first.
            // Honour that for 'count'; sort a copy by value for 'alpha'
            // (compareValues: locale-aware, case-insensitive, digit runs
            // compared as numbers) without mutating the facets.
            const items = filterSortOrder === 'alpha'
              ? [...rawItems].sort((a, b) => compareValues(a.value, b.value))
              : rawItems
            return (
              <div key={key} style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                <span style={{
                  fontSize: 10, fontWeight: 700,
                  color: 'var(--color-text-faint)',
                  textTransform: 'uppercase', letterSpacing: '0.08em',
                  minWidth: 84, textAlign: 'right', flexShrink: 0, paddingTop: 6,
                }}>
                  {metaFieldLabel(t, key)}
                </span>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {items.map(item => (
                    <Chip
                      key={item.value}
                      active={selected.includes(item.value)}
                      count={item.count}
                      onClick={() => onToggleFilter(key, item.value)}
                    >
                      {item.value}
                    </Chip>
                  ))}
                </div>
              </div>
            )
          })}

          {activeFilterCount > 0 && (
            <div style={{ paddingLeft: 96 }}>
              <button
                type="button"
                className="btn-reset"
                onClick={onClearFilters}
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
                {t('index.clearFilters', { count: activeFilterCount })}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
