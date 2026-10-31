import { adminFetch, adminGet, authHeaders, bearerHeaders, buildQuery, downloadCsv, getToken } from './shared'

export type PageViewKind = 'top' | 'viewer' | 'tour'

export type ServerTz = {
  abbr: string | null
  tz_name: string | null
  utc_offset: string | null
}

export type PageViewPayload = {
  kind: PageViewKind
  // Public_id tokens (from the URL), resolved to internal ids server-side.
  slide_public_id?: string | null
  tour_public_id?: string | null
}

// The beacon is a fetch() fired from the already-loaded SPA, so the
// request's own `Referer` header always points back at the app's own origin and
// the backend drops it as a self-referral. The real inbound referrer lives
// only in `document.referrer`, captured by the browser at page-load time
// and stable for the document's lifetime. Forward it so the backend can
// record where the visit actually came from. Same-origin referrers (in-app
// full reloads) are returned as "" and ignored server-side.
function externalReferrer(): string {
  try {
    const ref = document.referrer
    if (!ref) return ''
    const u = new URL(ref)
    if (u.host === window.location.host) return ''
    return ref
  } catch {
    return ''
  }
}

// Best-effort fire-and-forget. `keepalive: true` only matters for the
// "user closes the tab / clicks an external link while the beacon is
// in flight" case — for in-app SPA navigations the hook fires *after*
// the new route mounts, so there's no unload to survive. `credentials:
// same-origin` pulls in cookies / cached Basic-auth credentials so the
// server-side identity probe can attach the right viewer to the row.
// The promise is awaited only for typing; callers MUST NOT depend on
// success.
export async function recordPageView(payload: PageViewPayload): Promise<void> {
  try {
    // Attach the admin JWT (if any) so the backend can stamp the staff
    // role onto the PageView row and exclude it from learner-traffic
    // aggregates. Tokenless beacons (learners / anonymous) remain
    // `actor_role = NULL` server-side.
    await fetch('/api/analytics/pageview', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ ...payload, referrer: externalReferrer() }),
      credentials: 'same-origin',
      keepalive: true,
    })
  } catch {
    // Beacon failures are silent by design — analytics must never
    // surface a network error to the user.
  }
}

// ===== Per-session metrics: slide dwell + tour progress =====
//
// Both endpoints behave identically to recordPageView: 204 on success or
// silent drop on any unrecoverable error. The frontend MUST NOT rely on
// the promise resolving. Where possible, the unmount/visibility paths
// use navigator.sendBeacon (defined below) so a tab-close still
// delivers the final timing.

export type SlideDwellPayload = {
  // Slide's public_id token (from the URL); resolved to the internal id server-side.
  slide_public_id: string
  // Client-minted UUID v4 correlating the visibilitychange / pagehide /
  // unmount beacons of a single viewer-page session. The backend reconciles
  // repeated sends into one row via max(dwell_ms).
  session_id: string
  // ISO 8601 with timezone offset, e.g. "2026-05-20T10:30:00+09:00".
  // The backend normalises to UTC; client local time is irrelevant.
  started_at: string
  ended_at: string
  dwell_ms: number
}

export type TourEventStart = {
  event: 'start'
  session_id: string
  tour_public_id: string
  started_at: string
  step_index: number
  total_steps: number
}

export type TourEventProgress = {
  event: 'progress'
  session_id: string
  tour_public_id: string
  step_index: number
  total_dwell_ms: number
  // When total_dwell_ms was read, stamped by the clock that stamped the
  // session's started_at: the server caps the dwell at sent_at - started_at,
  // which a skewed client clock leaves intact (its own "now" would not).
  sent_at: string
}

export type TourEventComplete = {
  event: 'complete'
  session_id: string
  tour_public_id: string
  completed_at: string
  step_index: number
  total_dwell_ms: number
}

export type TourEventPayload = TourEventStart | TourEventProgress | TourEventComplete

