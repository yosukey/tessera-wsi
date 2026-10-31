import { useCallback, useRef, useState } from 'react'

// One request at a time for a control: `run(fn)` starts `fn` unless an
// earlier run is still out, in which case it returns undefined without
// calling it. The guard is a ref, so a second click in the same tick (before
// `busy` has re-rendered) is refused too. `busy` drives the lock on the
// fields the request sent (see docs/developer-guide.md, "Admin forms: a
// request in flight"). An error from `fn` is passed on to the caller.
export function useInFlight(): {
  busy: boolean
  run: <T>(fn: () => Promise<T>) => Promise<T | undefined>
} {
  const { key, run: runKeyed } = useInFlightKey<true>()
  const run = useCallback(<T,>(fn: () => Promise<T>) => runKeyed(true, fn), [runKeyed])
  return { busy: key !== null, run }
}

// useInFlight for per-row actions: one request at a time across the rows,
// and `key` says whose it is (null while none is out), so only that row
// locks.
export function useInFlightKey<K>(): {
  key: K | null
  run: <T>(key: K, fn: () => Promise<T>) => Promise<T | undefined>
} {
  const out = useRef(false)
  const [key, setKey] = useState<K | null>(null)
  const run = useCallback(async <T,>(k: K, fn: () => Promise<T>): Promise<T | undefined> => {
    if (out.current) return undefined
    out.current = true
    setKey(() => k)
    try {
      return await fn()
    } finally {
      out.current = false
      setKey(null)
    }
  }, [])
  return { key, run }
}
