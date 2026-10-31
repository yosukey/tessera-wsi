import React, { Suspense, lazy, useEffect, useRef } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router'
import { getToken, getViewerSettings } from '../lib/api'
import { useAnalyticsBeacon } from '../hooks/useAnalyticsBeacon'
import { BASE_KEYS as INDEX_FILTER_KEYS } from '../components/index/types'
import AnalyticsNoticeToast from '../components/AnalyticsNoticeToast'

// Route-level code splitting. Each page (and the admin group in particular,
// which drags in the dashboard / analytics / backup / tour-editor surfaces)
// becomes its own chunk so a public top-page or viewer visitor doesn't
// download the admin SPA up front. Pages already render their own loading
// states, and chunk fetches are fast on the LAN / self-hosted deployments
// this app targets, so the Suspense fallback is intentionally empty to avoid
// a flash of an extra spinner on top of the page's own one.
const IndexPage = lazy(() => import('./IndexPage'))
const ViewerPage = lazy(() => import('./ViewerPage'))
const TourViewerPage = lazy(() => import('./TourViewerPage'))
const Login = lazy(() => import('./Login'))
const AdminLogin = lazy(() => import('./AdminLogin'))
const AdminDashboard = lazy(() => import('./AdminDashboard'))
const AdminAccount = lazy(() => import('./AdminAccount'))
const AdminAnalyticsPage = lazy(() => import('./AdminAnalyticsPage'))
const AdminServerPage = lazy(() => import('./AdminServerPage'))
const AdminPerformancePage = lazy(() => import('./AdminPerformancePage'))
const AdminHistoryPage = lazy(() => import('./AdminHistoryPage'))
const AdminLogsPage = lazy(() => import('./AdminLogsPage'))
const AdminTourEditor = lazy(() => import('./AdminTourEditor'))
const AdminLayout = lazy(() => import('./AdminLayout'))

// A GA4 measurement ID, the only kind the dashboard accepts (the server checks
// the same shape on save). Checked again here, before the ID reaches a script
// URL: a value that arrived by other means - a restored or hand-edited
// database row - must not load a Tag Manager container or anything else.
const GA_MEASUREMENT_ID_RE = /^G-[A-Z0-9]{4,32}$/

// Routes GA must never run on (see useGoogleAnalytics for why).
function isGaExcludedRoute(path: string): boolean {
  return path === '/login' || path.startsWith('/admin')
}

// The only query parameters GA is told about: the SPA's own view state — the
// tour step, and the index page's search, sort, view tab and filters (the
// fixed stain / organ / lab / tags ones, taken from the index page's own
// BASE_KEYS so a filter added there isn't silently dropped here, plus `facet`
// for the configurable ones) — and GA's campaign tags (utm_*), which exist to
// be read by it. Everything else is dropped, above all `sync` — the
// live-session id, which is all a follower link needs to join the session —
// along with `from`, `debug`, and the hash. An allowlist rather than a list of
// secrets, so a parameter added later stays out of Google's hands until
// someone decides otherwise. The path itself is sent as is, slide and tour ids
// included (see docs/operations.md "Google Analytics").
const GA_QUERY_PARAMS = new Set(['step', 'q', 'sort', 'order', 'type', 'facet', ...INDEX_FILTER_KEYS])

function gaQuery(search: string): string {
  const kept = new URLSearchParams()
  for (const [key, value] of new URLSearchParams(search)) {
    if (GA_QUERY_PARAMS.has(key) || key.startsWith('utm_')) kept.append(key, value)
  }
  const qs = kept.toString()
  return qs ? `?${qs}` : ''
}

// document.referrer, stripped like the page URL. gtag otherwise reports it
// verbatim as page_referrer, and the Referrer-Policy (strict-origin-when-
// cross-origin) keeps the full URL for a same-origin full-page load — so the
// page a follower left by a reload or a full-page link, ?sync= and all, would
// reach Google as the next page's referrer. A same-origin referrer is reduced
// to its path plus the allowlisted query (gaQuery); a cross-origin one is
// passed on as the browser gives it — origin-only under the browsers' default
// policy, and in any case the other site's URL, not this one's.
function gaReferrer(): string {
  const ref = document.referrer
  if (!ref) return ''
  let url: URL
  try { url = new URL(ref) } catch { return '' }
  if (url.origin !== window.location.origin) return ref
  return url.origin + url.pathname + gaQuery(url.search)
}

