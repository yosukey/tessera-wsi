// @vitest-environment jsdom
import React, { useRef } from 'react'
import { act, render, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The follower socket and the session peek are the hook's only I/O.
const sockets = vi.hoisted(() => [] as Array<{ opts: any; close: ReturnType<typeof vi.fn> }>)
vi.mock('../lib/sync', () => ({
  SyncFollower: class {
    opts: any
    close = vi.fn()
    constructor(opts: any) {
      this.opts = opts
      sockets.push(this as any)
    }
  },
}))
vi.mock('../lib/api', () => ({
  peekSyncSession: vi.fn(async (sid: string) => ({
    slide_public_id: 'S1',
    slide_catalog_id: null,
    slide_title: `session ${sid}`,
    require_name: false,
  })),
}))

import { useSyncFollower } from './useSyncFollower'

type Probe = {
  hook: ReturnType<typeof useSyncFollower>
  navigate: ReturnType<typeof useNavigate>
  location: ReturnType<typeof useLocation>
}

function Harness({ probe, osd }: { probe: { current: Probe | null }; osd?: unknown }) {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const osdRef = useRef((osd ?? null) as never)
  const hook = useSyncFollower(params.get('sync') || '', id, osdRef)
  probe.current = { hook, navigate: useNavigate(), location: useLocation() }
  return null
}

function mount(url: string, osd?: unknown) {
  const probe: { current: Probe | null } = { current: null }
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/slides/:id" element={<Harness probe={probe} osd={osd} />} />
      </Routes>
    </MemoryRouter>,
  )
  return probe as { current: Probe }
}

const view = (slide: string) => ({ type: 'view', slide_public_id: slide, bounds: [0, 0, 1, 1] }) as any

beforeEach(() => { sockets.length = 0 })
afterEach(() => vi.clearAllMocks())

describe('useSyncFollower session lifetime', () => {
  it('closes the old session when ?sync= changes and ignores its late frames', async () => {
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
    act(() => probe.current.hook.joinSession('alice'))
    expect(sockets).toHaveLength(1)
    const a = sockets[0]

    act(() => probe.current.navigate('/slides/S1?sync=B'))
    expect(a.close).toHaveBeenCalled()
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))

    // A frame session A had in flight must not move the page to A's slide
    // under B's query (which would give /slides/OLD?sync=B).
    act(() => a.opts.onView(view('OLD_SESSION_SLIDE')))
    expect(probe.current.location.pathname).toBe('/slides/S1')
    expect(probe.current.location.search).toBe('?sync=B')
  })

  it('tears the session down when ?sync= is removed', async () => {
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
    act(() => probe.current.hook.joinSession(null))
    const a = sockets[0]
    act(() => probe.current.navigate('/slides/S1'))
    expect(a.close).toHaveBeenCalled()
    expect(probe.current.hook.followerStatus).toBe('idle')
  })

  it('keeps the socket across a presenter-driven slide change', async () => {
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
    act(() => probe.current.hook.joinSession('bob'))
    const a = sockets[0]
    act(() => a.opts.onView(view('S2')))
    expect(probe.current.location.pathname).toBe('/slides/S2')
    expect(probe.current.location.search).toBe('?sync=A')
    expect(a.close).not.toHaveBeenCalled()
  })

  it("ignores a superseded socket's end frame after a double Join", async () => {
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
    act(() => probe.current.hook.joinSession('carol'))
    act(() => probe.current.hook.joinSession('carol'))
    const [first, second] = sockets
    act(() => first.opts.onEnd({ reason: 'gone' }))
    expect(probe.current.hook.followerStatus).toBe('connected')
    expect(second.close).not.toHaveBeenCalled()
  })
})

describe('a refused follower sees its own cause', () => {
  it.each([
    // 4401: sign-in needed; 4400: name required (same overlay).
    ['unauthorized', 'unauthorized'],
    ['rejected', 'unauthorized'],
    // 4403: the IP filter (or origin check), which no sign-in fixes.
    ['forbidden', 'forbidden'],
  ])('a %s socket state ends in the %s overlay', async (state, status) => {
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
    act(() => probe.current.hook.joinSession('erin'))
    const socket = sockets[0]
    act(() => socket.opts.onStateChange(state))
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe(status))
    expect(socket.close).toHaveBeenCalled()
  })
})

