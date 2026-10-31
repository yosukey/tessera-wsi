// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PRESENTER_RECONNECT_BACKOFF_MS,
  RECONNECT_BACKOFF_MS,
  SyncFollower,
  SyncPresenter,
  backoffWithJitter,
} from './sync'

// sync_room.PRESENTER_GRACE_SECONDS: the presenter must re-attach within this.
const SERVER_GRACE_MS = 45_000

describe('reconnect back-off schedules', () => {
  afterEach(() => vi.restoreAllMocks())

  it('presenter schedule caps at 8 s and keeps an attempt inside the grace window', () => {
    expect(Math.max(...PRESENTER_RECONNECT_BACKOFF_MS)).toBe(8_000)
    // Worst case: every delay at the top of its jitter band (the full step).
    vi.spyOn(Math, 'random').mockReturnValue(1)
    let t = 0
    let attemptsAfter32s = 0
    for (let i = 0; i < 12; i++) {
      t += backoffWithJitter(i, PRESENTER_RECONNECT_BACKOFF_MS)
      if (t >= 32_000 && t <= SERVER_GRACE_MS) attemptsAfter32s++
    }
    // A 30 s-capped schedule would leave no attempt between ~31.5 s and ~61.5 s.
    expect(attemptsAfter32s).toBeGreaterThanOrEqual(1)
    expect(backoffWithJitter(50, PRESENTER_RECONNECT_BACKOFF_MS)).toBeLessThanOrEqual(10_000)
  })

  it('followers keep the long 30 s cap', () => {
    expect(Math.max(...RECONNECT_BACKOFF_MS)).toBe(30_000)
  })
})

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
  send(_data?: string) {}
  close() {}
}

describe('reconnect timing on a real socket wrapper', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  // Drop every socket as soon as it is created; return the number of sockets
  // that exist after `cycles` rounds of "advance `stepMs`, drop the new socket".
  function drive(cycles: number, stepMs: number): number {
    for (let i = 0; i < cycles; i++) {
      const ws = FakeWS.instances[FakeWS.instances.length - 1]
      ws.onclose?.({ code: 1006 })
      vi.advanceTimersByTime(stepMs)
    }
    return FakeWS.instances.length
  }

  it('presenter retries at least every ~10 s however long the outage lasts', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const p = new SyncPresenter({ sessionId: 's', token: 't', onAudience: () => {} })
    // 10 s per step is enough for every presenter delay (max 8 s * 1.25).
    expect(drive(10, 10_000)).toBe(11)
    p.close()
  })

  it('followers back off past 10 s once the schedule climbs', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    // Same 10 s steps: the 16 s / 30 s steps cannot all fit, so fewer sockets.
    expect(drive(10, 10_000)).toBeLessThan(11)
    f.close()
  })

  // The server accepts the upgrade before its checks, so a refusal it then
  // sends as a close code still fires onopen first.
  function acceptThenClose(code: number) {
    const ws = FakeWS.instances[FakeWS.instances.length - 1]
    ws.readyState = 1
    ws.onopen?.()
    ws.onclose?.({ code })
  }

  it.each([4429, 1008, 1013])('an accept-then-close %i keeps backing off', (code) => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    // Resetting the backoff in onopen would re-dial every ~0.5 s forever: 40
    // sockets in 40 steps of the first delay (500 ms * 1.25).
    for (let i = 0; i < 40; i++) {
      acceptThenClose(code)
      vi.advanceTimersByTime(625)
    }
    expect(FakeWS.instances.length).toBeLessThan(10)
    f.close()
  })

  it('a connection that stayed up resets the backoff', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    for (let i = 0; i < 5; i++) {
      acceptThenClose(4429)
      vi.advanceTimersByTime(60_000)
    }
    // The next socket is admitted (a frame arrives), stays up, then drops.
    const ws = FakeWS.instances[FakeWS.instances.length - 1]
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'attached' }) })
    vi.advanceTimersByTime(10_000)
    const before = FakeWS.instances.length
    ws.onclose?.({ code: 1006 })
    vi.advanceTimersByTime(500)
    expect(FakeWS.instances.length).toBe(before + 1)
    f.close()
  })

  it('a socket admitted and dropped at once keeps backing off', () => {
    // A send-backlog 1013 on a client that cannot keep up: admitted, then
    // closed. Resetting on the first frame would re-dial it every 500 ms.
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    for (let i = 0; i < 40; i++) {
      const ws = FakeWS.instances[FakeWS.instances.length - 1]
      ws.readyState = 1
      ws.onopen?.()
      ws.onmessage?.({ data: JSON.stringify({ type: 'attached' }) })
      ws.onclose?.({ code: 1013 })
      vi.advanceTimersByTime(500)
    }
    expect(FakeWS.instances.length).toBeLessThan(10)
    f.close()
  })
})

