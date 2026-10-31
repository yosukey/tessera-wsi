// @vitest-environment jsdom
import { cleanup, renderHook, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const events = vi.hoisted(() => [] as any[])
vi.mock('../lib/api', () => ({
  recordTourEvent: (p: any) => { events.push(p) },
  recordTourStart: (p: any) => { events.push(p); return Promise.resolve() },
}))
import { useTourSession } from './useTourSession'

afterEach(() => {
  // A test that failed before its unmount would otherwise leave its hook's
  // window listeners sending into the next test's events.
  cleanup()
  vi.useRealTimers()
})

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('useTourSession dwell after completion', () => {
  it('reports the time spent on the final step of a one-step tour', async () => {
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    events.length = 0
    const { unmount } = renderHook(() => useTourSession('ONESTEP', 0, 1))
    await flush()
    vi.advanceTimersByTime(120_000)
    unmount()
    await flush()
    const summary = events.map((e) => [e.event, e.total_dwell_ms ?? null])
    expect(summary[0][0]).toBe('start')
    expect(summary.some(([ev]) => ev === 'complete')).toBe(true)
    const last = events[events.length - 1]
    expect(last.event).toBe('progress')
    expect(last.total_dwell_ms).toBeGreaterThanOrEqual(120_000)
  })

  it('does not resend an unchanged dwell right after completing', async () => {
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    events.length = 0
    const { unmount } = renderHook(() => useTourSession('NOCHANGE', 0, 1))
    await flush()
    unmount()
    await flush()
    expect(events.filter((e) => e.event === 'progress')).toHaveLength(0)
  })
})

function transition(type: 'pagehide' | 'pageshow', persisted: boolean) {
  window.dispatchEvent(Object.assign(new Event(type), { persisted }))
}

describe('useTourSession back/forward cache', () => {
  it('resumes the same session on a bfcache restore, not counting the time spent frozen', async () => {
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    events.length = 0
    const { rerender, unmount } = renderHook(
      ({ step }) => useTourSession('BFCACHE', step, 3),
      { initialProps: { step: 1 } },
    )
    await flush()
    vi.advanceTimersByTime(30_000)
    act(() => transition('pagehide', true))
    await flush()
    const sid = events[0].session_id
    // The persisted pagehide flushes progress, as an unloading one does.
    expect(events[events.length - 1]).toMatchObject({ event: 'progress', session_id: sid, step_index: 1, total_dwell_ms: 30_000 })

    // Ten minutes frozen in the cache (the page stays visible to jsdom, so
    // only the pause keeps the clock from counting them).
    vi.advanceTimersByTime(600_000)
    act(() => transition('pageshow', true))
    await flush()
    vi.advanceTimersByTime(10_000)
    rerender({ step: 2 })
    await flush()
    vi.advanceTimersByTime(5_000)
    unmount()
    await flush()

    expect(events.filter((e) => e.event === 'start')).toHaveLength(1)
    expect(new Set(events.map((e) => e.session_id))).toEqual(new Set([sid]))
    const complete = events.filter((e) => e.event === 'complete')
    expect(complete).toHaveLength(1)
    expect(complete[0]).toMatchObject({ step_index: 2, total_dwell_ms: 40_000 })
    expect(events[events.length - 1]).toMatchObject({ event: 'progress', total_dwell_ms: 45_000 })
  })

  it('sends no second "complete" for a restore on the final step', async () => {
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    events.length = 0
    const { unmount } = renderHook(() => useTourSession('BFLAST', 2, 3))
    await flush()
    expect(events.filter((e) => e.event === 'complete')).toHaveLength(1)
    vi.advanceTimersByTime(20_000)
    act(() => transition('pagehide', true))
    vi.advanceTimersByTime(300_000)
    act(() => transition('pageshow', true))
    await flush()
    vi.advanceTimersByTime(7_000)
    unmount()
    await flush()
    expect(events.filter((e) => e.event === 'start')).toHaveLength(1)
    expect(events.filter((e) => e.event === 'complete')).toHaveLength(1)
    expect(new Set(events.map((e) => e.session_id)).size).toBe(1)
    // The reader's time on the last step, before and after the cache, minus
    // the frozen stretch.
    expect(events[events.length - 1]).toMatchObject({ event: 'progress', total_dwell_ms: 27_000 })
  })

  it('keeps the session across a plain (unloading) pagehide', async () => {
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    events.length = 0
    const { unmount } = renderHook(() => useTourSession('PLAINHIDE', 0, 3))
    await flush()
    vi.advanceTimersByTime(5_000)
    act(() => transition('pagehide', false))
    act(() => transition('pageshow', false))
    vi.advanceTimersByTime(5_000)
    unmount()
    await flush()
    expect(events.filter((e) => e.event === 'start')).toHaveLength(1)
    expect(new Set(events.map((e) => e.session_id)).size).toBe(1)
    expect(events[events.length - 1].total_dwell_ms).toBe(10_000)
  })
})

describe('useTourSession progress stamps', () => {
  it('stamps each progress beacon on the clock that stamped started_at', async () => {
    // The server caps total_dwell_ms at sent_at - started_at, so that span
    // must cover the reported dwell — including after the wall clock has been
    // set back mid-tour.
    vi.useFakeTimers({ toFake: ['performance', 'Date'] })
    vi.setSystemTime(new Date('2026-03-01T09:00:00Z'))
    events.length = 0
    const { rerender, unmount } = renderHook(
      ({ step }) => useTourSession('STAMPS', step, 3),
      { initialProps: { step: 0 } },
    )
    await flush()
    vi.advanceTimersByTime(90_000)
    rerender({ step: 1 })
    await flush()
    vi.setSystemTime(new Date(Date.now() - 600_000))
    vi.advanceTimersByTime(30_000)
    unmount()
    await flush()
    const start = events.find((e) => e.event === 'start')
    const progress = events.filter((e) => e.event === 'progress')
    expect(progress.map((p) => p.total_dwell_ms)).toEqual([0, 90_000, 120_000])
    for (const p of progress) {
      expect(Date.parse(p.sent_at) - Date.parse(start.started_at)).toBeGreaterThanOrEqual(p.total_dwell_ms)
    }
  })
})
