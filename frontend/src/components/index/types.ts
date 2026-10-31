import type { IndexTypeFilter } from '../../lib/api'

export const BASE_KEYS = new Set(['stain', 'organ', 'lab', 'tags'])
export const SORT_OPTIONS = ['title', 'catalog_id', 'wsi_filename'] as const
export const TYPE_OPTIONS: IndexTypeFilter[] = ['all', 'slides', 'tours']

export type SortField = typeof SORT_OPTIONS[number]
export type Order = 'asc' | 'desc'
export type ViewMode = 'tile' | 'list'
export type ThumbnailSize = 'sm' | 'md' | 'lg'

export const THUMBNAIL_SIZE_OPTIONS: ThumbnailSize[] = ['sm', 'md', 'lg']

export const THUMBNAIL_TILE_SIZES: Record<ThumbnailSize, { thumbHeight: number; gridMin: number }> = {
  sm: { thumbHeight: 90,  gridMin: 140 },
  md: { thumbHeight: 120, gridMin: 175 },
  lg: { thumbHeight: 148, gridMin: 210 },
}

export const THUMBNAIL_LIST_SIZES: Record<ThumbnailSize, { thumbWidth: number; thumbHeight: number }> = {
  sm: { thumbWidth: 160, thumbHeight: 107 },
  md: { thumbWidth: 200, thumbHeight: 134 },
  lg: { thumbWidth: 240, thumbHeight: 160 },
}

export function selectedValues(sp: URLSearchParams, key: string): string[] {
  if (BASE_KEYS.has(key)) return sp.getAll(key).filter(Boolean)
  const selected: string[] = []
  sp.getAll('facet').forEach((v) => {
    // Split on the FIRST ':' only. JS's split(limit) truncates the tail
    // instead of keeping it (unlike Python's maxsplit), so a value that
    // itself contains a colon (e.g. "WHO: grade II") would otherwise be
    // dropped — leaving the writer-stored token unmatched and the filter
    // chip stuck inactive. Keys never contain ':' (validated on creation).
    const idx = v.indexOf(':')
    if (idx < 0) return
    const k = v.slice(0, idx)
    const value = v.slice(idx + 1)
    if (k === key && value) selected.push(value)
  })
  return selected
}
