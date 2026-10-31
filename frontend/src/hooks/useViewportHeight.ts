import { useEffect, useState } from 'react'

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
 */
export function useViewportHeight(): string {
  const [height, setHeight] = useState<string>('100dvh')
  useEffect(() => {
    const measure = () => {
      const h = window.visualViewport?.height ?? window.innerHeight
      if (h > 0) setHeight(`${Math.round(h)}px`)
    }
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('orientationchange', measure)
    window.visualViewport?.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('orientationchange', measure)
      window.visualViewport?.removeEventListener('resize', measure)
    }
  }, [])
  return height
}
