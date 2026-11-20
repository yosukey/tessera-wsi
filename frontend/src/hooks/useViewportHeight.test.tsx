// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useViewportHeight } from './useViewportHeight'

const setInnerHeight = (h: number) =>
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: h })

describe('useViewportHeight', () => {
  const originalInnerHeight = window.innerHeight
  beforeEach(() => {
    vi.useFakeTimers()
    setInnerHeight(1180)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    setInnerHeight(originalInnerHeight)
  })

  it('measures the viewport on mount and on resize', () => {
    const { result } = renderHook(() => useViewportHeight())
    expect(result.current).toBe('1180px')
    setInnerHeight(900)
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(result.current).toBe('900px')
  })

  // iPadOS Safari: the rotation's events arrive while the viewport still has
  // its pre-rotation size, and nothing follows once it has settled.
  it('measures again once a rotation has settled', () => {
    const { result } = renderHook(() => useViewportHeight())
    act(() => { window.dispatchEvent(new Event('orientationchange')) })
    expect(result.current).toBe('1180px')
    // Still the old size after the next frame (and the first re-check)...
    act(() => { vi.advanceTimersByTime(150) })
    expect(result.current).toBe('1180px')
    // ...and settled only later: a later re-check picks it up.
    setInnerHeight(820)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe('820px')
  })

  // A WKWebView browser (Chrome on iPad) may send only a `resize`, and send it
  // before the viewport has its new size.
  it('measures again after a resize that came with the old size', () => {
    const { result } = renderHook(() => useViewportHeight())
    act(() => { window.dispatchEvent(new Event('resize')) })
    act(() => { vi.advanceTimersByTime(150) })
    expect(result.current).toBe('1180px')
    setInnerHeight(820)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe('820px')
  })

  describe('with ResizeObserver', () => {
    const had = (globalThis as { ResizeObserver?: unknown }).ResizeObserver
    let observers: Array<{ cb: () => void; targets: Element[]; disconnected: boolean }>
    beforeEach(() => {
      observers = []
      class FakeResizeObserver {
        entry: { cb: () => void; targets: Element[]; disconnected: boolean }
        constructor(cb: () => void) { this.entry = { cb, targets: [], disconnected: false }; observers.push(this.entry) }
        observe(el: Element) { this.entry.targets.push(el) }
        disconnect() { this.entry.disconnected = true }
        unobserve() {}
      }
      ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = FakeResizeObserver
    })
    afterEach(() => {
      ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = had
    })

    // No event at all: only the layout viewport (<html>) changed size.
    it('measures when <html> changes size without any event', () => {
      const { result } = renderHook(() => useViewportHeight())
      expect(observers).toHaveLength(1)
      expect(observers[0].targets).toEqual([document.documentElement])
      setInnerHeight(820)
      act(() => { observers[0].cb() })
      expect(result.current).toBe('820px')
    })

    it('disconnects on unmount', () => {
      const { unmount } = renderHook(() => useViewportHeight())
      unmount()
      expect(observers[0].disconnected).toBe(true)
    })
  })

  describe('with screen.orientation', () => {
    const had = Object.getOwnPropertyDescriptor(window.screen, 'orientation')
    let orientation: EventTarget
    beforeEach(() => {
      orientation = new EventTarget()
      Object.defineProperty(window.screen, 'orientation', { configurable: true, value: orientation })
    })
    afterEach(() => {
      if (had) Object.defineProperty(window.screen, 'orientation', had)
      else delete (window.screen as unknown as Record<string, unknown>).orientation
    })

    it('measures again after its change event too', () => {
      const { result } = renderHook(() => useViewportHeight())
      act(() => { orientation.dispatchEvent(new Event('change')) })
      setInnerHeight(820)
      act(() => { vi.advanceTimersByTime(1000) })
      expect(result.current).toBe('820px')
    })

    it('stops listening to it once unmounted', () => {
      const { unmount } = renderHook(() => useViewportHeight())
      unmount()
      const before = vi.getTimerCount()
      orientation.dispatchEvent(new Event('change'))
      expect(vi.getTimerCount()).toBe(before)
    })
  })

  it('stops measuring once unmounted', () => {
    const { unmount } = renderHook(() => useViewportHeight())
    const before = vi.getTimerCount()
    act(() => { window.dispatchEvent(new Event('orientationchange')) })
    expect(vi.getTimerCount()).toBeGreaterThan(before)
    unmount()
    expect(vi.getTimerCount()).toBe(before)
  })
})
