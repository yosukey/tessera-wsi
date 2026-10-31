import { useEffect } from 'react'

// Class set on <html> while a page using the hook is mounted; the rules in
// styles/global.css turn overscroll off for the document and, on a touch
// screen, sideways for every element and fully for the vertical panels marked
// .overscroll-panel (portalled modals and popovers included).
export const NO_OVERSCROLL_CLASS = 'no-overscroll'

// Mounts holding the class. A count rather than a flag so a route change
// whose old and new pages both use the hook can't drop the class in the
// middle of the swap, whatever order the effects run in.
let holders = 0

/**
 * Turn off overscroll — iOS Safari's rubber-band bounce, Android Chrome's
 * stretch and its pull-to-refresh — while the calling page is mounted.
 *
 * On a tablet it makes the fixed-height viewer pages and the admin pages feel
 * loose: dragging a side panel past its end stretches the panel, and a drag
 * on anything that doesn't scroll stretches the whole page with it. Scrolling
 * itself still works everywhere; on a touch screen a drag that reaches the end
 * of a marked panel stops there instead of moving the page (see global.css).
 * Other pages (the index page) keep the browser's default, pull-to-refresh
 * included.
 */
export function useNoOverscroll(): void {
  useEffect(() => {
    const root = document.documentElement
    holders += 1
    root.classList.add(NO_OVERSCROLL_CLASS)
    return () => {
      holders -= 1
      if (holders === 0) root.classList.remove(NO_OVERSCROLL_CLASS)
    }
  }, [])
}
