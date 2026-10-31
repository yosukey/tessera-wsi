import type { ServerTz } from '../api/analytics'
import type { Locale } from '../i18n'

// Render an ISO 8601 timestamp using the timezone embedded in the string
// rather than the browser's local zone. The backend serialises slide
// upload timestamps in the server's local TZ (see _serializers.server_local_iso),
// and the admin UI surfaces that same wall-clock regardless of where
// the operator's browser happens to be — otherwise an admin in a different
// region sees a different "uploaded at" than the server-side audit log.
//
// The parsed Date is shifted by the embedded offset and then formatted as if
// it were UTC; the locale-aware wall-clock readout that comes out is the
// server's local time. Falls back to the browser-local readout for naive
// strings (no trailing Z or ±HH:MM) so callers that pass timestamps without
// zone info (e.g. acquired_at) keep their current behaviour.

function parseOffsetMinutes(iso: string): number | null {
  const m = iso.match(/(Z|[+-]\d{2}:?\d{2})$/)
  if (!m) return null
  if (m[1] === 'Z') return 0
  const parts = m[1].match(/([+-])(\d{2}):?(\d{2})/)
  if (!parts) return null
  const sign = parts[1] === '+' ? 1 : -1
  return sign * (parseInt(parts[2], 10) * 60 + parseInt(parts[3], 10))
}

// `locale` picks the app's UI language for the readout; without it the
// browser's locale is used (the callers that predate the option).
export function formatInEmbeddedOffset(
  raw: string | undefined,
  opts?: { dateOnly?: boolean; locale?: Locale },
): string {
  if (!raw) return '—'
  const d = new Date(raw)
  if (isNaN(d.getTime())) return '—'
  const intlLocale = opts?.locale ? intlLocaleFor(opts.locale) : undefined
  const offsetMin = parseOffsetMinutes(raw)
  if (offsetMin === null) {
    return opts?.dateOnly ? d.toLocaleDateString(intlLocale) : d.toLocaleString(intlLocale)
  }
  const shifted = new Date(d.getTime() + offsetMin * 60_000)
  const fmtOpts: Intl.DateTimeFormatOptions = opts?.dateOnly
    ? { timeZone: 'UTC' }
    : { timeZone: 'UTC', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }
  return opts?.dateOnly
    ? shifted.toLocaleDateString(intlLocale, fmtOpts)
    : shifted.toLocaleString(intlLocale, fmtOpts)
}

function intlLocaleFor(locale: Locale): string {
  return locale === 'ja' ? 'ja-JP' : 'en-US'
}

// The zone label for a column of times rendered with formatInServerTz. The
// IANA name when there is one: an abbreviation or offset is only right for
// part of the year in a zone with summer time (a "CET" heading over rows
// from July, which formatInServerTz renders in CEST).
export function serverTzLabel(tz: ServerTz | null): string {
  if (!tz) return ''
  return tz.tz_name || tz.abbr || (tz.utc_offset ? `UTC${tz.utc_offset}` : '')
}

function parseUtcOffsetMinutes(off: string | null | undefined): number | null {
  if (!off) return null
  const m = /^([+-])(\d{2}):(\d{2})$/.exec(off)
  if (!m) return null
  const sign = m[1] === '-' ? -1 : 1
  return sign * (Number(m[2]) * 60 + Number(m[3]))
}

// The calendar date (YYYY-MM-DD) `daysAgo` days before today in the server's
// analytics timezone — the zone the analytics date filters are read in, which
// need not be the browser's: an admin looking at a server several zones east is
// already on "tomorrow" there for part of the day. Falls back to the `+HH:MM`
// offset when the server has no IANA name, then to the browser's own date.
export function todayInServerTz(tz: ServerTz | null, daysAgo = 0, now: Date = new Date()): string {
  let ymd = tz?.tz_name ? zonedDateParts(now, tz.tz_name) : null
  const offMin = parseUtcOffsetMinutes(tz?.utc_offset)
  if (!ymd && offMin !== null) {
    const shifted = new Date(now.getTime() + offMin * 60_000)
    ymd = [shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, shifted.getUTCDate()]
  }
  if (!ymd) ymd = [now.getFullYear(), now.getMonth() + 1, now.getDate()]
  const [y, m, d] = ymd
  // Calendar arithmetic on the date alone (UTC has no DST to trip over).
  const day = new Date(Date.UTC(y, m - 1, d - daysAgo))
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${day.getUTCFullYear()}-${pad(day.getUTCMonth() + 1)}-${pad(day.getUTCDate())}`
}

function zonedDateParts(now: Date, timeZone: string): [number, number, number] | null {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone, year: 'numeric', month: 'numeric', day: 'numeric',
    }).formatToParts(now)
    const get = (type: string) => Number(parts.find(p => p.type === type)?.value)
    const out: [number, number, number] = [get('year'), get('month'), get('day')]
    return out.every(Number.isFinite) ? out : null
  } catch {
    return null
  }
}

// Render `isoStr` in the server's analytics timezone (the zone the history /
// log / analytics since-until filters are read in) rather than the browser's.
// `weekday: false` drops the weekday for compact table columns.
export function formatInServerTz(
  isoStr: string,
  tz: ServerTz | null,
  locale: Locale,
  opts: { weekday?: boolean } = {},
): string {
  const weekday = opts.weekday ?? true
  if (!isoStr) return ''
  const d = new Date(isoStr)
  if (isNaN(d.getTime())) return isoStr
  // `weekday` is locale-driven so the abbreviation matches the rest of
  // the UI ("Mon" in en, "月" in ja). Time fields stay 24-hour to keep
  // the column width predictable across locales.
  const intlLocale = intlLocaleFor(locale)
  if (tz?.tz_name) {
    try {
      return new Intl.DateTimeFormat(intlLocale, {
        timeZone: tz.tz_name,
        year: 'numeric', month: '2-digit', day: '2-digit',
        ...(weekday ? { weekday: 'short' as const } : {}),
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false,
      }).format(d)
    } catch { /* fall through */ }
  }
  // No IANA name (degenerate host: env / /etc/localtime / /etc/timezone all
  // unresolvable). If the server still surfaced a `+HH:MM` offset, shift the
  // instant manually and render in UTC so the wall clock matches the badge.
  // Without this branch the column header would show the server's zone while the cells
  // render in the browser's local TZ.
  const offMin = parseUtcOffsetMinutes(tz?.utc_offset)
  if (offMin !== null) {
    const shifted = new Date(d.getTime() + offMin * 60_000)
    try {
      return new Intl.DateTimeFormat(intlLocale, {
        timeZone: 'UTC',
        year: 'numeric', month: '2-digit', day: '2-digit',
        ...(weekday ? { weekday: 'short' as const } : {}),
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false,
      }).format(shifted)
    } catch { /* fall through */ }
  }
  try {
    return new Intl.DateTimeFormat(intlLocale, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      ...(weekday ? { weekday: 'short' as const } : {}),
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }).format(d)
  } catch {
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`
  }
}
