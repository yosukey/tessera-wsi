import { useEffect, type RefObject } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

// A control inside a disabled <fieldset> (a LockFieldset while its request is
// out) has no `disabled` of its own but can't take focus either: `:disabled`
// covers both. (Filtered after the query, which keeps document order.)
const canFocus = (el: HTMLElement) => !el.matches(':disabled')

// The open dialogs' containers, the most recently opened last. Only that one
// keeps Tab inside itself: a dialog opened over another (an image zoomed from
// the slide editor) must not have the one beneath pull focus back out.
const openDialogs: HTMLElement[] = []

/**
 * Focus handling for a modal dialog, for as long as the calling component is
 * mounted and ``active`` (for a dialog rendered conditionally inside a
 * component that stays mounted): focus moves into the dialog (to the element
 * marked ``data-autofocus``, else its first focusable element, else the
 * container, which should carry tabIndex={-1}), Tab / Shift+Tab cycle inside
 * it instead of reaching the page behind the backdrop, and on close focus
 * returns to the element that had it before — normally the trigger. Escape is
 * the dialog's own to handle. (A control inside must not use React's
 * ``autoFocus``: it would move focus before the trigger is recorded.)
 */
export function useDialogFocus(ref: RefObject<HTMLElement | null>, active = true): void {
  useEffect(() => {
    if (!active) return
    const root = ref.current
    if (!root) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusables = () => Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(canFocus)
    openDialogs.push(root)
    const autofocus = Array.from(root.querySelectorAll<HTMLElement>('[data-autofocus]')).find(canFocus)
    ;(autofocus ?? focusables()[0] ?? root).focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || openDialogs[openDialogs.length - 1] !== root) return
      const els = focusables()
      if (els.length === 0) {
        e.preventDefault()
        root.focus()
        return
      }
      const first = els[0]
      const last = els[els.length - 1]
      const focused = document.activeElement
      const inside = focused instanceof Node && root.contains(focused)
      // The container itself (focused on open when it holds no control, or
      // by a click on its background) comes before every control in tab
      // order, so Shift+Tab from it would leave the dialog too.
      if (e.shiftKey && (focused === first || focused === root || !inside)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (focused === last || !inside)) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      const at = openDialogs.lastIndexOf(root)
      if (at >= 0) openDialogs.splice(at, 1)
      if (previous && previous.isConnected) previous.focus()
    }
  }, [ref, active])
}
