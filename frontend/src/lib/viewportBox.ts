// What the page is actually shown in, measured, versus what CSS lays it out
// against.
//
// CSS sizes the page against the layout viewport (the initial containing
// block): `%` on <html>/<body>/#root, `vw` / `vh` and media queries all read
// it. Normally it is the screen. Chrome on iPad (CriOS 158 on iPadOS 27, as
// reported from a device) leaves it at the size the page was loaded at when
// the iPad is rotated: html.clientWidth/Height stay 1194x696 (landscape) while
// window.innerWidth/Height and visualViewport report the upright 834x1056 and
// no zoom. Laid out 1194px wide on an 834px screen, every page ran off it,
// and the orientation media queries answered "landscape" in portrait.
//
// So the visible viewport is measured, and while the two disagree the app
// sizes itself from the measurement instead (see useViewportBox and the
// `html.vp-stale` rules in styles/global.css).

export type ViewportBox = {
  // The visible viewport, in CSS px.
  width: number
  height: number
  // The layout viewport has fallen out of step with the screen.
  stale: boolean
}

// How far apart (CSS px) the layout and visible widths may be before the
// layout viewport counts as stale: sub-pixel rounding stays well under it.
const STALE_TOLERANCE_PX = 2

export function measureViewportBox(): ViewportBox {
  const vv = window.visualViewport
  const width = vv?.width ?? window.innerWidth
  const height = vv?.height ?? window.innerHeight
  const layoutWidth = document.documentElement.clientWidth
  // A pinch zoom also makes the visible width differ from the layout's, but
  // then the page scale isn't 1: only an unzoomed mismatch is the browser's.
  const unzoomed = Math.abs((vv?.scale ?? 1) - 1) < 0.01
  const stale = unzoomed && width > 0 && layoutWidth > 0 && Math.abs(layoutWidth - width) > STALE_TOLERANCE_PX
  return { width: Math.round(width), height: Math.round(height), stale }
}

// Evaluates a media query list the app uses for layout — comma-separated
// alternatives of `and`-joined `(max-width: Npx)`, `(min-width: Npx)`,
// `(max-height: Npx)`, `(min-height: Npx)` and `(orientation: …)` — for a
// viewport of the given size, as a browser would. null for anything else, so
// the caller can fall back to matchMedia.
export function matchesViewportQuery(query: string, width: number, height: number): boolean | null {
  let supported = true
  const result = query.split(',').some((alternative) => alternative.trim().split(/\s+and\s+/).every((condition) => {
    const m = /^\((max-width|min-width|max-height|min-height|orientation):\s*(\d+(?:\.\d+)?px|portrait|landscape)\)$/.exec(condition.trim())
    if (!m) {
      supported = false
      return false
    }
    const [, feature, value] = m
    if (feature === 'orientation') return (height >= width ? 'portrait' : 'landscape') === value
    if (!value.endsWith('px')) {
      supported = false
      return false
    }
    const px = parseFloat(value)
    switch (feature) {
      case 'max-width': return width <= px
      case 'min-width': return width >= px
      case 'max-height': return height <= px
      default: return height >= px
    }
  }))
  return supported ? result : null
}

export const VIEWPORT_STALE_CLASS = 'vp-stale'

// Mark <html> for the `html.vp-stale` rules and hand them the measured size;
// remove both once the layout viewport is back in step.
export function applyViewportBox(box: ViewportBox, root: HTMLElement = document.documentElement): void {
  if (box.stale) {
    root.classList.add(VIEWPORT_STALE_CLASS)
    root.style.setProperty('--vp-width', `${box.width}px`)
    root.style.setProperty('--vp-height', `${box.height}px`)
  } else if (root.classList.contains(VIEWPORT_STALE_CLASS)) {
    root.classList.remove(VIEWPORT_STALE_CLASS)
    root.style.removeProperty('--vp-width')
    root.style.removeProperty('--vp-height')
  }
}
