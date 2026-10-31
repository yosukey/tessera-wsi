// @vitest-environment jsdom
import React from 'react'
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The presenter socket and the REST calls are the hook's only I/O.
const sockets = vi.hoisted(
  () => [] as Array<{
    opts: any
    close: ReturnType<typeof vi.fn>
    sendView: ReturnType<typeof vi.fn>
    sendCursor: ReturnType<typeof vi.fn>
    clientId: string
  }>,
)
// Set ``throwNext`` to make the next SyncPresenter constructor throw, as the
// WebSocket constructor does on a blocked URL.
const ctl = vi.hoisted(() => ({ throwNext: false }))
vi.mock('../lib/sync', () => ({
  SyncPresenter: class {
    opts: any
    close = vi.fn()
    sendView = vi.fn()
    sendCursor = vi.fn()
    clientId = `p-${sockets.length}`
    constructor(opts: any) {
      if (ctl.throwNext) {
        ctl.throwNext = false
        throw new Error('SecurityError')
      }
      this.opts = opts
      sockets.push(this as any)
    }
  },
}))
vi.mock('../lib/api', () => ({
  createSyncSession: vi.fn(async () => ({
    session_id: 'SID',
    slide_id: 1,
    slide_public_id: 'P1',
    follower_url: 'http://x/slides/P1?sync=SID',
  })),
  endSyncSession: vi.fn(async () => {}),
  getToken: vi.fn(() => 'tok'),
  listOwnedSyncSessions: vi.fn(async () => []),
}))

import { ApiCodeError, UnauthorizedError } from '../lib/api/shared'
import { I18nProvider } from '../lib/i18n'
import { useSyncPresenterSession, type SyncPresenterSession } from './useSyncPresenterSession'
import en from '../lib/i18n/en'

function mount(enabled = false, viewer: unknown = null) {
  const probe: { current: SyncPresenterSession | null } = { current: null }
  function Harness({ v }: { v: unknown }) {
    probe.current = useSyncPresenterSession(1, v as never, enabled)
    return null
  }
  const { unmount, rerender } = render(
    <I18nProvider>
      <Harness v={viewer} />
    </I18nProvider>,
  )
  const setViewer = (v: unknown) => rerender(
    <I18nProvider>
      <Harness v={v} />
    </I18nProvider>,
  )
  return Object.assign(probe as { current: SyncPresenterSession }, { unmount, setViewer })
}

// A stand-in for an OpenSeadragon viewer: records handlers, and maps element
// pixels to image pixels as image = (pixel + pan) * 10 (and back), so a test
// can move the view under a still pointer by changing ``pan``. Open unless a
// test says otherwise.
function fakeViewer() {
  const handlers: Record<string, Set<(e?: unknown) => void>> = {}
  const state = {
    open: true,
    pan: 0,
    rotation: 0,
    current: { x: 0, y: 0, width: 1, height: 1, degrees: 0 },
    target: { x: 0, y: 0, width: 1, height: 1, degrees: 0 },
  }
  const viewer = {
    state,
    element: document.createElement('div'),
    isOpen: () => state.open,
    addHandler: (name: string, h: (e?: unknown) => void) => {
      (handlers[name] ??= new Set()).add(h)
    },
    addOnceHandler: (name: string, h: (e?: unknown) => void) => {
      const once = (e?: unknown) => { handlers[name]?.delete(once); h(e) }
      ;(handlers[name] ??= new Set()).add(once)
    },
    removeHandler: (name: string, h: (e?: unknown) => void) => { handlers[name]?.delete(h) },
    raise: (name: string) => { for (const h of [...(handlers[name] ?? [])]) h({}) },
    handlerCount: (name: string) => handlers[name]?.size ?? 0,
    viewport: {
      getBounds: (current?: boolean) => ({ ...(current ? state.current : state.target) }),
      getRotation: () => state.rotation,
      pointFromPixel: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
      pixelFromPoint: (p: { x: number; y: number }) => ({ x: p.x, y: p.y }),
      viewportToImageCoordinates: (p: { x: number; y: number }) =>
        ({ x: (p.x + state.pan) * 10, y: p.y * 10 }),
      imageToViewportCoordinates: (img: { x: number; y: number }) =>
        ({ x: img.x / 10 - state.pan, y: img.y / 10 }),
    },
  }
  return viewer
}

