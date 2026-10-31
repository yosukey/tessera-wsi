import type { Facets, IndexTypeFilter, SlideListItem, TourSummary } from '../../lib/api'
import { BASE_KEYS, selectedValues, type SortField } from './types'

// Request and result shaping for the index page. Search, filtering, sorting
// and the max_index_items cap all run server-side — /api/slides for the
// "All" / "Slides" views, /api/index/tours for "Tours" — so the page renders
// each response as-is; these helpers only build the requests and arrange a
// slide card's inline tours.

type RequestOpts = {
  // viewer_settings.default_sort_field, used when the URL names no sort.
  defaultSort: SortField
  // viewer_settings.max_index_items: one page of this size is all that is fetched.
  cap: number
}

// The URL's search / filter / sort params (forwarded verbatim, so a filter
// whose chip is no longer shown keeps narrowing — see activeFilterCount),
// minus the view selector, pinned to the first page at the cap.
function baseParams(searchParams: URLSearchParams, { defaultSort, cap }: RequestOpts): URLSearchParams {
  const params = new URLSearchParams(searchParams)
  params.delete('type')
  params.delete('match_tours')
  params.set('page', '1')
  params.set('per_page', String(cap))
  if (!params.has('sort')) params.set('sort', defaultSort)
  return params
}

// The URL params that filter on metadata: a base category's own param, or
// `facet=key:value` for the others (see types.selectedValues).
const FILTER_PARAMS = [...BASE_KEYS, 'facet']

// /api/slides params for the "All" and "Slides" views. "All" lets q and the
// filters match a slide through its tours — their titles and their metadata
// (the server's default); "Slides" shows no tours that could explain such a
// hit, so it matches the slides' own fields and metadata.
export function slideListParams(searchParams: URLSearchParams, type: IndexTypeFilter, opts: RequestOpts): URLSearchParams {
  const params = baseParams(searchParams, opts)
  const narrowed = !!params.get('q') || FILTER_PARAMS.some((k) => params.getAll(k).length > 0)
  if (type === 'slides' && narrowed) params.set('match_tours', '0')
  return params
}

// /api/facets with each value's count for the given view — the size of that
// view's result for the value alone — and every category re-sorted most-used
// first (the server orders by the "Slides" count), value as the tie-break.
export function facetsForView(facets: Facets, type: IndexTypeFilter): Facets {
  const pick = (item: Facets['groups'][string][number]) => (
    type === 'all' ? item.all_count
      : type === 'tours' ? item.tour_count
        : item.count
  )
  const groups: Facets['groups'] = {}
  for (const [key, items] of Object.entries(facets.groups)) {
    groups[key] = items
      .map((item) => ({ ...item, count: pick(item) }))
      .sort((a, b) => (b.count - a.count) || (a.value < b.value ? -1 : a.value > b.value ? 1 : 0))
  }
  return { groups }
}

// /api/index/tours params for the "Tours" view: the same q / filters / sort /
// cap. The server applies the filters to each tour's own metadata only (a
// value its slide passes down doesn't count) and q to the tour's own fields
// or its slide's.
export function tourIndexParams(searchParams: URLSearchParams, opts: RequestOpts): URLSearchParams {
  return baseParams(searchParams, opts)
}

// /api/tours arrives ordered by (slide_id, id), so grouping keeps each
// slide's tours in creation order.
export function groupToursBySlide(tours: TourSummary[]): Map<number, TourSummary[]> {
  const m = new Map<number, TourSummary[]>()
  for (const t of tours) {
    const arr = m.get(t.slide_id)
    if (arr) arr.push(t)
    else m.set(t.slide_id, [t])
  }
  return m
}

// A slide card's inline tour list in the "All" view: the tours the search or
// the filters matched (server order) first, then the slide's other tours. A slide can be
// in the results only because of a tour, and just the first
// index_tours_per_slide entries show before the rest collapse, so without
// this the reason for the hit could sit hidden behind the expander. A matched
// tour missing from `own` (published after the page took its /api/tours
// snapshot) is rebuilt from the match rather than dropped.
export function inlineTours(slide: SlideListItem, own: TourSummary[] = []): TourSummary[] {
  const matched = slide.matched_tours ?? []
  if (matched.length === 0) return own
  const byPublicId = new Map(own.map(t => [t.public_id, t]))
  const head = matched.map((m): TourSummary => byPublicId.get(m.public_id) ?? {
    id: m.id,
    public_id: m.public_id,
    slide_public_id: slide.public_id,
    slide_id: slide.id,
    title: m.title,
    description: '',
    is_published: true,
  })
  const matchedIds = new Set(matched.map(m => m.public_id))
  return [...head, ...own.filter(t => !matchedIds.has(t.public_id))]
}

// The card's inline cap: index_tours_per_slide, but at least 1 when a tour
// matched the search or the filters, so a deployment that lists no tours inline (0) still
// shows why a tour-only hit is there.
export function inlineTourCap(slide: SlideListItem, toursPerSlide: number): number {
  return (slide.matched_tours?.length ?? 0) > 0 ? Math.max(toursPerSlide, 1) : toursPerSlide
}

export type SlideLabel = { title: string; catalog_id: string }

// Accumulates the title / catalog id of every slide a response has carried,
// for the live-session banner. It names the slide a presenter is on, which
// need not be in the current (searched, filtered, capped) result, so it reads
// from everything seen since the page loaded instead. Returns `prev` itself
// when nothing changed, so a re-fetch of the same page doesn't re-render.
export function rememberSlideLabels(
  prev: Map<number, SlideLabel>,
  slides: Iterable<{ id: number; title: string; catalog_id: string }>,
): Map<number, SlideLabel> {
  let next: Map<number, SlideLabel> | null = null
  for (const s of slides) {
    const known = (next ?? prev).get(s.id)
    if (known && known.title === s.title && known.catalog_id === s.catalog_id) continue
    if (!next) next = new Map(prev)
    next.set(s.id, { title: s.title, catalog_id: s.catalog_id })
  }
  return next ?? prev
}

// The filter categories the panel shows: of the admin's list, in its order,
// those with a value of positive count in `facets` (one view's, see
// facetsForView), plus any with a value selected in the URL, so its chip can
// still be turned off where it counts 0 (a value only tours carry, in
// "Slides").
export function visibleFilterKeys(filterAttrs: string[], facets: Facets, searchParams: URLSearchParams): string[] {
  return filterAttrs.filter((k) =>
    (facets.groups[k] || []).some((it) => (it.count ?? 0) > 0)
    || selectedValues(searchParams, k).length > 0)
}
