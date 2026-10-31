// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type OpenSeadragon from 'openseadragon'
import type { Slide } from '../lib/api'

// OpenSeadragon itself is replaced by a recorder: these tests are about the
// options and handlers the page hands it.
const osd = vi.hoisted(() => {
  const created: Array<{ options: Record<string, any>; handlers: Record<string, Set<() => void>> }> = []
  const factory = (options: Record<string, any>) => {
    const handlers: Record<string, Set<() => void>> = {}
    created.push({ options, handlers })
    const element = document.createElement('div')
    return {
      options,
      element,
      addHandler: (name: string, h: () => void) => { (handlers[name] ??= new Set()).add(h) },
      addOnceHandler: () => {},
      removeHandler: (name: string, h: () => void) => { handlers[name]?.delete(h) },
      destroy: () => {},
      world: { draw: () => {} },
      viewport: { getBounds: () => ({}), getRotation: () => 0, resize: () => {} },
    }
  }
  return { created, factory }
})
vi.mock('openseadragon', () => {
  const OpenSeadragon = Object.assign((o: Record<string, any>) => osd.factory(o), {
    Point: class { constructor(public x: number, public y: number) {} },
    Rect: class {},
  })
  return { default: OpenSeadragon }
})
const osdHelpers = vi.hoisted(() => ({ onOpenFailed: null as null | (() => void) }))
const noFlip = vi.hoisted(() => ({ attached: [] as unknown[], detached: [] as unknown[] }))
vi.mock('../lib/osd', () => ({
  OSD_PREFIX_URL: '/osd-images/',
  osdMaxTilesPerFrame: () => 4,
  loadTileSource: async (id: string) => ({ ok: true, tileSource: `/dzi/${id}.dzi` }),
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

const api = vi.hoisted(() => ({
  getAuthState: vi.fn(),
  getSlide: vi.fn(),
  getViewerSettings: vi.fn(),
  adminSaveSlideImageAdjust: vi.fn(),
  isStaffFromToken: vi.fn(() => false),
  isManagerOrAboveFromToken: vi.fn(() => false),
}))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getToken: () => null,
  ...api,
}))

const presenter = vi.hoisted(() => ({ session: null as null | Record<string, unknown> }))
const follower = vi.hoisted(() => ({ refitToLatestView: vi.fn(() => false) }))
vi.mock('../hooks/useSyncPresenterSession', () => ({
  useSyncPresenterSession: () => ({
    session: presenter.session,
    audience: { type: 'audience', count: 0, followers: [] },
    connState: 'open',
    startError: null,
    resumable: null,
    idleNotice: null,
    dismissIdleNotice: () => {},
    start: async () => {},
    resume: async () => {},
    stop: async () => {},
  }),
}))
vi.mock('../hooks/useSyncFollower', () => ({
  useSyncFollower: () => ({
    followerStatus: 'connecting',
    followerSlideRef: null,
    followerRequireName: false,
    followerConnState: 'open',
    followerHasConnected: false,
    endedReason: null,
    presenterAbsent: false,
    presenterGraceRemaining: null,
    cursorOverlayRef: { current: null },
    refitToLatestView: follower.refitToLatestView,
    reprojectCursor: () => {},
    joinSession: () => {},
    cancelJoin: () => {},
    leaveSession: () => {},
  }),
}))
vi.mock('../hooks/useSlideDwell', () => ({ useSlideDwell: () => {} }))
vi.mock('../components/SyncPresenterSidebar', () => ({ default: () => <div data-testid="presenter-sidebar" /> }))
vi.mock('../components/SlideSwitcher', () => ({ default: () => <div data-testid="slide-switcher" /> }))
vi.mock('../components/SyncPresenterLauncher', () => ({ default: () => <div data-testid="presenter-launcher" /> }))
vi.mock('../components/SyncIdleToast', () => ({ default: () => null }))
vi.mock('../components/FollowerJoinDialog', () => ({ default: () => null }))

import { DEFAULT_VIEWER_SETTINGS } from '../lib/api'
import { RateLimitError } from '../lib/api/shared'
import { I18nProvider } from '../lib/i18n'
import ViewerPage from './ViewerPage'

function slide(id: number, over: Partial<Slide> = {}): Slide {
  return {
    id, public_id: `s${id}`, catalog_id: `C-${id}`, title: `Slide ${id}`, thumb_url: '',
    width: 1000, height: 1000, mpp_x: 0, mpp_y: 0, vendor: '', wsi_filename: `s${id}.svs`,
    attributes: {}, is_published: true, tile_version: 1,
    show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
    image_adjust_brightness: null, image_adjust_contrast: null, image_adjust_saturation: null,
    ...over,
  } as unknown as Slide
}

let navigate: (to: string) => void = () => {}
function Nav() {
  const nav = useNavigate()
  navigate = (to) => nav(to)
  return null
}

