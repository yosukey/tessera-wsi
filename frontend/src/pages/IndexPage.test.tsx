// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SlideListItem, TourIndexItem, TourSummary } from '../lib/api'

// The page's catalog I/O. Everything else in lib/api (constants, clamps,
// isAbortError) stays real.
const api = vi.hoisted(() => ({
  getViewerSettings: vi.fn(),
  listSlides: vi.fn(),
  listTourIndex: vi.fn(),
  listAllPublishedTours: vi.fn(),
  getFacets: vi.fn(),
}))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getAuthState: vi.fn(async () => ({ mode: 'none', authenticated: false })),
  ...api,
}))
// The live-session discovery socket would open a WebSocket.
vi.mock('../lib/discovery', () => ({ DiscoverySocket: class { close() {} } }))

import { I18nProvider } from '../lib/i18n'
import IndexPage from './IndexPage'

function settings(over: Record<string, unknown> = {}) {
  return {
    site_title: 'Atlas', footer_text: '', default_index_type: 'all', default_sort_field: 'catalog_id',
    filter_sort_order: 'count', show_filter_button: false, default_view_mode: 'tile',
    default_thumbnail_size: 'lg', max_index_items: 100, index_tours_per_slide: 1,
    index_card_attrs: [], index_filter_attrs: [], show_admin_link: false, seasonal_backgrounds: [],
    header_default_image_url: '', header_default_title_color: '',
    ...over,
  }
}

function slide(id: number, title: string, matched: Array<{ id: number; title: string }> = []): SlideListItem {
  return {
    id, public_id: `s${id}`, catalog_id: `C-${id}`, title, thumb_url: '',
    width: 1, height: 1, mpp_x: 0, mpp_y: 0, vendor: '', wsi_filename: '', attributes: {},
    show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
    matched_tours: matched.map(m => ({ id: m.id, public_id: `t${m.id}`, title: m.title })),
  }
}

function tour(id: number, slideId: number, title: string): TourSummary {
  return { id, public_id: `t${id}`, slide_public_id: `s${slideId}`, slide_id: slideId, title, description: '', is_published: true }
}

function tourItem(id: number, title: string, parent: { id: number; title: string }): TourIndexItem {
  return {
    ...tour(id, parent.id, title),
    slide: { id: parent.id, public_id: `s${parent.id}`, catalog_id: `C-${parent.id}`, title: parent.title, thumb_url: '', attributes: {} },
  }
}

const page = <T,>(items: T[], total = items.length) => ({ items, total, page: 1, per_page: 100 })

function renderAt(url: string) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[url]}>
        <IndexPage />
      </MemoryRouter>
    </I18nProvider>,
  )
}

const lastParams = (fn: ReturnType<typeof vi.fn>) => {
  const calls = fn.mock.calls
  return calls[calls.length - 1][0] as URLSearchParams
}

