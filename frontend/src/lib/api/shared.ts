import { STORAGE_KEYS } from '../storageKeys'
export type Slide = {
  id: number
  // Unguessable public identifier. This — not the numeric ``id`` — is what
  // every public URL and tile source is keyed by (so link-only slides can't be
  // enumerated). The numeric ``id`` stays for internal keying / admin calls.
  public_id: string
  catalog_id: string
  title: string
  thumb_url: string
  width: number
  height: number
  mpp_x: number
  mpp_y: number
  // Original MPP as recorded from the WSI file at upload. Equals mpp_x/mpp_y
  // unless the admin manually overrode them. 0 when the file did not provide MPP.
  mpp_x_file?: number
  mpp_y_file?: number
  // Objective magnification recorded in the WSI metadata
  // (``openslide.objective-power``). 0 when the file did not provide one;
  // the dashboard then falls back to the MPP-derived estimate.
  objective_power_file?: number
  vendor: string
  wsi_filename: string
  file_size_bytes?: number
  attributes: Record<string, string[]>
  show_zoom_indicator: boolean | null
  show_scale_bar: boolean | null
  show_metadata_panel: boolean | null
  // Per-slide image-adjustment preset (brightness/contrast/saturation). null
  // = no preset for that channel (viewer falls back to identity 1.0). Present
  // on both public and admin responses so the preset applies from first paint.
  image_adjust_brightness?: number | null
  image_adjust_contrast?: number | null
  image_adjust_saturation?: number | null
  // Real publish state on admin responses and on the public detail endpoint
  // (getSlide), where it is false for a draft fetched with adminToken. The
  // public list carries only published slides and does not fill it in (it
  // reads false there), so don't consult it on list items.
  is_published?: boolean
  // Link-only ("unlisted") visibility. Only present on admin responses; when
  // true, a published slide is reachable by direct URL but hidden from the
  // public index listing, search, and facets. The public routes already omit
  // unlisted slides, so anything fetched from a public route has this unset.
  unlisted?: boolean
  // Vendor-associated images extracted from the WSI file. ``macro_url`` (the
  // slide overview, suitable for orientation) is exposed publicly when the
  // slide is published; ``label_url`` (which often carries patient
  // identifiers / barcodes) is only populated on admin responses.
  macro_url?: string
  label_url?: string
  // Scan/acquisition timestamp from the WSI file (ISO 8601, e.g.
  // "2024-09-12T14:32:01" or "2024-09-12"). Empty when the file did not
  // record one or the format is unrecognised.
  acquired_at?: string
  // Image credit (see lib/slideCredit): the rights holder's notice, a license
  // id from SLIDE_LICENSES, and an http(s) source link. "" = not set. Public,
  // so the viewer shows it wherever the image is shown.
  credit_text?: string
  license_id?: string
  source_url?: string
  // Server-side timestamp of when the slide was uploaded via the admin
  // dashboard (ISO 8601 with timezone offset, e.g.
  // "2024-09-12T14:32:01+00:00").
  created_at?: string
  // WSI content generation, bumped by every WSI replace: the `?v=` of the
  // slide's /dzi/ URLs (see lib/tileUrl). Reported by the slide detail
  // endpoints and the admin list; null / absent elsewhere (e.g. the public
  // index list), where a viewer would fall back to the unversioned URL.
  tile_version?: number | null
}

// SZI (zipped DZI) slides skip the server-side DeepZoom generator and are
// rendered by the szi-tile-source library reading the archive directly via
// HTTP Range. The display branch keys off the file extension so the rest of
// the schema stays unchanged.
export function isSziSlide(s: { wsi_filename?: string } | null | undefined): boolean {
  return !!s?.wsi_filename && /\.szi$/i.test(s.wsi_filename)
}

// FastAPI returns errors in two shapes:
//   { detail: "human readable string" }                 (raise HTTPException(...))
//   { detail: [{ loc, msg, type, ... }, ...] }          (RequestValidationError)
// parseApiError flattens both into a single human-readable string so call
// sites can do `throw new Error(await parseApiError(r))` without each one
// reinventing the JSON dance and silently producing "[object Object]".
export async function parseApiError(r: Response, fallback = 'request failed'): Promise<string> {
  try {
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('application/json')) {
      // An HTML body is an nginx error page (502/504 while the app restarts,
      // …), never a message meant for display; report the status instead.
      if (ct.includes('text/html')) return `${fallback} (HTTP ${r.status})`
      const text = await r.text()
      return text.trim() || fallback
    }
    const body: any = await r.json()
    const d = body?.detail
    if (d == null) return fallback
    if (typeof d === 'string') return d
    if (Array.isArray(d)) {
      return d
        .map((e: any) => e?.msg ?? (typeof e === 'string' ? e : JSON.stringify(e)))
        .join(', ')
    }
    if (typeof d === 'object') return JSON.stringify(d)
    return String(d)
  } catch {
    return fallback
  }
}

