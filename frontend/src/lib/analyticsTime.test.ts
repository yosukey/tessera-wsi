import { afterEach, describe, expect, it, vi } from 'vitest'
import { createForegroundClock, createSessionTimestamps } from './analyticsTime'

// Wall clock (Date.now) and monotonic clock (performance.now), stepped by hand.
function clocks(startWall = Date.UTC(2026, 0, 5, 12)) {
  let wall = startWall
  let mono = 1000
  vi.spyOn(Date, 'now').mockImplementation(() => wall)
  vi.spyOn(performance, 'now').mockImplementation(() => mono)
  return {
    elapse(ms: number) { wall += ms; mono += ms },
    stepWall(ms: number) { wall += ms },
  }
}

afterEach(() => vi.restoreAllMocks())

describe('foreground dwell clock', () => {
  it('ignores the wall clock being set back', () => {
    const c = clocks()
    const clock = createForegroundClock()
    clock.start()
    c.elapse(1000)
    c.stepWall(-60_000)
    clock.stop()
    expect(clock.total()).toBe(1000)
  })

  it('ignores the wall clock jumping forward', () => {
    const c = clocks()
    const clock = createForegroundClock()
    clock.start()
    c.elapse(1000)
    c.stepWall(3_600_000)
    expect(clock.total()).toBe(1000)
  })

  it('counts nothing between pause and resume', () => {
    const c = clocks()
    const clock = createForegroundClock()
    clock.start()
    c.elapse(1000)
    clock.pause()
    c.elapse(60_000)
    expect(clock.total()).toBe(1000)
    clock.resume()
    c.elapse(200)
    clock.resume() // a second resume does not restart the running stretch
    c.elapse(500)
    expect(clock.total()).toBe(1700)
  })
})

describe('session timestamps', () => {
  it('never stamps an end before the start', () => {
    const c = clocks()
    const stamps = createSessionTimestamps()
    c.elapse(5000)
    c.stepWall(-3_600_000)
    expect(Date.parse(stamps.nowIso())).toBeGreaterThanOrEqual(Date.parse(stamps.startedAt))
  })

  it('follows the wall clock in the normal case', () => {
    const c = clocks()
    const stamps = createSessionTimestamps()
    c.elapse(90_000)
    expect(Date.parse(stamps.nowIso()) - Date.parse(stamps.startedAt)).toBe(90_000)
  })
})
