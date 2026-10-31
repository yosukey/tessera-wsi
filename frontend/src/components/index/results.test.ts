import { describe, expect, it } from 'vitest'
import type { SlideListItem, TourSummary } from '../../lib/api'
import {
  facetsForView,
  visibleFilterKeys,
  groupToursBySlide,
  inlineTourCap,
  inlineTours,
  rememberSlideLabels,
  slideListParams,
  tourIndexParams,
} from './results'

const opts = { defaultSort: 'catalog_id' as const, cap: 2000 }

function tour(id: number, slideId: number, title = `Tour ${id}`): TourSummary {
  return {
    id, public_id: `t${id}`, slide_public_id: `s${slideId}`, slide_id: slideId,
    title, description: '', is_published: true,
  }
}

function slide(id: number, matched: Array<{ id: number; title: string }> = []): SlideListItem {
  return {
    id, public_id: `s${id}`, catalog_id: `C-${id}`, title: `Slide ${id}`, thumb_url: '',
    width: 1, height: 1, mpp_x: 0, mpp_y: 0, vendor: '', wsi_filename: '', attributes: {},
    show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
    matched_tours: matched.map(m => ({ id: m.id, public_id: `t${m.id}`, title: m.title })),
  }
}

describe('slideListParams', () => {
  it('strips the view selector and pins one capped page with the default sort', () => {
    const p = slideListParams(new URLSearchParams('type=all&page=3&per_page=7'), 'all', opts)
    expect(p.get('type')).toBeNull()
    expect(p.get('page')).toBe('1')
    expect(p.get('per_page')).toBe('2000')
    expect(p.get('sort')).toBe('catalog_id')
  })

  it('keeps an explicit sort and forwards filters verbatim', () => {
    const p = slideListParams(
      new URLSearchParams('sort=title&order=desc&stain=HE&stain=PAS&facet=grade:II'),
      'all', opts,
    )
    expect(p.get('sort')).toBe('title')
    expect(p.get('order')).toBe('desc')
    expect(p.getAll('stain')).toEqual(['HE', 'PAS'])
    expect(p.getAll('facet')).toEqual(['grade:II'])
  })

  it('lets "All" match through tour titles (server default) but not "Slides"', () => {
    const all = slideListParams(new URLSearchParams('q=mitosis'), 'all', opts)
    expect(all.get('q')).toBe('mitosis')
    expect(all.has('match_tours')).toBe(false)

    const slides = slideListParams(new URLSearchParams('q=mitosis'), 'slides', opts)
    expect(slides.get('q')).toBe('mitosis')
    expect(slides.get('match_tours')).toBe('0')
  })

  it('sends no match_tours without a search or filter, and drops a stray one from the URL', () => {
    expect(slideListParams(new URLSearchParams(''), 'slides', opts).has('match_tours')).toBe(false)
    expect(slideListParams(new URLSearchParams('sort=title'), 'slides', opts).has('match_tours')).toBe(false)
    expect(slideListParams(new URLSearchParams('q=x&match_tours=0'), 'all', opts).has('match_tours')).toBe(false)
  })

  it('keeps "Slides" to the slides\' own metadata when filtering', () => {
    expect(slideListParams(new URLSearchParams('stain=HE'), 'slides', opts).get('match_tours')).toBe('0')
    expect(slideListParams(new URLSearchParams('facet=level:basic'), 'slides', opts).get('match_tours')).toBe('0')
    expect(slideListParams(new URLSearchParams('facet=level:basic'), 'all', opts).has('match_tours')).toBe(false)
  })
})

describe('facetsForView', () => {
  const facets = {
    groups: {
      stain: [
        { value: 'HE', count: 3, all_count: 3, tour_count: 1 },
        { value: 'PAS', count: 1, all_count: 4, tour_count: 5 },
        { value: 'IHC', count: 0, all_count: 0, tour_count: 0 },
      ],
    },
  }

  it("uses each view's count and re-sorts most-used first", () => {
    const pairs = (type: 'all' | 'slides' | 'tours') =>
      facetsForView(facets, type).groups.stain.map((i) => `${i.value}:${i.count}`)
    expect(pairs('slides')).toEqual(['HE:3', 'PAS:1', 'IHC:0'])
    expect(pairs('all')).toEqual(['PAS:4', 'HE:3', 'IHC:0'])
    expect(pairs('tours')).toEqual(['PAS:5', 'HE:1', 'IHC:0'])
  })
})