function useGoogleAnalytics() {
  const location = useLocation()
  // Keyed on what GA is sent, not the raw query: joining or leaving a live
  // session, or toggling ?debug, is not a new page to report.
  const query = gaQuery(location.search)
  // Memoise the viewer-settings fetch so route changes don't re-hit the API:
  // resolves once to the GA id (or null when GA is not configured).
  const gaIdPromise = useRef<Promise<string | null> | null>(null)

  useEffect(() => {
    if (typeof window === 'undefined') return
    const path = location.pathname || ''
    // The admin SPA holds the JWT in sessionStorage, so any third-party script
    // running on those routes (a compromised GTM container, a malicious tag
    // pushed by mistake, etc.) could exfiltrate it. GA is skipped on /admin/*
    // and /login. This check runs on every navigation, and admin entry points
    // are full-page loads (see IndexHeader / Login), so
    // the GA script can never be present on an admin route — even in a session
    // that started on a public page. Keying on location also means a session
    // that started on /login still gets GA once it reaches a public route.
    if (isGaExcludedRoute(path)) return
    // The route check alone is not enough: an admin preview (a public route
    // such as /slides/X?from=admin, reached by SPA navigation or opened in a
    // new tab that copied the token from its opener) runs with the JWT sitting
    // in this tab's sessionStorage. Skip GA whenever a staff token is present,
    // whatever the route. The opener copy runs at module load, before GA could
    // have been injected. Login can happen in a document that already loaded GA while
    // token-less (SPA/history navigation between /admin and a public page),
    // so AdminLogin checks isThirdPartyAnalyticsLoaded() and enters the
    // dashboard with a full page load in that case — the token-holding page
    // then starts from a fresh document and this check keeps GA out of it.
    if (getToken()) return

    // Snapshot this visit's path and URL together, from the router location
    // this effect runs for, so the two always describe the same page. Both
    // carry only the allowlisted query, and never the hash (see gaQuery).
    const pagePath = path + query
    const pageLocation = window.location.origin + pagePath
    const pageReferrer = gaReferrer()
    // Set by the cleanup once a newer navigation (or unmount) supersedes this
    // visit. Only matters while the first settings fetch is in flight — later
    // the callback runs a microtask after this effect. Without it, every visit
    // made during that wait fires its own page_view once the settings land,
    // carrying whatever title is on screen by then (a later page's, not its
    // own). A page left before GA could even be configured (typically an
    // in-app redirect on first load) is therefore not reported; the visit
    // that is on screen is.
    let superseded = false

    if (!gaIdPromise.current) {
      const p = getViewerSettings()
        .then(vs => {
          const id = vs.ga_measurement_id?.trim() || null
          return id && GA_MEASUREMENT_ID_RE.test(id) ? id : null
        })
      gaIdPromise.current = p
      // Don't let a *transient* settings-fetch failure (e.g. a 429 burst on the
      // first public-route render) permanently memoise "no GA" for the whole
      // session: clear the ref on rejection so the next navigation re-attempts.
      // A successful resolve to null (GA simply not configured) is retained and
      // never re-fetched.
      p.catch(() => { if (gaIdPromise.current === p) gaIdPromise.current = null })
    }
    gaIdPromise.current.then(id => {
      if (superseded || !id) return
      // Re-check both exclusions right before touching the page: the checks
      // above ran before the await, and a staff token can be stored or the
      // document can move to an admin route in the meantime. The live
      // window.location is used rather than the router location, so a URL
      // that has already changed but whose render (and the cleanup that sets
      // `superseded`) hasn't committed yet is still caught.
      if (isGaExcludedRoute(window.location.pathname) || getToken()) return
      const w = window as any
      if (!document.getElementById('ga-script')) {
        // Initialize dataLayer and gtag in JS to avoid needing 'unsafe-inline'
        // in script-src CSP. send_page_view:false because each SPA
        // navigation is reported explicitly below (the auto pageview would only ever fire
        // for the landing route and double-count it).
        w.dataLayer = w.dataLayer || []
        w.gtag = function gtag() { w.dataLayer.push(arguments) }
        w.gtag('js', new Date())
        // Google signals and ad personalisation stay off: this is a measurement
        // tag for the operator's own page counts, and an educational site's
        // visitors (students) are not for building advertising audiences.
        w.gtag('config', id, {
          send_page_view: false,
          allow_google_signals: false,
          allow_ad_personalization_signals: false,
        })
        const s = document.createElement('script')
        s.id = 'ga-script'
        s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`
        s.async = true
        document.head.appendChild(s)
      }
      if (typeof w.gtag === 'function') {
        // Also the default for the hits gtag sends on its own (the
        // user_engagement it sends on leaving a page, enhanced-measurement
        // scroll / click events), which otherwise take document.location —
        // the raw URL, ?sync= and all — and document.referrer (gaReferrer).
        // An empty referrer is sent as '' rather than left out, so gtag has
        // nothing of its own to fall back to.
        w.gtag('set', { page_location: pageLocation, page_referrer: pageReferrer })
        w.gtag('event', 'page_view', {
          page_path: pagePath,
          page_location: pageLocation,
          page_referrer: pageReferrer,
          // Read at send time rather than snapshotted: pages set their title
          // once their own data loads, so an earlier read would only be
          // staler — and `superseded` guarantees this is still the same visit.
          page_title: document.title,
        })
      }
    }).catch(() => { /* transient settings-fetch failure; ref cleared above so the next navigation retries */ })
    return () => { superseded = true }
  }, [location.pathname, query])
}

function useTabFavicon() {
  // index.html ships a static <link rel="icon" href="/favicon.ico">. When an
  // admin replaces the favicon, the server-side mapping returns an ETag-busted
  // URL (/favicon.ico?v=<etag>) but the static href in index.html doesn't
  // change — Chrome in particular caches the bare /favicon.ico response well
  // past the 5-minute max-age, so the tab keeps showing the previous icon
  // until the user hard-refreshes. Rewriting the <link> href to include the
  // current ETag forces the browser to re-fetch when the favicon actually
  // changes. The cross-origin LAN-direct probe (operator-injected, targets
  // lan.wsi.<domain>/favicon.ico without a query string) is unaffected: this
  // only mutates the same-origin <link> element.
  useEffect(() => {
    getViewerSettings().then(vs => {
      const href = vs.favicon_url
      if (!href) return
      let link = document.querySelector('link[rel="icon"]') as HTMLLinkElement | null
      if (!link) {
        link = document.createElement('link')
        link.rel = 'icon'
        document.head.appendChild(link)
      }
      if (link.getAttribute('href') !== href) link.setAttribute('href', href)
    }).catch(() => {})
  }, [])
}

export default function App() {
  useGoogleAnalytics()
  useTabFavicon()
  useAnalyticsBeacon()
  // Toast is rendered unconditionally; it hides itself on admin/login
  // routes via useLocation so the viewer-settings fetch only happens on
  // initial app load instead of once per public↔admin transition.
  return (
    <>
      <Suspense fallback={null}>
        <Routes>
          <Route path="/" element={<IndexPage />} />
          <Route path="/slides/:id" element={<ViewerPage />} />
          <Route path="/slides/:id/tours/:tourId" element={<TourViewerPage />} />
          <Route path="/login" element={<Login />} />

          <Route path="/admin" element={<AdminLayout><AdminLogin /></AdminLayout>} />
          <Route path="/admin/dashboard" element={<AdminLayout><AdminDashboard /></AdminLayout>} />
          <Route path="/admin/account" element={<AdminLayout><AdminAccount /></AdminLayout>} />
          <Route path="/admin/analytics" element={<AdminLayout><AdminAnalyticsPage /></AdminLayout>} />
          <Route path="/admin/server" element={<AdminLayout><AdminServerPage /></AdminLayout>} />
          <Route path="/admin/performance" element={<AdminLayout><AdminPerformancePage /></AdminLayout>} />
          <Route path="/admin/history" element={<AdminLayout><AdminHistoryPage /></AdminLayout>} />
          <Route path="/admin/logs" element={<AdminLayout><AdminLogsPage /></AdminLayout>} />
          <Route path="/admin/tours/:tourId" element={<AdminLayout><AdminTourEditor /></AdminLayout>} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
      <AnalyticsNoticeToast />
    </>
  )
}
