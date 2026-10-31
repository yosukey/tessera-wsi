import { adminGet, adminSend, appendTextOrClear, authHeaders, throwApi } from './shared'

export type IndexTypeFilter = 'all' | 'slides' | 'tours'

// Mirrors NetworkSettings.analytics_mode on the backend.
//   - 'off'    no access analytics are recorded at all
//   - 'hashed' user identities are HMAC-SHA256 pseudonymised at write time
//   - 'plain'  user identities are stored verbatim (email/username)
export type AnalyticsMode = 'off' | 'hashed' | 'plain'

export type SeasonalBackground = {
  slot: number
  enabled: boolean
  label: string
  start_month: number
  start_day: number
  end_month: number
  end_day: number
  // Public responses: the public image route (enabled slots only). Admin
  // responses: the admin route, which also serves a disabled slot and needs
  // the bearer (load it with adminFetchImageBlobUrl / useAdminBlobUrl).
  image_url: string
  has_image: boolean
  title_color: string
}

export type ViewerSettings = {
  logo_url: string
  favicon_url: string
  copyright_msg: string
  extra_attrs: string[]
  index_card_attrs: string[]
  index_filter_attrs: string[]
  site_title: string
  footer_text: string
  show_zoom_indicator: boolean
  show_scale_bar: boolean
  show_metadata_panel: boolean
  default_index_type: IndexTypeFilter
  show_filter_button: boolean
  default_view_mode: 'tile' | 'list'
  default_sort_field: 'title' | 'catalog_id' | 'wsi_filename'
  // Order of the top-page filter chips within each category:
  //   'count' -> most-used values first (the default)
  //   'alpha' -> alphabetical by value
  filter_sort_order: 'count' | 'alpha'
  ga_measurement_id: string
  max_zoom_pixel_ratio: number
  show_admin_link: boolean
  default_thumbnail_size: 'sm' | 'md' | 'lg'
  max_index_items: number
  // How many of a slide's tours the top page lists inline under that slide in
  // the "All (slides & tours)" view before the rest collapse behind an
  // expander. Display cap only — every tour is fetched and reachable in one
  // click; 0 lists none by default. Clamped to
  // [INDEX_TOURS_PER_SLIDE_MIN, INDEX_TOURS_PER_SLIDE_MAX] on both sides.
  index_tours_per_slide: number
  // Advanced OpenSeadragon rendering tuning (see docs/developer-guide.md
  // "Viewer rendering tuning"). immediate_render: fetch display-resolution
  // tiles first and skip in-between levels vs. load coarse-to-fine.
  // max_image_cache_count: number of decoded tiles OSD keeps in memory (a
  // count, not bytes), clamped to [MAX_IMAGE_CACHE_COUNT_MIN, _MAX].
  immediate_render: boolean
  max_image_cache_count: number
  header_default_image_url: string
  header_default_title_color: string
  seasonal_backgrounds: SeasonalBackground[]
  // Access-analytics flags surfaced from NetworkSettings so the frontend
  // can both honour the operator's "off / pseudonymous / plain" choice
  // and decide whether to show the one-time recording-notice toast.
  analytics_mode: AnalyticsMode
  analytics_notice_toast_enabled: boolean
}

// Shape returned by the *admin* settings endpoint
// (GET /api/admin/viewer/settings). It deliberately omits the four fields
// that live on NetworkSettings rather than ViewerSettings
// (ga_measurement_id / seasonal_backgrounds / analytics_mode /
// analytics_notice_toast_enabled) — the dashboard reads those through their
// own sections (network / seasonal), so the admin viewer-settings handler
// never serialises them. Typing the admin getter as the full ViewerSettings
// would falsely promise these fields are present.
export type AdminViewerSettings = Omit<
  ViewerSettings,
  'ga_measurement_id' | 'seasonal_backgrounds' | 'analytics_mode' | 'analytics_notice_toast_enabled'
>

// Hard bounds enforced by the backend's normalize_max_index_items.
// Re-declared on the client so the admin form can validate before posting
// and so the IndexPage can clamp a malformed value the server sent in
// edge-case test setups.
export const MAX_INDEX_ITEMS_MIN = 100
export const MAX_INDEX_ITEMS_MAX = 5000
export const MAX_INDEX_ITEMS_DEFAULT = 2000

