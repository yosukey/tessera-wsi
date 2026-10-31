// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate, type NavigateFunction } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Every viewer built, with its 'open' handlers: the page waits for 'open'
// before it draws a step's annotations.
const osd = vi.hoisted(() => ({ viewers: [] as { element: HTMLElement; opened: (() => void)[] }[] }))
vi.mock('openseadragon', () => {
  const OpenSeadragon = Object.assign(({ element }: { element: HTMLElement }) => {
    const v = {
      element,
      opened: [] as (() => void)[],
      addHandler: () => {},
      addOnceHandler: (name: string, fn: () => void) => { if (name === 'open') v.opened.push(fn) },
      removeHandler: () => {},
      destroy: () => {},
      // Overlays are real children of the viewer element, as in OSD.
      addOverlay: ({ element: el }: { element: HTMLElement }) => { element.appendChild(el) },
      removeOverlay: (el: HTMLElement) => { el.remove() },
      viewport: {
        getRotation: () => 0,
        imageToViewportCoordinates: () => ({}),
        imageToViewportRectangle: () => ({ rotate: () => ({}) }),
      },
    }
    osd.viewers.push(v)
    return v
  }, { Point: class {}, Rect: class {}, Placement: { BOTTOM: 'bottom', CENTER: 'center' } })
  return { default: OpenSeadragon }
})
const osdHelpers = vi.hoisted(() => ({ onOpenFailed: null as null | (() => void) }))
const noFlip = vi.hoisted(() => ({ attached: [] as unknown[], detached: [] as unknown[] }))
vi.mock('../lib/osd', () => ({
  OSD_PREFIX_URL: '/osd-images/',
  osdMaxTilesPerFrame: () => 4,
  loadTileSource: async (id: string) => ({ ok: true, tileSource: `/dzi/${id}.dzi` }),
  applyStepTransition: () => {},
  attachZoomClamp: () => () => {},
  attachTwoFingerRotate: () => () => {},
  attachHomeResetsRotation: () => () => {},
  attachNoFlipKey: (v: unknown) => {
    noFlip.attached.push(v)
    return () => { noFlip.detached.push(v) }
  },
  attachMouseCoordsTracker: () => () => {},
  attachOpenFailed: (_v: unknown, onFail: () => void) => {
    osdHelpers.onOpenFailed = onFail
    return () => {}
  },
}))
const api = vi.hoisted(() => ({ getSlide: vi.fn(), getTour: vi.fn(), getViewerSettings: vi.fn() }))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getAuthState: vi.fn(async () => ({ mode: 'none', authenticated: false })),
  ...api,
}))
vi.mock('../hooks/useTourSession', () => ({ useTourSession: () => {} }))

import { DEFAULT_VIEWER_SETTINGS } from '../lib/api'
import { I18nProvider } from '../lib/i18n'
import TourViewerPage from './TourViewerPage'

// The router's navigate, for moving to another tour while the page stays
// mounted (as browser back / forward between two tour URLs does).
let navigateTo: NavigateFunction = () => {}
function NavigateProbe() {
  navigateTo = useNavigate()
  return null
}

