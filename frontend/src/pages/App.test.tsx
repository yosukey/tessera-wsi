// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate, type NavigateFunction } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Only App's own wiring (here: what Google Analytics is sent) is under test;
// the pages and the in-app analytics are stubbed out.
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getToken: () => null,
  getViewerSettings: vi.fn(async () => ({ ga_measurement_id: 'G-TEST1234', favicon_url: null })),
}))
vi.mock('../hooks/useAnalyticsBeacon', () => ({ useAnalyticsBeacon: () => {} }))
vi.mock('../components/AnalyticsNoticeToast', () => ({ default: () => null }))
vi.mock('./IndexPage', () => ({ default: () => null }))
vi.mock('./ViewerPage', () => ({ default: () => null }))
vi.mock('./TourViewerPage', () => ({ default: () => null }))

import App from './App'
import { getViewerSettings } from '../lib/api'

type Gtag = { dataLayer?: IArguments[]; gtag?: unknown }

// The gtag() calls App made, as arrays.
function gtagCalls(): unknown[][] {
  return ((window as unknown as Gtag).dataLayer ?? []).map((args) => Array.from(args))
}
function pageViews(): Record<string, unknown>[] {
  return gtagCalls().filter((c) => c[0] === 'event' && c[1] === 'page_view').map((c) => c[2] as Record<string, unknown>)
}

let navigate: NavigateFunction
function Navigator() {
  navigate = useNavigate()
  return null
}
function renderAt(url: string) {
  return render(<MemoryRouter initialEntries={[url]}><App /><Navigator /></MemoryRouter>)
}

afterEach(() => {
  cleanup()
  const w = window as unknown as Gtag
  delete w.dataLayer
  delete w.gtag
  document.getElementById('ga-script')?.remove()
})

describe('App: Google Analytics page views', () => {
  it('leaves the live-session id, other unlisted query parameters and the hash out', async () => {
    renderAt('/slides/abc?sync=SECRET-SID&step=2&from=admin&debug=1&utm_source=mail#frag')
    await waitFor(() => expect(pageViews()).toHaveLength(1))
    const path = '/slides/abc?step=2&utm_source=mail'
    expect(pageViews()[0]).toMatchObject({ page_path: path, page_location: window.location.origin + path })
    // Also the default for the hits gtag sends by itself.
    expect(gtagCalls()).toContainEqual(['set', expect.objectContaining({ page_location: window.location.origin + path })])
    const sent = JSON.stringify(gtagCalls())
    expect(sent).not.toContain('SECRET-SID')
    expect(sent).not.toContain('frag')
  })

  it("keeps the index page's search, sort and filter", async () => {
    renderAt('/?q=liver&sort=title&order=desc&type=tours')
    await waitFor(() => expect(pageViews()).toHaveLength(1))
    expect(pageViews()[0].page_path).toBe('/?q=liver&sort=title&order=desc&type=tours')
  })

  it("keeps the index page's fixed and configurable filters, and reports a filter change as a new page", async () => {
    renderAt('/?type=slides&stain=HE&organ=liver&lab=L1&tags=x&facet=grade:2&sync=SECRET-SID')
    await waitFor(() => expect(pageViews()).toHaveLength(1))
    expect(pageViews()[0].page_path).toBe('/?type=slides&stain=HE&organ=liver&lab=L1&tags=x&facet=grade%3A2')
    await act(async () => { navigate('/?type=slides&stain=HE&organ=liver&lab=L1&tags=x&facet=grade:2&facet=site:a') })
    await waitFor(() => expect(pageViews()).toHaveLength(2))
    expect(pageViews()[1].page_path).toBe('/?type=slides&stain=HE&organ=liver&lab=L1&tags=x&facet=grade%3A2&facet=site%3Aa')
    expect(JSON.stringify(gtagCalls())).not.toContain('SECRET-SID')
  })

  describe('page_referrer', () => {
    function stubReferrer(ref: string) {
      vi.spyOn(document, 'referrer', 'get').mockReturnValue(ref)
    }
    afterEach(() => { vi.restoreAllMocks() })

    it('strips a same-origin referrer the same way as the page URL', async () => {
      stubReferrer(window.location.origin + '/slides/abc?sync=SECRET-SID&step=3&debug=1#frag')
      renderAt('/')
      await waitFor(() => expect(pageViews()).toHaveLength(1))
      const ref = window.location.origin + '/slides/abc?step=3'
      expect(pageViews()[0].page_referrer).toBe(ref)
      // Also the default for the hits gtag sends by itself.
      expect(gtagCalls()).toContainEqual(['set', expect.objectContaining({ page_referrer: ref })])
      expect(JSON.stringify(gtagCalls())).not.toContain('SECRET-SID')
    })

    it('passes a cross-origin referrer through and sends an empty one as empty', async () => {
      stubReferrer('https://search.example/')
      renderAt('/')
      await waitFor(() => expect(pageViews()).toHaveLength(1))
      expect(pageViews()[0].page_referrer).toBe('https://search.example/')
      cleanup()
      const w = window as unknown as Gtag
      delete w.dataLayer
      delete w.gtag
      document.getElementById('ga-script')?.remove()
      stubReferrer('')
      renderAt('/')
      await waitFor(() => expect(pageViews()).toHaveLength(1))
      expect(pageViews()[0].page_referrer).toBe('')
    })
  })

  it('reports a new step, but not joining a live session, as a new page', async () => {
    renderAt('/slides/abc/tours/t1?step=1')
    await waitFor(() => expect(pageViews()).toHaveLength(1))
    // Settled before the next navigation, so it would have been reported.
    await act(async () => { navigate('/slides/abc/tours/t1?step=1&sync=SECRET-SID') })
    await new Promise((r) => setTimeout(r, 0))
    expect(pageViews()).toHaveLength(1)
    await act(async () => { navigate('/slides/abc/tours/t1?step=2&sync=SECRET-SID') })
    await waitFor(() => expect(pageViews()).toHaveLength(2))
    expect(pageViews()[1].page_path).toBe('/slides/abc/tours/t1?step=2')
    expect(JSON.stringify(gtagCalls())).not.toContain('SECRET-SID')
  })
})

describe('App: Google Analytics tag', () => {
  it('turns Google signals and ad personalisation off in the tag configuration', async () => {
    renderAt('/slides/abc')
    await waitFor(() => expect(pageViews()).toHaveLength(1))
    expect(gtagCalls()).toContainEqual([
      'config', 'G-TEST1234',
      { send_page_view: false, allow_google_signals: false, allow_ad_personalization_signals: false },
    ])
    expect(document.getElementById('ga-script')?.getAttribute('src')).toBe(
      'https://www.googletagmanager.com/gtag/js?id=G-TEST1234',
    )
  })

  it.each(['GTM-ABCD123', 'UA-12345-1', 'G-abcd', 'G-TEST1234&l=x', 'https://evil.example/x.js', '../x'])(
    'never loads a script for %s, which is not a GA4 measurement ID',
    async (id) => {
      const settings = vi.mocked(getViewerSettings)
      const original = settings.getMockImplementation()
      settings.mockImplementation((async () => ({ ga_measurement_id: id, favicon_url: null })) as unknown as typeof getViewerSettings)
      try {
        renderAt('/slides/abc')
        await new Promise((r) => setTimeout(r, 20))
        expect(document.getElementById('ga-script')).toBeNull()
        expect(gtagCalls()).toEqual([])
      } finally {
        if (original) settings.mockImplementation(original)
      }
    },
  )
})