function renderAt(url: string) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[url]}>
        <Nav />
        <Routes>
          <Route path="/slides/:id" element={<ViewerPage />} />
          <Route path="/login" element={<div data-testid="login-page" />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

const lastOptions = () => osd.created[osd.created.length - 1].options

beforeEach(() => {
  osd.created.length = 0
  osdHelpers.onOpenFailed = null
  noFlip.attached.length = 0
  noFlip.detached.length = 0
  presenter.session = null
  api.getAuthState.mockResolvedValue({ mode: 'none', authenticated: false })
  api.getSlide.mockImplementation(async (publicId: string) => slide(Number(publicId.slice(1))))
  api.getViewerSettings.mockResolvedValue({ ...DEFAULT_VIEWER_SETTINGS, show_metadata_panel: false })
  api.isStaffFromToken.mockReturnValue(false)
  api.isManagerOrAboveFromToken.mockReturnValue(false)
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ViewerPage OpenSeadragon options', () => {
  it('builds a follower viewer with every gesture, the keyboard and the nav buttons off', async () => {
    renderAt('/slides/s1?sync=room1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    const o = lastOptions()
    for (const device of ['gestureSettingsMouse', 'gestureSettingsTouch', 'gestureSettingsPen', 'gestureSettingsUnknown']) {
      expect(o[device]).toMatchObject({
        clickToZoom: false, dblClickToZoom: false, dblClickDragToZoom: false, scrollToZoom: false,
        dragToPan: false, pinchToZoom: false, flickEnabled: false,
      })
    }
    expect(o.keyboardNavEnabled).toBe(false)
    expect(o.showZoomControl).toBe(false)
    expect(o.showHomeControl).toBe(false)
    expect(o.showFullPageControl).toBe(false)
    expect(o.showRotationControl).toBe(false)
    expect(o.showNavigator).toBe(false)
  })

  it('leaves a follower no OpenSeadragon gesture on any pointer type', async () => {
    renderAt('/slides/s1?sync=room1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    const o = lastOptions()
    // OpenSeadragon merges each gestureSettings* block into its own per-device
    // defaults, so a flag the page leaves out keeps OSD's default (touch
    // double-tap-drag zoom is on). Resolve the options with the real library.
    const RealOSD = (await vi.importActual<{ default: typeof OpenSeadragon }>('openseadragon')).default
    const element = document.createElement('div')
    document.body.appendChild(element)
    const gestures = Object.fromEntries(Object.entries(o).filter(([k]) => k.startsWith('gestureSettings')))
    const v = RealOSD({ element, drawer: 'html', ...gestures } as OpenSeadragon.Options)
    try {
      for (const type of ['mouse', 'touch', 'pen', 'unknown']) {
        const settings = v.gestureSettingsByDeviceType(type) as unknown as Record<string, unknown>
        // zoomToRefPoint only says where a zoom is centred, not whether one happens.
        const enabled = Object.keys(settings).filter((k) => settings[k] === true && k !== 'zoomToRefPoint')
        expect(enabled, type).toEqual([])
      }
    } finally {
      v.destroy()
      element.remove()
    }
  })

  it('keeps the regular viewer interactive, with single-click zoom off', async () => {
    renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    const o = lastOptions()
    expect(o.gestureSettingsMouse).toEqual({ clickToZoom: false })
    expect(o.keyboardNavEnabled).toBeUndefined()
    expect(o.showZoomControl).toBeUndefined()
    expect(o.showNavigator).toBe(true)
  })

  it('passes the per-frame tile-load limit to OpenSeadragon', async () => {
    renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    expect(lastOptions().maxTilesPerFrame).toBe(4)
  })
})

describe('ViewerPage load failures', () => {
  it('shows the translated banner when OpenSeadragon cannot open the slide', async () => {
    renderAt('/slides/s1')
    await waitFor(() => expect(osdHelpers.onOpenFailed).not.toBeNull())
    act(() => osdHelpers.onOpenFailed!())
    expect(screen.getByText('Failed to load the slide.')).toBeTruthy()
  })
})

describe('ViewerPage presenter chrome', () => {
  it('keeps a live session on screen on an unpublished slide', async () => {
    api.isStaffFromToken.mockReturnValue(true)
    api.getSlide.mockResolvedValue(slide(2, { is_published: false }))
    presenter.session = { session_id: 'room1' }
    renderAt('/slides/s2?from=admin')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    expect(screen.getByTestId('presenter-sidebar')).toBeTruthy()
    expect(screen.getByTestId('slide-switcher')).toBeTruthy()
    // Starting one stays impossible there.
    expect(screen.queryByTestId('presenter-launcher')).toBeNull()
  })

  it('folds a saved image-adjust preset only into the slide it was saved for', async () => {
    api.isManagerOrAboveFromToken.mockReturnValue(true)
    let finishSave!: (v: unknown) => void
    api.adminSaveSlideImageAdjust.mockReturnValue(new Promise((r) => { finishSave = r }))
    renderAt('/slides/s1?from=admin')
    await waitFor(() => expect(osd.created).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'Image adjustments' }))
    fireEvent.change(screen.getByRole('slider', { name: /Brightness/ }), { target: { value: '1.3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as preset' }))
    expect(api.adminSaveSlideImageAdjust).toHaveBeenCalledWith(1, expect.objectContaining({ brightness: 1.3 }))

    // The presenter moves on to slide 2 before the save comes back.
    await act(async () => { navigate('/slides/s2?from=admin') })
    await waitFor(() => expect(api.getSlide).toHaveBeenCalledWith('s2', expect.anything()))
    await act(async () => {
      finishSave({ image_adjust_brightness: 1.3, image_adjust_contrast: 1, image_adjust_saturation: 1 })
    })
    // Slide 2 is still at its own (identity) preset: nothing to reset.
    expect((screen.getByRole('button', { name: 'Reset' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('ViewerPage pinch-zoom', () => {
  it('keeps a pinch on the slide overlays from zooming the page', async () => {
    api.getSlide.mockResolvedValue(slide(1, { show_metadata_panel: true, credit_text: 'Courtesy of the lab' }))
    const { container } = renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    await waitFor(() => expect(screen.getByText('Courtesy of the lab')).toBeTruthy())
    expect((container.firstElementChild as HTMLElement).style.touchAction).toBe('pan-x pan-y')
    expect((container.firstElementChild as HTMLElement).classList.contains('no-pinch-zoom')).toBe(true)
    const targets = {
      infoPanel: screen.getByText('Slide 1'),
      credit: screen.getByText('Courtesy of the lab'),
      about: screen.getByRole('button', { name: 'About this app' }),
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

describe('ViewerPage slide fetch', () => {
  afterEach(() => vi.useRealTimers())

  it('retries a rate-limited slide fetch instead of staying empty', async () => {
    // A whole class behind one address follows the presenter's switch at once.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.getSlide.mockRejectedValueOnce(new RateLimitError())
    renderAt('/slides/s1?sync=room1')
    await waitFor(() => expect(screen.getByText(/Too many requests/i)).toBeTruthy())
    expect(osd.created).toHaveLength(0)
    await act(async () => { vi.advanceTimersByTime(1_600) })
    await waitFor(() => expect(osd.created).toHaveLength(1))
    expect(api.getSlide).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/Too many requests/i)).toBeNull()
  })

  it('sends the visitor to sign in when the session expired while the page stayed open', async () => {
    // The page probed once on opening (signed in), then the session lapsed; the
    // next slide read is refused with 401. There is no probe on every slide
    // change, so the refusal itself is what catches this.
    api.getAuthState
      .mockResolvedValueOnce({ mode: 'ldap', authenticated: true, app_session: true })
      .mockResolvedValue({ mode: 'ldap', authenticated: false, app_session: false })
    renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    const refused = Object.assign(new Error('Not authenticated'), { status: 401 })
    api.getSlide.mockRejectedValueOnce(refused)
    act(() => navigate('/slides/s2'))
    await waitFor(() => expect(screen.getByTestId('login-page')).toBeTruthy())
    expect(api.getAuthState).toHaveBeenCalledTimes(2)
  })

  it('does not probe again for a failure that is not a 401', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: true, app_session: true })
    renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    api.getSlide.mockRejectedValueOnce(new Error('boom'))
    act(() => navigate('/slides/s2'))
    await waitFor(() => expect(screen.getByText('Failed to load the slide.')).toBeTruthy())
    expect(api.getAuthState).toHaveBeenCalledTimes(1)
  })

  it('does not retry other failures', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.getSlide.mockRejectedValueOnce(new Error('boom'))
    renderAt('/slides/s1')
    await waitFor(() => expect(screen.getByText('Failed to load the slide.')).toBeTruthy())
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(api.getSlide).toHaveBeenCalledTimes(1)
  })
})

describe('ViewerPage follower resize', () => {
  it("puts the presenter's field back once OSD has finished resizing", async () => {
    renderAt('/slides/s1?sync=room1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    const { handlers } = osd.created[0]
    const raise = (name: string) => { for (const h of handlers[name] ?? []) h() }
    raise('viewport-change')
    expect(follower.refitToLatestView).not.toHaveBeenCalled()
    // OSD raises 'resize' before re-centring; the refit waits for the
    // viewport-change of the same update.
    raise('resize')
    expect(follower.refitToLatestView).not.toHaveBeenCalled()
    raise('viewport-change')
    expect(follower.refitToLatestView).toHaveBeenCalledTimes(1)
    raise('viewport-change')
    expect(follower.refitToLatestView).toHaveBeenCalledTimes(1)
  })

  it('leaves a regular viewer alone on resize', async () => {
    renderAt('/slides/s1')
    await waitFor(() => expect(osd.created).toHaveLength(1))
    expect(osd.created[0].handlers.resize?.size ?? 0).toBe(0)
  })
})

describe('ViewerPage keeps the slide from being mirrored', () => {
  it.each([['a regular viewer', '/slides/s1'], ['a follower', '/slides/s1?sync=room1']])(
    "turns OpenSeadragon's f key off for %s",
    async (_label, url) => {
      const { unmount } = renderAt(url)
      await waitFor(() => expect(osd.created).toHaveLength(1))
      expect(noFlip.attached).toHaveLength(1)
      unmount()
      expect(noFlip.detached).toEqual(noFlip.attached)
    },
  )
})
