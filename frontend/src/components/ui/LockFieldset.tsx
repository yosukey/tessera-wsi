import React, { useEffect, useRef } from 'react'

type Props = {
  // While true, every control inside is disabled (buttons included).
  locked: boolean
  // Layout of the group (display, gap, a border of its own, …), laid over
  // the frameless default.
  style?: React.CSSProperties
  children: React.ReactNode
}

// Holds what a request sent still while it is in flight, so its answer can't
// drop or overwrite an edit made meanwhile. A <fieldset disabled> without the
// browser's frame. See docs/developer-guide.md, "Admin forms: a request in
// flight".
//
// Disabling a control drops its focus, so the control that had it when the
// lock began gets it back when the lock ends: after Enter in a field, a wrong
// password or code can be retyped at once, and the next value added straight
// after the last. Not if the user has clicked or moved focus elsewhere
// meanwhile, nor if the control is gone; and without scrolling the page.
export function LockFieldset({ locked, style, children }: Props) {
  const ref = useRef<HTMLFieldSetElement | null>(null)
  const wasLocked = useRef(locked)
  const focused = useRef<HTMLElement | null>(null)
  // Read while rendering the lock, before the commit disables the control and
  // the browser moves its focus away.
  if (locked && !wasLocked.current) {
    const active = document.activeElement
    focused.current = active instanceof HTMLElement && ref.current?.contains(active) ? active : null
  }
  wasLocked.current = locked
  useEffect(() => {
    if (!locked) return
    // The user has gone on to something else: leave their focus alone.
    const away = (e: Event) => {
      if (!(e.target instanceof Node) || !ref.current?.contains(e.target)) focused.current = null
    }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('focusin', away, true)
    return () => {
      document.removeEventListener('pointerdown', away, true)
      document.removeEventListener('focusin', away, true)
    }
  }, [locked])
  useEffect(() => {
    if (locked) return
    const el = focused.current
    focused.current = null
    const active = document.activeElement
    if (el && el.isConnected && ref.current?.contains(el) && (!active || active === document.body || active === el)) {
      el.focus({ preventScroll: true })
    }
  }, [locked])
  return (
    <fieldset ref={ref} disabled={locked} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, ...style }}>
      {children}
    </fieldset>
  )
}
