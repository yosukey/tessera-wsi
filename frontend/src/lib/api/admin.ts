import {
  adminFetch,
  adminGet,
  adminSend,
  appendTextOrClear,
  authHeaders,
  GatewayError,
  Slide,
  throwApi,
  xhrUpload,
} from './shared'
import { triggerDownload } from '../clipboard'
import { displayFilename } from '../admin/validation'

// ===== Admin: slides =====

export async function adminListSlides(opts: { signal?: AbortSignal } = {}) {
  return adminGet<Slide[]>('/api/admin/slides', 'admin slides failed', { signal: opts.signal, cache: 'no-store' })
}

// A slide's full detail by numeric id, drafts included, with the admin-only
// fields (unlisted, label_url, the file's own MPP / objective power, size).
// The public getSlide() also serves drafts to a staff token, but it is keyed
// by public_id and leaves those fields out; the slide and tour editors start
// from the numeric id, so they use this one.
export async function adminGetSlide(slideId: number, opts: { signal?: AbortSignal } = {}): Promise<Slide> {
  return adminGet<Slide>(
    `/api/admin/slides/${slideId}`,
    'admin slide failed',
    { signal: opts.signal, cache: 'no-store' },
  )
}

export async function adminUploadSlide(
  file: File,
  opts: { onProgress?: (percent: number) => void; onSent?: () => void; signal?: AbortSignal } = {},
) {
  const form = new FormData()
  form.append('file', file)
  return xhrUpload<{ ok: boolean; slide_id: number }>('/api/admin/slides/upload', form, 'upload failed', {
    ...opts,
    gatewayError: (status) => new GatewayError(status),
  })
}

export async function adminUpdateSlide(slideId: number, payload: Record<string, any>) {
  const form = new FormData()
  Object.entries(payload).forEach(([k, v]) => form.append(k, String(v ?? '')))
  return adminSend('PATCH', `/api/admin/slides/${slideId}`, form, 'update slide failed')
}

export async function adminDeleteSlide(slideId: number) {
  return adminSend('DELETE', `/api/admin/slides/${slideId}`, undefined, 'delete slide failed')
}

// Fetch an admin-protected image (e.g. macro / label) and convert it to a
// blob URL the caller can drop into <img src=...>. The macro/label endpoints
// require a Bearer token, which a plain <img> can't carry; the blob URL
// pattern keeps the auth on the JS side without inventing signed query-string
// tokens for every preview-only endpoint. Returns "" on any non-OK response
// (e.g. a 404 for a slide without that image) so callers can simply hide the
// image; a network error or an abort rejects. Caller owns revoke().
export async function adminFetchImageBlobUrl(url: string, opts: { signal?: AbortSignal } = {}): Promise<string> {
  const r = await fetch(url, {
    headers: authHeaders(),
    signal: opts.signal,
    cache: 'no-store',
  })
  if (!r.ok) return ''
  // Everything this is used for is an image. Anything else (a download, a JSON
  // error page, a path the caller should never have been sent to) is not
  // buffered into a Blob just because the request carried the bearer.
  if (!(r.headers.get('content-type') || '').toLowerCase().startsWith('image/')) return ''
  const blob = await r.blob()
  return URL.createObjectURL(blob)
}

export async function adminPublishSlide(slideId: number, publish: boolean) {
  const form = new FormData()
  form.append('publish', publish ? '1' : '0')
  return adminSend<{ ok: boolean; is_published: boolean; cascaded_tours: number }>(
    'POST',
    `/api/admin/slides/${slideId}/publish`,
    form,
    'publish slide failed',
  )
}

// Toggle a slide's link-only ("unlisted") visibility. Independent of publish:
// a published+unlisted slide stays reachable by direct URL but is dropped from
// the public index listing, search, and facets.
export async function adminSetSlideUnlisted(slideId: number, unlisted: boolean) {
  const form = new FormData()
  form.append('unlisted', unlisted ? '1' : '0')
  return adminSend<{ ok: boolean; unlisted: boolean }>(
    'POST',
    `/api/admin/slides/${slideId}/unlisted`,
    form,
    'set unlisted failed',
  )
}

