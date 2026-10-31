// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const warn = vi.hoisted(() => vi.fn())
vi.mock('./logger', () => ({ logger: { warn, error: vi.fn(), info: vi.fn(), debug: vi.fn() } }))

import { DiscoverySocket } from './discovery'

class FakeWS {
  static instances: FakeWS[] = []
  static OPEN = 1
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeWS.instances.push(this)
  }
  send() {}
  close() {}
}

const last = () => FakeWS.instances[FakeWS.instances.length - 1]

describe('DiscoverySocket', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    warn.mockClear()
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('warns about a failed upgrade when the first socket never got a frame', () => {
    const d = new DiscoverySocket({ onEvent: () => {} })
    last().onclose?.({ code: 1006 })
    expect(warn).toHaveBeenCalledTimes(1)
    d.close()
  })

  it('does not blame the proxy for a 1006 that ends a healthy session', () => {
    const events: unknown[] = []
    const d = new DiscoverySocket({ onEvent: (e) => events.push(e) })
    const ws = last()
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'snapshot', sessions: [] }) })
    expect(events).toHaveLength(1)
    ws.onclose?.({ code: 1006 }) // server restart / network drop
    // ...nor for the retries that fail while the server is still down.
    vi.advanceTimersByTime(1_000)
    last().onclose?.({ code: 1006 })
    expect(warn).not.toHaveBeenCalled()
    d.close()
  })

  it('backs off on the shared sync.ts schedule until a frame arrives', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const d = new DiscoverySocket({ onEvent: () => {} })
    // A full channel (4503) accepts, then closes: no frame, so no reset.
    for (let i = 0; i < 20; i++) {
      const ws = last()
      ws.readyState = 1
      ws.onopen?.()
      ws.onclose?.({ code: 4503 })
      vi.advanceTimersByTime(625)
    }
    expect(FakeWS.instances.length).toBeLessThan(6)
    d.close()
  })

  // An auth backend that is down at upgrade time (1013) or a full login window
  // (4429) must not switch the banner off for good, as a refusal (4401) does.
  it.each([
    [1013, true],
    [4429, true],
    [4401, false],
  ])('a %i close retries: %s', (code, retries) => {
    const d = new DiscoverySocket({ onEvent: () => {} })
    const ws = last()
    ws.readyState = 1
    ws.onopen?.()
    ws.onclose?.({ code })
    vi.advanceTimersByTime(60_000)
    expect(FakeWS.instances.length).toBe(retries ? 2 : 1)
    d.close()
  })
})

describe('DiscoverySocket keeps no stale list', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  const snapshot = (sessions: unknown[]) => ({ data: JSON.stringify({ type: 'snapshot', sessions }) })
  const one = [{ sid: 's1', slide_id: 1, slide_public_id: 'p1' }]

  it.each([4401, 4403])('a final %i clears the banner at once', (code) => {
    const events: Array<{ type: string; sessions?: unknown[] }> = []
    const d = new DiscoverySocket({ onEvent: (e) => events.push(e as never) })
    last().onmessage?.(snapshot(one))
    last().onclose?.({ code })
    expect(events[events.length - 1]).toEqual({ type: 'snapshot', sessions: [] })
    d.close()
  })

  it('a channel down for a while clears the list; a quick reconnect does not', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const events: Array<{ type: string; sessions?: unknown[] }> = []
    const d = new DiscoverySocket({ onEvent: (e) => events.push(e as never) })
    last().onmessage?.(snapshot(one))
    last().onclose?.({ code: 1006 })
    vi.advanceTimersByTime(1_000)
    last().onmessage?.(snapshot(one)) // back within the window
    vi.advanceTimersByTime(20_000)
    expect(events.filter((e) => e.type === 'snapshot').map((e) => e.sessions)).toEqual([one, one])
    // Down past the window (every retry fails): cleared.
    last().onclose?.({ code: 1006 })
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(4_000)
      last().onclose?.({ code: 1006 })
    }
    expect(events[events.length - 1]).toEqual({ type: 'snapshot', sessions: [] })
    d.close()
  })

  it('a silent socket is dropped at the ping timeout and replaced', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const d = new DiscoverySocket({ onEvent: () => {} })
    const ws = last()
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.(snapshot([]))
    vi.advanceTimersByTime(90_000)
    expect(ws.onclose).toBeNull()
    vi.advanceTimersByTime(100)
    expect(FakeWS.instances.length).toBeGreaterThan(1)
    d.close()
  })
})

describe('DiscoverySocket coming back online', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('reconnects within the first step instead of the backoff it had reached', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const d = new DiscoverySocket({ onEvent: () => {} })
    for (let i = 0; i < 6; i++) {
      last().onclose?.({ code: 1006 })
      vi.advanceTimersByTime(60_000)
    }
    const before = FakeWS.instances.length
    last().onclose?.({ code: 1006 }) // next wait: the 30 s cap
    window.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(500)
    expect(FakeWS.instances).toHaveLength(before + 1)
    d.close()
    window.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(60_000)
    expect(FakeWS.instances).toHaveLength(before + 1)
  })
})

describe('DiscoverySocket in a hidden tab', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('clears the list once the tab has been hidden past the stale window', () => {
    let visibility = 'visible'
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility as DocumentVisibilityState)
    const events: Array<{ type: string; sessions?: unknown[] }> = []
    const d = new DiscoverySocket({ onEvent: (e) => events.push(e as never) })
    last().onmessage?.({ data: JSON.stringify({ type: 'snapshot', sessions: [{ sid: 's', slide_id: 1, slide_public_id: 'p' }] }) })
    visibility = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(10_000)
    expect(events[events.length - 1]).toEqual({ type: 'snapshot', sessions: [] })
    d.close()
  })
})