function pointer(type: string, x: number, y: number, isPrimary = true) {
  const ev = new MouseEvent(type, { clientX: x, clientY: y })
  Object.defineProperty(ev, 'isPrimary', { value: isPrimary })
  return ev
}

const text = (key: string) => (en as Record<string, unknown>)[key] as string

beforeEach(() => {
  sockets.length = 0
  try { localStorage.clear() } catch { /* ignore */ }
})
afterEach(() => vi.clearAllMocks())

async function started() {
  const probe = mount()
  await act(async () => { await probe.current.start() })
  expect(sockets).toHaveLength(1)
  return { probe, opts: sockets[0].opts }
}

describe('useSyncPresenterSession messages go through i18n', () => {
  it.each([
    ['gone', 'sync.sessionEnded'],
    ['replaced', 'sync.replaced'],
    ['unauthorized', 'sync.unauthorized'],
    // 4400 / 4403 / 4409 name their own cause, not an expired sign-in.
    ['rejected', 'sync.presenterRejected'],
    ['forbidden', 'sync.accessBlocked'],
    ['notOwner', 'sync.notOwner'],
  ])('%s state shows the localized %s message', async (state, key) => {
    const { probe, opts } = await started()
    act(() => opts.onStateChange(state))
    await waitFor(() => expect(probe.current.startError).toBe(text(key)))
  })

  it.each(['rejected', 'forbidden', 'notOwner'])(
    'a %s refusal releases the session without ending the room',
    async (state) => {
      const api = await import('../lib/api')
      const { probe, opts } = await started()
      act(() => opts.onStateChange(state))
      await waitFor(() => expect(probe.current.session).toBeNull())
      expect(sockets[0].close).toHaveBeenCalled()
      probe.unmount()
      expect(api.endSyncSession).not.toHaveBeenCalled()
    },
  )

  it('shows the localized login-required message when the token vanished', async () => {
    const api = await import('../lib/api')
    ;(api.getToken as any).mockReturnValueOnce(null)
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(probe.current.startError).toBe(text('sync.loginRequired'))
  })
})

describe('presenter notices', () => {
  it('slide_unavailable becomes an "unavailable" toast notice', async () => {
    const { probe, opts } = await started()
    act(() => opts.onNotice('slide_unavailable'))
    expect(probe.current.idleNotice).toEqual({ kind: 'unavailable' })
  })

  it('ignores unknown notice codes', async () => {
    const { probe, opts } = await started()
    act(() => opts.onNotice('something_else'))
    expect(probe.current.idleNotice).toBeNull()
  })

  it('max_age_warning and the max_age end carry their own notices', async () => {
    const { probe, opts } = await started()
    act(() => opts.onMaxAgeWarning(240))
    expect(probe.current.idleNotice).toEqual({ kind: 'maxAgeWarning', seconds: 240 })
    act(() => opts.onEnded('max_age'))
    expect(probe.current.idleNotice).toEqual({ kind: 'ended', reason: 'max_age' })
  })
})

describe('a released presenter instance cannot touch the next session', () => {
  it('Stop then Start: the old instance\'s late reports are ignored', async () => {
    const { probe, opts: oldOpts } = await started()
    await act(async () => { await probe.current.stop() })
    await act(async () => { await probe.current.start() })
    expect(sockets).toHaveLength(2)
    // The ``end`` frame of the room Stop just ended, landing late.
    act(() => {
      oldOpts.onEnded('max_age')
      oldOpts.onStateChange('gone')
      oldOpts.onAudience({ type: 'audience', count: 9, followers: [] })
    })
    expect(probe.current.session?.session_id).toBe('SID')
    expect(probe.current.startError).toBeNull()
    expect(probe.current.idleNotice).toBeNull()
    expect(probe.current.audience.count).toBe(0)
    expect(sockets[1].close).not.toHaveBeenCalled()
    // The live instance still drives the state.
    act(() => sockets[1].opts.onAudience({ type: 'audience', count: 2, followers: [] }))
    expect(probe.current.audience.count).toBe(2)
  })
})

describe('end on unmount', () => {
  it('is scoped by the presenter client id', async () => {
    const api = await import('../lib/api')
    const probe = mount()
    await act(async () => { await probe.current.start() })
    probe.unmount()
    expect(api.endSyncSession).toHaveBeenCalledWith('SID', sockets[0].clientId)
    expect(sockets[0].close).toHaveBeenCalled()
  })
})