// Type guard for one of the error classes below. `instanceof` alone misses an
// instance from a second copy of this module (a test's mocked module graph);
// the `name` each class sets covers those. The name is passed in rather than
// read from `cls.name`, which minification renames.
export function errorGuard<T extends Error>(
  cls: abstract new (...args: any[]) => T,
  name: string,
): (err: unknown) => err is T {
  return (err: unknown): err is T => err instanceof cls
    || (!!err && typeof err === 'object' && (err as { name?: unknown }).name === name)
}

// Distinguishable error type for HTTP 429 responses (nginx rate limit).
// Components can detect it with `instanceof` and substitute a friendlier,
// localized message instead of dumping the raw nginx HTML body. The
// API layer can't import the React i18n context, so the translation
// step has to live at the call site.
export class RateLimitError extends Error {
  constructor() {
    super('rate-limited')
    this.name = 'RateLimitError'
  }
}

export const isRateLimitError = errorGuard(RateLimitError, 'RateLimitError')

// Distinguishable error type for HTTP 413 responses (nginx / app size limit).
// Same pattern as RateLimitError: the raw nginx body is HTML, so the caller
// must catch this and substitute a localized message.
export class FileTooLargeError extends Error {
  constructor() {
    super('file-too-large')
    this.name = 'FileTooLargeError'
  }
}

export const isFileTooLargeError = errorGuard(FileTooLargeError, 'FileTooLargeError')

// Distinguishable error type for transport-level failures (the request never
// received an HTTP status: connection reset, DNS failure, TLS error, browser
// offline). Kept separate from HTTP-status errors so the bulk upload queue
// can retry these while treating a real 4xx/5xx response as terminal.
export class NetworkError extends Error {
  constructor() {
    super('network-error')
    this.name = 'NetworkError'
  }
}

export const isNetworkError = errorGuard(NetworkError, 'NetworkError')

// A proxy answered for the app (isGatewayStatus, no app JSON detail): nginx's
// 502 / 504 or Cloudflare's 52x, typically a read timeout while the app is
// still working on a large upload, or the app being down. Whether the app
// acted on the request is unknown once its body was sent, so upload callers
// treat it like a NetworkError: retry before onSent, verify after it.
export class GatewayError extends Error {
  status: number
  constructor(status: number) {
    super(`gateway-error-${status}`)
    this.name = 'GatewayError'
    this.status = status
  }
}

export const isGatewayError = errorGuard(GatewayError, 'GatewayError')

// Raised when the server rejected the caller's credentials (401) or refused
// the caller's role (403). Distinguished from every other HTTP failure so a
// caller can bounce to the login screen for a genuine auth failure WITHOUT
// doing so for a transient 429 / 500 / gateway hiccup, which would evict an
// admin holding a perfectly valid token.
export class UnauthorizedError extends Error {
  status: number
  constructor(status: number) {
    super(`unauthorized-${status}`)
    this.name = 'UnauthorizedError'
    this.status = status
  }
}

export const isUnauthorizedError = errorGuard(UnauthorizedError, 'UnauthorizedError')

// Error carrying a language-independent code + params instead of a
// pre-rendered message. The backend returns
// `detail: { code, params }`; the API layer can't reach the React i18n
// context, so — exactly like RateLimitError — the catch site translates
// `code` through `t()` (see lib/admin/apiError.ts → apiErrorMessage).
export class ApiCodeError extends Error {
  code: string
  params?: Record<string, string | number>
  // The server's current copy of the resource, when the error is a revision
  // conflict (409 from the tour/step editor endpoints; see
  // lib/api/tours → stepConflictOf / tourConflictOf). Sent beside `params`,
  // which holds only i18n interpolation scalars.
  current?: unknown
  constructor(code: string, params?: Record<string, string | number>, current?: unknown) {
    super(code)
    this.name = 'ApiCodeError'
    this.code = code
    this.params = params
    this.current = current
  }
}

