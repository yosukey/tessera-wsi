import { BASE_KEYS, metaFieldLabel } from '../metaLabel'

export { BASE_KEYS }
export const META_DISPLAY_KEYS = ['stain', 'organ', 'lab', 'tags']
export const SORT_OPTIONS = ['id', 'title', 'catalog_id', 'wsi_filename', 'vendor', 'pixels', 'mpp', 'tours', 'created_at'] as const
export type SortField = typeof SORT_OPTIONS[number]
export type SortOrder = 'asc' | 'desc'
export type ToursFilter = 'all' | 'has' | 'none'

// The slide upload cap comes from the server (GET /api/admin/storage →
// max_upload_bytes, i.e. WSI_MAX_UPLOAD_BYTES): an operator can raise or lower
// it, so a client-side number would refuse files the server takes or name the
// wrong limit. null = not known (the fetch failed or is still pending); then
// nothing is refused here and the server's own 413 decides.
export function exceedsUploadLimit(size: number, limit: number | null | undefined): boolean {
  return limit != null && size > limit
}
// Single-file formats only (an upload is one file, so the multi-file MRXS /
// VMS / VMU formats can never be stored whole). Keep in sync with
// DEFAULT_WSI_ALLOWED_EXTENSIONS in services/app/wsi_app/config.py.
export const ACCEPTED_WSI_EXTENSIONS = ['.svs', '.ndpi', '.tif', '.tiff', '.scn', '.bif', '.szi']

// The extensions to accept: the server's own list (GET /api/admin/storage →
// allowed_extensions, i.e. WSI_ALLOWED_EXTENSIONS) once it is known, so an
// operator's narrower or wider list is what the screen checks; the default
// list until then (or from a server that does not report it).
export function acceptedWsiExtensions(server?: readonly string[] | null): string[] {
  return server && server.length > 0 ? server.map((e) => e.toLowerCase()) : ACCEPTED_WSI_EXTENSIONS
}

// Cap on how many files a single bulk-upload batch may hold. Primarily guards
// the browser (rendering/holding many large File handles) and prevents runaway
// batches; the real storage limiter is the free-space preflight
// (lib/admin/uploadSpace.ts, the server's own per-file rule).
export const MAX_BULK_UPLOAD_FILES = 50

// Upload size caps for the small viewer-settings assets. Each entry must
// stay in lockstep with the matching `_*_MAX_BYTES` constant in
// services/app/wsi_app/routers/admin_settings.py — the client-side check
// is purely there to avoid a doomed multi-MB round-trip; the server is
// still the source of truth and will 413 on anything past these limits.
export const UPLOAD_LIMITS = {
  LOGO: { bytes: 2 * 1024 * 1024, label: '2 MB' },
  FAVICON: { bytes: 512 * 1024, label: '512 KB' },
  HEADER_IMAGE: { bytes: 2 * 1024 * 1024, label: '2 MB' },
} as const

export const CTRL_CHAR_RE = /[\x00-\x1f\x7f]/

// 6-digit hex color (with leading #). Mirrors the server's
// _HEX_COLOR_RE in admin_settings.py so client-side validation agrees
// with what _normalize_title_color will accept.
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/
export function isValidHexColor(v: string): boolean {
  return HEX_COLOR_RE.test(v)
}

// Mirrors CATALOG_ID_MAX_LEN / TITLE_MAX_LEN / ATTR_KEY_MAX_LEN /
// ATTR_VALUE_MAX_LEN in services/app/wsi_app/admin_helpers.py.
const META_FIELD_LIMITS: Record<string, number> = {
  catalog_id: 128,
  title: 255,
}
const ATTR_KEY_MAX_LEN = 128
const ATTR_VALUE_MAX_LEN = 255

export type AttrMap = Record<string, string[]>
export type TFn = (key: string, params?: Record<string, string | number>) => string

// What the edit form was seeded with (or last saved).
export type MetadataBaseline = { fields: Record<string, string>; attrs: AttrMap }

// Length in characters (code points), as the server's len() counts — not
// UTF-16 units, which count an emoji or a rare kanji twice.
function charCount(s: string): number {
  return [...s].length
}

