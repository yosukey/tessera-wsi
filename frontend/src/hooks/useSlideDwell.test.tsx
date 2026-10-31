// @vitest-environment jsdom
import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ recordSlideDwell: vi.fn() }))
vi.mock('../lib/api', () => api)

import { useSlideDwell } from './useSlideDwell'

// The foreground clock reads performance.now(); it is stepped by hand
// alongside the faked interval so dwell totals are exact.
let mono = 0
let hidden = false

function step(ms: number) {
  mono += ms
  vi.advanceTimersByTime(ms)
}

function setHidden(h: boolean) {
  hidden = h
  document.dispatchEvent(new Event('visibilitychange'))
}

function transition(type: 'pagehide' | 'pageshow', persisted: boolean) {
  window.dispatchEvent(Object.assign(new Event(type), { persisted }))
}

beforeEach(() => {
  mono = 0
  hidden = false
  vi.spyOn(performance, 'now').mockImplementation(() => mono)
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  api.recordSlideDwell.mockReset()
  delete (document as { hidden?: boolean }).hidden
})

const sent = () => api.recordSlideDwell.mock.calls.map(c => c[0])

describe('useSlideDwell', () => {
  it('sends the running total from the heartbeat under one session id', () => {
    renderHook(() => useSlideDwell('slide-a'))
    step(120_000)
    step(120_000)
    expect(sent()).toHaveLength(2)
    expect(sent()[0]).toMatchObject({ slide_public_id: 'slide-a', dwell_ms: 120_000 })
    expect(sent()[1]).toMatchObject({ dwell_ms: 240_000, session_id: sent()[0].session_id })
  })

  it('sends nothing from the heartbeat while the tab is hidden', () => {
    renderHook(() => useSlideDwell('slide-a'))
    step(10_000)
    setHidden(true)
    expect(sent()).toHaveLength(1) // the flush on hiding
    step(240_000)
    expect(sent()).toHaveLength(1)
  })

  it('skips a trivially short visit', () => {
    const { unmount } = renderHook(() => useSlideDwell('slide-a'))
    step(1000)
    unmount()
    expect(sent()).toHaveLength(0)
  })

  it('ends the session on a bfcache pagehide and starts a fresh one on restore', () => {
    const { unmount } = renderHook(() => useSlideDwell('slide-a'))
    step(5000)
    transition('pagehide', true)
    expect(sent()).toHaveLength(1)
    const first = sent()[0]
    expect(first.dwell_ms).toBe(5000)

    // Time spent frozen in the cache counts for neither session.
    mono += 600_000
    transition('pageshow', true)
    step(3000)
    unmount()
    expect(sent()).toHaveLength(2)
    expect(sent()[1].dwell_ms).toBe(3000)
    expect(sent()[1].session_id).not.toBe(first.session_id)
  })

  it('keeps the session across a plain (unloading) pagehide', () => {
    const { unmount } = renderHook(() => useSlideDwell('slide-a'))
    step(5000)
    transition('pagehide', false)
    step(5000)
    unmount()
    expect(sent().map(p => p.dwell_ms)).toEqual([5000, 10_000])
    expect(new Set(sent().map(p => p.session_id)).size).toBe(1)
  })
})