// Prefer navigator.sendBeacon for unmount/visibilitychange paths: it
// queues the request with the browser's unload machinery so a fast tab
// close doesn't cancel it. Falls back to keepalive fetch when sendBeacon
// is unavailable or the browser refused the queue (e.g. payload too
// large) so dev environments and older browsers still record.
function postBeacon(url: string, body: unknown): void {
  try {
    const json = JSON.stringify(body)
    // Staff (admin/manager/instructor) carry a JWT in sessionStorage. The
    // backend stamps actor_role from it so staff QA traversals are excluded
    // from the default learner dwell / tour averages — same contract as the
    // pageview beacon. sendBeacon() cannot attach an Authorization header, so
    // when a token is present keepalive fetch is used instead. The marginally
    // weaker tab-close delivery only affects staff rows, which the default
    // aggregates filter out anyway, so nothing learner-facing is lost.
    const token = getToken()
    if (token) {
      void fetch(url, {
        method: 'POST',
        headers: bearerHeaders(token, { 'Content-Type': 'application/json' }),
        body: json,
        credentials: 'same-origin',
        keepalive: true,
      }).catch(() => {})
      return
    }
    const nav = navigator as Navigator & {
      sendBeacon?: (url: string, data?: BodyInit | null) => boolean
    }
    if (nav.sendBeacon) {
      // A throw from sendBeacon (some engines reject a Blob whose type is not
      // CORS-safelisted, others throw on a closing document) must fall
      // through to the keepalive fetch below, not into the outer swallow.
      // text/plain is a safelisted type, so no engine refuses it on those
      // grounds; the backend parses the body with request.json(), which
      // ignores the Content-Type header, so the payload reads the same.
      try {
        const blob = new Blob([json], { type: 'text/plain;charset=UTF-8' })
        if (nav.sendBeacon(url, blob)) return
      } catch { /* fall back to fetch */ }
    }
    void fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: json,
      credentials: 'same-origin',
      keepalive: true,
    }).catch(() => {})
  } catch {
    // Same swallow rule as recordPageView.
  }
}

export function recordSlideDwell(payload: SlideDwellPayload): void {
  postBeacon('/api/analytics/slide-dwell', payload)
}

export function recordTourEvent(payload: TourEventPayload): void {
  postBeacon('/api/analytics/tour-event', payload)
}

// The "start" event creates the tour_sessions row that every later
// progress/complete event updates; the server silently drops those when the
// row does not exist yet. Two independent requests are not delivered in
// order, so the caller must be able to wait for "start" to settle before
// sending anything else — sendBeacon gives no such signal. Start is sent at
// mount (never during unload), so a keepalive fetch loses nothing here.
// Resolves (never rejects) once the request has settled either way.
export function recordTourStart(payload: TourEventStart): Promise<void> {
  try {
    return fetch('/api/analytics/tour-event', {
      method: 'POST',
      // Same staff-bearer contract as postBeacon.
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
      credentials: 'same-origin',
      keepalive: true,
    }).then(() => {}, () => {})
  } catch {
    return Promise.resolve()
  }
}

// ===== Admin endpoints =====

function analyticsGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return adminGet<T>(path, 'analytics failed', { signal, cache: 'no-store' })
}

export type AnalyticsTotals = {
  top: number
  viewer: number
  tour: number
}

export type AnalyticsDailyRow = {
  day: string
  top: number
  viewer: number
  tour: number
}

export type AnalyticsSummary = {
  days: number
  start_date?: string
  end_date?: string
  totals: AnalyticsTotals
  unique_ips: number
  unique_users: number
  daily: AnalyticsDailyRow[]
}

// Period selector — either a preset window of N days back from "now",
// or an explicit calendar-day [start, end] range. The backend accepts
// either form on every aggregate endpoint.
export type AnalyticsRange =
  | { mode: 'preset'; days: number }
  | { mode: 'custom'; start: string; end: string }

function rangeToQuery(r: AnalyticsRange): Record<string, unknown> {
  if (r.mode === 'preset') return { days: r.days }
  return { start_date: r.start, end_date: r.end }
}

