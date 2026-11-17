import { useEffect, useState } from 'react'

// After a rotation, when to measure again (ms): iPadOS Safari can fire
// `orientationchange` and even the first `resize` while the viewport still
// has its pre-rotation size, and sends nothing once it has settled.
const ROTATION_SETTLE_DELAYS_MS = [100, 300, 700]

/**
 * Full-viewport height that is correct from the first paint on iPadOS Safari.
 *
 * Relying on the CSS `100dvh` unit alone leaves the bottom of a full-height
 * viewer layout (scale bar, zoom indicator, info panel, …) clipped below the
 * visible area on iPad: Safari computes `dvh` against the wrong viewport on
 * the first paint of a client-side route and only corrects it once an
 * orientation change forces a viewport recalculation — exactly the "rotate
 * the device and it appears" symptom.
 *
 * Measuring the visible viewport in an effect (which runs after layout) and
 * keeping it in sync with resize / orientation events sidesteps that: the
 * pixel height lands on the first commit after mount, no rotation needed.
 * `100dvh` stays as the pre-measurement default so the very first paint is
 * still full-height on browsers without `visualViewport`.
 *
 * A rotation is measured again a few times while it settles (see
 * ROTATION_SETTLE_DELAYS_MS): kept at the stale size, the layout would stay
 * as tall as the portrait screen in landscape (its bottom off screen) or as
 * short as the landscape one in portrait (a blank band below it).
 */
export function useViewportHeight(): string {
  const [height, setHeight] = useState<string>('100dvh')
  useEffect(() => {
    const measure = () => {
      const h = window.visualViewport?.height ?? window.innerHeight
      if (h > 0) setHeight(`${Math.round(h)}px`)
    }
    let timers: number[] = []
    let frame: number | null = null
    const settle = () => {
      measure()
      timers.forEach((id) => window.clearTimeout(id))
      if (frame !== null) window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => { frame = null; measure() })
      timers = ROTATION_SETTLE_DELAYS_MS.map((ms) => window.setTimeout(measure, ms))
    }
    const orientation = typeof screen !== 'undefined' ? screen.orientation : undefined
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('orientationchange', settle)
    orientation?.addEventListener?.('change', settle)
    window.visualViewport?.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('orientationchange', settle)
      orientation?.removeEventListener?.('change', settle)
      window.visualViewport?.removeEventListener('resize', measure)
      timers.forEach((id) => window.clearTimeout(id))
      if (frame !== null) window.cancelAnimationFrame(frame)
    }
  }, [])
  return height
}
