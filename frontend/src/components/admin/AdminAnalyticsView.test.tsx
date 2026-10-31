// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The analytics endpoints are the view's only I/O; i18n is real.
const api = vi.hoisted(() => ({
  adminGetAnalyticsDashboard: vi.fn(),
  adminGetTourCompletion: vi.fn(),
  adminGetSlideDwell: vi.fn(),
  adminGetRecentPageViews: vi.fn(),
  adminClearAnalytics: vi.fn(),
  adminClearAnalyticsUser: vi.fn(),
  adminPreviewAnalyticsUser: vi.fn(),
  adminPseudonymizeAnalytics: vi.fn(),
  adminDownloadAnalyticsCsv: vi.fn(),
  isAdminFromToken: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { ApiCodeError, RateLimitError, throwApi } from '../../lib/api'
import type { AnalyticsDashboard, RecentPageView } from '../../lib/api'
import { todayInServerTz } from '../../lib/admin/datetime'
import { I18nProvider } from '../../lib/i18n'
import AdminAnalyticsView from './AdminAnalyticsView'

const TOKYO = { abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }

function dashboard(over: Partial<AnalyticsDashboard> = {}): AnalyticsDashboard {
  return {
    summary: {
      days: 30,
      totals: { top: 1, viewer: 2, tour: 0 },
      unique_ips: 1,
      unique_users: 1,
      daily: [{ day: '2026-01-02', top: 1, viewer: 2, tour: 0 }],
    },
    top_slides: [
      { slide_id: 1, public_id: '', title: 'Gone slide', catalog_id: '', views: 3, unique_ips: 1 },
      { slide_id: 2, public_id: 'p2', title: 'Live slide', catalog_id: '', views: 2, unique_ips: 1 },
    ],
    top_tours: [],
    referrers: [],
    ua_breakdown: [],
    top_users: [],
    server_tz: TOKYO,
    ...over,
  }
}

function recentRow(id: number): RecentPageView {
  return {
    id, occurred_at: '2026-01-01T20:00:00+00:00', kind: 'top',
    slide_id: null, slide_public_id: '', slide_title: '', tour_id: null, tour_public_id: '', tour_title: '',
    user_id: null, user_email: '', client_ip: `10.0.0.${id % 250}`, ua_class: 'desktop', user_agent: '',
    referrer_host: '', actor_role: null,
  }
}

beforeEach(() => {
  api.adminGetAnalyticsDashboard.mockResolvedValue(dashboard())
  api.adminGetTourCompletion.mockResolvedValue([])
  api.adminGetSlideDwell.mockResolvedValue([])
  api.adminGetRecentPageViews.mockResolvedValue([recentRow(1)])
  api.isAdminFromToken.mockReturnValue(true)
  vi.spyOn(window, 'alert').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.restoreAllMocks()
  document.body.style.overflow = ''
})

async function mount() {
  render(<I18nProvider><AdminAnalyticsView /></I18nProvider>)
  await screen.findByText('Live slide')
  await screen.findByText('10.0.0.1')
}

// The dashboard has finished loading again: the Refresh button reads
// "Loading…" while a fetch is in flight.
async function settled() {
  await screen.findByRole('button', { name: 'Refresh' })
  await act(async () => {})
}

// A destructive loop as lib/api runs it: `onBatch` for each acknowledged
// batch, then the outcome.
type Opts = { onBatch?: () => void }
const afterBatches = <T,>(n: number, outcome: () => Promise<T>) => async (...args: unknown[]) => {
  const opts = args.find((a): a is Opts => typeof a === 'object' && a !== null && 'onBatch' in a)
  for (let i = 0; i < n; i++) opts?.onBatch?.()
  return outcome()
}
const CLEAR_OK = {
  ok: true, complete: true, deleted: 1, deleted_page_views: 1, deleted_slide_dwell_events: 0, deleted_tour_sessions: 0,
}

describe('AdminAnalyticsView', () => {
  it('links only slides that still have a public page', async () => {
    await mount()
    expect(screen.getByText('Gone slide').closest('a')).toBeNull()
    expect(screen.getByText('Live slide').closest('a')?.getAttribute('href')).toBe('/slides/p2')
  })

  it('labels times and day buckets with the IANA zone name', async () => {
    await mount()
    expect(screen.getByRole('columnheader', { name: /Time\s*\(Asia\/Tokyo\)/ })).toBeTruthy()
    expect(screen.getByText(/days bucketed in Asia\/Tokyo/)).toBeTruthy()
    // The daily bars carry their counts as accessible text.
    expect(screen.getByRole('img', { name: '2026-01-02: Top page 1, Viewer 2, Tour 0' })).toBeTruthy()
  })

  it('clears the whole dashboard block when its request fails, side panels included', async () => {
    await mount()
    api.adminGetAnalyticsDashboard.mockRejectedValueOnce(new Error('boom'))
    api.adminGetTourCompletion.mockResolvedValueOnce([{
      tour_id: 9, public_id: 't9', slide_public_id: 'p2', tour_title: 'New window tour', slide_id: 2,
      slide_title: 'x', sessions_total: 1, sessions_completed: 1, completion_rate: 1,
      avg_completion_ms: 1000, avg_dwell_ms: 1000,
    }])
    fireEvent.click(screen.getByRole('button', { name: 'Last 7 days' }))
    await screen.findByText('boom')
    expect(screen.queryByText('Live slide')).toBeNull()
    expect(screen.queryByText('Total views')).toBeNull()
    expect(screen.queryByText('New window tour')).toBeNull()
  })

  it('gives the recent log its own error, clears its rows, and drops the error on success', async () => {
    await mount()
    api.adminGetRecentPageViews.mockRejectedValueOnce(new RateLimitError())
    fireEvent.change(screen.getByDisplayValue('All kinds'), { target: { value: 'viewer' } })
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Too many requests/)
    expect(screen.queryByText('10.0.0.1')).toBeNull()
    // The dashboard's own banner is untouched by a log failure.
    expect(screen.getByText('Live slide')).toBeTruthy()
    api.adminGetRecentPageViews.mockResolvedValueOnce([recentRow(2)])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText('10.0.0.2')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says when the recent log hit its row cap', async () => {
    api.adminGetRecentPageViews.mockResolvedValue(Array.from({ length: 200 }, (_, i) => recentRow(i + 1)))
    await mount()
    expect(screen.getByText(/Showing the latest 200 rows only/)).toBeTruthy()
  })

  it('opens the custom range without fetching, seeded with the preset\'s N days', async () => {
    await mount()
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Custom range' }))
    const start = screen.getByLabelText('Start date') as HTMLInputElement
    expect(start.value).toBe(todayInServerTz(TOKYO, 29))
    expect((screen.getByLabelText('End date') as HTMLInputElement).value).toBe(todayInServerTz(TOKYO))
    expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls)
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    expect(api.adminGetAnalyticsDashboard.mock.lastCall![0]).toEqual({
      mode: 'custom', start: todayInServerTz(TOKYO, 29), end: todayInServerTz(TOKYO),
    })
  })

  it('notes that Include bots does not reach the dwell / tour panels', async () => {
    await mount()
    expect(screen.queryByText(/"Include bots" does not apply here/)).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Include bots' }))
    expect(screen.getAllByText(/"Include bots" does not apply here/)).toHaveLength(2)
  })

  it('wipes everything with an explicit all scope and refreshes even after a failure', async () => {
    await mount()
    api.adminClearAnalytics.mockImplementationOnce(afterBatches(1, () => Promise.reject(new Error('half done'))))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'DELETE' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Yes, delete' }))
    await screen.findByText('half done')
    expect(api.adminClearAnalytics).toHaveBeenCalledWith({ all: true, onBatch: expect.any(Function) })
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    // The refresh's own start (which empties the dashboard banner) leaves the
    // action's failure on screen.
    await settled()
    expect(screen.getByRole('alert').textContent).toContain('half done')
  })

  it('keeps a failed ranged clear\'s reason past the refresh, until dismissed', async () => {
    await mount()
    // The first request is refused part-way through: a 500 after committed
    // chunks, so the dashboard is refreshed although no batch came back.
    api.adminClearAnalytics.mockRejectedValueOnce(new ApiCodeError('adminDash.apiErr.analyticsDeleteFailed'))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(dialog.querySelector('input[type="date"]')!, { target: { value: '2026-01-01' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    await settled()
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('Failed to delete analytics data')
    fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Failed to delete analytics data')).toBeNull()
  })

  it('shows the matched subjects and row counts before a user purge', async () => {
    await mount()
    vi.spyOn(window, 'prompt').mockReturnValue('alice')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.adminPreviewAnalyticsUser.mockResolvedValue({
      subjects: ['alice', 'ldap:alice'], page_views: 1200, slide_dwell_events: 3, tour_sessions: 1,
    })
    api.adminClearAnalyticsUser.mockResolvedValue({
      ok: true, complete: true, deleted: 1204,
      deleted_page_views: 1200, deleted_slide_dwell_events: 3, deleted_tour_sessions: 1,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Purge user…' }))
    await waitFor(() => expect(api.adminClearAnalyticsUser).toHaveBeenCalledWith('alice', expect.anything()))
    const msg = confirm.mock.calls[0][0] as string
    expect(msg).toContain('ldap:alice')
    expect(msg).toContain('Rows to delete: 1,204 (page views 1,200, slide dwell 3, tour sessions 1)')
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('Deleted 1,204 analytics rows for the requested user.'))
  })

  it('does not send a purge the preview found nothing for', async () => {
    await mount()
    vi.spyOn(window, 'prompt').mockReturnValue('nobody')
    const confirm = vi.spyOn(window, 'confirm')
    api.adminPreviewAnalyticsUser.mockResolvedValue({
      subjects: [], page_views: 0, slide_dwell_events: 0, tour_sessions: 0,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Purge user…' }))
    await waitFor(() => expect(window.alert).toHaveBeenCalled())
    expect(confirm).not.toHaveBeenCalled()
    expect(api.adminClearAnalyticsUser).not.toHaveBeenCalled()
  })

  it('pseudonymizes after a confirm, reports the total and refreshes', async () => {
    await mount()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.adminPseudonymizeAnalytics.mockImplementation(afterBatches(1, () => Promise.resolve({
      updated: { page_views: 1000, slide_dwell_events: 20, tour_sessions: 5 }, complete: true,
    })))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Pseudonymize plaintext rows…' }))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('Pseudonymized 1,025 analytics rows.'))
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
  })

  it('shows the missing-salt reason when pseudonymizing is refused, and skips the refresh', async () => {
    await mount()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.adminPseudonymizeAnalytics.mockRejectedValue(new ApiCodeError('adminDash.apiErr.analyticsSaltUnavailable'))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Pseudonymize plaintext rows…' }))
    await screen.findByText(/hashing salt is configured/)
    // Refused before any batch: nothing changed, so nothing is refetched —
    // and the reason stays.
    await settled()
    await act(async () => {})
    expect(screen.getByText(/hashing salt is configured/)).toBeTruthy()
    expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls)
  })

  it('refreshes after a pseudonymize that failed past its first batch, keeping the reason', async () => {
    await mount()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    // A rate limit is a refusal, but the batch before it was committed.
    api.adminPseudonymizeAnalytics.mockImplementation(afterBatches(1, () => Promise.reject(new RateLimitError())))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Pseudonymize plaintext rows…' }))
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    await settled()
    expect(screen.getByRole('alert').textContent).toMatch(/Too many requests/)
  })

  it('links only tours whose tour and slide both still have a public page', async () => {
    const tour = (id: number, slide: string, pub: string, title: string) => ({
      tour_id: id, public_id: pub, slide_public_id: slide, tour_title: title,
      slide_id: id, slide_title: 's', views: 1, unique_ips: 1,
    })
    api.adminGetAnalyticsDashboard.mockResolvedValue(dashboard({
      top_tours: [tour(1, 'p2', '', 'Gone tour'), tour(2, '', 't3', 'Orphan tour'), tour(3, 'p2', 't3', 'Live tour')],
    }))
    await mount()
    expect(screen.getByText('Gone tour').closest('a')).toBeNull()
    expect(screen.getByText('Orphan tour').closest('a')).toBeNull()
    expect(screen.getByText('Live tour').closest('a')?.getAttribute('href')).toBe('/slides/p2/tours/t3')
  })

  it('keeps the dashboard\'s load failure when a CSV is downloaded', async () => {
    await mount()
    api.adminGetAnalyticsDashboard.mockRejectedValueOnce(new Error('boom'))
    fireEvent.click(screen.getByRole('button', { name: 'Last 7 days' }))
    await screen.findByText('boom')
    api.adminDownloadAnalyticsCsv.mockResolvedValueOnce(undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
    await waitFor(() => expect(api.adminDownloadAnalyticsCsv).toHaveBeenCalled())
    await screen.findByRole('button', { name: 'Download CSV' })
    expect(screen.getByRole('alert').textContent).toBe('boom')
  })

  it('shows a CSV failure beside its button, past a dashboard refresh', async () => {
    await mount()
    api.adminDownloadAnalyticsCsv.mockRejectedValueOnce(new Error('csv broke'))
    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
    await screen.findByText('csv broke')
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    await settled()
    expect(screen.getByRole('alert').textContent).toBe('csv broke')
  })

  it('treats a role rejection as nothing changed, in the UI\'s words', async () => {
    await mount()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    // require_admin's 403, as throwApi turns it into an error.
    const forbidden = await throwApi(new Response(JSON.stringify({ detail: 'Forbidden' }), {
      status: 403, headers: { 'content-type': 'application/json' },
    }), 'x').catch((e) => e)
    api.adminPseudonymizeAnalytics.mockRejectedValue(forbidden)
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Pseudonymize plaintext rows…' }))
    await screen.findByText(/Only administrators can change analytics data/)
    await settled()
    await act(async () => {})
    expect(screen.queryByText('Forbidden')).toBeNull()
    expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls)
  })

  it('says an IP-filter refusal is the IP filter\'s, not a role rejection', async () => {
    await mount()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    // ip_filter_middleware's 403, as throwApi turns it into an error.
    const denied = await throwApi(new Response(JSON.stringify({ detail: 'Access denied' }), {
      status: 403, headers: { 'content-type': 'application/json' },
    }), 'x').catch((e) => e)
    api.adminPseudonymizeAnalytics.mockRejectedValue(denied)
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Pseudonymize plaintext rows…' }))
    await screen.findByText(/IP access control refused this request/)
    await settled()
    expect(screen.queryByText(/Only administrators/)).toBeNull()
    expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls)
  })

  it('shortens hashed IPs and users in the recent log, filtering on the full digest', async () => {
    const ipDigest = 'a'.repeat(12) + 'b'.repeat(52)
    const userDigest = 'c'.repeat(12) + 'd'.repeat(52)
    api.adminGetRecentPageViews.mockResolvedValue([
      recentRow(1),
      { ...recentRow(2), client_ip: ipDigest, user_email: userDigest },
    ])
    await mount()
    // A raw IP is shown whole.
    expect(screen.getByText('10.0.0.1')).toBeTruthy()
    const ip = screen.getByText('aaaaaaaaaaaa…')
    expect(ip.getAttribute('title')).toBe(ipDigest)
    expect(screen.queryByText(ipDigest)).toBeNull()
    fireEvent.click(ip)
    expect((screen.getByPlaceholderText('Filter by IP') as HTMLInputElement).value).toBe(ipDigest)
    const user = screen.getByText('cccccccccccc…')
    expect(user.getAttribute('title')).toBe(userDigest)
    fireEvent.click(user)
    expect((screen.getByPlaceholderText('Filter by user') as HTMLInputElement).value).toBe(userDigest)
  })

  it('shortens hashed users in the top-users ranking, filtering on the full digest', async () => {
    const userDigest = 'e'.repeat(12) + 'f'.repeat(52)
    api.adminGetAnalyticsDashboard.mockResolvedValue(dashboard({
      top_users: [
        { user_email: userDigest, user_id: null, views: 4, unique_days: 2, unique_ips: 1 },
        { user_email: 'alice@example.org', user_id: 7, views: 2, unique_days: 1, unique_ips: 1 },
      ],
    }))
    await mount()
    // A name (Identified mode) is shown whole.
    expect(screen.getByRole('button', { name: 'alice@example.org' })).toBeTruthy()
    const user = screen.getByText('eeeeeeeeeeee…')
    expect(user.getAttribute('title')).toBe(userDigest)
    expect(screen.queryByText(userDigest)).toBeNull()
    fireEvent.click(user)
    expect((screen.getByPlaceholderText('Filter by user') as HTMLInputElement).value).toBe(userDigest)
  })

  it('floors the recent log\'s page column in px, not in the header\'s small em', async () => {
    // jsdom has no layout; what can be checked is that the floor is an
    // absolute width that holds six 12px full-width characters ("トップページ"),
    // not '6em' of the 10px header font (60px).
    await mount()
    const th = screen.getByRole('columnheader', { name: 'Page' })
    expect(th.style.minWidth).toMatch(/^\d+px$/)
    expect(parseFloat(th.style.minWidth)).toBeGreaterThanOrEqual(6 * 12)
  })

  it('reports a purge whose delete failed and refreshes, keeping the reason', async () => {
    await mount()
    vi.spyOn(window, 'prompt').mockReturnValue('alice')
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.adminPreviewAnalyticsUser.mockResolvedValue({
      subjects: ['alice'], page_views: 5, slide_dwell_events: 0, tour_sessions: 0,
    })
    // An uncoded failure may follow committed batches: refreshed.
    api.adminClearAnalyticsUser.mockRejectedValue(new Error('delete broke'))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Purge user…' }))
    await screen.findByText('delete broke')
    await waitFor(() => expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls + 1))
    await settled()
    expect(screen.getByRole('alert').textContent).toContain('delete broke')
    expect(window.alert).not.toHaveBeenCalled()
  })

  it('does not refresh after a purge whose delete was never sent', async () => {
    await mount()
    vi.spyOn(window, 'prompt').mockReturnValue('alice')
    api.adminPreviewAnalyticsUser.mockRejectedValue(new Error('preview down'))
    const calls = api.adminGetAnalyticsDashboard.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Purge user…' }))
    await screen.findByText(/Could not check which analytics rows match/)
    await settled()
    expect(screen.getByRole('alert').textContent).toMatch(/Nothing was deleted/)
    expect(api.adminClearAnalyticsUser).not.toHaveBeenCalled()
    expect(api.adminGetAnalyticsDashboard.mock.calls.length).toBe(calls)
  })
})