export function clampMaxIndexItems(n: number): number {
  if (!isFinite(n)) return MAX_INDEX_ITEMS_DEFAULT
  if (n < MAX_INDEX_ITEMS_MIN) return MAX_INDEX_ITEMS_MIN
  if (n > MAX_INDEX_ITEMS_MAX) return MAX_INDEX_ITEMS_MAX
  return Math.round(n)
}

// Hard bounds on the per-slide inline tour cap, mirrored from the backend's
// normalize_index_tours_per_slide so the admin form can validate before POST
// and the IndexPage can clamp a malformed value the server sent. 0 is a
// legitimate value ("list none inline; everything behind the expander"), so
// callers must not treat it as "unset" and substitute the default.
export const INDEX_TOURS_PER_SLIDE_MIN = 0
export const INDEX_TOURS_PER_SLIDE_MAX = 50
export const INDEX_TOURS_PER_SLIDE_DEFAULT = 3

export function clampIndexToursPerSlide(n: number): number {
  if (!isFinite(n)) return INDEX_TOURS_PER_SLIDE_DEFAULT
  if (n < INDEX_TOURS_PER_SLIDE_MIN) return INDEX_TOURS_PER_SLIDE_MIN
  if (n > INDEX_TOURS_PER_SLIDE_MAX) return INDEX_TOURS_PER_SLIDE_MAX
  return Math.round(n)
}

// Hard bounds on OpenSeadragon's in-memory decoded-tile cache, mirrored from
// the backend's normalize_max_image_cache_count so the admin form can both
// validate before POST and clamp a malformed value the server sent. The
// server re-clamps on read and write, so these are the only bounds the GUI
// can reach; raising the ceiling further requires editing the constant in
// services/app/wsi_app/_serializers.py and rebuilding.
export const MAX_IMAGE_CACHE_COUNT_MIN = 100
export const MAX_IMAGE_CACHE_COUNT_MAX = 1000
export const MAX_IMAGE_CACHE_COUNT_DEFAULT = 200

export function clampMaxImageCacheCount(n: number): number {
  if (!isFinite(n)) return MAX_IMAGE_CACHE_COUNT_DEFAULT
  if (n < MAX_IMAGE_CACHE_COUNT_MIN) return MAX_IMAGE_CACHE_COUNT_MIN
  if (n > MAX_IMAGE_CACHE_COUNT_MAX) return MAX_IMAGE_CACHE_COUNT_MAX
  return Math.round(n)
}

// Hard bounds on the digital-zoom ceiling, mirrored from the backend's
// normalize_max_zoom_pixel_ratio. The admin section already normalises the
// field on blur/save; this clamp is the API-layer guardrail so a malformed
// value can never leave the client (parity with clampMaxIndexItems /
// clampMaxImageCacheCount, which the same PATCH already runs through). The
// 0.1-step rounding matches the section's normaliseMaxZoom (round-half-up).
export const MAX_ZOOM_PIXEL_RATIO_MIN = 1.0
export const MAX_ZOOM_PIXEL_RATIO_MAX = 2.0
export const MAX_ZOOM_PIXEL_RATIO_DEFAULT = 1.0

export function clampMaxZoomPixelRatio(n: number): number {
  if (!isFinite(n)) return MAX_ZOOM_PIXEL_RATIO_DEFAULT
  const stepped = Math.round(n * 10) / 10
  if (stepped < MAX_ZOOM_PIXEL_RATIO_MIN) return MAX_ZOOM_PIXEL_RATIO_MIN
  if (stepped > MAX_ZOOM_PIXEL_RATIO_MAX) return MAX_ZOOM_PIXEL_RATIO_MAX
  return stepped
}

