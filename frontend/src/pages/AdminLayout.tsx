import React from 'react'
import { useNoOverscroll } from '../hooks/useNoOverscroll'
import { useNoPageZoom } from '../hooks/useNoPageZoom'

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  // Tablet admins: no rubber-band / stretch at the scroll edges, and the page
  // doesn't zoom on a pinch, double tap or input focus (see useNoPageZoom for
  // what each browser honours).
  useNoOverscroll()
  useNoPageZoom()
  // At least the screen's height: 100% of #root (the layout viewport), not
  // 100vh, which on iPadOS Safari can be taller than what is on screen and
  // would leave every admin page scrolling past its own bottom.
  return <div className="admin-theme" style={{ minHeight: '100%' }}>{children}</div>
}
