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
    setInnerHeight(820)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe('820px')
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