function renderTour() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/slides/s1/tours/t1']}>
        <NavigateProbe />
        <Routes>
          <Route path="/slides/:id/tours/:tourId" element={<TourViewerPage />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

const tourDetail = (steps: unknown[]) => ({
  tour: { id: 1, public_id: 't1', slide_public_id: 's1', slide_id: 1, title: 'Renal tour', description: '', is_published: true },
  steps,
})

beforeEach(() => {
  osd.viewers.length = 0
  osdHelpers.onOpenFailed = null
  noFlip.attached.length = 0
  noFlip.detached.length = 0
  api.getSlide.mockResolvedValue({
    id: 1, public_id: 's1', catalog_id: 'C-1', title: 'Kidney', attributes: {}, wsi_filename: 's1.svs',
    mpp_x: 0, mpp_y: 0, tile_version: 1, show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
  })
  api.getViewerSettings.mockResolvedValue({ ...DEFAULT_VIEWER_SETTINGS })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TourViewerPage', () => {
  it('says "No steps." only once the tour has loaded and really has none', async () => {
    let finish!: (v: unknown) => void
    api.getTour.mockReturnValue(new Promise((r) => { finish = r }))
    renderTour()
    await waitFor(() => expect(api.getTour).toHaveBeenCalled())
    expect(screen.queryByText('No steps.')).toBeNull()
    await act(async () => { finish(tourDetail([])) })
    expect(screen.getByText('No steps.')).toBeTruthy()
  })

  it('shows the translated banner when OpenSeadragon cannot open the slide', async () => {
    api.getTour.mockResolvedValue(tourDetail([]))
    renderTour()
    await waitFor(() => expect(osdHelpers.onOpenFailed).not.toBeNull())
    act(() => osdHelpers.onOpenFailed!())
    expect(screen.getByText('Failed to load the slide.')).toBeTruthy()
  })

  it('keeps a pinch on the step panel or the slide overlays from zooming the page', async () => {
    api.getSlide.mockResolvedValue({
      id: 1, public_id: 's1', catalog_id: 'C-1', title: 'Kidney', attributes: {}, wsi_filename: 's1.svs',
      mpp_x: 0, mpp_y: 0, tile_version: 1, show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
      credit_text: 'Courtesy of the lab',
    })
    api.getTour.mockResolvedValue(tourDetail([]))
    const { container } = renderTour()
    await waitFor(() => expect(screen.getByText('No steps.')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Courtesy of the lab')).toBeTruthy())
    expect((container.firstElementChild as HTMLElement).style.touchAction).toBe('pan-x pan-y')
    expect((container.firstElementChild as HTMLElement).classList.contains('no-pinch-zoom')).toBe(true)
    const targets = {
      stepPanel: screen.getByText('No steps.'),
      infoPanel: screen.getByText('Kidney'),
      credit: screen.getByText('Courtesy of the lab'),
      imageAdjust: screen.getByRole('button', { name: 'Image adjustments' }),
    }
    for (const [where, el] of Object.entries(targets)) {
      const pinch = new Event('touchmove', { bubbles: true, cancelable: true })
      Object.defineProperty(pinch, 'touches', { value: { length: 2 } })
      el.dispatchEvent(pinch)
      expect(pinch.defaultPrevented, where).toBe(true)
    }
  })
})

describe("TourViewerPage drops a tour's annotations when it moves on", () => {
  const pinStep = {
    id: 11, order_index: 0, center_x: 100, center_y: 100, fov_um_w: 50, fov_um_h: 50, rotation_deg: 0,
    pins: [{ x: 100, y: 100, shape: 'pin', color: '#ff0000', rotation_deg: 0, label: 'old pin', z: 0 }],
    shapes_l0: [], description_md: 'Step one', transition_mode: 'none', revision: 0,
  }

  it('to another tour of the same slide that fails to load', async () => {
    api.getTour.mockResolvedValueOnce(tourDetail([pinStep]))
    renderTour()
    await waitFor(() => expect(osd.viewers).toHaveLength(1))
    const viewer = osd.viewers[0]
    act(() => viewer.opened.forEach((fn) => fn()))
    await waitFor(() => expect(viewer.element.textContent).toContain('old pin'))
    const pin = [...viewer.element.querySelectorAll('*')].find((el) => el.textContent === 'old pin')!

    api.getTour.mockRejectedValueOnce(new Error('404'))
    act(() => navigateTo('/slides/s1/tours/t2'))
    await waitFor(() => expect(screen.getByText('Failed to load the tour.')).toBeTruthy())
    // Same slide, so the same viewer: only the overlays had to go.
    expect(osd.viewers).toHaveLength(1)
    expect(pin.isConnected).toBe(false)
    expect(viewer.element.textContent).not.toContain('old pin')
  })
})

describe('TourViewerPage keeps the slide from being mirrored', () => {
  it("turns OpenSeadragon's f key off", async () => {
    api.getTour.mockResolvedValue(tourDetail([]))
    const { unmount } = renderTour()
    await waitFor(() => expect(noFlip.attached).toHaveLength(1))
    unmount()
    expect(noFlip.detached).toEqual(noFlip.attached)
  })
})