export const isApiCodeError = errorGuard(ApiCodeError, 'ApiCodeError')

// Recognise the structured `{ code, params }` detail shape and build an
// ApiCodeError from it. Returns null for the plain string / validation-array
// detail shapes so callers fall back to plain string handling.
export function apiCodeErrorFromDetail(detail: any): ApiCodeError | null {
  if (
    detail && typeof detail === 'object' && !Array.isArray(detail)
    && typeof detail.code === 'string'
  ) {
    return new ApiCodeError(detail.code, detail.params, detail.current)
  }
  return null
}

export async function throwApi(r: Response, fallback: string): Promise<never> {
  if (r.status === 429) throw new RateLimitError()
  if (r.status === 413) throw new FileTooLargeError()
  // Peek at a JSON body for the structured {code, params} detail before
  // falling back to string flattening. Clone so parseApiError can still
  // read the (single-use) response body on the fallback path.
  if ((r.headers.get('content-type') || '').includes('application/json')) {
    let coded: ApiCodeError | null = null
    try {
      const body = await r.clone().json()
      coded = apiCodeErrorFromDetail(body?.detail)
    } catch { /* not JSON after all / parse error — fall through */ }
    if (coded) throw coded
  }
  const message = await parseApiError(r, fallback)
  if (r.status === 401 && isStaffTokenRejection(message)) throw staffTokenRejected()
  // Still a plain Error (callers display its message and branch on the typed
  // errors above), but carrying the status it was answered with: a role
  // rejection (require_admin's 403 "Forbidden") is otherwise indistinguishable
  // from a 500 that may follow committed work (see httpStatusOf).
  // Non-enumerable, so the error still deep-equals a bare `new Error(message)`.
  const err = new Error(message)
  Object.defineProperty(err, 'status', { value: r.status, configurable: true, writable: true })
  throw err
}

// The HTTP status a failed request was answered with, when its error records
// one: UnauthorizedError's, or the one throwApi attaches to the plain Error it
// throws for an uncoded failure. undefined for a transport failure, an abort,
// the typed errors that stand for their status (RateLimitError, …) and errors
// from anywhere else.
export function httpStatusOf(err: unknown): number | undefined {
  const status = err && typeof err === 'object' ? (err as { status?: unknown }).status : undefined
  return typeof status === 'number' ? status : undefined
}

// `detail` strings auth.get_current_user answers with when the Authorization
// header's staff token is absent or no longer valid. Other 401s (wrong login /
// restore password, gated public pages) keep their own messages.
const STAFF_TOKEN_REJECTED = new Set(['Invalid token', 'Missing token'])
let redirectingToLogin = false

export function isStaffTokenRejection(detail: string): boolean {
  return STAFF_TOKEN_REJECTED.has(detail)
}

/**
 * The staff JWT expired, was revoked (password change, user removed) or never
 * existed. From an admin sub-page, drop the token and return to the login
 * screen instead of leaving a dead page that answers every click with an error;
 * returns the error for the caller to throw/reject with.
 */
export function staffTokenRejected(): UnauthorizedError {
  if (typeof window !== 'undefined' && /^\/admin\/.+/.test(window.location.pathname)) {
    try {
      sessionStorage.removeItem(STORAGE_KEYS.adminToken)
      localStorage.removeItem(STORAGE_KEYS.adminToken)
    } catch { /* storage unavailable */ }
    // Once per page load - a screen firing several requests at once gets
    // several 401s - and replace() so Back does not return to the dead page
    // only to be sent here again.
    if (!redirectingToLogin) {
      redirectingToLogin = true
      window.location.replace('/admin')
      // A beforeunload prompt (an upload in flight, unsaved tour edits) can
      // keep the page open when the user declines to leave; allow a later 401
      // to try again instead of staying dead for the page's lifetime.
      setTimeout(() => { redirectingToLogin = false }, 3000)
    }
  }
  return new UnauthorizedError(401)
}

/** Test hook: forget that a login redirect already started. */
export function _resetLoginRedirectForTests(): void {
  redirectingToLogin = false
}

// The staff (admin dashboard) JWT this tab holds, or null. auth.ts explains
// why it lives in sessionStorage and how a new tab receives it; it is read
// here so the request helpers below need no import from auth.ts, which
// itself imports this module.
export function getToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEYS.adminToken)
  } catch {
    return null
  }
}

