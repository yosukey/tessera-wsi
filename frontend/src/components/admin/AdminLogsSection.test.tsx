// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The two list endpoints are the sections' only I/O; i18n and ui are real.
const api = vi.hoisted(() => ({
  adminGetErrorLogs: vi.fn(),
  adminGetErrorLog: vi.fn(),
  adminClearErrorLogs: vi.fn(),
  adminDownloadErrorLogsCsv: vi.fn(),
  adminGetAuditLogs: vi.fn(),
  adminDownloadAuditLogsCsv: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { ApiCodeError, RateLimitError } from '../../lib/api'
import { I18nProvider } from '../../lib/i18n'
import AdminLogsSection from './AdminLogsSection'
import AdminHistorySection from './AdminHistorySection'

// 2026-01-01T20:00Z is 2026-01-02 05:00 in Tokyo, whatever zone the test
// runner (and so the browser-local formatter) happens to be in.
const OCCURRED_AT = '2026-01-01T20:00:00+00:00'
const TOKYO = { abbr: 'JST', tz_name: 'Asia/Tokyo', utc_offset: '+09:00' }

beforeEach(() => {
  api.adminGetErrorLogs.mockResolvedValue({
    total: 1,
    server_tz: TOKYO,
    items: [{
      id: 1, occurred_at: OCCURRED_AT, level: 'ERROR', logger: 'wsi_app.x', message: 'boom',
      module: null, func: null, line: null, has_traceback: false, request_path: null, request_method: null,
    }],
  })
  api.adminGetAuditLogs.mockResolvedValue({
    total: 1,
    server_tz: TOKYO,
    items: [{
      id: 1, occurred_at: OCCURRED_AT, actor_id: null, actor_username: 'root', actor_role: 'admin',
      action: 'slide.update', target: '1', details: {},
    }],
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function mount(node: React.ReactNode) {
  return render(<I18nProvider>{node}</I18nProvider>)
}

describe('AdminLogsSection', () => {
  it('shows times in the server timezone the date filters use', async () => {
    mount(<AdminLogsSection />)
    await screen.findByText('boom')
    expect(screen.getByText(/01\/02\/2026, 05:00:00/)).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: /Time \(Asia\/Tokyo\)/ })).toBeTruthy()
  })

  it('offers INFO and DEBUG in the level filter', async () => {
    mount(<AdminLogsSection />)
    await screen.findByText('boom')
    const options = screen.getAllByRole('option').map(o => o.textContent)
    expect(options).toEqual(expect.arrayContaining(['ERROR', 'WARNING', 'CRITICAL', 'INFO', 'DEBUG']))
  })
})

describe('AdminHistorySection', () => {
  it('shows times in the server timezone the date filters use', async () => {
    mount(<AdminHistorySection />)
    await screen.findByText('root')
    expect(screen.getByText(/01\/02\/2026, 05:00:00/)).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: /Time \(Asia\/Tokyo\)/ })).toBeTruthy()
  })
  it('labels failed app logins with their own category', async () => {
    api.adminGetAuditLogs.mockResolvedValue({
      total: 1,
      server_tz: TOKYO,
      items: [{
        id: 2, occurred_at: OCCURRED_AT, actor_id: null, actor_username: 'alice', actor_role: null,
        action: 'app.login.failure', target: null, details: {},
      }],
    })
    mount(<AdminHistorySection />)
    await screen.findByText('alice')
    expect(screen.getAllByText('App login').length).toBeGreaterThan(0)
  })
})

describe('list errors', () => {
  it('says why a request failed when the server or the rate limit says so', async () => {
    api.adminGetAuditLogs.mockRejectedValue(new ApiCodeError('adminHistory.invalidDate', { value: '2026-13-01' }))
    mount(<AdminHistorySection />)
    expect(await screen.findByText('Invalid date: 2026-13-01')).toBeTruthy()
    cleanup()

    api.adminGetErrorLogs.mockRejectedValue(new RateLimitError())
    mount(<AdminLogsSection />)
    expect(await screen.findByText(/Too many requests/i)).toBeTruthy()
  })

  it('falls back to the generic message for anything else', async () => {
    api.adminGetErrorLogs.mockRejectedValue(new Error('logs failed (HTTP 502)'))
    mount(<AdminLogsSection />)
    expect(await screen.findByText('Failed to load logs.')).toBeTruthy()
  })
})

function lastCall(fn: { mock: { calls: any[][] } }): any[] {
  return fn.mock.calls[fn.mock.calls.length - 1]
}

function logItem(id: number, extra: Record<string, unknown> = {}) {
  return {
    id, occurred_at: OCCURRED_AT, level: 'ERROR', logger: 'wsi_app.x', message: `msg ${id}`,
    module: null, func: null, line: null, has_traceback: false, request_path: null, request_method: null,
    ...extra,
  }
}

function logsPage(items: unknown[], extra: Record<string, unknown> = {}) {
  return { total: items.length, snapshot_id: 100, newer: 0, level_counts: {}, server_tz: TOKYO, items, ...extra }
}

describe('AdminLogsSection behaviour', () => {
  it('labels the time column with the zone name, not a summer-time abbreviation', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(1)], {
      server_tz: { abbr: 'CET', tz_name: 'Europe/Berlin', utc_offset: '+01:00' },
    }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 1')
    expect(screen.getByRole('columnheader', { name: /Time \(Europe\/Berlin\)/ })).toBeTruthy()
    expect(screen.queryByRole('columnheader', { name: /CET/ })).toBeNull()
  })

  it('fetches the traceback only when the row is expanded', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(7, { has_traceback: true })]))
    api.adminGetErrorLog.mockResolvedValue({ ...logItem(7), has_traceback: true, traceback: 'Traceback: KeyError' })
    mount(<AdminLogsSection />)
    await screen.findByText('msg 7')
    expect(api.adminGetErrorLog).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Show traceback/i }))
    expect(await screen.findByText('Traceback: KeyError')).toBeTruthy()
    expect(api.adminGetErrorLog).toHaveBeenCalledWith(7, expect.anything())
    // Collapsing and re-opening reuses what was fetched.
    fireEvent.click(screen.getByRole('button', { name: /Hide traceback/i }))
    fireEvent.click(screen.getByRole('button', { name: /Show traceback/i }))
    expect(api.adminGetErrorLog).toHaveBeenCalledTimes(1)
  })

  it('reads later pages from the first page\'s snapshot and offers the newer rows', async () => {
    const many = Array.from({ length: 50 }, (_, i) => logItem(200 - i))
    api.adminGetErrorLogs
      .mockResolvedValueOnce(logsPage(many, { total: 60, snapshot_id: 200 }))
      .mockResolvedValueOnce(logsPage([logItem(150)], { total: 60, snapshot_id: 200, newer: 3 }))
      .mockResolvedValue(logsPage([logItem(203)], { total: 63, snapshot_id: 203 }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 200')
    expect(api.adminGetErrorLogs.mock.calls[0][0].max_id).toBeUndefined()
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    await screen.findByText('msg 150')
    expect(api.adminGetErrorLogs.mock.calls[1][0]).toMatchObject({ offset: 50, max_id: 200 })
    expect(screen.getByText(/3 new entries/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show them' }))
    await screen.findByText('msg 203')
    expect(api.adminGetErrorLogs.mock.calls[2][0]).toMatchObject({ offset: 0 })
    expect(api.adminGetErrorLogs.mock.calls[2][0].max_id).toBeUndefined()
  })

  it('steps back to the last page when the current one emptied', async () => {
    const many = Array.from({ length: 50 }, (_, i) => logItem(200 - i))
    api.adminGetErrorLogs
      .mockResolvedValueOnce(logsPage(many, { total: 60 }))
      .mockResolvedValueOnce(logsPage([], { total: 40 }))
      .mockResolvedValue(logsPage([logItem(5)], { total: 40 }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 200')
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    await screen.findByText('msg 5')
    expect(api.adminGetErrorLogs.mock.calls[2][0].offset).toBe(0)
  })

  it('filters by a level from its count', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(1)], { level_counts: { ERROR: 4, WARNING: 9 } }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 1')
    fireEvent.click(screen.getByText('9').closest('button')!)
    await waitFor(() => expect(lastCall(api.adminGetErrorLogs)[0].level).toBe('WARNING'))
  })

  it('clears up to the snapshot after confirmation', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(1)], { snapshot_id: 321 }))
    api.adminClearErrorLogs.mockResolvedValue({ deleted: 12 })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    mount(<AdminLogsSection />)
    await screen.findByText('msg 1')
    fireEvent.click(screen.getByRole('button', { name: 'Clear logs' }))
    expect(api.adminClearErrorLogs).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Clear logs' }))
    expect(await screen.findByText('Cleared 12 row(s).')).toBeTruthy()
    expect(api.adminClearErrorLogs).toHaveBeenCalledWith(321)
    confirm.mockRestore()
  })

  it('exports with the traceback choice and can cancel', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(1)]))
    let seenSignal: AbortSignal | undefined
    api.adminDownloadErrorLogsCsv.mockImplementation((_params: unknown, opts: { signal: AbortSignal }) => {
      seenSignal = opts.signal
      return new Promise((_resolve, reject) => {
        opts.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })
    mount(<AdminLogsSection />)
    await screen.findByText('msg 1')
    fireEvent.click(screen.getByLabelText('Include tracebacks'))
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(api.adminDownloadErrorLogsCsv.mock.calls[0][0]).toMatchObject({ include_traceback: false })
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(seenSignal?.aborted).toBe(true)
    expect(await screen.findByRole('button', { name: 'Export CSV' })).toBeTruthy()
    expect(screen.queryByText(/Failed to export/)).toBeNull()
  })
})

