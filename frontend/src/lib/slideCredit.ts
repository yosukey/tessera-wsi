// Per-slide image credit: who holds the rights to a WSI and the terms it is
// shown under. The backend validates and stores three fields on the slide
// (services/app/wsi_app/slide_credit.py); this module maps the license id to
// what the viewer renders.

export type SlideCredit = {
  credit_text?: string
  license_id?: string
  source_url?: string
}

export type SlideLicense = {
  // Display name. The license's short English name (not translated),
  // except 'custom', whose label is an i18n key (see licenseLabel).
  name: string
  // Canonical deed URL, or '' when the license has none to link to.
  url: string
}

// Keep the ids in sync with LICENSE_IDS in services/app/wsi_app/slide_credit.py.
// Insertion order is the order of the admin <select>.
export const SLIDE_LICENSES: Record<string, SlideLicense> = {
  'CC-BY-4.0': { name: 'CC BY 4.0', url: 'https://creativecommons.org/licenses/by/4.0/' },
  'CC-BY-SA-4.0': { name: 'CC BY-SA 4.0', url: 'https://creativecommons.org/licenses/by-sa/4.0/' },
  'CC-BY-NC-4.0': { name: 'CC BY-NC 4.0', url: 'https://creativecommons.org/licenses/by-nc/4.0/' },
  'CC-BY-NC-SA-4.0': { name: 'CC BY-NC-SA 4.0', url: 'https://creativecommons.org/licenses/by-nc-sa/4.0/' },
  'CC-BY-ND-4.0': { name: 'CC BY-ND 4.0', url: 'https://creativecommons.org/licenses/by-nd/4.0/' },
  'CC-BY-NC-ND-4.0': { name: 'CC BY-NC-ND 4.0', url: 'https://creativecommons.org/licenses/by-nc-nd/4.0/' },
  'CC0-1.0': { name: 'CC0 1.0', url: 'https://creativecommons.org/publicdomain/zero/1.0/' },
  'PDM': { name: 'Public Domain Mark 1.0', url: 'https://creativecommons.org/publicdomain/mark/1.0/' },
  'ARR': { name: 'All rights reserved', url: '' },
  'custom': { name: 'viewer.credit.customLicense', url: '' },
}

// The license's display name, translated only for 'custom'. '' for an unset
// or unknown id (a newer server's id this build does not know shows nothing
// rather than a raw identifier).
export function licenseLabel(id: string | undefined, t: (key: string) => string): string {
  if (!id) return ''
  const lic = SLIDE_LICENSES[id]
  if (!lic) return ''
  return id === 'custom' ? t(lic.name) : lic.name
}

export function licenseUrl(id: string | undefined): string {
  return (id && SLIDE_LICENSES[id]?.url) || ''
}

// Mirror the widths in services/app/wsi_app/slide_credit.py, counted in
// characters (code points) as the server counts.
export const CREDIT_TEXT_MAX_LEN = 512
export const SOURCE_URL_MAX_LEN = 1024

// The source link only when it is what the server accepts
// (slide_credit.clean_source_url): an absolute http(s) URL of at most
// SOURCE_URL_MAX_LEN characters with a host right after the "//", no
// userinfo, no "%" in the host, no whitespace, invisible characters or
// backslash — and one the browser can parse, which the server's port and
// host checks stand in for. slideCredit.sourceUrlCases.json holds the cases
// both sides are tested against. This also keeps a hand-edited or restored
// row from ever rendering as a javascript: link.
export function safeSourceUrl(raw: string | undefined): string {
  const v = (raw || '').trim()
  if ([...v].length > SOURCE_URL_MAX_LEN) return ''
  // "https:///host" parses in a browser (it skips the extra slashes) but has
  // an empty authority to the server.
  if (!/^https?:\/\/[^/]/i.test(v) || /[\s\p{C}\\]/u.test(v)) return ''
  // "https://trusted.example@elsewhere.test/" opens elsewhere.test, and the
  // browser decodes a "%" escape in the host, so either way the host it
  // opens is not the one written.
  if (/^[a-z]+:\/\/[^/?#]*[@%]/i.test(v)) return ''
  let u: URL
  try {
    u = new URL(v)
  } catch {
    return ''
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return ''
  if (!u.hostname) return ''
  return v
}

export function hasSlideCredit(c: SlideCredit | null | undefined, t: (key: string) => string): boolean {
  if (!c) return false
  return !!((c.credit_text || '').trim() || licenseLabel(c.license_id, t) || safeSourceUrl(c.source_url))
}
