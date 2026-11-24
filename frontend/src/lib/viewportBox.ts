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

import type { CSSProperties } from 'react'

export type ViewportBox = {
  // The visible viewport, in CSS px, at page scale 1 (a pinch zoom
  // magnifies the page; it doesn't make the space the layout gets smaller).
  width: number
  height: number
  // The layout viewport has fallen out of step with the screen.
  stale: boolean
  // ...and is wider than the screen (stale only): the extra width lies
  // past the screen's right edge, so it must not be scrollable into.
  clipX: boolean
}

// How far apart (CSS px) the layout and visible widths may be before the
// layout viewport counts as stale: sub-pixel rounding stays well under it.
const STALE_TOLERANCE_PX = 2

// The visible viewport's size at page scale 1, in CSS px. visualViewport
// reports the zoomed-in region while the page is pinch-zoomed (a trackpad
// pinch on a desktop, a two-finger pinch on a tablet): width / height are
// then the unzoomed size divided by the scale, so multiplying back gives the
// space the page actually has. Without visualViewport (only browsers far
// older than the ones this fix targets), innerWidth / innerHeight.
export function visibleViewportSize(): { width: number; height: number; scale: number } {
  const vv = window.visualViewport
  const scale = vv && vv.scale > 0 ? vv.scale : 1
  return {
    width: vv ? vv.width * scale : window.innerWidth,
    height: vv ? vv.height * scale : window.innerHeight,
    scale,
  }
}

export function measureViewportBox(): ViewportBox {
  const vv = window.visualViewport
  const { width, height, scale } = visibleViewportSize()
  const layoutWidth = document.documentElement.clientWidth
  // A pinch zoom also makes the visible width differ from the layout's, but
  // then the page scale isn't 1: only an unzoomed mismatch is the browser's.
  const unzoomed = Math.abs(scale - 1) < 0.01
  // Only visualViewport is comparable with clientWidth: both leave out a
  // classic (space-taking) scrollbar, whereas innerWidth includes it, so
  // without visualViewport every desktop page with a vertical scrollbar
  // would look ~15px "stale". Every browser with the stale-viewport bug
  // (Chrome / WebKit on iPad) has visualViewport, so nothing is lost.
  const stale = Boolean(vv) && unzoomed && width > 0 && layoutWidth > 0
    && Math.abs(layoutWidth - width) > STALE_TOLERANCE_PX
  const clipX = stale && layoutWidth > width
  return { width: Math.round(width), height: Math.round(height), stale, clipX }
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
// Added alongside VIEWPORT_STALE_CLASS while the stale layout viewport is
// wider than the screen (see `clipX`). Only that direction hides horizontal
// overflow: the other way round (loaded upright, turned to landscape) the
// app is wider than <html>, and hiding the overflow there would risk
// clipping the app's right-hand part off a screen that has room for it.
export const VIEWPORT_CLIP_X_CLASS = 'vp-stale-clip-x'

// Mark <html> for the `html.vp-stale` rules and hand them the measured size;
// remove both once the layout viewport is back in step.
export function applyViewportBox(box: ViewportBox, root: HTMLElement = document.documentElement): void {
  if (box.stale) {
    root.classList.add(VIEWPORT_STALE_CLASS)
    root.classList.toggle(VIEWPORT_CLIP_X_CLASS, box.clipX)
    root.style.setProperty('--vp-width', `${box.width}px`)
    root.style.setProperty('--vp-height', `${box.height}px`)
  } else if (root.classList.contains(VIEWPORT_STALE_CLASS)) {
    root.classList.remove(VIEWPORT_STALE_CLASS, VIEWPORT_CLIP_X_CLASS)
    root.style.removeProperty('--vp-width')
    root.style.removeProperty('--vp-height')
  }
}

// ---- position: fixed against the measured viewport -------------------------
//
// A `position: fixed` box is placed against the browser's viewport, not
// #root, so the `html.vp-stale` rules that size #root don't reach it: while
// the layout viewport is stale, `inset: 0` / `right: 12px` / `bottom: 4px`
// may resolve against the old orientation's size, putting a modal's centre,
// a close button or a bottom-anchored panel off screen. These helpers anchor
// fixed boxes to the measured --vp-width / --vp-height instead. Each falls
// back to exactly the plain CSS value while the viewport is in step (the
// variables are unset then), and each is correct whichever box the browser
// resolves fixed positioning against while stale — the stale layout
// viewport or the visible one — because the `100%` terms cancel out when
// that box already is the visible size.

// A full-viewport fixed overlay (modal backdrop, lightbox): use in place of
// `position: 'fixed', inset: 0`.
export const FULL_VIEWPORT_FIXED: CSSProperties = {
  position: 'fixed',
  top: 0,
  left: 0,
  width: 'var(--vp-width, 100%)',
  height: 'var(--vp-height, 100%)',
}

function cssLength(offset: number | string): string {
  return typeof offset === 'number' ? `${offset}px` : offset
}

// `right` for a fixed box `offset` from the visible viewport's right edge.
export function fixedRight(offset: number | string): string {
  return `calc(100% - var(--vp-width, 100%) + ${cssLength(offset)})`
}

// `bottom` for a fixed box `offset` from the visible viewport's bottom edge.
export function fixedBottom(offset: number | string): string {
  return `calc(100% - var(--vp-height, 100%) + ${cssLength(offset)})`
}