describe('start errors are localized', () => {
  it('no token: login required', async () => {
    const api = await import('../lib/api')
    ;(api.createSyncSession as any).mockRejectedValueOnce(new UnauthorizedError(401))
    ;(api.getToken as any).mockReturnValueOnce(null)
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(probe.current.startError).toBe(text('sync.loginRequired'))
  })

  it('a rejected token: session expired', async () => {
    const api = await import('../lib/api')
    ;(api.createSyncSession as any).mockRejectedValueOnce(new UnauthorizedError(401))
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(probe.current.startError).toBe(text('common.sessionExpired'))
  })

  it('a coded server refusal', async () => {
    const api = await import('../lib/api')
    ;(api.createSyncSession as any).mockRejectedValueOnce(
      new ApiCodeError('sync.apiErr.slideUnpublished'),
    )
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(probe.current.startError).toBe(text('sync.apiErr.slideUnpublished'))
  })
})

describe('resume', () => {
  it('is offered for a session the server still sees a presenter on', async () => {
    const api = await import('../lib/api')
    const held = {
      session_id: 'OLD', slide_id: 1, slide_public_id: 'P1', slide_title: null,
      slide_catalog_id: null, require_name: false,
      follower_url: 'http://x/slides/P1?sync=OLD', presenter_connected: true,
    }
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([held])
    const probe = mount(true)
    await waitFor(() => expect(probe.current.resumable?.session_id).toBe('OLD'))
    await act(async () => { await probe.current.resume() })
    // A fresh SyncPresenter: its hello carries no resume marker, so the server
    // takes the session over from the old socket instead of superseding it.
    expect(sockets).toHaveLength(1)
    expect(sockets[0].opts.sessionId).toBe('OLD')
    expect(api.createSyncSession).not.toHaveBeenCalled()
  })
})

async function presenting(viewer = fakeViewer()) {
  const probe = mount(false, viewer)
  await act(async () => { await probe.current.start() })
  const p = sockets[0]
  return { probe, viewer, p }
}

describe('view frames', () => {
  afterEach(() => vi.useRealTimers())

  it('sends where the view is heading as soon as it moves, then the settled view', async () => {
    const { viewer, p } = await presenting()
    // The mount-time frame of the open image.
    expect(p.sendView).toHaveBeenCalledTimes(1)
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)

    // A wheel zoom sets a new target; the springs only start moving.
    viewer.state.target = { x: 0.25, y: 0.25, width: 0.5, height: 0.5, degrees: 0 }
    viewer.raise('viewport-change')
    expect(p.sendView).toHaveBeenCalledTimes(2)
    expect(p.sendView.mock.calls[1][0].bounds).toEqual([0.25, 0.25, 0.5, 0.5])

    // The animation toward that unchanged target sends nothing more ...
    for (let i = 0; i < 10; i++) {
      viewer.raise('viewport-change')
      vi.advanceTimersByTime(100)
    }
    expect(p.sendView).toHaveBeenCalledTimes(2)

    // ... nor does settling on it.
    viewer.state.current = { ...viewer.state.target }
    viewer.raise('animation-finish')
    vi.advanceTimersByTime(200)
    expect(p.sendView).toHaveBeenCalledTimes(2)
  })

  it('a moving target is sent at most every 200 ms, the trailing frame current', async () => {
    const { viewer, p } = await presenting()
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)
    const sent = () => p.sendView.mock.calls.length - 1 // minus the mount frame
    for (let i = 1; i <= 10; i++) {
      viewer.state.target = { x: i / 100, y: 0, width: 1, height: 1, degrees: 0 }
      viewer.raise('viewport-change')
      vi.advanceTimersByTime(15)
    }
    // 150 ms of a drag: the leading frame and none yet from the trailing edge.
    expect(sent()).toBe(1)
    vi.advanceTimersByTime(60)
    expect(sent()).toBe(2)
    expect(p.sendView.mock.lastCall![0].bounds[0]).toBe(0.1)
  })

  it('sends nothing before the image is open', async () => {
    const viewer = fakeViewer()
    viewer.state.open = false
    const { p } = await presenting(viewer)
    viewer.raise('viewport-change')
    expect(p.sendView).not.toHaveBeenCalled()
    viewer.state.open = true
    viewer.raise('open')
    expect(p.sendView).toHaveBeenCalledTimes(1)
  })
})