// `extra` plus `Authorization: Bearer <token>`. Without a token there is no
// Authorization header at all: the request is refused either way, but a
// literal "Bearer null" reads as a malformed credential in the access log
// rather than an unauthenticated request.
export function bearerHeaders(
  token: string | null | undefined,
  extra: Record<string, string> = {},
): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra }
}

// bearerHeaders with the staff token this tab holds.
export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return bearerHeaders(getToken(), extra)
}

export type AdminRequestInit = {
  method?: string
  // A FormData body goes as-is (multipart); anything else as JSON.
  body?: FormData | object
  signal?: AbortSignal
  cache?: RequestCache
  keepalive?: boolean
}

// fetch() with the staff token attached. A non-2xx answer throws the typed
// error throwApi derives from it, `fallback` being the message of last resort.
export async function adminFetch(url: string, fallback: string, init: AdminRequestInit = {}): Promise<Response> {
  const { body, ...rest } = init
  const json = body !== undefined && !(body instanceof FormData)
  const r = await fetch(url, {
    ...rest,
    headers: authHeaders(json ? { 'Content-Type': 'application/json' } : {}),
    body: json ? JSON.stringify(body) : (body as FormData | undefined),
  })
  if (!r.ok) await throwApi(r, fallback)
  return r
}

// An authenticated GET answering JSON.
export async function adminGet<T = any>(
  url: string,
  fallback: string,
  opts: { signal?: AbortSignal; cache?: RequestCache } = {},
): Promise<T> {
  const r = await adminFetch(url, fallback, opts)
  return r.json() as Promise<T>
}

// An authenticated write (POST / PATCH / PUT / DELETE) answering JSON.
export async function adminSend<T = any>(
  method: 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  url: string,
  body: FormData | object | undefined,
  fallback: string,
): Promise<T> {
  const r = await adminFetch(url, fallback, { method, body })
  return r.json() as Promise<T>
}

// A FastAPI `detail` (string, or a validation-error list) as one display
// string; null when there is none to show.
function detailText(detail: unknown): string | null {
  if (!detail) return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((e: any) => e?.msg ?? JSON.stringify(e)).join(', ')
  return JSON.stringify(detail)
}

// Statuses a proxy in front of the app answers with when the app did not
// answer (in time): nginx's 502 / 503 / 504, and Cloudflare's 520-524 (520
// unknown error, 521 web server down, 522 connection timed out, 523 origin
// unreachable, 524 a timeout occurred - its 100 s response timeout). The
// app's own 503 carries a JSON detail, which callers check first.
export function isGatewayStatus(status: number): boolean {
  return status === 502 || status === 503 || status === 504 || (status >= 520 && status <= 524)
}

// The typed error throwApi would raise, for a non-2xx XHR answer (throwApi
// reads a fetch Response): 413 → FileTooLargeError, 429 → RateLimitError, 401
// → UnauthorizedError (dropping a rejected staff token like throwApi does), a
// `{ code, params }` detail → ApiCodeError, otherwise the detail's text or
// `fallback`. `gatewayError` stands for a gateway status (isGatewayStatus)
// without a detail: the proxy's own page, the app never having answered.
export function xhrToTypedError(
  xhr: XMLHttpRequest,
  fallback: string,
  gatewayError?: (status: number) => Error,
): Error {
  let detail: unknown = null
  try { detail = JSON.parse(xhr.responseText || '{}')?.detail } catch { /* not JSON */ }
  if (xhr.status === 401) {
    return typeof detail === 'string' && isStaffTokenRejection(detail)
      ? staffTokenRejected()
      : new UnauthorizedError(401)
  }
  if (xhr.status === 413) return new FileTooLargeError()
  if (xhr.status === 429) return new RateLimitError()
  const coded = apiCodeErrorFromDetail(detail)
  if (coded) return coded
  const text = detailText(detail)
  if (text) return new Error(text)
  if (gatewayError && isGatewayStatus(xhr.status)) return gatewayError(xhr.status)
  return new Error(fallback)
}