describe('AdminHistorySection filters', () => {
  it('sends the target and system filters', async () => {
    api.adminGetAuditLogs.mockResolvedValue({
      total: 0, snapshot_id: 5, newer: 0, server_tz: TOKYO, items: [],
    })
    mount(<AdminHistorySection />)
    await screen.findByText(/No history/i)
    fireEvent.change(screen.getByPlaceholderText('contains…'), { target: { value: ' slide-9 ' } })
    fireEvent.click(screen.getByLabelText('System only'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(lastCall(api.adminGetAuditLogs)[0]).toMatchObject({
      target: 'slide-9', system: true, offset: 0,
    }))
  })
})

describe('export busy', () => {
  it('says another export is running rather than "too many requests"', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(1)]))
    api.adminDownloadErrorLogsCsv.mockRejectedValue(new ApiCodeError('adminExport.busy'))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 1')
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByText('Another export is already running. Try again when it has finished.')).toBeTruthy()
  })
})

describe('AdminLogsSection paging after a clear and the NEW marker', () => {
  afterEach(() => { try { localStorage.clear() } catch { /* */ } })

  it('goes back to page 1 when everything was cleared under a later page', async () => {
    const many = Array.from({ length: 50 }, (_, i) => logItem(200 - i))
    api.adminGetErrorLogs
      .mockResolvedValueOnce(logsPage(many, { total: 60 }))
      .mockResolvedValueOnce(logsPage([], { total: 0 }))
      .mockResolvedValue(logsPage([logItem(900, { message: 'cleared marker' })], { total: 1 }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 200')
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    await screen.findByText('cleared marker')
    expect(api.adminGetErrorLogs.mock.calls[2][0].offset).toBe(0)
  })

  it('marks rows newer than the last visit', async () => {
    localStorage.setItem('wsi.adminLogs.lastSeenId', '3')
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(5), logItem(2)], { snapshot_id: 5 }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 5')
    expect(screen.getAllByText('NEW')).toHaveLength(1)
    await waitFor(() => expect(localStorage.getItem('wsi.adminLogs.lastSeenId')).toBe('5'))
  })

  it('drops the NEW baseline when ids went backwards (the database was replaced)', async () => {
    localStorage.setItem('wsi.adminLogs.lastSeenId', '500')
    api.adminGetErrorLogs.mockResolvedValue(logsPage([logItem(10)], { snapshot_id: 10 }))
    mount(<AdminLogsSection />)
    await screen.findByText('msg 10')
    expect(screen.queryByText('NEW')).toBeNull()
    await waitFor(() => expect(localStorage.getItem('wsi.adminLogs.lastSeenId')).toBe('10'))
  })
})

describe('AdminLogsSection under StrictMode', () => {
  it('a filter change goes back to page 1 even with the double render', async () => {
    const many = Array.from({ length: 50 }, (_, i) => logItem(200 - i))
    api.adminGetErrorLogs.mockResolvedValue(logsPage(many, { total: 300, level_counts: { ERROR: 300 } }))
    render(<React.StrictMode><I18nProvider><AdminLogsSection /></I18nProvider></React.StrictMode>)
    await screen.findByText('msg 200')
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => expect(lastCall(api.adminGetErrorLogs)[0].offset).toBe(50))
    fireEvent.click(screen.getByText('300').closest('button')!)
    await waitFor(() => expect(lastCall(api.adminGetErrorLogs)[0].level).toBe('ERROR'))
    expect(lastCall(api.adminGetErrorLogs)[0].offset).toBe(0)
    expect(lastCall(api.adminGetErrorLogs)[0].max_id).toBeUndefined()
  })

  it('keeps the Clear button usable when the filtered list is empty', async () => {
    api.adminGetErrorLogs.mockResolvedValue(logsPage([], { total: 0, snapshot_id: 40 }))
    mount(<AdminLogsSection />)
    await screen.findByText('No log entries match.')
    expect((screen.getByRole('button', { name: 'Clear logs' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