export type CsvDataset =
  | 'summary'
  | 'daily'
  | 'top-slides'
  | 'top-tours'
  | 'top-users'
  | 'referrers'
  | 'ua'
  | 'recent'

export type TopSlideRow = {
  slide_id: number
  // public_id lets the dashboard link to the token-keyed public viewer.
  public_id: string
  title: string
  catalog_id: string
  views: number
  unique_ips: number
}

export type TopTourRow = {
  tour_id: number
  // The tour's and parent slide's public_id tokens, for building the
  // token-keyed viewer/tour URL. slide_id is kept for internal correlation.
  public_id: string
  slide_public_id: string
  tour_title: string
  slide_id: number
  slide_title: string
  views: number
  unique_ips: number
}

export type ReferrerRow = {
  referrer_host: string
  views: number
}

export type UaRow = {
  ua_class: string
  views: number
}

export type TopUserRow = {
  user_email: string
  user_id: number | null
  views: number
  unique_days: number
  unique_ips: number
}

export type ActorRoleFilter = 'admin' | 'manager' | 'instructor' | 'none' | ''

export type RecentPageView = {
  id: number
  occurred_at: string
  kind: PageViewKind
  slide_id: number | null
  slide_public_id: string
  slide_title: string
  tour_id: number | null
  tour_public_id: string
  tour_title: string
  user_id: number | null
  user_email: string
  client_ip: string
  ua_class: string
  user_agent: string
  referrer_host: string
  actor_role: 'admin' | 'manager' | 'instructor' | null
}

export type RecentParams = {
  limit?: number
  user_id?: number | null
  user_email?: string
  client_ip?: string
  kind?: PageViewKind
  days?: number
  start_date?: string
  end_date?: string
  include_bots?: boolean
  include_staff?: boolean
  actor_role?: ActorRoleFilter
}

export type AnalyticsDashboard = {
  summary: AnalyticsSummary
  top_slides: TopSlideRow[]
  top_tours: TopTourRow[]
  referrers: ReferrerRow[]
  ua_breakdown: UaRow[]
  top_users: TopUserRow[]
  server_tz: ServerTz
}

// Bundled fetch used by the admin analytics tab. The dashboard UI calls
// this in place of six per-section endpoints so a refresh consumes one
// nginx rate-limit token, not six.
export function adminGetAnalyticsDashboard(
  range: AnalyticsRange,
  opts: { limit?: number; include_bots?: boolean; include_staff?: boolean; signal?: AbortSignal } = {},
) {
  const qs = buildQuery({
    ...rangeToQuery(range),
    limit: opts.limit,
    include_bots: opts.include_bots,
    include_staff: opts.include_staff,
  })
  return analyticsGet<AnalyticsDashboard>(`/api/admin/analytics/dashboard${qs}`, opts.signal)
}

export function adminGetRecentPageViews(params: RecentParams = {}, signal?: AbortSignal) {
  return analyticsGet<RecentPageView[]>(`/api/admin/analytics/recent${buildQuery(params)}`, signal)
}

export type AnalyticsClearResult = {
  ok: boolean
  // False when the server stopped at its per-request time budget with rows
  // possibly left; the same request is then repeated (see deleteUntilComplete).
  complete: boolean
  deleted: number
  deleted_page_views: number
  deleted_slide_dwell_events: number
  deleted_tour_sessions: number
}