// Built-in defaults the viewers use until getViewerSettings() resolves (or
// for good if it fails). Every field is populated so an OSD-init effect's
// numeric dependencies (max_zoom_pixel_ratio, max_image_cache_count) never
// transition from undefined → number and force a viewer rebuild. Declared
// after the *_DEFAULT constants above because it reads them at module init.
export const DEFAULT_VIEWER_SETTINGS: ViewerSettings = {
  logo_url: '', favicon_url: '', copyright_msg: '', extra_attrs: [], index_card_attrs: [], index_filter_attrs: [], site_title: '', footer_text: '',
  show_zoom_indicator: false, show_scale_bar: true, show_metadata_panel: true,
  default_index_type: 'all', show_filter_button: false,
  default_view_mode: 'tile', default_sort_field: 'catalog_id',
  ga_measurement_id: '', max_zoom_pixel_ratio: MAX_ZOOM_PIXEL_RATIO_DEFAULT,
  immediate_render: true, max_image_cache_count: MAX_IMAGE_CACHE_COUNT_DEFAULT,
  show_admin_link: false, default_thumbnail_size: 'lg',
  max_index_items: MAX_INDEX_ITEMS_DEFAULT, index_tours_per_slide: INDEX_TOURS_PER_SLIDE_DEFAULT,
  header_default_image_url: '', header_default_title_color: '',
  seasonal_backgrounds: [],
  analytics_mode: 'off', analytics_notice_toast_enabled: true,
  filter_sort_order: 'count',
}

// Module-scoped coalescing + short-TTL cache for the PUBLIC viewer settings.
//
// A single public page load fires getViewerSettings() from four independent
// places — App's Google-Analytics loader and favicon hook, the
// analytics-notice toast, and the page component (IndexPage / ViewerPage /
// TourViewerPage). Without coalescing each would be a separate
// /api/viewer/settings round trip, spending four of nginx's per-minute
// `wsi_api` rate-limit tokens on identical responses. That would make a burst
// of reloads — or a single reload over a freshly-woken, still-stale
// connection — far more likely to trip the 429 limiter. Coalescing concurrent
// callers onto one in-flight request, and briefly caching the resolved value,
// collapses those four calls to one.
//
// Safety of the cache: these are public, slow-changing *display* settings, so
// a few seconds of staleness within one page session is harmless. The state
// is module-scoped, so it is wiped by any full page reload and can never mask
// a genuine settings change across loads. Admin reads go through
// adminGetViewerSettings(), which is deliberately left uncached.
const VIEWER_SETTINGS_TTL_MS = 30_000
let vsCache: { value: ViewerSettings; at: number } | null = null
let vsInflight: Promise<ViewerSettings> | null = null

function fetchViewerSettings(): Promise<ViewerSettings> {
  // The shared request runs without any caller's AbortSignal: a component
  // unmounting (and aborting its controller) must not cancel the fetch the
  // other concurrent callers are awaiting. Per-caller aborts are honoured in
  // getViewerSettings() against the returned promise instead.
  const p = (async () => {
    const r = await fetch('/api/viewer/settings')
    if (!r.ok) await throwApi(r, 'viewer settings failed')
    return r.json() as Promise<ViewerSettings>
  })()
  vsInflight = p
  p.then(
    (value) => { vsCache = { value, at: Date.now() } },
    () => { /* a failed refresh must not poison an existing cached value */ },
  ).finally(() => { if (vsInflight === p) vsInflight = null })
  return p
}

export function getViewerSettings(opts: { signal?: AbortSignal } = {}): Promise<ViewerSettings> {
  const fresh = vsCache && (Date.now() - vsCache.at) < VIEWER_SETTINGS_TTL_MS
  const base = fresh
    ? Promise.resolve(vsCache!.value)
    : (vsInflight ?? fetchViewerSettings())
  const { signal } = opts
  if (!signal) return base
  // Honour a per-caller AbortSignal without disturbing the shared request:
  // the signal only ever rejects *this* caller's promise (with an
  // AbortError, matching isAbortError()), never the underlying fetch.
  return new Promise<ViewerSettings>((resolve, reject) => {
    const abort = () => {
      const err = new Error('Aborted')
      err.name = 'AbortError'
      reject(err)
    }
    if (signal.aborted) { abort(); return }
    signal.addEventListener('abort', abort, { once: true })
    base.then(
      (v) => { signal.removeEventListener('abort', abort); resolve(v) },
      (e) => { signal.removeEventListener('abort', abort); reject(e) },
    )
  })
}

