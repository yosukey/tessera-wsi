import { describe, expect, it } from 'vitest'
import { formatInEmbeddedOffset, formatInServerTz, serverTzLabel, todayInServerTz } from './datetime'

const ISO = '2026-01-01T20:00:00+00:00'

describe('formatInServerTz', () => {
  it('renders in the IANA zone, whatever the browser zone is', () => {
    const tz = { abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }
    expect(formatInServerTz(ISO, tz, 'en', { weekday: false })).toBe('01/02/2026, 05:00:00')
  })

  it('falls back to the fixed offset when the host has no zone name', () => {
    const tz = { abbr: null, tz_name: null, utc_offset: '+09:00' }
    expect(formatInServerTz(ISO, tz, 'en', { weekday: false })).toBe('01/02/2026, 05:00:00')
  })

  it('includes the weekday by default and can drop it', () => {
    const tz = { abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }
    expect(formatInServerTz(ISO, tz, 'en')).toMatch(/Fri/)
    expect(formatInServerTz(ISO, tz, 'en', { weekday: false })).not.toMatch(/Fri/)
  })

  it('passes an unparseable value through and blanks an empty one', () => {
    expect(formatInServerTz('not a date', null, 'en')).toBe('not a date')
    expect(formatInServerTz('', null, 'en')).toBe('')
  })
})

// 2026-01-31T20:00Z: already 2026-02-01 in Tokyo, still 2026-01-31 in New York.
const NOW = new Date('2026-01-31T20:00:00Z')

describe('todayInServerTz', () => {
  it("reads today in the server's analytics zone, not the browser's", () => {
    expect(todayInServerTz({ abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }, 0, NOW))
      .toBe('2026-02-01')
    expect(todayInServerTz({ abbr: 'EST', tz_name: 'America/New_York', utc_offset: '-05:00' }, 0, NOW))
      .toBe('2026-01-31')
  })

  it('falls back to the UTC offset when the server has no zone name', () => {
    expect(todayInServerTz({ abbr: null, tz_name: null, utc_offset: '+09:00' }, 0, NOW)).toBe('2026-02-01')
  })

  it('counts days back across month and year boundaries', () => {
    const tokyo = { abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }
    expect(todayInServerTz(tokyo, 1, NOW)).toBe('2026-01-31')
    expect(todayInServerTz(tokyo, 32, NOW)).toBe('2025-12-31')
  })
})

describe('serverTzLabel', () => {
  it('prefers the zone name, which holds all year round', () => {
    expect(serverTzLabel({ abbr: 'CET', tz_name: 'Europe/Berlin', utc_offset: '+01:00' })).toBe('Europe/Berlin')
  })
  it('falls back to the abbreviation, then the offset', () => {
    expect(serverTzLabel({ abbr: 'JST', tz_name: null, utc_offset: '+09:00' })).toBe('JST')
    expect(serverTzLabel({ abbr: null, tz_name: null, utc_offset: '+09:00' })).toBe('UTC+09:00')
    expect(serverTzLabel(null)).toBe('')
  })
})

describe('formatInEmbeddedOffset locale', () => {
  it('uses the app language when one is given', () => {
    const iso = '2026-01-02T05:00:00+09:00'
    expect(formatInEmbeddedOffset(iso, { locale: 'ja' })).toBe('2026/1/2 05:00:00')
    // \s: newer ICU puts a narrow no-break space before AM.
    expect(formatInEmbeddedOffset(iso, { locale: 'en' })).toMatch(/^1\/2\/2026, 05:00:00\sAM$/)
  })
})