// A stand-in for the follower's OpenSeadragon viewer that records what the
// hook applies. Image pixels map to element pixels as element = image / 10.
function fakeOsd() {
  const calls: unknown[][] = []
  const state = { rotation: 0 }
  return {
    calls,
    state,
    addOnceHandler: () => {},
    removeHandler: () => {},
    viewport: {
      getRotation: () => state.rotation,
      setRotation: (d: number, immediately?: boolean) => {
        state.rotation = d
        calls.push(['setRotation', d, !!immediately])
      },
      fitBounds: (r: { x: number; y: number; width: number; height: number; degrees: number }, immediately?: boolean) =>
        calls.push(['fitBounds', [r.x, r.y, r.width, r.height, r.degrees], !!immediately]),
      imageToViewerElementCoordinates: (p: { x: number; y: number }) => ({ x: p.x / 10, y: p.y / 10 }),
    },
  }
}

async function joined(osd: ReturnType<typeof fakeOsd>) {
  const probe = mount('/slides/S1?sync=A', osd)
  await waitFor(() => expect(probe.current.hook.followerStatus).toBe('join'))
  act(() => probe.current.hook.joinSession('dan'))
  return { probe, socket: sockets[0] }
}

describe('applying the presenter\'s view', () => {
  it('leaves an unchanged rotation alone', async () => {
    const osd = fakeOsd()
    const { socket } = await joined(osd)
    act(() => socket.opts.onView({ ...view('S1'), rotation_deg: 90 }))
    expect(osd.calls).toEqual([
      ['setRotation', 90, false],
      ['fitBounds', [0, 0, 1, 1, 0], false],
    ])
    osd.calls.length = 0
    // The next frame of the same movement: re-issuing the angle would restart
    // the rotation spring.
    act(() => socket.opts.onView({ ...view('S1'), rotation_deg: 90, bounds: [0.1, 0, 1, 1] }))
    expect(osd.calls).toEqual([
      ['fitBounds', [0.1, 0, 1, 1, 0], false],
    ])
  })

  it('refits to the last frame at once, only for the slide on screen', async () => {
    const osd = fakeOsd()
    const { probe, socket } = await joined(osd)
    expect(probe.current.hook.refitToLatestView()).toBe(false)
    act(() => socket.opts.onView({ ...view('S1'), rotation_deg: 180, bounds_deg: 30 }))
    osd.calls.length = 0
    osd.state.rotation = 0
    expect(probe.current.hook.refitToLatestView()).toBe(true)
    expect(osd.calls).toEqual([
      ['setRotation', 180, true],
      ['fitBounds', [0, 0, 1, 1, 30], true],
    ])
  })

})

