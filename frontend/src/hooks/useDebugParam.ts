import { useCallback } from 'react'
import { useSearchParams } from 'react-router'

/**
 * The viewers' OpenSeadragon debug mode, kept in the URL as ``?debug=1`` so
 * it survives a reload. Returns the current state and a toggle that flips it
 * while leaving the rest of the query string as it is.
 */
export function useDebugParam(): [boolean, () => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const enabled = searchParams.get('debug') === '1'
  const toggle = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (next.get('debug') === '1') next.delete('debug')
      else next.set('debug', '1')
      return next
    }, { replace: true })
  }, [setSearchParams])
  return [enabled, toggle]
}
