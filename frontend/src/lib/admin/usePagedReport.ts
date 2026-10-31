import { useCallback, useEffect, useRef, useState } from 'react'
import { isAbortError, type ServerTz } from '../api'

// What the history / error-log listings answer with.
export type PagedReportResponse<T> = {
  total: number
  snapshot_id: number
  newer: number
  server_tz: ServerTz
  items: T[]
}

export type PageQuery = { limit: number; offset: number; max_id?: number }

// Offset paging over a snapshot, shared by the audit history and the error
// log. The first page fixes the snapshot (the newest id at that moment) and
// every later page is read from it, so rows written while the operator pages
// don't shift the pages under them; `newer` counts what arrived since, and
// `reload` starts over from a fresh snapshot.
//
// `filtersKey` identifies the applied filters (a change starts over at page 1
// on a fresh snapshot); `fetchPage` is read through a ref, so it may be a new
// closure on every render.
export function usePagedReport<T, R extends PagedReportResponse<T>>(
  fetchPage: (q: PageQuery, signal: AbortSignal) => Promise<R>,
  filtersKey: string,
  onError: (err: unknown) => string,
  pageSize: number,
) {
  const [items, setItems] = useState<T[]>([])
  const [response, setResponse] = useState<R | null>(null)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const snapshotRef = useRef<number | null>(null)
  const ctrlRef = useRef<AbortController | null>(null)
  const fetchRef = useRef(fetchPage)
  fetchRef.current = fetchPage
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  const [lastFiltersKey, setLastFiltersKey] = useState(filtersKey)

  // A filter change is a fresh snapshot from page 1. Done during render (not
  // in an effect) so the fetch below never runs once with the old offset.
  // The previous key is state, not a ref: StrictMode renders twice and keeps
  // only the second pass's state updates, so a ref updated in the first pass
  // would make the second one see "no change" and the reset to page 1 would
  // be lost. (React's "adjusting state when a prop changes" pattern.)
  if (lastFiltersKey !== filtersKey) {
    setLastFiltersKey(filtersKey)
    snapshotRef.current = null
    if (offset !== 0) setOffset(0)
  }

  useEffect(() => {
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    setLoading(true)
    setError('')
    const q: PageQuery = { limit: pageSize, offset }
    if (snapshotRef.current !== null) q.max_id = snapshotRef.current
    fetchRef.current(q, ctrl.signal)
      .then(res => {
        if (ctrl.signal.aborted) return
        snapshotRef.current = res.snapshot_id
        // A page emptied under the operator (retention trimmed the oldest
        // rows, or a clear) would otherwise show "nothing here" with the
        // pager hidden and no way back: step to the last page that exists
        // (the first, when nothing is left at all).
        if (res.items.length === 0 && offset > 0) {
          setOffset(res.total > 0 ? Math.floor((res.total - 1) / pageSize) * pageSize : 0)
          return
        }
        setItems(res.items)
        setResponse(res)
      })
      .catch(err => {
        if (!isAbortError(err)) setError(onErrorRef.current(err))
      })
      .finally(() => {
        // Only the live request may clear the spinner. A superseded request
        // (aborted because offset/filters changed) settles AFTER the new run
        // has already set loading=true; without this guard its finally would
        // flip loading back to false mid-flight, re-enabling the Prev/Next
        // buttons and letting a fast second click double-advance the page.
        if (!ctrl.signal.aborted) setLoading(false)
      })
    return () => ctrl.abort()
  }, [offset, filtersKey, reloadKey, pageSize])

  // Start over from page 1 on a fresh snapshot (the Refresh button, the
  // "new entries" banner, after a clear).
  const reload = useCallback(() => {
    // Drop the request in flight now, not at the next effect run: its answer
    // would otherwise land first and put its old snapshot back.
    ctrlRef.current?.abort()
    snapshotRef.current = null
    setOffset(0)
    setReloadKey(k => k + 1)
  }, [])

  return {
    items,
    total: response?.total ?? 0,
    newer: response?.newer ?? 0,
    snapshotId: response?.snapshot_id ?? null,
    serverTz: response?.server_tz ?? null,
    response,
    offset,
    setOffset,
    loading,
    error,
    setError,
    reload,
  }
}

// A CSV export that can be cancelled and reports the bytes received. The
// in-flight download is aborted on unmount, so leaving the page stops it.
export function useCsvExport(onError: (err: unknown) => string) {
  const [exporting, setExporting] = useState(false)
  const [received, setReceived] = useState(0)
  const [error, setError] = useState('')
  const ctrlRef = useRef<AbortController | null>(null)

  useEffect(() => () => ctrlRef.current?.abort(), [])

  const start = useCallback(async (
    run: (opts: { signal: AbortSignal; onProgress: (n: number) => void }) => Promise<void>,
  ) => {
    if (ctrlRef.current) return
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    setExporting(true)
    setReceived(0)
    setError('')
    try {
      await run({ signal: ctrl.signal, onProgress: setReceived })
    } catch (err) {
      if (!isAbortError(err) && !ctrl.signal.aborted) setError(onError(err))
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null
      setExporting(false)
    }
  }, [onError])

  const cancel = useCallback(() => { ctrlRef.current?.abort() }, [])

  return { exporting, received, error, start, cancel }
}