export type XhrUploadOptions = {
  // Upload progress in whole percent, rounded down: 100 only once the whole
  // body has been sent (just before onSent), or the server answered a 2xx.
  // Still, a caller that must know the body has gone (an error after that
  // point may mean the server acted on it) listens to onSent, not for 100.
  onProgress?: (percent: number) => void
  // Called once the whole body has been sent, before the server answers.
  onSent?: () => void
  // Aborting rejects with an AbortError, like fetch.
  signal?: AbortSignal
  // Ignore an abort once the whole body is sent: the server acts on the upload
  // from then on, and the caller needs its answer (a job id) rather than to
  // strand what the server started.
  keepAfterSent?: boolean
  // See xhrToTypedError.
  gatewayError?: (status: number) => Error
}

// POST a multipart form with the staff token, through XMLHttpRequest rather
// than fetch for its upload progress events. Resolves with the JSON answer of
// a 2xx; rejects with xhrToTypedError's error for any other status, with
// NetworkError when no HTTP answer came back (connection reset, offline),
// which callers can retry where a 4xx/5xx is final, and with an AbortError
// when `opts.signal` aborts.
export function xhrUpload<T>(
  url: string,
  form: FormData,
  fallback: string,
  opts: XhrUploadOptions = {},
): Promise<T> {
  const { onProgress, onSent, signal, keepAfterSent, gatewayError } = opts
  const abortError = () => new DOMException('Aborted', 'AbortError')
  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const xhr = new XMLHttpRequest()
    let bodySent = false
    const onAbort = () => {
      if (keepAfterSent && bodySent) return
      xhr.abort()
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort)
    const cleanup = () => signal?.removeEventListener('abort', onAbort)

    xhr.open('POST', url)
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v)
    xhr.upload.onprogress = (event) => {
      if (!onProgress || !event.lengthComputable || bodySent) return
      // Floored and held at 99 until upload.onload below. Rounding would
      // report 100 at 99.5 % sent, which callers would take for "fully sent" with
      // ~25 MB of a 5 GB file still to go: the upload queue would then file a dropped
      // connection as "verify on the server" instead of retrying it.
      onProgress(Math.min(99, Math.floor((event.loaded / event.total) * 100)))
    }
    xhr.upload.onload = () => {
      // Some engines end the upload phase with this load event when the
      // server answers before the body is all out (an early 413 / 401 / 429
      // from nginx or the app), so the event alone does not mean "sent":
      // reporting it then would flip callers to "processing" / "staging" and
      // drop a restore's Cancel button just before the error arrived. An
      // answer that has started (HEADERS_RECEIVED = 2; a literal so a test
      // double needs no constants) is left to onload. bodySent stays false
      // too, so a keepAfterSent abort still aborts.
      if (xhr.readyState >= 2) return
      bodySent = true
      onProgress?.(100)
      onSent?.()
    }
    xhr.onload = () => {
      cleanup()
      if (xhr.status >= 200 && xhr.status < 300) {
        // Normally already said by upload.onload.
        if (!bodySent) onProgress?.(100)
        try {
          resolve(JSON.parse(xhr.responseText || '{}'))
        } catch {
          reject(new Error('Server returned non-JSON response'))
        }
        return
      }
      reject(xhrToTypedError(xhr, fallback, gatewayError))
    }
    xhr.onerror = () => { cleanup(); reject(new NetworkError()) }
    xhr.onabort = () => { cleanup(); reject(abortError()) }
    xhr.send(form)
  })
}

// Append a clearable free-text setting to a settings PATCH body. FastAPI
// parses an empty form value as "field not sent", so a blanked field would be
// silently ignored ("Saved", but the old value stays). Every clearable field
// has a companion clear_<key>=1 flag on the server; send that instead.
export function appendTextOrClear(form: FormData, key: string, value: string) {
  if (value) form.append(key, value)
  else form.append(`clear_${key}`, '1')
}

// Useful for callers that want to ignore AbortController-triggered fetch
// rejections in their useEffect cleanup paths.
export function isAbortError(err: unknown): boolean {
  return !!err && typeof err === 'object' && (err as any).name === 'AbortError'
}

export type FacetItem = {
  value: string
  // Listed slides carrying the value themselves (the "Slides" view's count).
  count: number
  // The "All" view's (slides that carry it, or have a listed tour that does)
  // and the "Tours" view's (tours whose metadata — their slide's plus their
  // own — carries it).
  all_count: number
  tour_count: number
}
export type Facets = {
  groups: Record<string, FacetItem[]>
}

// A listed tour that is a reason its slide is in an /api/slides result.
export type MatchedTour = { id: number; public_id: string; title: string }

