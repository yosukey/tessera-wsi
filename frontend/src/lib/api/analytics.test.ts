// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  adminClearAnalytics,
  adminClearAnalyticsUser,
  adminDownloadAnalyticsCsv,
  adminPreviewAnalyticsUser,
  adminPseudonymizeAnalytics,
  recordSlideDwell,
} from './analytics'
import { buildQuery } from './shared'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function batch(complete: boolean, pv: number, sde = 0, ts = 0) {
  return json({
    ok: true, complete, deleted: pv,
    deleted_page_views: pv, deleted_slide_dwell_events: sde, deleted_tour_sessions: ts,
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('buildQuery', () => {
  it('drops empty values and sends booleans as 1 / 0', () => {
    expect(buildQuery({ a: 'x y', b: undefined, c: null, d: '', e: true, f: false, g: 0 }))
      .toBe('?a=x+y&e=1&f=0&g=0')
    expect(buildQuery({ a: undefined })).toBe('')
  })
})

describe('analytics deletes', () => {
  it('repeats a ranged clear until the server reports it complete, summing the counts', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(batch(false, 10_000))
      .mockResolvedValueOnce(batch(false, 10_000, 5))
      .mockResolvedValueOnce(batch(true, 42, 0, 3))
    vi.stubGlobal('fetch', fetchMock)
    const r = await adminClearAnalytics({ fromDate: '2026-01-01', toDate: '2026-01-31' })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    for (const call of fetchMock.mock.calls) {
      expect(call[0]).toBe('/api/admin/analytics/data?from_date=2026-01-01&to_date=2026-01-31')
      expect(call[1].method).toBe('DELETE')
    }
    expect(r).toMatchObject({
      complete: true, deleted_page_views: 20_042, deleted_slide_dwell_events: 5, deleted_tour_sessions: 3,
    })
  })

  it('stops when an incomplete batch deleted nothing', async () => {
    const fetchMock = vi.fn().mockResolvedValue(batch(false, 0))
    vi.stubGlobal('fetch', fetchMock)
    const r = await adminClearAnalyticsUser('a@example.com')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    // In the body, never the URL (access logs keep the request line).
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/analytics/user')
    expect((fetchMock.mock.calls[0][1].body as FormData).get('identifier')).toBe('a@example.com')
    expect(r.complete).toBe(false)
  })

  it('reports each acknowledged batch, and not the one that failed', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(batch(false, 10))
      .mockResolvedValueOnce(json({ detail: { code: 'adminDash.apiErr.analyticsDeleteFailed' } }, 500)))
    const onBatch = vi.fn()
    await expect(adminClearAnalytics({ fromDate: '2026-01-01', onBatch })).rejects.toBeTruthy()
    expect(onBatch).toHaveBeenCalledTimes(1)
    // A pseudonymize refused outright reports none.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      json({ detail: { code: 'adminDash.apiErr.analyticsSaltUnavailable' } }, 409)))
    const none = vi.fn()
    await expect(adminPseudonymizeAnalytics({ onBatch: none })).rejects.toBeTruthy()
    expect(none).not.toHaveBeenCalled()
  })

  it('surfaces a failed batch as an error', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(batch(false, 10))
      .mockResolvedValueOnce(json({ detail: { code: 'adminDash.apiErr.analyticsDeleteFailed' } }, 500)))
    await expect(adminClearAnalytics({ fromDate: '2026-01-01' })).rejects.toMatchObject({
      code: 'adminDash.apiErr.analyticsDeleteFailed',
    })
  })
})

describe('analytics clear scope', () => {
  it('sends all=1 for a wipe-everything and no date params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(batch(true, 7))
    vi.stubGlobal('fetch', fetchMock)
    await adminClearAnalytics({ all: true, fromDate: '2026-01-01' })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/analytics/data?all=1')
  })

  it('never sends an empty date param', async () => {
    const fetchMock = vi.fn().mockResolvedValue(batch(true, 1))
    vi.stubGlobal('fetch', fetchMock)
    await adminClearAnalytics({ fromDate: '', toDate: '2026-02-01' })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/analytics/data?to_date=2026-02-01')
  })
})