describe('a permanent refusal reports its own cause and stops reconnecting', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  // 4400 (no usable hello) and 4403 (IP filter / origin) are not an expired
  // sign-in, and 4409 (the room belongs to another staff account) is neither;
  // a room that is gone is 4404 → 'gone'.
  const cases: Array<[number, string]> = [
    [4400, 'rejected'],
    [4401, 'unauthorized'],
    [4403, 'forbidden'],
    [4409, 'notOwner'],
    [4404, 'gone'],
    [4000, 'replaced'],
  ]

  it.each(cases)('presenter: %i → %s', (code, expected) => {
    const states: string[] = []
    const p = new SyncPresenter({
      sessionId: 's', token: 't', onAudience: () => {},
      onStateChange: (s) => states.push(s),
    })
    const ws = FakeWS.instances[0]
    ws.readyState = 1
    ws.onopen?.()
    ws.onclose?.({ code })
    expect(states[states.length - 1]).toBe(expected)
    vi.advanceTimersByTime(120_000)
    expect(FakeWS.instances).toHaveLength(1)
    p.close()
  })

  it.each([[4400, 'rejected'], [4401, 'unauthorized'], [4403, 'forbidden'], [4404, 'gone']])(
    'follower: %i → %s',
    (code, expected) => {
      const states: string[] = []
      const f = new SyncFollower({
        sessionId: 's', onView: () => {}, onStateChange: (s: string) => states.push(s),
      } as never)
      const ws = FakeWS.instances[0]
      ws.readyState = 1
      ws.onopen?.()
      ws.onclose?.({ code: code as number })
      expect(states[states.length - 1]).toBe(expected)
      vi.advanceTimersByTime(120_000)
      expect(FakeWS.instances).toHaveLength(1)
      f.close()
    },
  )
})

describe('a closed instance reports nothing more', () => {
  beforeEach(() => {
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => vi.unstubAllGlobals())

  it.each([4000, 4404, 4401, 4503, 1000])(
    'a %i close crossing the consumer close() is not reported',
    (code) => {
      const states: string[] = []
      const audiences: unknown[] = []
      const p = new SyncPresenter({
        sessionId: 's', token: 't',
        onAudience: (a) => audiences.push(a),
        onStateChange: (s) => states.push(s),
      })
      const ws = FakeWS.instances[0]
      ws.readyState = 1
      ws.onopen?.()
      states.length = 0
      p.close() // Stop / Start: the consumer is done with this instance.
      ws.onmessage?.({ data: JSON.stringify({ type: 'audience', count: 1, followers: [] }) })
      ws.onclose?.({ code })
      expect(states).toEqual([])
      expect(audiences).toEqual([])
      expect(FakeWS.instances).toHaveLength(1) // and no reconnect
    },
  )
})

describe('presenter pointer after the hello', () => {
  beforeEach(() => {
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
  })
  afterEach(() => vi.unstubAllGlobals())

  function openSocket() {
    const ws = FakeWS.instances[FakeWS.instances.length - 1]
    const sent: Array<Record<string, unknown>> = []
    ws.send = (data: string) => { sent.push(JSON.parse(data)) }
    ws.readyState = 1
    ws.onopen?.()
    return { ws, sent }
  }

  it('a new instance hides the pointer right after its hello', () => {
    // A Resume / takeover: the previous tab's dot must not stay frozen on the
    // followers' screens until this presenter moves over the slide.
    const p = new SyncPresenter({ sessionId: 's', token: 't', onAudience: () => {} })
    const { sent } = openSocket()
    expect(sent.map((m) => m.type)).toEqual(['hello', 'cursor'])
    // Marked as a replay: relayed, but not presenter activity on the server.
    expect(sent[1]).toEqual({ type: 'cursor', x: 0, y: 0, visible: false, replay: true })
    p.close()
  })

  it('a reconnect replays the pointer the presenter is showing', () => {
    vi.useFakeTimers()
    try {
      const p = new SyncPresenter({ sessionId: 's', token: 't', onAudience: () => {} })
      const first = openSocket()
      p.sendCursor({ x: 5, y: 6, visible: true })
      first.ws.onclose?.({ code: 1006 })
      vi.advanceTimersByTime(10_000)
      expect(FakeWS.instances).toHaveLength(2)
      const { sent } = openSocket()
      expect(sent[sent.length - 1]).toEqual({ type: 'cursor', x: 5, y: 6, visible: true, replay: true })
      p.close()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('connection state and recovery', () => {
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

  it("reports 'open' on the server's first frame, not on the bare socket open", () => {
    // The server accepts every upgrade before its checks: a refused socket
    // opens too, and reporting 'open' would then clear state it would not re-send.
    const states: string[] = []
    const f = new SyncFollower({
      sessionId: 's', onView: () => {}, onStateChange: (s: string) => states.push(s),
    } as never)
    const ws = FakeWS.instances[0]
    ws.readyState = 1
    ws.onopen?.()
    expect(states).toEqual(['connecting'])
    ws.onmessage?.({ data: JSON.stringify({ type: 'attached' }) })
    expect(states).toEqual(['connecting', 'open'])
    f.close()
  })

  it('a silent socket is given up on at the ping timeout and replaced at once', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const states: string[] = []
    const closes: number[] = []
    const p = new SyncPresenter({
      sessionId: 's', token: 't', onAudience: () => {}, onStateChange: (s) => states.push(s),
    })
    const ws = FakeWS.instances[0]
    ws.close = () => { closes.push(1) }
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'audience', count: 0, followers: [] }) })
    // No frame for longer than the 30 s timeout: the next 15 s ping tick drops it
    // without waiting for the browser's own close (which a dead path delays).
    vi.advanceTimersByTime(45_000)
    expect(closes).toHaveLength(1)
    expect(ws.onclose).toBeNull()
    expect(states[states.length - 1]).toBe('closed')
    vi.advanceTimersByTime(100)
    expect(FakeWS.instances).toHaveLength(2)
    p.close()
  })

  it('coming back online reconnects soon instead of waiting out the backoff', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    for (let i = 0; i < 6; i++) {
      FakeWS.instances[FakeWS.instances.length - 1].onclose?.({ code: 1006 })
      vi.advanceTimersByTime(60_000)
    }
    const before = FakeWS.instances.length
    FakeWS.instances[before - 1].onclose?.({ code: 1006 })
    window.dispatchEvent(new Event('online'))
    // Within the first step (jittered, so a class does not arrive together),
    // not the 30 s the schedule had reached.
    vi.advanceTimersByTime(500)
    expect(FakeWS.instances).toHaveLength(before + 1)
    f.close()
    // And a closed instance no longer listens.
    window.dispatchEvent(new Event('online'))
    expect(FakeWS.instances).toHaveLength(before + 1)
  })

  it('jitter spreads a step from a tenth of it to all of it', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    expect(backoffWithJitter(0)).toBe(50)
    vi.spyOn(Math, 'random').mockReturnValue(1)
    expect(backoffWithJitter(0)).toBe(500)
  })
})


