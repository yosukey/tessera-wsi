// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { applyViewportBox, matchesViewportQuery, measureViewportBox, VIEWPORT_STALE_CLASS } from './viewportBox'

// The page as the browser reports it: html.client (the layout viewport) and
// the visible viewport (visualViewport, or innerWidth/Height without it).
function viewport({ layout, visible, scale = 1, noVisualViewport = false }: {
  layout: [number, number]; visible: [number, number]; scale?: number; noVisualViewport?: boolean
}) {
  const de = document.documentElement
  vi.spyOn(de, 'clientWidth', 'get').mockReturnValue(layout[0])
  vi.spyOn(de, 'clientHeight', 'get').mockReturnValue(layout[1])
  vi.stubGlobal('innerWidth', visible[0])
  vi.stubGlobal('innerHeight', visible[1])
  vi.stubGlobal('visualViewport', noVisualViewport ? undefined : { width: visible[0], height: visible[1], scale })
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  applyViewportBox({ width: 0, height: 0, stale: false })
})

describe('measureViewportBox', () => {
  // Chrome on iPad Pro 11", loaded in landscape and turned upright, as its
  // ?vpdebug=1 panel showed: html.client 1194x696, inner / visual 834x1056 @1.
  it('flags the layout viewport Chrome on iPad leaves behind after a rotation', () => {
    viewport({ layout: [1194, 696], visible: [834, 1056] })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: true })
  })

  it('flags it the other way round too (loaded upright, turned to landscape)', () => {
    viewport({ layout: [834, 1056], visible: [1194, 696] })
    expect(measureViewportBox().stale).toBe(true)
  })

  it('is not stale when the two agree, give or take rounding', () => {
    viewport({ layout: [834, 1056], visible: [834.4, 1056] })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: false })
  })

  // A pinch zoom shrinks the visible viewport too; that is the user's doing.
  it('is not stale while the page is zoomed', () => {
    viewport({ layout: [834, 1056], visible: [417, 528], scale: 2 })
    expect(measureViewportBox().stale).toBe(false)
  })

  it('falls back to innerWidth / innerHeight without visualViewport', () => {
    viewport({ layout: [1194, 696], visible: [834, 1056], noVisualViewport: true })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: true })
  })
})

describe('applyViewportBox', () => {
  it('marks <html> with the measured size while stale and clears it after', () => {
    const de = document.documentElement
    applyViewportBox({ width: 834, height: 1056, stale: true })
    expect(de.classList.contains(VIEWPORT_STALE_CLASS)).toBe(true)
    expect(de.style.getPropertyValue('--vp-width')).toBe('834px')
    expect(de.style.getPropertyValue('--vp-height')).toBe('1056px')
    applyViewportBox({ width: 1194, height: 696, stale: false })
    expect(de.classList.contains(VIEWPORT_STALE_CLASS)).toBe(false)
    expect(de.style.getPropertyValue('--vp-width')).toBe('')
  })
})

describe('matchesViewportQuery', () => {
  it('evaluates width, height and orientation conditions', () => {
    expect(matchesViewportQuery('(max-width: 800px)', 800, 600)).toBe(true)
    expect(matchesViewportQuery('(max-width: 800px)', 801, 600)).toBe(false)
    expect(matchesViewportQuery('(min-width: 600px)', 600, 400)).toBe(true)
    expect(matchesViewportQuery('(max-height: 500px)', 900, 501)).toBe(false)
    expect(matchesViewportQuery('(min-height: 500px)', 900, 500)).toBe(true)
    expect(matchesViewportQuery('(orientation: portrait)', 834, 1056)).toBe(true)
    expect(matchesViewportQuery('(orientation: landscape)', 834, 1056)).toBe(false)
  })

  it('combines with `and` and `,` as a media query list does', () => {
    const q = '(max-width: 720px), (orientation: portrait) and (max-width: 1400px)'
    expect(matchesViewportQuery(q, 834, 1056)).toBe(true)
    expect(matchesViewportQuery(q, 1194, 696)).toBe(false)
    expect(matchesViewportQuery(q, 700, 400)).toBe(true)
  })

  it('returns null for what it cannot evaluate', () => {
    expect(matchesViewportQuery('(any-pointer: coarse)', 800, 600)).toBeNull()
    expect(matchesViewportQuery('(max-width: 50em)', 800, 600)).toBeNull()
    expect(matchesViewportQuery('(max-width: 800px), (hover: hover)', 900, 600)).toBeNull()
  })
})
