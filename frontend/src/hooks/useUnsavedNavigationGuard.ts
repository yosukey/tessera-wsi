import { useCallback, useEffect, useRef } from 'react'

// The sentinel tokens this page load's guards have pushed or taken over. An
// entry carrying any other token was pushed before the page was reloaded.
const knownTokens = new Set<string>()

/**
 * Confirm before browser Back / Forward discards unsaved edits.
 *
 * The app uses <BrowserRouter>, so react-router's useBlocker is unavailable.
 * Instead, the first time ``dirty`` turns true a sentinel history entry (same
 * URL) is pushed on top of the page's own. Back then lands on the page's own
 * entry — same URL, so the router has nothing to re-render — and the popstate
 * handler asks for confirmation:
 *
 *   - cancel:  the sentinel is pushed again, so the user is back where they were;
 *   - confirm: (or no longer dirty) one more history.back() leaves the page,
 *     so the sentinel never costs the user an extra Back press.
 *
 * In-app exits must go through the returned ``leave(go)``: navigating from
 * the sentinel would stack the next page on top of it, so Back from there
 * would land on the sentinel and then on the page's own entry (two presses to
 * get past one page), and a ``replace`` would overwrite only the sentinel,
 * keeping the page reachable by Back (a deleted tour reopening). ``leave``
 * steps back off the sentinel first and runs ``go`` from the page's own entry.
 * It does not ask: the caller confirms discarding edits itself.
 *
 * A reload keeps the sentinel in place (the page reloads on top of it); the
 * guard that mounts on it takes it over, so it still costs no extra Back press.
 *
 * Limits: a multi-entry jump (long-press Back menu) has already navigated by
 * the time popstate fires and is not intercepted; ``beforeunload`` covers
 * tab close / reload.
 */
export function useUnsavedNavigationGuard(dirty: boolean, message: string): (go: () => void) => void {
  const dirtyRef = useRef(dirty)
  const messageRef = useRef(message)
  // Pathname the sentinel was pushed on, or null while no sentinel is in place.
  const sentinelPath = useRef<string | null>(null)
  // An in-app exit waiting for the step back off the sentinel to land.
  const pendingExit = useRef<(() => void) | null>(null)
  useEffect(() => {
    dirtyRef.current = dirty
    messageRef.current = message
  }, [dirty, message])

  // Identifies this hook instance's sentinel entry in history.state, so that
  // unmounting can tell its own entry from a page the user navigated on to.
  const token = useRef(`g${Math.random().toString(36).slice(2)}`)
  const onSentinel = () =>
    sentinelPath.current !== null
    && window.location.pathname === sentinelPath.current
    && window.history.state?.wsiNavGuard === token.current
  const pushSentinel = () => {
    knownTokens.add(token.current)
    window.history.pushState({ ...(window.history.state ?? {}), wsiNavGuard: token.current }, '')
  }
  // Mounted on a sentinel an earlier load of the page pushed (the page was
  // reloaded with one in place): treat it as this guard's own, or Back would
  // first land on the page's own entry - the same page - before leaving it.
  // A sentinel this page load pushed belongs to a guard that is unmounting
  // (and stepping off it) and is left alone.
  useEffect(() => {
    const inherited: unknown = window.history.state?.wsiNavGuard
    if (typeof inherited !== 'string' || knownTokens.has(inherited)) return
    knownTokens.add(inherited)
    token.current = inherited
    sentinelPath.current = window.location.pathname
  }, [])
  useEffect(() => {
    if (!dirty || sentinelPath.current !== null) return
    sentinelPath.current = window.location.pathname
    pushSentinel()
  }, [dirty])

  // A component that remounts on the same URL (a dashboard tab switched away
  // and back) would otherwise leave one sentinel per mount behind, each costing
  // the user an extra Back press. Take this guard's own entry off the stack
  // when nothing is at stake any more - but only while it is the current entry:
  // after a navigation elsewhere, popping would undo the user's navigation.
  useEffect(() => () => {
    if (!dirtyRef.current && onSentinel()) {
      sentinelPath.current = null
      window.history.back()
    }
  }, [])

  useEffect(() => {
    const onPop = () => {
      const exit = pendingExit.current
      if (exit) {
        // Back on the page's own entry: carry out the in-app exit from here.
        pendingExit.current = null
        sentinelPath.current = null
        exit()
        return
      }
      const path = sentinelPath.current
      if (path === null) return
      if (window.location.pathname !== path) {
        // Already somewhere else (multi-entry jump): nothing left to guard.
        sentinelPath.current = null
        return
      }
      if (dirtyRef.current && !window.confirm(messageRef.current)) {
        pushSentinel()
        return
      }
      sentinelPath.current = null
      window.history.back()
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  return useCallback((go: () => void) => {
    if (pendingExit.current) {
      // Called again before the step back has landed (a double click): the
      // later exit replaces the earlier one. A second history.back() would
      // step past the page's own entry as well.
      pendingExit.current = go
      return
    }
    if (!onSentinel()) {
      go()
      return
    }
    pendingExit.current = go
    window.history.back()
  }, [])
}