// Save the per-slide image-adjustment preset on its own. Used by the in-viewer
// adjust panel so a manager can dial values with live preview and persist them
// without round-tripping the whole metadata form. The server clamps each
// channel and collapses identity (1.0) to NULL (no preset).
export async function adminSaveSlideImageAdjust(
  slideId: number,
  adjust: { brightness: number; contrast: number; saturation: number },
) {
  const form = new FormData()
  form.append('brightness', String(adjust.brightness))
  form.append('contrast', String(adjust.contrast))
  form.append('saturation', String(adjust.saturation))
  const r = await adminFetch(
    `/api/admin/slides/${slideId}/image-adjust`,
    'save image adjustment failed',
    { method: 'PUT', body: form },
  )
  return r.json() as Promise<{
    ok: boolean
    image_adjust_brightness: number | null
    image_adjust_contrast: number | null
    image_adjust_saturation: number | null
  }>
}

// ===== Admin: bulk slide operations =====
//
// Each helper hits a single server-side batch endpoint rather than looping
// per-slide requests, which would trip nginx's 120r/m burst=40 rate limit.

export type BulkDeleteResult = {
  ok: boolean
  deleted: number[]
  not_found: number[]
  failed: { slide_id: number; error: string }[]
}

export async function adminBulkDeleteSlides(slideIds: number[]): Promise<BulkDeleteResult> {
  return adminSend('POST', '/api/admin/slides/bulk/delete', { slide_ids: slideIds }, 'bulk delete failed')
}

export type BulkPublishResult = {
  slide_id: number
  ok: boolean
  is_published: boolean
  cascaded_tours: number
  error: string | null
}

export async function adminBulkPublishSlides(
  slideIds: number[],
  publish: boolean,
): Promise<{ ok: boolean; results: BulkPublishResult[] }> {
  return adminSend('POST', '/api/admin/slides/bulk/publish', { slide_ids: slideIds, publish }, 'bulk publish failed')
}

export async function adminBulkSlideAttributes(
  slideIds: number[],
  key: string,
  values: string[],
  mode: 'add' | 'remove',
): Promise<{ ok: boolean; changed: number[]; not_found: number[] }> {
  return adminSend(
    'POST',
    '/api/admin/slides/bulk/attributes',
    { slide_ids: slideIds, key, values, mode },
    'bulk attributes failed',
  )
}

// ===== Admin: metadata options =====

export type MetadataOptions = Record<string, string[]>

export async function adminListMetadataOptions(opts: { signal?: AbortSignal } = {}): Promise<{ options: MetadataOptions }> {
  return adminGet('/api/admin/metadata/options', 'list metadata options failed', { signal: opts.signal })
}

// A metadata write whose 409 answer is a result the caller reports (a
// duplicate value, slides changed since the usage probe) rather than an error.
async function sendAccepting409<T>(
  method: 'POST' | 'PATCH' | 'DELETE',
  url: string,
  body: FormData | undefined,
  fallback: string,
): Promise<T> {
  const r = await fetch(url, { method, headers: authHeaders(), body })
  if (r.status !== 409 && !r.ok) await throwApi(r, fallback)
  return r.json() as Promise<T>
}

export async function adminAddMetadataOption(key: string, value: string) {
  const form = new FormData()
  form.append('key', key)
  form.append('value', value)
  // The backend returns 409 with `{ok: false, duplicate: true}` for an
  // already-registered (key, value); surface that to the caller as a normal
  // return value (not an exception) so the dashboard can show a specific
  // "already exists" message instead of the generic fallback.
  return sendAccepting409<{ ok: boolean; id: number; duplicate: boolean }>(
    'POST', '/api/admin/metadata/options', form, 'add metadata option failed',
  )
}

export type MetadataOptionUsage = {
  key: string
  value: string
  slide_count: number
  published_slide_count: number
  // The tours whose own metadata carries the value (tour_attributes).
  tour_count: number
  published_tour_count: number
  // Digest of the exact slide (and tour) set counted above. Passed back to
  // the delete, which refuses (409, `changed`) when the slides / tours it
  // would strip are no longer that set — even at an unchanged count.
  slides_fingerprint: string
}

export async function adminGetMetadataOptionUsage(key: string, value: string): Promise<MetadataOptionUsage> {
  return adminGet(
    `/api/admin/metadata/options/usage?key=${encodeURIComponent(key)}&value=${encodeURIComponent(value)}`,
    'get metadata option usage failed',
  )
}