describe('tourIndexParams', () => {
  it('forwards q, filters and sort with the cap', () => {
    const p = tourIndexParams(
      new URLSearchParams('type=tours&q=mito&organ=Liver&facet=grade:II&sort=title&match_tours=0'),
      { defaultSort: 'catalog_id', cap: 300 },
    )
    expect(p.get('type')).toBeNull()
    expect(p.has('match_tours')).toBe(false)
    expect(p.get('q')).toBe('mito')
    expect(p.getAll('organ')).toEqual(['Liver'])
    expect(p.getAll('facet')).toEqual(['grade:II'])
    expect(p.get('sort')).toBe('title')
    expect(p.get('per_page')).toBe('300')
    expect(p.get('page')).toBe('1')
  })
})

describe('groupToursBySlide', () => {
  it('groups in arrival order', () => {
    const m = groupToursBySlide([tour(1, 10), tour(2, 20), tour(3, 10)])
    expect(m.get(10)!.map(t => t.id)).toEqual([1, 3])
    expect(m.get(20)!.map(t => t.id)).toEqual([2])
  })
})

describe('inlineTours', () => {
  const own = [tour(1, 10, 'Overview'), tour(2, 10, 'Mitosis'), tour(3, 10, 'Stroma')]

  it('returns the slide\'s tours unchanged without matches', () => {
    expect(inlineTours(slide(10), own)).toBe(own)
    expect(inlineTours(slide(10))).toEqual([])
  })

  it('puts the matched tours first, in server order, without duplicates', () => {
    const s = slide(10, [{ id: 3, title: 'Stroma' }, { id: 2, title: 'Mitosis' }])
    expect(inlineTours(s, own).map(t => t.id)).toEqual([3, 2, 1])
  })

  it('rebuilds a matched tour the /api/tours snapshot does not have yet', () => {
    const s = slide(10, [{ id: 9, title: 'Brand new' }])
    const out = inlineTours(s, own)
    expect(out.map(t => t.id)).toEqual([9, 1, 2, 3])
    expect(out[0]).toMatchObject({ public_id: 't9', slide_id: 10, slide_public_id: 's10', title: 'Brand new' })
  })
})

describe('inlineTourCap', () => {
  it('shows at least the first match even when no tours are listed inline', () => {
    expect(inlineTourCap(slide(1, [{ id: 1, title: 'x' }]), 0)).toBe(1)
    expect(inlineTourCap(slide(1, [{ id: 1, title: 'x' }]), 3)).toBe(3)
    expect(inlineTourCap(slide(1), 0)).toBe(0)
  })
})

describe('rememberSlideLabels', () => {
  it('accumulates labels and keeps identity when nothing changed', () => {
    const a = rememberSlideLabels(new Map(), [{ id: 1, title: 'One', catalog_id: 'C1' }])
    expect(a.get(1)).toEqual({ title: 'One', catalog_id: 'C1' })
    expect(rememberSlideLabels(a, [{ id: 1, title: 'One', catalog_id: 'C1' }])).toBe(a)
    const b = rememberSlideLabels(a, [{ id: 2, title: 'Two', catalog_id: 'C2' }, { id: 1, title: 'Renamed', catalog_id: 'C1' }])
    expect(b).not.toBe(a)
    expect(b.get(1)!.title).toBe('Renamed')
    expect(b.get(2)!.title).toBe('Two')
  })
})

describe('visibleFilterKeys', () => {
  const facets = {
    groups: {
      stain: [{ value: 'HE', count: 2, all_count: 2, tour_count: 0 }],
      level: [{ value: 'basic', count: 0, all_count: 0, tour_count: 0 }],
      lab: [],
    },
  }

  it('keeps the admin order and drops categories with nothing to pick', () => {
    expect(visibleFilterKeys(['level', 'lab', 'stain'], facets, new URLSearchParams())).toEqual(['stain'])
  })

  it('keeps a category with a value selected, so its chip can be cleared', () => {
    expect(visibleFilterKeys(['level', 'stain'], facets, new URLSearchParams('facet=level:basic'))).toEqual(['level', 'stain'])
  })
})
