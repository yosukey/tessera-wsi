import { useCallback, useInsertionEffect, useRef } from 'react'

/**
 * A callback whose identity never changes but which always runs the latest
 * `fn` (and so sees the latest props / state). For handlers handed to a
 * memoised child: a fresh closure per render would defeat React.memo, so the
 * whole list below would re-render on every keystroke in an unrelated input.
 */
export function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const ref = useRef(fn)
  // Updated in an insertion effect (runs before any layout effect or event can
  // call the wrapper) rather than during render: a render that React discards
  // (an interrupted transition) must not leave its closure behind.
  useInsertionEffect(() => {
    ref.current = fn
  })
  return useCallback((...args: A) => ref.current(...args), [])
}