export type MetadataDeleteResult = {
  ok: boolean
  // Number of MetadataOption rows removed. 0 or 1 for a value-level delete
  // (idempotent — 0 when only orphan slide_attributes existed); the full
  // rowcount for a key-level delete.
  options_removed: number
  // Number of slide_attributes rows actually removed (raw rowcount).
  slide_attrs_removed: number
  // Number of distinct slides whose attributes were affected. Differs from
  // slide_attrs_removed only for category-level deletes when a slide has
  // multiple values under the same key.
  slides_affected: number
  // The same two for tours' own metadata (tour_attributes).
  tour_attrs_removed?: number
  tours_affected?: number
  // Set (with ok=false and nothing deleted) on a 409 when the slides / tours
  // using the value / category changed since the usage probe; slide_count and
  // tour_count are the current numbers.
  changed?: boolean
  slide_count?: number
  tour_count?: number
}

// DELETE uses query parameters (not a form body). RFC 9110 leaves DELETE
// request bodies "undefined", so reverse proxies are free to drop them; query
// parameters survive that path.
// `expectedFingerprint` (the usage probe's slides_fingerprint) makes the delete
// conditional; a 409 `changed` comes back as a normal return value.
export async function adminDeleteMetadataOption(
  key: string,
  value: string,
  expectedFingerprint?: string,
): Promise<MetadataDeleteResult> {
  let url = `/api/admin/metadata/options?key=${encodeURIComponent(key)}&value=${encodeURIComponent(value)}`
  if (expectedFingerprint) url += `&expected_fingerprint=${encodeURIComponent(expectedFingerprint)}`
  return sendAccepting409('DELETE', url, undefined, 'delete metadata option failed')
}

export type MetadataKeyUsage = {
  key: string
  slide_count: number
  published_slide_count: number
  tour_count: number
  published_tour_count: number
  // See MetadataOptionUsage.slides_fingerprint.
  slides_fingerprint: string
}

export async function adminGetMetadataKeyUsage(key: string): Promise<MetadataKeyUsage> {
  return adminGet(`/api/admin/metadata/keys/usage?key=${encodeURIComponent(key)}`, 'get metadata key usage failed')
}

export type MetadataCountEntry = { total: number; published: number }

export type MetadataCounts = {
  keys: Record<string, MetadataCountEntry>
  values: Record<string, Record<string, MetadataCountEntry>>
  // The same, counting tours by their own metadata.
  tour_keys: Record<string, MetadataCountEntry>
  tour_values: Record<string, Record<string, MetadataCountEntry>>
}

export async function adminGetMetadataCounts(opts: { signal?: AbortSignal } = {}): Promise<MetadataCounts> {
  return adminGet('/api/admin/metadata/counts', 'get metadata counts failed', { signal: opts.signal })
}

// See adminDeleteMetadataOption for `expectedFingerprint`.
export async function adminDeleteMetadataKey(key: string, expectedFingerprint?: string): Promise<MetadataDeleteResult> {
  let url = `/api/admin/metadata/keys?key=${encodeURIComponent(key)}`
  if (expectedFingerprint) url += `&expected_fingerprint=${encodeURIComponent(expectedFingerprint)}`
  return sendAccepting409('DELETE', url, undefined, 'delete metadata key failed')
}

export type MetadataRenameResult = {
  ok: boolean
  renamed?: boolean
  // Present (with target_exists) on a 409 when the new value already exists
  // under the same key — as a registered option or an orphan slide attribute.
  duplicate?: boolean
  target_exists?: boolean
  slides_affected?: number
  slide_attrs_updated?: number
  tours_affected?: number
  tour_attrs_updated?: number
}

// Rename a metadata value in place; existing slide assignments follow. A 409
// (target already exists) is surfaced as a normal return value carrying
// `target_exists` rather than thrown, mirroring adminAddMetadataOption.
export async function adminRenameMetadataOption(
  key: string,
  oldValue: string,
  newValue: string,
): Promise<MetadataRenameResult> {
  const form = new FormData()
  form.append('key', key)
  form.append('old_value', oldValue)
  form.append('new_value', newValue)
  return sendAccepting409('PATCH', '/api/admin/metadata/options', form, 'rename metadata option failed')
}