// Checks the slide editor's catalog ID / title / attributes the way
// admin_helpers.validate_slide_metadata does, so a doomed save is refused
// before the round trip. The catalog ID and title are checked trimmed, as the
// server strips them first; attribute keys and values as sent. With a
// baseline, only what differs from it is checked: a stored value this check
// would refuse must not block saving unrelated fields.
export function validateMetadata(
  fields: Record<string, string>,
  attrs: AttrMap,
  t: TFn,
  baseline?: MetadataBaseline,
): string | null {
  const errors: string[] = []

  for (const [key, max] of Object.entries(META_FIELD_LIMITS)) {
    const val = (fields[key] ?? '').trim()
    if (baseline && val === (baseline.fields[key] ?? '').trim()) continue
    const label = metaFieldLabel(t, key)
    const len = charCount(val)
    if (len > max) {
      errors.push(t('adminDash.validationMaxLen', { label, max, len }))
    }
    if (CTRL_CHAR_RE.test(val)) {
      errors.push(t('adminDash.validationCtrlChar', { label }))
    }
  }

  for (const [key, vals] of Object.entries(attrs)) {
    if (!key.trim()) continue
    const storedVals = baseline?.attrs[key]
    if (!storedVals) {
      const shortKey = [...key].slice(0, 30).join('')
      if (charCount(key) > ATTR_KEY_MAX_LEN) {
        errors.push(t('adminDash.validationAttrKeyMaxLen', { key: shortKey, max: ATTR_KEY_MAX_LEN }))
      }
      if (CTRL_CHAR_RE.test(key)) {
        errors.push(t('adminDash.validationAttrKeyCtrlChar', { key: shortKey }))
      }
      if (/[,:]/.test(key)) {
        errors.push(t('adminDash.validationAttrKeyInvalidChars', { key }))
      }
    }
    for (const val of vals) {
      if (storedVals?.includes(val)) continue
      const len = charCount(val)
      if (len > ATTR_VALUE_MAX_LEN) {
        errors.push(t('adminDash.validationAttrValMaxLen', { key, max: ATTR_VALUE_MAX_LEN, len }))
      }
      if (CTRL_CHAR_RE.test(val)) {
        errors.push(t('adminDash.validationAttrValCtrlChar', { key }))
      }
    }
  }

  return errors.length > 0 ? errors.join('\n') : null
}

export function validateMppInput(raw: string, label: string, t: TFn): string | null {
  const s = (raw ?? '').trim()
  if (!s) return null
  const n = Number(s)
  if (!Number.isFinite(n) || n < 0 || n > 1000) {
    return t('adminDash.validationMppRange', { label })
  }
  return null
}

export function formatMpp(mpp_x: number, mpp_y: number): string {
  if (!mpp_x && !mpp_y) return ''
  const x = mpp_x ? mpp_x.toFixed(4).replace(/\.?0+$/, '') : '—'
  const y = mpp_y ? mpp_y.toFixed(4).replace(/\.?0+$/, '') : '—'
  return `${x} / ${y} μm/px`
}

// A stored WSI filename the way it was uploaded: without the `<slide id>_`
// prefix the server adds on disk. Given the slide's id, only that exact prefix
// is stripped (as the server does for the download name); without it, any
// leading `<digits>_`.
export function displayFilename(wsiFilename: string, slideId?: number): string {
  if (slideId == null) return wsiFilename.replace(/^\d+_/, '')
  const prefix = `${slideId}_`
  return wsiFilename.startsWith(prefix) ? wsiFilename.slice(prefix.length) : wsiFilename
}

const BINARY_BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB']
const DECIMAL_BYTE_UNITS = ['B', 'kB', 'MB', 'GB', 'TB']
// Fraction digits per unit: whole B and KB, one decimal for MB, two from GB.
const BYTE_UNIT_DIGITS = [0, 0, 1, 2, 2]

// A byte count for display in the largest unit it reaches: binary (1024)
// units, or with `decimal` SI (1000) units to sit beside the MB/s rates the
// performance benchmark reports. null / undefined (unknown) → "—"; callers
// for which 0 also means unknown pass `bytes || null`.
export function formatBytes(bytes: number | null | undefined, opts: { decimal?: boolean } = {}): string {
  if (bytes == null) return '—'
  const base = opts.decimal ? 1000 : 1024
  const units = opts.decimal ? DECIMAL_BYTE_UNITS : BINARY_BYTE_UNITS
  let i = 0
  while (i < units.length - 1 && bytes >= base ** (i + 1)) i++
  if (i === 0) return `${bytes} B`
  return `${(bytes / base ** i).toFixed(BYTE_UNIT_DIGITS[i])} ${units[i]}`
}

// A size limit for display: binary units like formatBytes, without trailing
// zeros, so the 8 GiB slide default reads "8 GB" and the 2.5 MiB tour-asset
// default "2.5 MB".
export function formatByteLimit(bytes: number): string {
  let i = 0
  while (i < BINARY_BYTE_UNITS.length - 1 && bytes >= 1024 ** (i + 1)) i++
  return `${Number((bytes / 1024 ** i).toFixed(2))} ${BINARY_BYTE_UNITS[i]}`
}

export function formatUptime(sec: number): string {
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
