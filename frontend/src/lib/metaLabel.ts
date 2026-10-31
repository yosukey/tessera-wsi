type T = (key: string, params?: Record<string, string | number>) => string

// The built-in slide fields and metadata categories (re-exported from
// lib/admin/validation for the admin code). Defined here so this module has
// no imports and validation.ts can use metaFieldLabel without an import cycle.
export const BASE_KEYS = new Set(['catalog_id', 'title', 'stain', 'organ', 'lab', 'tags'])

// Built-in metadata categories have translations under `adminDash.fieldXxx`.
// Admin-added custom categories are user-entered strings — rendered
// verbatim because there is no place to attach a translation.
export function metaFieldLabel(t: T, key: string): string {
  if (!BASE_KEYS.has(key)) return key
  const pascal = key.split('_').filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join('')
  return t(`adminDash.field${pascal}`)
}