// ===== Admin: network settings =====

export type IpRule = { ip: string; action: 'allow' | 'deny'; note: string }

export type NetworkSettings = {
  ip_filter_enabled: boolean
  ip_filter_default: 'allow' | 'deny'
  ip_rules: IpRule[]
  mdns_enabled: boolean
  mdns_active: boolean
  ga_measurement_id: string
  // Access-analytics recording mode and the notice-toast visibility flag.
  // The salt itself never leaves the server; analytics_hash_salt_available
  // is a derived boolean that just tells the admin UI whether selecting
  // "hashed" is safe right now.
  analytics_mode: 'off' | 'hashed' | 'plain'
  analytics_notice_toast_enabled: boolean
  analytics_hash_salt_available: boolean
}

export async function adminGetNetworkSettings(opts: { signal?: AbortSignal } = {}): Promise<NetworkSettings> {
  return adminGet('/api/admin/network/settings', 'network settings failed', { signal: opts.signal })
}

export async function adminUpdateNetworkSettings(payload: {
  ip_filter_enabled: boolean
  ip_filter_default: 'allow' | 'deny'
  ip_rules: IpRule[]
  mdns_enabled: boolean
  ga_measurement_id: string
  analytics_mode: 'off' | 'hashed' | 'plain'
  analytics_notice_toast_enabled: boolean
  // Set after the operator confirmed the self-lockout warning; tells the
  // server's matching guard to accept the save instead of returning 422.
  override_ip_lockout?: boolean
}): Promise<NetworkSettings & { ok: boolean; mdns_ok: boolean; mdns_msg: string }> {
  const form = new FormData()
  form.append('ip_filter_enabled', payload.ip_filter_enabled ? '1' : '0')
  form.append('ip_filter_default', payload.ip_filter_default)
  form.append('ip_rules', JSON.stringify(payload.ip_rules))
  form.append('mdns_enabled', payload.mdns_enabled ? '1' : '0')
  appendTextOrClear(form, 'ga_measurement_id', payload.ga_measurement_id)
  form.append('analytics_mode', payload.analytics_mode)
  form.append('analytics_notice_toast_enabled', payload.analytics_notice_toast_enabled ? '1' : '0')
  if (payload.override_ip_lockout) form.append('override_ip_lockout', '1')
  return adminSend('PATCH', '/api/admin/network/settings', form, 'update network settings failed')
}

export type MyIpInfo = {
  ip: string
  via_cloudflare_tunnel: boolean
}

export async function adminGetMyIp(opts: { signal?: AbortSignal } = {}): Promise<MyIpInfo> {
  return adminGet('/api/admin/network/my-ip', 'my-ip failed', { signal: opts.signal })
}

// ===== Admin: server info & stats =====

export type ServerNetworkInterface = {
  name: string
  ipv4: string | null
  netmask: string | null
  ipv6: string[]
  link_state: 'up' | 'down' | 'unknown'
  speed_mbps: number | null
  io: {
    bytes_sent: number
    bytes_recv: number
    packets_sent: number
    packets_recv: number
    errin: number
    errout: number
    dropin: number
    dropout: number
  } | null
}

// The source a running version was resolved from, in the order the server
// tries them: the VERSION stamp written at deploy, `git describe --tags`, the
// frontend/package.json fallback (not bumped by releases), or none.
export type AppVersionSource = 'VERSION' | 'git describe' | 'package.json' | 'unknown'