describe('analytics user preview + pseudonymize', () => {
  it('reads the purge preview for the identifier', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({
      subjects: ['alice', 'ldap:alice'], page_views: 3, slide_dwell_events: 1, tour_sessions: 0,
    }))
    vi.stubGlobal('fetch', fetchMock)
    const r = await adminPreviewAnalyticsUser('alice@example.com')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/analytics/user/preview')
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
    expect((fetchMock.mock.calls[0][1].body as FormData).get('identifier')).toBe('alice@example.com')
    expect(r.subjects).toEqual(['alice', 'ldap:alice'])
  })

  it('repeats the pseudonymize POST until complete, summing the counts', async () => {
    const step = (complete: boolean, pv: number, sde = 0, ts = 0) =>
      json({ updated: { page_views: pv, slide_dwell_events: sde, tour_sessions: ts }, complete })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(step(false, 500, 2))
      .mockResolvedValueOnce(step(true, 12, 0, 4))
    vi.stubGlobal('fetch', fetchMock)
    const r = await adminPseudonymizeAnalytics()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    for (const call of fetchMock.mock.calls) {
      expect(call[0]).toBe('/api/admin/analytics/pseudonymize')
      expect(call[1].method).toBe('POST')
    }
    expect(r).toEqual({ updated: { page_views: 512, slide_dwell_events: 2, tour_sessions: 4 }, complete: true })
  })

  it('stops when an incomplete pseudonymize batch converted nothing', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({
      updated: { page_views: 0, slide_dwell_events: 0, tour_sessions: 0 }, complete: false,
    }))
    vi.stubGlobal('fetch', fetchMock)
    const r = await adminPseudonymizeAnalytics()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(r.complete).toBe(false)
  })

  it('surfaces the missing-salt code', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      json({ detail: { code: 'adminDash.apiErr.analyticsSaltUnavailable' } }, 409)))
    await expect(adminPseudonymizeAnalytics()).rejects.toMatchObject({
      code: 'adminDash.apiErr.analyticsSaltUnavailable',
    })
  })
})

describe('analytics CSV export', () => {
  const filters = { user_email: 'a@example.com', client_ip: '10.0.0.1', kind: 'viewer' as const, actor_role: 'none' as const }

  it('sends the row filters for the raw recent dataset only', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ detail: 'stop' }, 500))
    vi.stubGlobal('fetch', fetchMock)
    await expect(adminDownloadAnalyticsCsv('top-slides', { mode: 'preset', days: 7 }, filters)).rejects.toThrow()
    await expect(adminDownloadAnalyticsCsv('recent', { mode: 'preset', days: 7 }, filters)).rejects.toThrow()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/analytics/export.csv?dataset=top-slides&days=7')
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/admin/analytics/export.csv?dataset=recent&days=7&user_email=a%40example.com&client_ip=10.0.0.1&kind=viewer&actor_role=none')
  })
})

describe('dwell beacon transport', () => {
  const payload = {
    slide_public_id: 'abc', session_id: '00000000-0000-4000-8000-000000000000',
    started_at: '2026-01-01T00:00:00+00:00', ended_at: '2026-01-01T00:01:00+00:00', dwell_ms: 60_000,
  }

  it('queues a text/plain blob with sendBeacon', () => {
    const sendBeacon = vi.fn().mockReturnValue(true)
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    Object.defineProperty(navigator, 'sendBeacon', { value: sendBeacon, configurable: true })
    recordSlideDwell(payload)
    expect(sendBeacon).toHaveBeenCalledTimes(1)
    const blob = sendBeacon.mock.calls[0][1] as Blob
    expect(blob.type).toMatch(/^text\/plain/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('falls back to a keepalive fetch when sendBeacon throws', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    Object.defineProperty(navigator, 'sendBeacon', {
      value: () => { throw new TypeError('refused') },
      configurable: true,
    })
    recordSlideDwell(payload)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/analytics/slide-dwell')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', keepalive: true })
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload)
  })
})
