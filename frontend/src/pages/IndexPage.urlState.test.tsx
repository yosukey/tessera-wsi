// @vitest-environment jsdom
// URL-state races on the index page. These run under a real BrowserRouter
// (the app's router) with its default, transition-wrapped location updates —
// the configuration in which a commit lags the URL — so they hold whichever
// way main.tsx configures it.
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SlideListItem } from '../lib/api'

const api = vi.hoisted(() => ({
  getViewerSettings: vi.fn(),
  getFacets: vi.fn(),
  listSlides: vi.fn(),
  listTourIndex: vi.fn(),
  listAllPublishedTours: vi.fn(),
}))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getAuthState: vi.fn(async () => ({ mode: 'none', authenticated: false })),
  ...api,
}))
vi.mock('../lib/discovery', () => ({ DiscoverySocket: class { close() {} } }))

import { I18nProvider } from '../lib/i18n'
import IndexPage from './IndexPage'

function settings(over: Record<string, unknown> = {}) {
  return {
    site_title: 'Atlas', footer_text: '', default_index_type: 'all', default_sort_field: 'catalog_id',
    filter_sort_order: 'count', show_filter_button: true, default_view_mode: 'tile',
    default_thumbnail_size: 'lg', max_index_items: 2000, index_tours_per_slide: 1,
    index_card_attrs: ['stain'], index_filter_attrs: ['stain'], show_admin_link: false, seasonal_backgrounds: [],
    header_default_image_url: '', header_default_title_color: '',
    ...over,
  }
}

function slides(n: number): SlideListItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 1, public_id: `s${i}`, catalog_id: `C-${i}`, title: `Slide ${i}`, thumb_url: '',
    width: 1, height: 1, mpp_x: 0, mpp_y: 0, vendor: '', wsi_filename: '', attributes: { stain: ['HE'] },
    show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null, matched_tours: [],
  }))
}

const params = () => new URLSearchParams(window.location.search)
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function mount() {
  return render(
    <I18nProvider>
      <BrowserRouter>
        <IndexPage />
      </BrowserRouter>
    </I18nProvider>,
  )
}

// A keystroke the way a browser delivers it: outside act(), so React gets to
// schedule (and interrupt) the work it causes as it would for real input.
function typeRaw(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  api.getViewerSettings.mockResolvedValue(settings())
  api.getFacets.mockResolvedValue({ groups: { stain: [{ value: 'HE', count: 5, all_count: 5, tour_count: 0 }, { value: 'PAS', count: 3, all_count: 3, tour_count: 0 }] } })
  api.listSlides.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 2000 })
  api.listTourIndex.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 2000 })
  api.listAllPublishedTours.mockResolvedValue([])
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

describe('IndexPage URL updates', () => {
  it('builds a change on the live URL, not on the last rendered one', async () => {
    mount()
    await screen.findByRole('button', { name: /^HE/ })
    // The URL moves on without the page having rendered it yet — what a
    // pending (transition) commit of an earlier update looks like.
    window.history.replaceState(window.history.state, '', '/?stain=HE')
    fireEvent.click(screen.getByRole('button', { name: /^PAS/ }))
    expect(params().getAll('stain')).toEqual(['HE', 'PAS'])
  })

  it('keeps both of two quick filter clicks on a large catalog', async () => {
    api.listSlides.mockResolvedValue({ items: slides(2000), total: 2000, page: 1, per_page: 2000 })
    mount()
    await screen.findByText('Slide 1999', undefined, { timeout: 15000 })
    const he = screen.getByRole('button', { name: /^HE/ })
    const pas = screen.getByRole('button', { name: /^PAS/ })
    // Raw clicks 15 ms apart: the second lands while the first one's
    // re-render of 2000 cards is still in progress.
    he.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await sleep(15)
    pas.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await waitFor(() => expect(params().getAll('stain')).toEqual(['HE', 'PAS']), { timeout: 10000 })
    await sleep(300)
    expect(params().getAll('stain')).toEqual(['HE', 'PAS'])
  }, 30000)

  it('does not let the search debounce undo a tab change made while it was pending', async () => {
    mount()
    await waitFor(() => expect(api.listSlides).toHaveBeenCalled())
    vi.useFakeTimers()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search slides and tours' }), { target: { value: 'liver' } })
    fireEvent.click(screen.getByRole('button', { name: 'Tours' }))
    // The tab change goes through at once, together with the pending search.
    expect(params().get('type')).toBe('tours')
    expect(params().get('q')).toBe('liver')
    await act(async () => { vi.advanceTimersByTime(400) })
    expect(params().get('type')).toBe('tours')
    expect(params().get('q')).toBe('liver')
    expect(screen.getByRole('button', { name: 'Tours' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('applies the search after its debounce without dropping a later filter', async () => {
    mount()
    await screen.findByRole('button', { name: /^HE/ })
    vi.useFakeTimers()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search slides and tours' }), { target: { value: 'liv' } })
    await act(async () => { vi.advanceTimersByTime(300) })
    expect(params().get('q')).toBe('liv')
    fireEvent.click(screen.getByRole('button', { name: /^HE/ }))
    await act(async () => { vi.advanceTimersByTime(400) })
    expect(params().get('q')).toBe('liv')
    expect(params().getAll('stain')).toEqual(['HE'])
  })

  it('never reverts the search box to an older query while the user keeps typing', async () => {
    api.listSlides.mockResolvedValue({ items: slides(2000), total: 2000, page: 1, per_page: 2000 })
    api.getViewerSettings.mockResolvedValue(settings({ index_filter_attrs: [] }))
    mount()
    await screen.findByText('Slide 1999', undefined, { timeout: 15000 })
    const input = screen.getByRole('textbox', { name: 'Search slides and tours' }) as HTMLInputElement
    typeRaw(input, 'abc')
    // The debounce fires and the router starts re-rendering for q=abc; the
    // next keystroke lands during that render.
    await sleep(308)
    typeRaw(input, 'abcd')
    const seen = new Set<string>()
    const t0 = performance.now()
    while (performance.now() - t0 < 250) { seen.add(input.value); await sleep(2) }
    expect([...seen]).toEqual(['abcd'])
    await waitFor(() => expect(params().get('q')).toBe('abcd'), { timeout: 5000 })
    expect(input.value).toBe('abcd')
  }, 30000)

  it('names the search box and reports the filter panel state', async () => {
    mount()
    expect(screen.getByRole('textbox', { name: 'Search slides and tours' })).toBeTruthy()
    const toggle = await screen.findByRole('button', { name: /^Filter/ })
    // show_filter_button opens the panel initially.
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toBeTruthy()
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('takes a query from back / forward navigation and drops a half-typed one', async () => {
    window.history.replaceState(null, '', '/?q=kidney')
    mount()
    const input = await screen.findByRole('textbox', { name: 'Search slides and tours' }) as HTMLInputElement
    expect(input.value).toBe('kidney')
    vi.useFakeTimers()
    fireEvent.change(input, { target: { value: 'kidneys' } })
    // Something else changes the URL (here: a history entry restored by Back)
    // before the debounce fires.
    await act(async () => {
      window.history.pushState(null, '', '/?q=liver')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(input.value).toBe('liver')
    await act(async () => { vi.advanceTimersByTime(400) })
    expect(params().get('q')).toBe('liver')
    expect(input.value).toBe('liver')
  })
})