export type ServerInfo = {
  hostname: string
  fqdn: string | null
  interfaces: ServerNetworkInterface[]
  os: {
    system: string
    release: string
    version: string
    machine: string
    distro_pretty_name: string | null
    glibc_version: string | null
    timezone: string | null
    tz_name: string | null
    utc_offset: string | null
    server_time: string | null
    boot_time: string | null
    virtualization: string
  }
  dmi: {
    sys_vendor: string | null
    product_name: string | null
    product_version: string | null
  }
  cpu: {
    model: string | null
    vendor: string | null
    logical: number
    physical: number | null
    sockets: number | null
    freq_current_mhz: number | null
    freq_max_mhz: number | null
    usage_total_pct: number | null
    usage_per_core_pct: number[] | null
    temperature_c: number | null
    cache: {
      l1d_kb: number | null
      l1i_kb: number | null
      l2_kb: number | null
      l3_kb: number | null
    }
    flags_subset: string[]
  }
  // null: the figure could not be read (shown as "—", never as 0).
  memory: {
    total_mb: number | null
    available_mb: number | null
    used_mb: number | null
    buffers_mb: number | null
    cached_mb: number | null
    swap_total_mb: number | null
    swap_used_mb: number | null
    swap_free_mb: number | null
    hugepages_total: number | null
    hugepages_size_kb: number | null
  }
  load_avg: { m1: number; m5: number; m15: number }
  uptime_seconds: number
  app_process: {
    pid: number
    threads: number | null
    fds: number | null
    rss_mb: number | null
    vm_peak_mb: number | null
    vm_size_mb: number | null
    started_at: string | null
    app_version: string
    // Where app_version was read from (services/app/wsi_app/version.py).
    app_version_source: AppVersionSource
    db_app_version: string | null
    python_version: string
    // Every app worker; the fields above are the one that answered.
    workers: {
      count: number
      rss_mb_total: number
      items: Array<{ pid: number; rss_mb: number; threads: number; current: boolean }>
    } | null
  }
  system_processes: { process_count: number | null }
  db: { kind: string; version: string | null }
  // Figures are null when the data directory could not be stat'ed;
  // used_pct is df's used / (used + available).
  disk_data_dir: {
    path: string
    total_gb: number | null
    used_gb: number | null
    free_gb: number | null
    used_pct: number | null
    fstype: string | null
    inodes_total: number | null
    inodes_free: number | null
    rotational: boolean | null
  }
  other_mounts: Array<{
    mountpoint: string
    fstype: string | null
    device: string
    total_gb: number
    used_gb: number
    free_gb: number
    used_pct: number
  }>
  disk_io_total: {
    read_bytes: number
    write_bytes: number
    read_count: number
    write_count: number
  } | null
  cgroup: {
    in_container: boolean
    container_kind: string | null
    // This process's cgroup v2 path; limits are the tightest on it or an
    // ancestor (a systemd MemoryMax= / CPUQuota= included).
    cgroup_path: string | null
    memory_max_bytes: number | null
    cpu_quota_us: number | null
    cpu_period_us: number | null
  }
  dzi_cache: { entries: number; max: number; bytes: number; max_bytes: number }
  nginx_cache: {
    path: string
    max_size: string | null
    inactive: string | null
    used_bytes: number | null
    file_count: number | null
    source: 'stat_file' | 'walk' | null
    // The walk stopped early: used_bytes / file_count are lower bounds.
    partial: boolean
    stat_updated_at: string | null
    stat_age_seconds: number | null
    // The stat timer has not rewritten the file for over five minutes.
    stat_stale: boolean
  }
  app_update: {
    enabled: boolean
    current: string
    // Where `current` was read from: an incomparable result then tells a
    // package.json fallback apart from a version string that does not parse.
    current_source: AppVersionSource
    latest: string | null
    update_available: boolean
    comparable: boolean
    checked_at: string | null
    last_attempt_at: string | null
  }
}

export type AdminStats = {
  slides: {
    total: number
    total_file_bytes: number
    avg_file_bytes: number
    largest_file_bytes: number
    largest_file_name: string
    with_mpp: number
    without_mpp: number
    with_catalog_id: number
    without_catalog_id: number
    published: number
    draft: number
    by_vendor: { label: string; count: number }[]
    by_extension: { label: string; count: number }[]
  }
  tours: {
    total: number
    published: number
    draft: number
    slides_with_tours: number
    slides_without_tours: number
  }
  // null for non-admin server viewers (backups are admin-only)
  backups: {
    count: number
    total_size_bytes: number
  } | null
}

export async function adminGetStats(opts: { signal?: AbortSignal } = {}): Promise<AdminStats> {
  return adminGet('/api/admin/stats', 'admin stats failed', { signal: opts.signal })
}

export async function adminGetServerInfo(opts: { signal?: AbortSignal } = {}): Promise<ServerInfo> {
  return adminGet('/api/admin/server/info', 'server info failed', { signal: opts.signal, cache: 'no-store' })
}

// ===== Admin: WSI file status =====

