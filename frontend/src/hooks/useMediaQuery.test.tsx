// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useMediaQuery } from './useMediaQuery'
import { resetViewportBoxForTests } from './useViewportBox'
import { VIEWPORT_STALE_CLASS } from '../lib/viewportBox'

// matchMedia answers from the layout viewport (here: always landscape 1194x696).
const cssLayout = { width: 1194, height: 696 }
function stubMatchMedia() {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(orientation: portrait)' ? cssLayout.height >= cssLayout.width : false,
    media: q, addEventListener() {}, removeEventListener() {},
  }))
}
function visible(w: number, h: number) {
  vi.stubGlobal('innerWidth', w)
  vi.stubGlobal('innerHeight', h)
  vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), { width: w, height: h, scale: 1 }))
}

describe('useMediaQuery', () => {
  beforeEach(() => {
    resetViewportBoxForTests()
    stubMatchMedia()
    vi.spyOn(document.documentElement, 'clientWidth', 'get').mockImplementation(() => cssLayout.width)
    vi.spyOn(document.documentElement, 'clientHeight', 'get').mockImplementation(() => cssLayout.height)
  })
  afterEach(() => {
    cleanup()
    resetViewportBoxForTests()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.documentElement.classList.remove(VIEWPORT_STALE_CLASS)
  })

  it('follows matchMedia while the layout viewport matches the screen', () => {
    visible(1194, 696)
    const { result } = renderHook(() => useMediaQuery('(orientation: portrait)'))
    expect(result.current).toBe(false)
    expect(document.documentElement.classList.contains(VIEWPORT_STALE_CLASS)).toBe(false)
  })

  // Chrome on iPad after turning upright: matchMedia still says landscape.
  it('answers for the measured screen while the layout viewport is stale', () => {
    visible(1194, 696)
    const { result } = renderHook(() => useMediaQuery('(orientation: portrait)'))
    expect(result.current).toBe(false)
    visible(834, 1056)
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(result.current).toBe(true)
    expect(document.documentElement.classList.contains(VIEWPORT_STALE_CLASS)).toBe(true)
    expect(document.documentElement.style.getPropertyValue('--vp-width')).toBe('834px')
  })

  it('falls back to matchMedia for a query it cannot evaluate', () => {
    visible(834, 1056)
    const { result } = renderHook(() => useMediaQuery('(any-pointer: coarse)'))
    expect(result.current).toBe(false)
  })
})