describe('the laser pointer', () => {
  afterEach(() => vi.useRealTimers())

  const lastCursor = (p: { sendCursor: ReturnType<typeof vi.fn> }) => p.sendCursor.mock.lastCall?.[0]

  it('follows the primary pointer only, and a second finger lifting does not hide it', async () => {
    const { viewer, p } = await presenting()
    viewer.element.dispatchEvent(pointer('pointermove', 10, 20))
    expect(lastCursor(p)).toEqual({ x: 100, y: 200, visible: true })
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)
    viewer.element.dispatchEvent(pointer('pointermove', 500, 500, false))
    viewer.element.dispatchEvent(pointer('pointerleave', 500, 500, false))
    vi.advanceTimersByTime(1000)
    expect(p.sendCursor).toHaveBeenCalledTimes(1)
  })

  it('ignores the pointer until the image is open', async () => {
    const viewer = fakeViewer()
    viewer.state.open = false
    const { p } = await presenting(viewer)
    viewer.element.dispatchEvent(pointer('pointermove', 10, 20))
    expect(p.sendCursor).not.toHaveBeenCalled()
  })

  it('is re-aimed when the view moves under a still pointer', async () => {
    const { viewer, p } = await presenting()
    viewer.element.dispatchEvent(pointer('pointermove', 10, 20))
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)
    // A redraw with nothing moved sends nothing.
    viewer.raise('viewport-change')
    expect(p.sendCursor).toHaveBeenCalledTimes(1)
    // Keyboard pan: the image slides under the pointer.
    viewer.state.pan = 5
    viewer.raise('viewport-change')
    expect(lastCursor(p)).toEqual({ x: 150, y: 200, visible: true })
    expect(p.sendCursor).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['the window losing focus', () => window.dispatchEvent(new Event('blur'))],
    ['the tab being hidden', () => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
    }],
  ])('is taken down on %s', async (_label, trigger) => {
    const { viewer, p } = await presenting()
    viewer.element.dispatchEvent(pointer('pointermove', 10, 20))
    try {
      trigger()
    } finally {
      delete (document as { visibilityState?: unknown }).visibilityState
    }
    expect(lastCursor(p)).toEqual({ x: 0, y: 0, visible: false })
    // Not repeated while nothing is shown.
    window.dispatchEvent(new Event('blur'))
    expect(p.sendCursor).toHaveBeenCalledTimes(2)
  })

  it('is taken down when the viewer goes (a slide switch)', async () => {
    const { probe, viewer, p } = await presenting()
    viewer.element.dispatchEvent(pointer('pointermove', 10, 20))
    act(() => probe.setViewer(null))
    expect(lastCursor(p)).toEqual({ x: 0, y: 0, visible: false })
    expect(viewer.handlerCount('viewport-change')).toBe(0)
  })
})

