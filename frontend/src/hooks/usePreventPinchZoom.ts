import { RefObject, useEffect } from 'react'

// Class for the element given to usePreventPinchZoom; styles/global.css gives
// its descendants touch-action: pan-x pan-y.
export const NO_PINCH_ZOOM_CLASS = 'no-pinch-zoom'

/**
 * Stop pinch gestures that land on `ref`'s element from zooming the page.
 *
 * Meant for the viewer pages (slide and tour): a tablet user pinches the slide
 * constantly, and a pinch that starts a little off it lands on a side panel
 * or an overlay over the slide instead and zooms the whole page — the slide
 * included — rather than doing nothing. Nothing there needs zooming, so every
 * page-zoom gesture that starts inside the element is cancelled:
 *
 * - two-finger `touchmove`: the touch pinch. preventDefault on a non-passive
 *   listener is what stops it on iOS Safari, which honours `touch-action`
 *   only partly (OpenSeadragon's MouseTracker does the same on its canvas);
 * - `gesturestart` / `gesturechange`: WebKit's own pinch events (iOS Safari,
 *   and trackpad pinch in macOS Safari);
 * - ctrl + `wheel`: how Chromium and Firefox deliver a trackpad pinch on
 *   desktop. A mouse's Ctrl + wheel over the panel is blocked with it; the
 *   keyboard zoom shortcuts still work.
 *
 * Single-finger touches and plain wheel events pass through, so panels still
 * scroll. An OpenSeadragon viewer inside keeps its own pinch: it reads the
 * gesture from pointer events, which cancelling these doesn't suppress, and
 * its listeners run before these (bubbling) anyway. Pair the hook with
 * `touch-action: pan-x pan-y` on the same element, plus NO_PINCH_ZOOM_CLASS
 * for its descendants, so browsers that honour touch-action never begin the
 * zoom at all. On iOS that CSS is the part that holds: a pinch whose first
 * finger already started scrolling a panel sends touchmove events that are
 * no longer cancelable.
 */
export function usePreventPinchZoom(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = ref.current
    if (!el) return
    return cancelPageZoomGestures(el)
  }, [ref])
}

/**
 * Attach the listeners described on usePreventPinchZoom to `target` and
 * return their cleanup. Split out so a page can cover the whole document
 * (portalled modals and popovers included) rather than one element — see
 * useNoPageZoom. `ctrlWheel: false` leaves ctrl + wheel alone, keeping the
 * desktop browser's own zoom; `touch: false` skips the touchmove listener,
 * whose non-passive registration makes every touch scroll under `target`
 * wait for the main thread.
 */
export function cancelPageZoomGestures(
  target: EventTarget,
  { ctrlWheel = true, gestures = true, touch = true }: { ctrlWheel?: boolean; gestures?: boolean; touch?: boolean } = {},
): () => void {
  const onTouchMove = (e: Event) => {
    if ((e as TouchEvent).touches.length >= 2) e.preventDefault()
  }
  const onGesture = (e: Event) => e.preventDefault()
  const onWheel = (e: Event) => {
    if ((e as WheelEvent).ctrlKey) e.preventDefault()
  }
  // Native listeners, explicitly non-passive: React registers its onTouchMove
  // / onWheel as passive, and a passive listener's preventDefault is ignored.
  // (Chromium also defaults touchmove / wheel on document to passive.)
  if (touch) target.addEventListener('touchmove', onTouchMove, { passive: false })
  if (gestures) {
    target.addEventListener('gesturestart', onGesture)
    target.addEventListener('gesturechange', onGesture)
  }
  if (ctrlWheel) target.addEventListener('wheel', onWheel, { passive: false })
  return () => {
    target.removeEventListener('touchmove', onTouchMove)
    target.removeEventListener('gesturestart', onGesture)
    target.removeEventListener('gesturechange', onGesture)
    target.removeEventListener('wheel', onWheel)
  }
}