// A ranged clear or a user purge deletes row by row, so the server works on
// it for a bounded time per request and answers `complete: false` while rows
// may remain. Repeat the identical request until it is done, adding up the
// per-table counts. A batch that deleted nothing also ends the loop, so a
// server that keeps answering incomplete cannot spin it forever.
//
// `onBatch` runs after each batch the server acknowledged, so a caller whose
// loop then fails part-way can tell "earlier batches were committed" (refresh
// what is on screen) from "the very first request was refused" (nothing
// changed); the thrown error alone cannot say which.
async function deleteUntilComplete(
  url: string,
  failMessage: string,
  signal?: AbortSignal,
  body?: () => FormData,
  onBatch?: () => void,
): Promise<AnalyticsClearResult> {
  const total: AnalyticsClearResult = {
    ok: true,
    complete: false,
    deleted: 0,
    deleted_page_views: 0,
    deleted_slide_dwell_events: 0,
    deleted_tour_sessions: 0,
  }
  for (;;) {
    const r = await adminFetch(url, failMessage, { method: 'DELETE', signal, cache: 'no-store', body: body?.() })
    const batch = (await r.json()) as AnalyticsClearResult
    onBatch?.()
    total.deleted += batch.deleted ?? 0
    total.deleted_page_views += batch.deleted_page_views ?? 0
    total.deleted_slide_dwell_events += batch.deleted_slide_dwell_events ?? 0
    total.deleted_tour_sessions += batch.deleted_tour_sessions ?? 0
    const batchRows = (batch.deleted_page_views ?? 0)
      + (batch.deleted_slide_dwell_events ?? 0)
      + (batch.deleted_tour_sessions ?? 0)
    if (batch.complete !== false || batchRows === 0) {
      total.complete = batch.complete !== false
      return total
    }
  }
}

// What a clear deletes: everything (`all`), or the inclusive date range.
// The server refuses a request that names neither (400
// analyticsClearScopeRequired), so a dropped date can never turn a ranged
// clear into a full wipe; `all` wins over any dates passed alongside it.
// buildQuery drops empty strings, so a blank date input is never sent.
export type AnalyticsClearScope = { all?: boolean; fromDate?: string; toDate?: string }

export function adminClearAnalytics(
  opts: AnalyticsClearScope & { signal?: AbortSignal; onBatch?: () => void } = {},
): Promise<AnalyticsClearResult> {
  const qs = opts.all
    ? buildQuery({ all: 1 })
    : buildQuery({ from_date: opts.fromDate, to_date: opts.toDate })
  return deleteUntilComplete(
    `/api/admin/analytics/data${qs}`, 'analytics clear failed', opts.signal, undefined, opts.onBatch,
  )
}

// What a user purge would delete, read before the destructive confirm: the
// stored subjects the identifier matches (the bare name and its ldap: /
// google: / basic: or hashed forms, as stored; at most
// ANALYTICS_PREVIEW_SUBJECT_CAP of them) and the row count per table.
export type AnalyticsUserPreview = {
  subjects: string[]
  page_views: number
  slide_dwell_events: number
  tour_sessions: number
}

// The server's cap on `subjects`: a list this long may have been cut short.
export const ANALYTICS_PREVIEW_SUBJECT_CAP = 50

// The identifier of a preview or purge travels in a form body, never the
// URL: nginx's and uvicorn's access logs keep the full request line, so a
// raw email in the query string would outlive the erasure it asked for.
function identifierForm(identifier: string): FormData {
  const form = new FormData()
  form.append('identifier', identifier)
  return form
}

export async function adminPreviewAnalyticsUser(identifier: string, signal?: AbortSignal) {
  const r = await adminFetch('/api/admin/analytics/user/preview', 'analytics failed', {
    method: 'POST', body: identifierForm(identifier), signal, cache: 'no-store',
  })
  return (await r.json()) as AnalyticsUserPreview
}

export type AnalyticsPseudonymizeResult = {
  updated: { page_views: number; slide_dwell_events: number; tour_sessions: number }
  complete: boolean
}