// The global viewer-overlay defaults a slide's tri-state overrides fall back
// to, for staff who can't read the admin-only /api/admin/viewer/settings (a
// manager editing a slide). Reads the PUBLIC settings endpoint — nothing a
// viewer can't already see — with the staff Bearer token so the app-access
// gate admits it in every auth mode. Bypasses the public cache above: the
// slide editor must not seed its "inherit" logic from a stale value.
export type ViewerOverlayDefaults = Pick<ViewerSettings, 'show_zoom_indicator' | 'show_scale_bar' | 'show_metadata_panel'>

export async function staffGetViewerOverlayDefaults(opts: { signal?: AbortSignal } = {}): Promise<ViewerOverlayDefaults> {
  const r = await fetch('/api/viewer/settings', {
    headers: authHeaders(),
    signal: opts.signal,
    cache: 'no-store',
  })
  if (!r.ok) await throwApi(r, 'viewer settings failed')
  const vs = (await r.json()) as ViewerSettings
  return {
    show_zoom_indicator: !!vs.show_zoom_indicator,
    show_scale_bar: !!vs.show_scale_bar,
    show_metadata_panel: !!vs.show_metadata_panel,
  }
}

export async function adminGetViewerSettings(opts: { signal?: AbortSignal } = {}): Promise<AdminViewerSettings> {
  return adminGet('/api/admin/viewer/settings', 'admin viewer settings failed', { signal: opts.signal })
}

export async function adminUpdateViewerSettings(payload: {
  copyright_msg: string
  site_title: string
  footer_text: string
  show_zoom_indicator: boolean
  show_scale_bar: boolean
  show_metadata_panel: boolean
  default_index_type: IndexTypeFilter
  show_filter_button: boolean
  default_view_mode: 'tile' | 'list'
  default_sort_field: 'title' | 'catalog_id' | 'wsi_filename'
  filter_sort_order: 'count' | 'alpha'
  max_zoom_pixel_ratio: number
  show_admin_link: boolean
  default_thumbnail_size: 'sm' | 'md' | 'lg'
  max_index_items: number
  index_tours_per_slide: number
  immediate_render: boolean
  max_image_cache_count: number
  header_default_title_color: string
}) {
  const form = new FormData()
  appendTextOrClear(form, 'copyright_msg', payload.copyright_msg)
  appendTextOrClear(form, 'site_title', payload.site_title)
  appendTextOrClear(form, 'footer_text', payload.footer_text)
  form.append('show_zoom_indicator', payload.show_zoom_indicator ? '1' : '0')
  form.append('show_scale_bar', payload.show_scale_bar ? '1' : '0')
  form.append('show_metadata_panel', payload.show_metadata_panel ? '1' : '0')
  form.append('default_index_type', payload.default_index_type)
  form.append('show_filter_button', payload.show_filter_button ? '1' : '0')
  form.append('default_view_mode', payload.default_view_mode)
  form.append('default_sort_field', payload.default_sort_field)
  form.append('filter_sort_order', payload.filter_sort_order)
  form.append('max_zoom_pixel_ratio', String(clampMaxZoomPixelRatio(payload.max_zoom_pixel_ratio)))
  form.append('show_admin_link', payload.show_admin_link ? '1' : '0')
  form.append('default_thumbnail_size', payload.default_thumbnail_size)
  form.append('max_index_items', String(clampMaxIndexItems(payload.max_index_items)))
  form.append('index_tours_per_slide', String(clampIndexToursPerSlide(payload.index_tours_per_slide)))
  form.append('immediate_render', payload.immediate_render ? '1' : '0')
  form.append('max_image_cache_count', String(clampMaxImageCacheCount(payload.max_image_cache_count)))
  // FastAPI form parsing collapses empty strings to "field absent", so the
  // value is paired with an explicit clear flag whenever the caller wants
  // to blank the color out.
  if (payload.header_default_title_color) {
    form.append('header_default_title_color', payload.header_default_title_color)
  } else {
    form.append('clear_header_default_title_color', '1')
  }
  return adminSend('PATCH', '/api/admin/viewer/settings', form, 'update viewer settings failed')
}