describe('peek retries, pointer hiding, slide notices and superseded ends', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('a peek the server could not answer yet is retried, then offered to retry', async () => {
    const api = await import('../lib/api')
    const { SyncPeekUnavailableError } = await import('../lib/api/sync')
    vi.useFakeTimers()
    // A class behind one NAT opening the link together: rate limited.
    ;(api.peekSyncSession as any).mockRejectedValueOnce(new SyncPeekUnavailableError())
    const probe = mount('/slides/S1?sync=A')
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(probe.current.hook.followerStatus).toBe('join')
    expect(api.peekSyncSession).toHaveBeenCalledTimes(2)

    // Still unavailable after every retry: the error, with a retry.
    ;(api.peekSyncSession as any).mockRejectedValue(new SyncPeekUnavailableError())
    act(() => probe.current.hook.retryPeek())
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(probe.current.hook.followerStatus).toBe('error')
    ;(api.peekSyncSession as any).mockResolvedValue({
      slide_public_id: 'S1', slide_catalog_id: null, slide_title: 't', require_name: false,
    })
    act(() => probe.current.hook.retryPeek())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(probe.current.hook.followerStatus).toBe('join')
  })

  it('any other peek failure is final at once', async () => {
    const api = await import('../lib/api')
    ;(api.peekSyncSession as any).mockRejectedValueOnce(new Error('boom'))
    const probe = mount('/slides/S1?sync=A')
    await waitFor(() => expect(probe.current.hook.followerStatus).toBe('error'))
    expect(api.peekSyncSession).toHaveBeenCalledTimes(1)
  })

  it("hides the pointer while the presenter's slide switch is still pending", async () => {
    const osd = { ...fakeOsd(), isOpen: () => true }
    const { probe, socket } = await joined(osd as never)
    const el = document.createElement('div')
    probe.current.hook.cursorOverlayRef.current = el
    act(() => socket.opts.onCursor({ type: 'cursor', x: 10, y: 20, visible: true }))
    expect(el.style.display).toBe('block')
    // A reconnect replays the next slide's view and pointer back to back,
    // before the page has moved to that slide.
    act(() => {
      socket.opts.onView(view('S2'))
      socket.opts.onCursor({ type: 'cursor', x: 10, y: 20, visible: true })
    })
    expect(el.style.display).toBe('none')
  })

  it('tracks the unavailable-slide notice until the presenter moves on', async () => {
    const { probe, socket } = await joined(fakeOsd())
    act(() => socket.opts.onNotice('slide_unavailable'))
    expect(probe.current.hook.followerSlideUnavailable).toBe(true)
    act(() => socket.opts.onNotice('slide_available'))
    expect(probe.current.hook.followerSlideUnavailable).toBe(false)
    act(() => socket.opts.onNotice('slide_unavailable'))
    act(() => socket.opts.onView(view('S2')))
    expect(probe.current.hook.followerSlideUnavailable).toBe(false)
  })

  it('a superseded end offers the session that replaced it', async () => {
    const { probe, socket } = await joined(fakeOsd())
    act(() => socket.opts.onEnd({
      type: 'end', reason: 'superseded', next: { sid: 'NEW', slide_public_id: 'P9' },
    }))
    expect(probe.current.hook.followerStatus).toBe('ended')
    expect(probe.current.hook.endedNext).toEqual({ sid: 'NEW', slide_public_id: 'P9' })
  })
})

describe('the unavailable-slide banner after a reconnect', () => {
  it('is cleared once the socket is admitted again', async () => {
    const { probe, socket } = await joined(fakeOsd())
    act(() => socket.opts.onNotice('slide_unavailable'))
    act(() => socket.opts.onStateChange('closed'))
    expect(probe.current.hook.followerSlideUnavailable).toBe(true)
    // Admitted means published: the slide_available sent meanwhile was lost.
    act(() => socket.opts.onStateChange('open'))
    expect(probe.current.hook.followerSlideUnavailable).toBe(false)
  })
})

describe('peek teardown, reconnect resets and end reasons', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('stops retrying the peek once the page is gone', async () => {
    const api = await import('../lib/api')
    const { SyncPeekUnavailableError } = await import('../lib/api/sync')
    vi.useFakeTimers()
    ;(api.peekSyncSession as any).mockRejectedValue(new SyncPeekUnavailableError())
    const probe = mount('/slides/S1?sync=A')
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    const calls = (api.peekSyncSession as any).mock.calls.length
    act(() => probe.current.navigate('/slides/S1')) // leaves the session: cleanup
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect((api.peekSyncSession as any).mock.calls.length).toBe(calls)
    ;(api.peekSyncSession as any).mockReset()
  })

  it('hides the pointer while the viewer has no open image', async () => {
    const osd = { ...fakeOsd(), isOpen: () => false }
    const { probe, socket } = await joined(osd as never)
    const el = document.createElement('div')
    el.style.display = 'block'
    probe.current.hook.cursorOverlayRef.current = el
    act(() => socket.opts.onCursor({ type: 'cursor', x: 10, y: 20, visible: true }))
    expect(el.style.display).toBe('none')
  })

  it("a re-seated socket clears the presenter-absence countdown", async () => {
    const { probe, socket } = await joined(fakeOsd())
    act(() => socket.opts.onPresenter({ type: 'presenter', present: false, grace_seconds: 30 }))
    expect(probe.current.hook.presenterAbsent).toBe(true)
    act(() => socket.opts.onStateChange('closed'))
    act(() => socket.opts.onStateChange('open'))
    expect(probe.current.hook.presenterAbsent).toBe(false)
  })

  it('takes a successor only from a superseded end', async () => {
    const { probe, socket } = await joined(fakeOsd())
    act(() => socket.opts.onEnd({ type: 'end', reason: 'ended_by_owner', next: { sid: 'N', slide_public_id: 'P' } }))
    expect(probe.current.hook.endedNext).toBeNull()
  })
})