beforeEach(() => {
  api.getViewerSettings.mockResolvedValue(settings())
  api.listSlides.mockResolvedValue(page([]))
  api.listTourIndex.mockResolvedValue(page([]))
  api.listAllPublishedTours.mockResolvedValue([])
  api.getFacets.mockResolvedValue({ groups: {} })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('IndexPage "All"', () => {
  it('lists the slides with their inline tours', async () => {
    api.listSlides.mockResolvedValue(page([slide(1, 'Liver'), slide(2, 'Kidney')]))
    api.listAllPublishedTours.mockResolvedValue([tour(11, 1, 'Portal triad')])
    renderAt('/')

    expect(await screen.findByText('Liver')).toBeTruthy()
    expect(screen.getByText('Kidney')).toBeTruthy()
    expect(await screen.findByText(/Portal triad/)).toBeTruthy()
    const p = lastParams(api.listSlides)
    expect(p.has('q')).toBe(false)
    expect(p.get('per_page')).toBe('100')
    expect(p.get('sort')).toBe('catalog_id')
    expect(api.listTourIndex).not.toHaveBeenCalled()
  })

  it('renders the search result as-is in one request, matched tours first', async () => {
    // Slide 5 matched only through its "Mitosis" tour; the server says so in
    // matched_tours. With one tour listed inline, that tour — not the older
    // "Overview" — is the one shown.
    api.listSlides.mockResolvedValue(page([slide(4, 'Mitosis atlas'), slide(5, 'Liver', [{ id: 52, title: 'Mitosis' }])], 2))
    api.listAllPublishedTours.mockResolvedValue([tour(51, 5, 'Overview'), tour(52, 5, 'Mitosis')])
    renderAt('/?q=mitosis')

    expect(await screen.findByText('Mitosis atlas')).toBeTruthy()
    expect(screen.getByText('Liver')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('+1 more')).toBeTruthy())
    expect(screen.getByText(/· Mitosis/)).toBeTruthy()
    expect(screen.queryByText(/Overview/)).toBeNull()

    // No second, unsearched slide list to merge against.
    expect(api.listSlides).toHaveBeenCalledTimes(1)
    const p = lastParams(api.listSlides)
    expect(p.get('q')).toBe('mitosis')
    expect(p.has('match_tours')).toBe(false)
  })

  it('shows the first matched tour even when no tours are listed inline', async () => {
    api.getViewerSettings.mockResolvedValue(settings({ index_tours_per_slide: 0 }))
    api.listSlides.mockResolvedValue(page([slide(5, 'Liver', [{ id: 52, title: 'Mitosis' }])]))
    api.listAllPublishedTours.mockResolvedValue([tour(51, 5, 'Overview'), tour(52, 5, 'Mitosis')])
    renderAt('/?q=mitosis')

    await waitFor(() => expect(screen.getByText(/· Mitosis/)).toBeTruthy())
    expect(screen.queryByText(/Overview/)).toBeNull()
  })

  it('raises the truncation banner from the result\'s own total', async () => {
    api.listSlides.mockResolvedValue(page([slide(1, 'Liver'), slide(2, 'Kidney')], 250))
    renderAt('/?q=a&stain=HE')
    expect(await screen.findByText('Showing 2 of 250 matching slides. Use search or filters to narrow results.')).toBeTruthy()
    expect(lastParams(api.listSlides).getAll('stain')).toEqual(['HE'])
  })

  it('does not flash the empty state while the first response is in flight', async () => {
    let resolve!: (v: unknown) => void
    api.listSlides.mockReturnValue(new Promise((r) => { resolve = r }))
    renderAt('/')
    await waitFor(() => expect(api.listSlides).toHaveBeenCalled())
    expect(screen.queryByText('No slides found.')).toBeNull()
    resolve(page([]))
    expect(await screen.findByText('No slides found.')).toBeTruthy()
  })
})

describe('IndexPage "Slides"', () => {
  it('searches the slides\' own fields only and fetches no tours', async () => {
    api.listSlides.mockResolvedValue(page([slide(4, 'Mitosis atlas')]))
    renderAt('/?type=slides&q=mitosis')

    expect(await screen.findByText('Mitosis atlas')).toBeTruthy()
    const p = lastParams(api.listSlides)
    expect(p.get('q')).toBe('mitosis')
    expect(p.get('match_tours')).toBe('0')
    expect(p.has('type')).toBe(false)
    expect(api.listAllPublishedTours).not.toHaveBeenCalled()
    expect(api.listTourIndex).not.toHaveBeenCalled()
  })

  it('sends a plain listing without a search', async () => {
    renderAt('/?type=slides')
    await waitFor(() => expect(api.listSlides).toHaveBeenCalled())
    expect(lastParams(api.listSlides).has('match_tours')).toBe(false)
  })
})

describe('IndexPage "Tours"', () => {
  it('asks the tour index with the search and filters and renders its items', async () => {
    api.listTourIndex.mockResolvedValue(page([tourItem(7, 'Glomeruli', { id: 3, title: 'Kidney' })], 5))
    renderAt('/?type=tours&q=mito&stain=HE&facet=grade:II')

    expect(await screen.findByText('Glomeruli')).toBeTruthy()
    // Parent card data comes with the tour, not from a slide list.
    expect(screen.getByText('C-3 · Kidney')).toBeTruthy()
    expect(screen.getByText('Showing 1 of 5 matching tours. Use search or filters to narrow results.')).toBeTruthy()

    const p = lastParams(api.listTourIndex)
    expect(p.get('q')).toBe('mito')
    expect(p.getAll('stain')).toEqual(['HE'])
    expect(p.getAll('facet')).toEqual(['grade:II'])
    expect(p.get('sort')).toBe('catalog_id')
    expect(p.get('per_page')).toBe('100')
    expect(p.has('type')).toBe(false)
    expect(api.listSlides).not.toHaveBeenCalled()
    expect(api.listAllPublishedTours).not.toHaveBeenCalled()
  })

  it('lists every tour without a search, and shows the empty state when none', async () => {
    renderAt('/?type=tours')
    expect(await screen.findByText('No tours found.')).toBeTruthy()
    expect(lastParams(api.listTourIndex).has('q')).toBe(false)
  })

  it('is fetched straight away when settings make it the default view', async () => {
    api.getViewerSettings.mockResolvedValue(settings({ default_index_type: 'tours', default_sort_field: 'title' }))
    api.listTourIndex.mockResolvedValue(page([tourItem(7, 'Glomeruli', { id: 3, title: 'Kidney' })]))
    renderAt('/')

    expect(await screen.findByText('Glomeruli')).toBeTruthy()
    expect(api.listTourIndex).toHaveBeenCalledTimes(1)
    expect(lastParams(api.listTourIndex).get('sort')).toBe('title')
    // The settings gate means no request ever went to the "All" endpoints.
    expect(api.listSlides).not.toHaveBeenCalled()
    expect(api.listAllPublishedTours).not.toHaveBeenCalled()
  })
})

describe('IndexPage view switches', () => {
  it("drops a view's results from an earlier query when switching back to it", async () => {
    let navigate: (to: string) => void = () => {}
    function Nav() {
      const nav = useNavigate()
      navigate = (to) => nav(to)
      return null
    }
    api.listSlides.mockResolvedValueOnce(page([slide(1, 'Liver'), slide(2, 'Kidney')], 500))
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/']}>
          <Nav />
          <IndexPage />
        </MemoryRouter>
      </I18nProvider>,
    )
    expect(await screen.findByText('Kidney')).toBeTruthy()
    await act(async () => { navigate('/?type=tours&q=glom') })
    await waitFor(() => expect(api.listTourIndex).toHaveBeenCalled())
    // Back to "All" under the new query; that request fails.
    api.listSlides.mockRejectedValueOnce(new Error('500'))
    await act(async () => { navigate('/?q=glom') })
    await waitFor(() => expect(api.listSlides).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeTruthy())
    // The unsearched list from before the switch is not shown under q=glom.
    expect(screen.queryByText('Kidney')).toBeNull()
    expect(screen.queryByText('Liver')).toBeNull()
  })
})

describe('IndexPage filter chips', () => {
  // "level:basic" only on a tour: counted in "All" / "Tours", 0 in "Slides".
  const facets = { groups: { level: [{ value: 'basic', count: 0, all_count: 1, tour_count: 1 }] } }

  beforeEach(() => {
    api.getViewerSettings.mockResolvedValue(settings({ index_filter_attrs: ['level'], show_filter_button: true }))
    api.getFacets.mockResolvedValue(facets)
  })

  it("shows a tour-only value with the view's count", async () => {
    renderAt('/?type=tours')
    expect(await screen.findByRole('button', { name: /basic/ })).toBeTruthy()
  })

  it('hides it in "Slides", where nothing carries it', async () => {
    renderAt('/?type=slides')
    await waitFor(() => expect(api.listSlides).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /basic/ })).toBeNull()
  })

  it('keeps it in "Slides" while it is selected, so it can be cleared', async () => {
    renderAt('/?type=slides&facet=level:basic')
    expect(await screen.findByRole('button', { name: /basic/ })).toBeTruthy()
  })
})