describe('follower notice frames and waking a hidden tab', () => {
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

  it('a follower hands notice frames to onNotice', () => {
    const codes: string[] = []
    const f = new SyncFollower({ sessionId: 's', onView: () => {}, onNotice: (c: string) => codes.push(c) } as never)
    const ws = FakeWS.instances[0]
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'notice', code: 'slide_unavailable' }) })
    ws.onmessage?.({ data: JSON.stringify({ type: 'notice', code: 5 }) })
    ws.onmessage?.({ data: JSON.stringify({ type: 'notice', code: 'slide_available' }) })
    expect(codes).toEqual(['slide_unavailable', 'slide_available'])
    f.close()
  })

  it('showing the tab again replaces a socket that went silent while hidden', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    let visibility = 'visible'
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility as DocumentVisibilityState)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    const ws = FakeWS.instances[0]
    ws.readyState = 1
    ws.onopen?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'attached' }) })
    visibility = 'hidden'
    // A background tab's timers are throttled: no ping tick ran meanwhile.
    vi.setSystemTime(Date.now() + 120_000)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(ws.onclose).not.toBeNull() // hidden: nothing happens yet
    visibility = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))
    expect(ws.onclose).toBeNull() // given up on
    vi.advanceTimersByTime(100)
    expect(FakeWS.instances).toHaveLength(2)
    f.close()
  })
})

describe('online wake jitter, replayed views and listener cleanup', () => {
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

  it('coming back online waits a jittered step, not 0 ms', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    FakeWS.instances[0].onclose?.({ code: 1006 })
    vi.advanceTimersByTime(500)
    FakeWS.instances[1].onclose?.({ code: 1006 }) // next wait: 1 s
    window.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(10)
    expect(FakeWS.instances).toHaveLength(2) // not at once: spread like any first step
    vi.advanceTimersByTime(490)
    expect(FakeWS.instances).toHaveLength(3)
    f.close()
  })

  it('a reconnect replays the view marked replay too', () => {
    const p = new SyncPresenter({ sessionId: 's', token: 't', onAudience: () => {} })
    const first = FakeWS.instances[0]
    first.readyState = 1
    first.onopen?.()
    p.sendView({ slide_id: 1, bounds: [0, 0, 1, 1], rotation_deg: 0 })
    first.onclose?.({ code: 1006 })
    vi.advanceTimersByTime(10_000)
    const sent: Array<Record<string, unknown>> = []
    const ws = FakeWS.instances[1]
    ws.send = (data: string) => { sent.push(JSON.parse(data)) }
    ws.readyState = 1
    ws.onopen?.()
    expect(sent.find((m) => m.type === 'view')).toMatchObject({ slide_id: 1, replay: true })
    p.close()
  })

  it('close() removes the wake listeners', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const f = new SyncFollower({ sessionId: 's', onView: () => {} } as never)
    f.close()
    expect(remove).toHaveBeenCalledWith('online', expect.any(Function))
  })
})