// Rewrites rows recorded in plain mode into the hashed form under the current
// salt (4xx analyticsSaltUnavailable without one). Like a ranged clear the
// server works for a bounded time per request, so the identical request is
// repeated until it answers complete, adding up the per-table counts; a batch
// that converted nothing also ends the loop, and `onBatch` reports each
// acknowledged batch (see deleteUntilComplete).
export async function adminPseudonymizeAnalytics(
  opts: { signal?: AbortSignal; onBatch?: () => void } = {},
): Promise<AnalyticsPseudonymizeResult> {
  const total: AnalyticsPseudonymizeResult = {
    updated: { page_views: 0, slide_dwell_events: 0, tour_sessions: 0 },
    complete: false,
  }
  for (;;) {
    const r = await adminFetch('/api/admin/analytics/pseudonymize', 'analytics pseudonymize failed', {
      method: 'POST', signal: opts.signal, cache: 'no-store',
    })
    const batch = (await r.json()) as Partial<AnalyticsPseudonymizeResult>
    opts.onBatch?.()
    const u = batch.updated ?? { page_views: 0, slide_dwell_events: 0, tour_sessions: 0 }
    total.updated.page_views += u.page_views ?? 0
    total.updated.slide_dwell_events += u.slide_dwell_events ?? 0
    total.updated.tour_sessions += u.tour_sessions ?? 0
    const batchRows = (u.page_views ?? 0) + (u.slide_dwell_events ?? 0) + (u.tour_sessions ?? 0)
    if (batch.complete !== false || batchRows === 0) {
      total.complete = batch.complete !== false
      return total
    }
  }
}

export function adminClearAnalyticsUser(
  identifier: string,
  opts: { signal?: AbortSignal; onBatch?: () => void } = {},
): Promise<AnalyticsClearResult> {
  return deleteUntilComplete(
    '/api/admin/analytics/user', 'analytics user-purge failed', opts.signal,
    () => identifierForm(identifier), opts.onBatch,
  )
}

export type TourCompletionRow = {
  tour_id: number
  public_id: string
  slide_public_id: string
  tour_title: string
  slide_id: number | null
  slide_title: string
  sessions_total: number
  sessions_completed: number
  completion_rate: number
  avg_completion_ms: number
  avg_dwell_ms: number
}

export type SlideDwellRow = {
  slide_id: number
  public_id: string
  title: string
  catalog_id: string
  sessions: number
  avg_dwell_ms: number
  total_dwell_ms: number
  unique_users: number
}

export function adminGetTourCompletion(
  range: AnalyticsRange,
  opts: { limit?: number; include_staff?: boolean; signal?: AbortSignal } = {},
) {
  const qs = buildQuery({
    ...rangeToQuery(range),
    limit: opts.limit ?? 20,
    include_staff: opts.include_staff,
  })
  return analyticsGet<TourCompletionRow[]>(`/api/admin/analytics/tour-completion${qs}`, opts.signal)
}

export function adminGetSlideDwell(
  range: AnalyticsRange,
  opts: { limit?: number; include_staff?: boolean; signal?: AbortSignal } = {},
) {
  const qs = buildQuery({
    ...rangeToQuery(range),
    limit: opts.limit ?? 20,
    include_staff: opts.include_staff,
  })
  return analyticsGet<SlideDwellRow[]>(`/api/admin/analytics/slide-dwell${qs}`, opts.signal)
}

// Trigger a CSV download for the given dataset (see downloadCsv).
export function adminDownloadAnalyticsCsv(
  dataset: CsvDataset,
  range: AnalyticsRange,
  opts: {
    limit?: number
    user_email?: string
    client_ip?: string
    kind?: PageViewKind
    include_bots?: boolean
    include_staff?: boolean
    actor_role?: ActorRoleFilter
  } = {},
): Promise<void> {
  // The per-row filters exist only on the raw `recent` rows; the server
  // answers 400 when one is passed for a pre-aggregated dataset rather than
  // silently ignoring it, so they are dropped here for every other dataset.
  const rowFilters = dataset === 'recent'
    ? {
        user_email: opts.user_email,
        client_ip: opts.client_ip,
        kind: opts.kind,
        actor_role: opts.actor_role || undefined,
      }
    : {}
  const qs = buildQuery({
    dataset,
    ...rangeToQuery(range),
    limit: opts.limit,
    ...rowFilters,
    include_bots: opts.include_bots,
    include_staff: opts.include_staff,
  })
  return downloadCsv(`/api/admin/analytics/export.csv${qs}`, {
    headers: authHeaders(),
    fallbackName: `analytics-${dataset}.csv`,
    failMessage: 'csv export failed',
  })
}