describe('notice toasts, supersede prompts, resume offers and settle timing', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const owned = (over: Record<string, unknown> = {}) => ({
    session_id: 'OTHER', slide_id: 2, slide_public_id: 'P2', slide_title: null,
    slide_catalog_id: null, require_name: false, follower_url: null,
    presenter_connected: true, ...over,
  })

  it.each(['replaced', 'unauthorized', 'gone'])(
    "a %s session takes its countdown toast down",
    async (state) => {
      const { probe, opts } = await started()
      act(() => opts.onIdleWarning(60))
      expect(probe.current.idleNotice?.kind).toBe('warning')
      act(() => opts.onStateChange(state))
      await waitFor(() => expect(probe.current.session).toBeNull())
      expect(probe.current.idleNotice).toBeNull()
    },
  )

  it('an end its toast explains leaves no second, sticky message', async () => {
    const { probe, opts } = await started()
    act(() => {
      opts.onEnded('presenter_idle')
      opts.onStateChange('gone')
    })
    await waitFor(() => expect(probe.current.session).toBeNull())
    expect(probe.current.idleNotice).toEqual({ kind: 'ended', reason: 'presenter_idle' })
    expect(probe.current.startError).toBeNull()
  })

  it.each([
    [true, 'sync.confirmSupersedeLive'],
    [false, 'sync.confirmSupersede'],
  ])('Start asks before replacing a session of this account (presenting: %s)', async (live, key) => {
    const api = await import('../lib/api')
    const confirm = vi.fn(() => false)
    vi.stubGlobal('confirm', confirm)
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([owned({ presenter_connected: live })])
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(confirm).toHaveBeenCalledWith(text(key))
    expect(api.createSyncSession).not.toHaveBeenCalled()
    expect(sockets).toHaveLength(0)

    confirm.mockReturnValue(true)
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([owned()])
    await act(async () => { await probe.current.start() })
    expect(api.createSyncSession).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
  })

  it('declining offers the session on this slide for resuming', async () => {
    const api = await import('../lib/api')
    vi.stubGlobal('confirm', vi.fn(() => false))
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([owned({ session_id: 'HERE', slide_id: 1 })])
    const probe = mount()
    await act(async () => { await probe.current.start() })
    expect(probe.current.resumable?.session_id).toBe('HERE')
  })

  it('the session just stopped is not offered for resuming', async () => {
    const api = await import('../lib/api')
    const probe = mount(true)
    await act(async () => { await probe.current.start() })
    // The probe after Stop is answered before the DELETE landed.
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([owned({ session_id: 'SID', slide_id: 1 })])
    await act(async () => { await probe.current.stop() })
    await act(async () => { await Promise.resolve() })
    expect(probe.current.resumable).toBeNull()
  })

  it('a resume whose socket cannot be built does not leave a dead live session', async () => {
    const api = await import('../lib/api')
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([owned({ session_id: 'OLD', slide_id: 1 })])
    const probe = mount(true)
    await waitFor(() => expect(probe.current.resumable?.session_id).toBe('OLD'))
    ctl.throwNext = true
    await act(async () => { await probe.current.resume() })
    expect(probe.current.session).toBeNull()
    // Worded like start()'s same failure (startErrorMessage).
    expect(probe.current.startError).toBe('SecurityError')
    // And still offered: the room was left for the presenter to resume.
    expect(probe.current.resumable?.session_id).toBe('OLD')
  })

  it.each([
    ['the idle countdown', (o: any) => o.onIdleWarning(60), 'warning'],
    ['the age-cap warning', (o: any) => o.onMaxAgeWarning(240), 'maxAgeWarning'],
  ])('a periodic notice does not replace %s', async (_label, warn, kind) => {
    const { probe, opts } = await started()
    act(() => warn(opts))
    act(() => opts.onNotice('slide_unpublished'))
    expect(probe.current.idleNotice?.kind).toBe(kind)
  })

  it('maps the mid-session notices to their toasts', async () => {
    const { probe, opts } = await started()
    act(() => opts.onNotice('slide_unpublished'))
    expect(probe.current.idleNotice).toEqual({ kind: 'unpublished' })
    act(() => opts.onNotice('unlisted_announced'))
    expect(probe.current.idleNotice).toEqual({ kind: 'unlistedAnnounced' })
  })

  it('a new animation inside the settle delay cancels the stale settled frame', async () => {
    const { viewer, p } = await presenting()
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)
    const before = p.sendView.mock.calls.length
    // The view stopped partway; the next wheel notch starts before 150 ms.
    viewer.state.current = { x: 0.1, y: 0, width: 1, height: 1, degrees: 0 }
    viewer.raise('animation-finish')
    vi.advanceTimersByTime(50)
    viewer.raise('animation-start')
    vi.advanceTimersByTime(500)
    expect(p.sendView.mock.calls.length).toBe(before)
  })
})

describe('restarting after Stop and rotation-only view frames', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('Start does not ask about the session just stopped', async () => {
    const api = await import('../lib/api')
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)
    const probe = mount()
    await act(async () => { await probe.current.start() })
    await act(async () => { await probe.current.stop() })
    // The server may not have reaped it yet when Start asks.
    ;(api.listOwnedSyncSessions as any).mockResolvedValueOnce([{
      session_id: 'SID', slide_id: 1, slide_public_id: 'P1', slide_title: null,
      slide_catalog_id: null, require_name: false, follower_url: null, presenter_connected: false,
    }])
    await act(async () => { await probe.current.start() })
    expect(confirm).not.toHaveBeenCalled()
    expect(sockets).toHaveLength(2)
  })

  it('a rotation with unchanged bounds is sent', async () => {
    const { viewer, p } = await presenting()
    vi.useFakeTimers()
    vi.advanceTimersByTime(1000)
    const before = p.sendView.mock.calls.length
    viewer.state.rotation = 90
    viewer.raise('viewport-change')
    expect(p.sendView.mock.calls.length).toBe(before + 1)
    expect(p.sendView.mock.lastCall![0].rotation_deg).toBe(90)
  })
})