export interface SlideFileStatusEntry {
  id: number
  catalog_id: string
  title: string
  wsi_filename: string
  file_exists: boolean
}

export interface SlideFileStatus {
  total: number
  present: number
  missing: number
  slides: SlideFileStatusEntry[]
  orphaned_files: string[]
  temp_files: string[]
  wsi_dir: string
}

// A transport-level failure rejects as NetworkError, so the caller can tell it
// apart from a 4xx/5xx — which matters most here: losing the response after a
// multi-GB body was fully sent must not read as "the replace failed, send it
// again". ``opts.onSent`` marks the point from which that holds.
export async function adminReplaceSlideWsi(
  slideId: number,
  file: File,
  onProgress?: (percent: number) => void,
  opts: { signal?: AbortSignal; onSent?: () => void } = {},
): Promise<{ ok: boolean; slide_id: number }> {
  const form = new FormData()
  form.append('file', file)
  return xhrUpload(`/api/admin/slides/${slideId}/wsi`, form, 'replace wsi failed', {
    onProgress,
    onSent: opts.onSent,
    signal: opts.signal,
    gatewayError: (status) => new GatewayError(status),
  })
}

// Mint a short-lived signed URL, then navigate to it via a synthetic <a>
// click. Mirrors the backup-download flow: the SPA can't put an
// Authorization header on a plain <a download> click, so the helper
// issues a query-string token the GET endpoint accepts. Letting the
// browser drive the download keeps multi-GB WSI files out of tab memory
// the way fetch+Blob would otherwise pin them.
export async function adminGetSlideDownloadUrl(slideId: number): Promise<string> {
  const r = await adminFetch(
    `/api/admin/slides/${slideId}/download_url`,
    'slide download url failed',
    { method: 'POST' },
  )
  const { url } = (await r.json()) as { url: string }
  return url
}

export async function adminDownloadSlide(slide: { id: number; wsi_filename?: string }): Promise<void> {
  const url = await adminGetSlideDownloadUrl(slide.id)
  // Strip the `<id>_` upload-time prefix so the saved file is named the way
  // the user originally uploaded it. The backend sets Content-Disposition
  // independently, but the <a download> attribute beats it for cross-origin
  // safety pruning so the same logic is mirrored here.
  triggerDownload(url, displayFilename(slide.wsi_filename || `slide-${slide.id}`, slide.id))
}

export async function adminGetSlideFileStatus(opts: { signal?: AbortSignal } = {}): Promise<SlideFileStatus> {
  return adminGet('/api/admin/slides/file-status', 'file status failed', { signal: opts.signal, cache: 'no-store' })
}

// The volumes a slide upload is written to and the reserve policy the server
// checks it against (admin_helpers.upload_space_short / disk_space.py), so the
// bulk-upload preflight can predict the server's verdict per file
// (lib/admin/uploadSpace.ts). A null free figure is unknown: the server's
// check passes on it, so the client must not block on it either.
export interface UploadSpaceInfo {
  // The volume data_dir/wsi really lives on (resolved through a symlink).
  target_free_bytes: number | null
  // The multipart spool's volume (TMPDIR).
  spool_free_bytes: number | null
  same_volume: boolean
  // Copies of the body written before the upload is staged (spool + wsi/).
  copies: number
  reserve_fraction: number
  reserve_min_bytes: number
}

// Data-volume byte counts for the bulk-upload preflight (manager+, or server
// access). free_bytes 0 = statvfs failed server-side, i.e. unknown. These
// measure data_dir, which is only for display: the preflight's verdict comes
// from upload_space.
export interface StorageInfo {
  total_bytes: number
  free_bytes: number
  used_bytes: number
  upload_space: UploadSpaceInfo
  // The server's per-file slide upload / replace cap (WSI_MAX_UPLOAD_BYTES).
  max_upload_bytes: number
  // The slide extensions the server accepts (WSI_ALLOWED_EXTENSIONS),
  // lower-cased with the dot. May be absent.
  allowed_extensions?: string[]
}

export async function adminGetStorage(opts: { signal?: AbortSignal } = {}): Promise<StorageInfo> {
  return adminGet('/api/admin/storage', 'storage info failed', { signal: opts.signal, cache: 'no-store' })
}