// Partial PATCH that only touches the three metadata-display attribute
// lists. Lives next to adminUpdateViewerSettings because they hit the
// same endpoint, but kept as a separate function so the Metadata tab's
// Save button can submit just these fields without dragging in (or
// silently overwriting) anything the Settings tab manages.
export async function adminUpdateMetadataDisplaySettings(payload: {
  extra_attrs: string[]
  index_card_attrs: string[]
  index_filter_attrs: string[]
}) {
  const form = new FormData()
  form.append('extra_attrs', JSON.stringify(payload.extra_attrs))
  form.append('index_card_attrs', JSON.stringify(payload.index_card_attrs))
  form.append('index_filter_attrs', JSON.stringify(payload.index_filter_attrs))
  return adminSend('PATCH', '/api/admin/viewer/settings', form, 'update metadata display settings failed')
}

export async function adminUploadViewerLogo(file: File) {
  const form = new FormData()
  form.append('file', file)
  return adminSend<{ ok: boolean; logo_url: string }>(
    'POST',
    '/api/admin/viewer/settings/logo',
    form,
    'upload logo failed',
  )
}

export async function adminDeleteViewerLogo() {
  return adminSend('DELETE', '/api/admin/viewer/settings/logo', undefined, 'delete logo failed')
}

export async function adminUploadViewerFavicon(file: File) {
  const form = new FormData()
  form.append('file', file)
  return adminSend<{ ok: boolean; favicon_url: string }>(
    'POST',
    '/api/admin/viewer/settings/favicon',
    form,
    'upload favicon failed',
  )
}

export async function adminDeleteViewerFavicon() {
  return adminSend('DELETE', '/api/admin/viewer/settings/favicon', undefined, 'delete favicon failed')
}

export async function adminUploadHeaderDefaultBackground(file: File) {
  const form = new FormData()
  form.append('file', file)
  return adminSend<{ ok: boolean; header_default_image_url: string }>(
    'POST',
    '/api/admin/viewer/settings/header-default-background',
    form,
    'upload header background failed',
  )
}

export async function adminDeleteHeaderDefaultBackground() {
  return adminSend(
    'DELETE',
    '/api/admin/viewer/settings/header-default-background',
    undefined,
    'delete header background failed',
  )
}

export async function adminResetHeaderDefault() {
  return adminSend('POST', '/api/admin/viewer/header-default/reset', undefined, 'reset header default failed')
}

export async function adminListSeasonalBackgrounds(
  opts: { signal?: AbortSignal } = {},
): Promise<SeasonalBackground[]> {
  return adminGet('/api/admin/viewer/seasonal-backgrounds', 'list seasonal backgrounds failed', { signal: opts.signal })
}

export async function adminUpdateSeasonalBackground(
  slot: number,
  payload: {
    enabled: boolean
    label: string
    start_month: number
    start_day: number
    end_month: number
    end_day: number
    title_color: string
  },
): Promise<SeasonalBackground> {
  const form = new FormData()
  form.append('enabled', payload.enabled ? '1' : '0')
  appendTextOrClear(form, 'label', payload.label)
  form.append('start_month', String(payload.start_month))
  form.append('start_day', String(payload.start_day))
  form.append('end_month', String(payload.end_month))
  form.append('end_day', String(payload.end_day))
  if (payload.title_color) {
    form.append('title_color', payload.title_color)
  } else {
    form.append('clear_title_color', '1')
  }
  return adminSend('PATCH', `/api/admin/viewer/seasonal-backgrounds/${slot}`, form, 'update seasonal background failed')
}

export async function adminResetSeasonalBackground(slot: number): Promise<SeasonalBackground> {
  return adminSend(
    'POST',
    `/api/admin/viewer/seasonal-backgrounds/${slot}/reset`,
    undefined,
    'reset seasonal background failed',
  )
}

export async function adminUploadSeasonalBackgroundImage(
  slot: number,
  file: File,
): Promise<SeasonalBackground> {
  const form = new FormData()
  form.append('file', file)
  return adminSend(
    'POST',
    `/api/admin/viewer/seasonal-backgrounds/${slot}/image`,
    form,
    'upload seasonal background failed',
  )
}

export async function adminDeleteSeasonalBackgroundImage(slot: number) {
  return adminSend(
    'DELETE',
    `/api/admin/viewer/seasonal-backgrounds/${slot}/image`,
    undefined,
    'delete seasonal background failed',
  )
}
