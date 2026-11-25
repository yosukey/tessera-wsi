// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  applyViewportBox, fixedBottom, fixedRight, FULL_VIEWPORT_FIXED, matchesViewportQuery, measureViewportBox,
  VIEWPORT_CLIP_X_CLASS, VIEWPORT_STALE_CLASS,
} from './viewportBox'

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
  applyViewportBox({ width: 0, height: 0, stale: false, clipX: false })
})

describe('measureViewportBox', () => {
  // Chrome on iPad Pro 11", loaded in landscape and turned upright, as its
  // ?vpdebug=1 panel showed: html.client 1194x696, inner / visual 834x1056 @1.
  it('flags the layout viewport Chrome on iPad leaves behind after a rotation', () => {
    viewport({ layout: [1194, 696], visible: [834, 1056] })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: true, clipX: true })
  })

  // Here the app is wider than the stale <html>: hiding horizontal overflow
  // would cut it, so only the stale flag is set.
  it('flags it the other way round too (loaded upright, turned to landscape)', () => {
    viewport({ layout: [834, 1056], visible: [1194, 696] })
    expect(measureViewportBox()).toEqual({ width: 1194, height: 696, stale: true, clipX: false })
  })

  it('is not stale when the two agree, give or take rounding', () => {
    viewport({ layout: [834, 1056], visible: [834.4, 1056] })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: false, clipX: false })
  })

  // A pinch zoom shrinks the visible viewport too; that is the user's doing.
  it('is not stale while the page is zoomed, and reports the unzoomed size', () => {
    viewport({ layout: [834, 1056], visible: [417, 528], scale: 2 })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: false, clipX: false })
  })

  it('falls back to innerWidth / innerHeight without visualViewport, never stale', () => {
    viewport({ layout: [1194, 696], visible: [834, 1056], noVisualViewport: true })
    expect(measureViewportBox()).toEqual({ width: 834, height: 1056, stale: false, clipX: false })
  })

  // innerWidth counts a classic scrollbar, clientWidth doesn't: comparing
  // them would flag every desktop page that scrolls.
  it('does not mistake a desktop scrollbar for a stale layout viewport', () => {
    viewport({ layout: [1265, 900], visible: [1280, 900], noVisualViewport: true })
    expect(measureViewportBox().stale).toBe(false)
  })
})

describe('applyViewportBox', () => {
  it('marks <html> with the measured size while stale and clears it after', () => {
    const de = document.documentElement
    applyViewportBox({ width: 834, height: 1056, stale: true, clipX: true })
    expect(de.classList.contains(VIEWPORT_STALE_CLASS)).toBe(true)
    expect(de.classList.contains(VIEWPORT_CLIP_X_CLASS)).toBe(true)
    expect(de.style.getPropertyValue('--vp-width')).toBe('834px')
    expect(de.style.getPropertyValue('--vp-height')).toBe('1056px')
    applyViewportBox({ width: 1194, height: 696, stale: false, clipX: false })
    expect(de.classList.contains(VIEWPORT_STALE_CLASS)).toBe(false)
    expect(de.classList.contains(VIEWPORT_CLIP_X_CLASS)).toBe(false)
    expect(de.style.getPropertyValue('--vp-width')).toBe('')
  })

  it('hides horizontal overflow only while the stale layout is the wider one', () => {
    const de = document.documentElement
    applyViewportBox({ width: 1194, height: 696, stale: true, clipX: false })
    expect(de.classList.contains(VIEWPORT_STALE_CLASS)).toBe(true)
    expect(de.classList.contains(VIEWPORT_CLIP_X_CLASS)).toBe(false)
  })
})

describe('fixed-position helpers', () => {
  // Resolve the helpers' CSS the way a browser would, for a fixed containing
  // block of `cb` and (when stale) the measured --vp-* size.
  function resolve(expr: string, cbPx: number, vpPx: number | null): number {
    const withVar = expr.replace(/var\(--vp-(?:width|height), 100%\)/g, vpPx === null ? '100%' : `${vpPx}px`)
    const inner = /^calc\((.*)\)$/.exec(withVar)![1].replace(/100%/g, `${cbPx}px`)
    return inner.split(/\s+(?=[+-]\s)/).reduce((sum, term) => {
      const m = /^([+-])?\s*(-?\d+(?:\.\d+)?)px$/.exec(term.trim())!
      return sum + (m[1] === '-' ? -1 : 1) * parseFloat(m[2])
    }, 0)
  }

  it('anchor to the visible edge whichever box fixed positioning uses', () => {
    // In step: plain offsets.
    expect(resolve(fixedRight(12), 1194, null)).toBe(12)
    expect(resolve(fixedBottom(4), 696, null)).toBe(4)
    // Stale, fixed resolved against the old 1194-wide layout: the button
    // sits 12px inside the visible 834px, not 348px past it.
    expect(1194 - resolve(fixedRight(12), 1194, 834)).toBe(834 - 12)
    // Stale, fixed resolved against the visible viewport already.
    expect(resolve(fixedRight(12), 834, 834)).toBe(12)
    // Turned the other way: bottom of a 696-high screen on a 1056-high layout.
    expect(1056 - resolve(fixedBottom(4), 1056, 696)).toBe(696 - 4)
  })

  it('size a full-viewport overlay to the measured viewport', () => {
    expect(FULL_VIEWPORT_FIXED).toMatchObject({
      position: 'fixed', top: 0, left: 0,
      width: 'var(--vp-width, 100%)', height: 'var(--vp-height, 100%)',
    })
    expect(FULL_VIEWPORT_FIXED).not.toHaveProperty('inset')
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
