import { useSyncExternalStore } from 'react'
import { applyViewportBox, measureViewportBox, type ViewportBox } from '../lib/viewportBox'

// When to measure again after a viewport change (ms): browsers on iPad can
// report a rotation while the viewport still has its old size, and send
// nothing once it has settled. (Same as useViewportHeight.)
const SETTLE_DELAYS_MS = [100, 300, 700]

// One measurement shared by every subscriber, kept current while at least one
// is subscribed (App always is), and mirrored onto <html> for the CSS rules.
let current: ViewportBox | null = null
const subscribers = new Set<() => void>()
let stopListening: (() => void) | null = null

function measure(): void {
  const next = measureViewportBox()
  applyViewportBox(next)
  if (current && current.width === next.width && current.height === next.height && current.stale === next.stale) return
  current = next
  subscribers.forEach((notify) => notify())
}

function startListening(): () => void {
  let timers: number[] = []
  let frame: number | null = null
  const settle = () => {
    measure()
    timers.forEach((id) => window.clearTimeout(id))
    if (frame !== null) window.cancelAnimationFrame(frame)
    frame = window.requestAnimationFrame(() => { frame = null; measure() })
    timers = SETTLE_DELAYS_MS.map((ms) => window.setTimeout(measure, ms))
  }
  const orientation = typeof screen !== 'undefined' ? screen.orientation : undefined
  // <html> is sized to the layout viewport (reset.css): this sees it change
  // (or finally catch up) whatever events arrive.
  const layoutViewport = typeof ResizeObserver === 'function' ? new ResizeObserver(settle) : null
  window.addEventListener('resize', settle)
  window.addEventListener('orientationchange', settle)
  orientation?.addEventListener?.('change', settle)
  window.visualViewport?.addEventListener('resize', settle)
  layoutViewport?.observe(document.documentElement)
  return () => {
    window.removeEventListener('resize', settle)
    window.removeEventListener('orientationchange', settle)
    orientation?.removeEventListener?.('change', settle)
    window.visualViewport?.removeEventListener('resize', settle)
    layoutViewport?.disconnect()
    timers.forEach((id) => window.clearTimeout(id))
    if (frame !== null) window.cancelAnimationFrame(frame)
  }
}

function subscribe(notify: () => void): () => void {
  subscribers.add(notify)
  if (!stopListening) {
    measure()
    stopListening = startListening()
  }
  return () => {
    subscribers.delete(notify)
    if (subscribers.size === 0 && stopListening) {
      stopListening()
      stopListening = null
    }
  }
}

function getSnapshot(): ViewportBox {
  if (!current) {
    current = measureViewportBox()
    applyViewportBox(current)
  }
  return current
}

/**
 * The visible viewport, measured, and whether the browser's layout viewport
 * has fallen out of step with it (see lib/viewportBox). Subscribing keeps
 * <html>'s `vp-stale` class and `--vp-width` / `--vp-height` current, which
 * the CSS uses to size the app to the screen while it is out of step.
 */
export function useViewportBox(): ViewportBox {
  return useSyncExternalStore(subscribe, getSnapshot)
}

// Tests: forget the shared measurement.
export function resetViewportBoxForTests(): void {
  stopListening?.()
  stopListening = null
  subscribers.clear()
  current = null
}
