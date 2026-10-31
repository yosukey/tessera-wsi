import { useEffect, useRef } from 'react'
import { matchPath, useLocation } from 'react-router'
import { recordPageView, type PageViewPayload } from '../lib/api/analytics'

// Routes that should never fire a beacon. Admin routes are excluded for
// the same reason GA is — keeping admin sessionStorage exposure off any
// third-party network — and `/login` is excluded so app-auth login
// pages don't get logged as anonymous "top page" views.
const SKIP_PREFIXES = ['/admin', '/login']

// Suppress duplicate sends within a short window. React Router fires a
// location change for back/forward navigation and for `replaceState`
// updates the SPA does internally (e.g. URL parameter sync); without
// this guard a single user navigation could log multiple rows.
const DEDUPE_WINDOW_MS = 5_000

function classify(pathname: string): PageViewPayload | null {
  for (const prefix of SKIP_PREFIXES) {
    if (pathname === prefix || pathname.startsWith(`${prefix}/`)) return null
  }
  if (pathname === '/') {
    return { kind: 'top' }
  }
  // The URL segments are the slide/tour public_id tokens. The beacon runs off
  // the URL alone (no loaded slide object), so it forwards the opaque tokens
  // and the backend resolves them to internal ids for storage.
  const tour = matchPath('/slides/:id/tours/:tourId', pathname)
  if (tour) {
    const slidePublicId = tour.params.id
    const tourPublicId = tour.params.tourId
    if (!slidePublicId || !tourPublicId) return null
    return { kind: 'tour', slide_public_id: slidePublicId, tour_public_id: tourPublicId }
  }
  const viewer = matchPath('/slides/:id', pathname)
  if (viewer) {
    const slidePublicId = viewer.params.id
    if (!slidePublicId) return null
    return { kind: 'viewer', slide_public_id: slidePublicId }
  }
  return null
}

function payloadKey(p: PageViewPayload): string {
  return `${p.kind}|${p.slide_public_id ?? ''}|${p.tour_public_id ?? ''}`
}

export function useAnalyticsBeacon(): void {
  const location = useLocation()
  // useRef so the dedupe state survives renders without re-triggering
  // the effect (which a useState would).
  const lastRef = useRef<{ key: string; at: number } | null>(null)
  const pathnameRef = useRef(location.pathname)
  pathnameRef.current = location.pathname

  useEffect(() => {
    const payload = classify(location.pathname)
    if (!payload) return

    const key = payloadKey(payload)
    const now = Date.now()
    const last = lastRef.current
    if (last && last.key === key && now - last.at < DEDUPE_WINDOW_MS) return
    lastRef.current = { key, at: now }

    void recordPageView(payload)
  }, [location.pathname])

  // A page restored from the back/forward cache is a new visit the router
  // never hears of: the document (and this hook's state) comes back as it
  // was frozen, so no location change fires. Record it from pageshow instead.
  // The dedupe window is bypassed on purpose: it exists for the router's
  // duplicate events within one navigation, and Back-then-Forward within a
  // few seconds is two real visits. (useSlideDwell starts its fresh dwell
  // session from the same event.)
  useEffect(() => {
    const onPageshow = (e: PageTransitionEvent) => {
      if (!e.persisted) return
      const payload = classify(pathnameRef.current)
      if (!payload) return
      lastRef.current = { key: payloadKey(payload), at: Date.now() }
      void recordPageView(payload)
    }
    window.addEventListener('pageshow', onPageshow)
    return () => window.removeEventListener('pageshow', onPageshow)
  }, [])
}
