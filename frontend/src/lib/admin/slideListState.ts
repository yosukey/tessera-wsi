import { STORAGE_KEYS } from '../storageKeys'
import { safeSessionGetItem, safeSessionRemoveItem, safeSessionSetItem } from '../safeStorage'
import { META_DISPLAY_KEYS, SORT_OPTIONS, type SortField, type SortOrder, type ToursFilter } from './validation'

// The dashboard slide list's search / sort / filters, kept in sessionStorage
// so they survive the dashboard unmounting: a visit to the Server Info page, a
// tour editor or a slide viewer and back does not come back to an unfiltered
// list. Scoped to the tab's session and dropped on logout (clearToken) and
// on a sign-in (AdminLogin, which an expired session reaches without a
// logout), so the next admin to sign in on this tab starts from the defaults.

export type SlideListState = {
  searchQ: string
  sortField: SortField
  sortOrder: SortOrder
  attrFilters: Record<string, Set<string>>
  vendorFilter: Set<string>
  toursFilter: ToursFilter
  filtersOpen: boolean
}

export const DEFAULT_SLIDE_LIST_STATE: SlideListState = {
  searchQ: '',
  sortField: 'catalog_id',
  sortOrder: 'asc',
  attrFilters: {},
  vendorFilter: new Set(),
  toursFilter: 'all',
  filtersOpen: false,
}

const TOURS_FILTERS: readonly ToursFilter[] = ['all', 'has', 'none']

function stringSet(raw: unknown): Set<string> {
  if (!Array.isArray(raw)) return new Set()
  return new Set(raw.filter((v): v is string => typeof v === 'string' && v !== ''))
}

// The saved state, each field checked on its own: one that is missing or
// malformed (a hand edit, a corrupt entry) falls back to its default instead
// of discarding the rest. Only the categories the filter panel shows are
// kept, since a value of any other key would narrow the list with no chip to
// turn it off.
export function loadSlideListState(): SlideListState {
  const d = DEFAULT_SLIDE_LIST_STATE
  const raw = safeSessionGetItem(STORAGE_KEYS.adminSlideListState)
  if (!raw) return { ...d, attrFilters: {}, vendorFilter: new Set() }
  let v: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object')
    v = parsed as Record<string, unknown>
  } catch {
    return { ...d, attrFilters: {}, vendorFilter: new Set() }
  }
  const attrFilters: Record<string, Set<string>> = {}
  const rawAttrs = v.attrFilters
  if (rawAttrs && typeof rawAttrs === 'object' && !Array.isArray(rawAttrs)) {
    for (const key of META_DISPLAY_KEYS) {
      const set = stringSet((rawAttrs as Record<string, unknown>)[key])
      if (set.size) attrFilters[key] = set
    }
  }
  return {
    searchQ: typeof v.searchQ === 'string' ? v.searchQ : d.searchQ,
    sortField: (SORT_OPTIONS as readonly unknown[]).includes(v.sortField) ? v.sortField as SortField : d.sortField,
    sortOrder: v.sortOrder === 'asc' || v.sortOrder === 'desc' ? v.sortOrder : d.sortOrder,
    attrFilters,
    vendorFilter: stringSet(v.vendorFilter),
    toursFilter: (TOURS_FILTERS as readonly unknown[]).includes(v.toursFilter) ? v.toursFilter as ToursFilter : d.toursFilter,
    filtersOpen: typeof v.filtersOpen === 'boolean' ? v.filtersOpen : d.filtersOpen,
  }
}

export function saveSlideListState(s: SlideListState): void {
  const attrFilters: Record<string, string[]> = {}
  for (const [key, set] of Object.entries(s.attrFilters)) {
    if (set.size) attrFilters[key] = [...set]
  }
  safeSessionSetItem(STORAGE_KEYS.adminSlideListState, JSON.stringify({
    searchQ: s.searchQ,
    sortField: s.sortField,
    sortOrder: s.sortOrder,
    attrFilters,
    vendorFilter: [...s.vendorFilter],
    toursFilter: s.toursFilter,
    filtersOpen: s.filtersOpen,
  }))
}

export function clearSlideListState(): void {
  safeSessionRemoveItem(STORAGE_KEYS.adminSlideListState)
}
