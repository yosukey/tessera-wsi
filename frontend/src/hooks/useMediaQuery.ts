import { useEffect, useState } from 'react'
import { matchesViewportQuery } from '../lib/viewportBox'
import { useViewportBox } from './useViewportBox'

/**
 * Subscribe to a CSS media query and re-render on match changes. Used for
 * layouts that need a JS branch (inline styles can't carry `@media` rules)
 * rather than a purely CSS-driven breakpoint.
 *
 * While the browser's layout viewport is out of step with the screen (Chrome
 * on iPad after a rotation; see lib/viewportBox), matchMedia answers for the
 * stale size, so a width / height / orientation query is evaluated against
 * the measured visible viewport instead.
 */
export function useMediaQuery(query: string): boolean {
  const box = useViewportBox()
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false,
  )
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  if (box.stale) {
    const measured = matchesViewportQuery(query, box.width, box.height)
    if (measured !== null) return measured
  }
  return matches
}
