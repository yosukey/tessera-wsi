import { useCallback, useEffect, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { getAuthState, isAbortError } from './api'
import { logger } from './logger'
import { loginUrlFor } from './nextPath'

/**
 * On mount, check whether app-access auth is enabled and the visitor is
 * authenticated. If a session is required but missing, redirect to /login.
 * Used by public pages (viewer, index, tour viewer) and the dashboard
 * sign-in page so that an
 * unauthenticated visitor sees the login screen instead of an empty page.
 *
 * Staff need no app-access session: getAuthState sends the tab's staff token
 * and the server reports a valid one as authenticated, so a manager opening a
 * draft preview or the presenter view from the dashboard never lands on a
 * viewer login they may have no account for. The server's answer is the only
 * one trusted: a token it rejected (revoked, suspended, password changed)
 * lets nothing through, whatever its expiry claims.
 */
export function useAuthGate(): () => void {
  const nav = useNavigate()
  // Read through a ref so a later in-page search-param change doesn't re-run
  // the probe; the URL is only needed at the moment the redirect fires.
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  // So is the navigate function. react-router 7 hands out a new one whenever
  // the pathname changes (useNavigate depends on it), and a page that stays
  // mounted across slides - the viewer, while a live-sync presenter moves on -
  // would otherwise re-run the probe on every slide change: one more
  // /api/auth/state per follower per move, from clients that often share one
  // address and one rate-limit budget.
  const navRef = useRef(nav)
  navRef.current = nav
  const aliveRef = useRef(true)
  const probe = useRef<() => void>(() => {})
  probe.current = () => {
    getAuthState()
      .then(s => {
        if (!aliveRef.current) return
        if (s.mode === 'none' || s.mode === 'basic') return
        // Remember the target so a shared live-sync / tour URL survives sign-in.
        if (!s.authenticated) navRef.current(loginUrlFor(locRef.current), { replace: true })
      })
      .catch((err) => {
        // Deliberately fail open: if /api/auth/state itself is unreachable the
        // page renders rather than trapping the visitor on a login screen the
        // server can't describe. Log it, though: a silent outage of this
        // endpoint would leave gated pages behaving as if auth were off.
        if (isAbortError(err)) return
        logger.warn('auth-state probe failed; rendering ungated', err)
      })
  }
  useEffect(() => {
    aliveRef.current = true
    probe.current()
    return () => { aliveRef.current = false }
  }, [])
  // The page probes once, when it opens. A page that stays mounted for long
  // (the viewer, following a presenter from slide to slide) can outlive the
  // session; the page calls this when one of its own requests is answered 401,
  // and the visitor goes to the sign-in screen as if the page had just opened.
  // It redirects only when the server says the visitor is not authenticated,
  // so a 401 for any other reason cannot loop.
  return useCallback(() => probe.current(), [])
}
