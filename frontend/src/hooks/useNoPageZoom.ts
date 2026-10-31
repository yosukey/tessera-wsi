import { useEffect } from 'react'
import { cancelPageZoomGestures } from './usePreventPinchZoom'

// Class set on <html> while mounted; styles/global.css gives it
// touch-action: pan-x pan-y.
export const NO_PAGE_ZOOM_CLASS = 'no-page-zoom'

// Appended to the viewport meta while mounted.
const NO_ZOOM_VIEWPORT = 'maximum-scale=1, user-scalable=no'

/**
 * Keep a touch device from zooming the page while the calling page is
 * mounted — the admin pages, which are laid out for the screen they're on
 * and only get harder to use when a stray pinch, a double tap or focusing a
 * 14px input zooms them on a tablet.
 *
 * No single switch does it everywhere, so three layers:
 *
 * - the viewport meta gains `maximum-scale=1, user-scalable=no`. Android
 *   Chrome honours both and stops pinch and double-tap zoom on its own; iOS
 *   Safari ignores user-scalable but still takes maximum-scale as "don't
 *   auto-zoom onto a focused input";
 * - `touch-action: pan-x pan-y` on <html> (NO_PAGE_ZOOM_CLASS), for browsers
 *   that apply it down the tree. WebKit may restart from `auto` at each
 *   scroll container, which is where most admin content sits, so on iOS
 *   don't count on it;
 * - on iOS / iPadOS, then, the pinch is stopped by cancelling WebKit's
 *   gesture events on the document (so modals and popovers portalled to
 *   <body> are covered too). Unlike the viewer pages, no touchmove listener:
 *   registered non-passive on the whole document it would make every scroll
 *   of these long, busy pages wait for the main thread. Only on touch
 *   devices: on a Mac the gesture events are the trackpad pinch, which stays
 *   the desktop user's call, as does ctrl + wheel.
 *
 * Not covered, and left as they are: the browser's own zoom commands
 * (ctrl / cmd +, Safari's page zoom) and the OS's accessibility zoom. A
 * picture opened full-screen from Markdown can't be pinched larger here either.
 *
 * Everything is restored on unmount, so the public pages keep the browser's
 * normal zoom.
 */
export function useNoPageZoom(): void {
  useEffect(() => {
    const root = document.documentElement
    root.classList.add(NO_PAGE_ZOOM_CLASS)

    const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]')
    const originalViewport = meta?.getAttribute('content') ?? null
    if (meta) {
      meta.setAttribute('content', originalViewport ? `${originalViewport}, ${NO_ZOOM_VIEWPORT}` : NO_ZOOM_VIEWPORT)
    }

    const stopGestures = cancelPageZoomGestures(document, {
      ctrlWheel: false,
      touch: false,
      gestures: navigator.maxTouchPoints > 0,
    })

    return () => {
      stopGestures()
      if (meta) {
        if (originalViewport === null) meta.removeAttribute('content')
        else meta.setAttribute('content', originalViewport)
      }
      root.classList.remove(NO_PAGE_ZOOM_CLASS)
    }
  }, [])
}