export type SlideListItem = Slide & {
  // With `q` or a filter (and match_tours left on), the slide's published,
  // listed tours that add to its match, in creation order: a q hit on the
  // tour's own title or metadata, or — when the slide's own metadata fails
  // the filters — the tour metadata that passes them. Why a slide that
  // matches only through a tour is in the result. Empty otherwise.
  matched_tours?: MatchedTour[]
}

export type SlideListResponse = {
  items: SlideListItem[]
  // Size of the whole match set (tour-title matches included), not of `items`
  // — which per_page caps — so total > items.length means truncation.
  total: number
  page: number
  per_page: number
}

export async function getFacets(opts: { signal?: AbortSignal } = {}) {
  const r = await fetch('/api/facets', { signal: opts.signal })
  if (!r.ok) await throwApi(r, 'facets failed')
  return r.json() as Promise<Facets>
}

// GET /api/slides. With `q`, slides also match through their tours' titles
// unless params carries match_tours=0 (the index page's "Slides" view).
//
// Always fetched from the network (`cache: 'no-store'`): the list is never
// worth serving from the browser cache.
export async function listSlides(params: URLSearchParams, opts: { signal?: AbortSignal } = {}) {
  const r = await fetch('/api/slides?' + params.toString(), { signal: opts.signal, cache: 'no-store' })
  if (!r.ok) await throwApi(r, 'slides failed')
  return r.json() as Promise<SlideListResponse>
}

// Public detail: keyed by the slide's public_id token (never the numeric id).
// Pass ``adminToken`` to preview a draft: the endpoint serves unpublished
// slides to a valid admin JWT (mirrors the /dzi/ draft-preview bypass), so the
// viewer needs no separate numeric admin endpoint.
export async function getSlide(
  publicId: string,
  opts: { signal?: AbortSignal; adminToken?: string | null } = {},
): Promise<Slide> {
  const r = await fetch(`/api/slides/${encodeURIComponent(publicId)}`, {
    signal: opts.signal,
    headers: bearerHeaders(opts.adminToken),
  })
  if (!r.ok) await throwApi(r, 'slide failed')
  return r.json()
}

// Query string for an admin report request: undefined / null / '' values are
// left out and booleans go as 1 / 0 (the report endpoints take integer flags).
export function buildQuery(params: Record<string, unknown>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    if (typeof v === 'boolean') sp.set(k, v ? '1' : '0')
    else sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export type DownloadOptions = {
  headers?: Record<string, string>
  fallbackName: string
  failMessage: string
  // Aborts the request and the body read (the download is then not saved).
  signal?: AbortSignal
  // Bytes of the body received so far, as they arrive.
  onProgress?: (receivedBytes: number) => void
}

// Fetch a CSV export and hand it to the browser as a download.
export function downloadCsv(url: string, opts: DownloadOptions): Promise<void> {
  return downloadAttachment(url, opts)
}

// Fetch a server-built file (CSV, JSON, …) and hand it to the browser as a
// download. fetch + blob rather than a plain <a download>, so the admin Bearer
// token can be attached. The file name comes from the server's
// Content-Disposition, falling back to `fallbackName` when a proxy stripped or
// mangled the header. The body is read chunk by chunk so a long export can
// report progress and be cancelled (`signal`) before it is all in.
export async function downloadAttachment(url: string, opts: DownloadOptions): Promise<void> {
  const r = await fetch(url, { headers: opts.headers, cache: 'no-store', signal: opts.signal })
  if (!r.ok) await throwApi(r, opts.failMessage)
  const cd = r.headers.get('content-disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i)
  let filename = opts.fallbackName
  if (m) {
    // A mangled header can leave a literal `%` that makes decodeURIComponent
    // throw URIError; keep the fallback name then.
    try { filename = decodeURIComponent(m[1].replace(/"$/, '')) } catch {}
  }
  const blob = await readBody(r, opts.onProgress)
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoke after a tick so the navigation has a chance to register the blob
  // URL on slower browsers.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

async function readBody(r: Response, onProgress?: (receivedBytes: number) => void): Promise<Blob> {
  const type = r.headers.get('content-type') || ''
  const reader = onProgress && r.body ? r.body.getReader() : null
  if (!reader) return r.blob()
  const chunks: BlobPart[] = []
  let received = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    received += value.byteLength
    onProgress!(received)
  }
  return new Blob(chunks, type ? { type } : undefined)
}
