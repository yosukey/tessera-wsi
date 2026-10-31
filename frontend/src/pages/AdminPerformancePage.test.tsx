// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminMe: vi.fn(),
  adminListSlides: vi.fn(),
  adminPerfListRuns: vi.fn(),
  adminPerfGetRun: vi.fn(),
  adminPerfStartRun: vi.fn(),
  adminPerfExport: vi.fn(),
  clearAdminSession: vi.fn(),
}))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../lib/i18n'
import { RateLimitError, UnauthorizedError, type PerfRunDetail, type PerfRunSummary } from '../lib/api'
import AdminPerformancePage from './AdminPerformancePage'

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/performance']}>
      <I18nProvider>
        <Routes>
          <Route path="/admin/performance" element={<AdminPerformancePage />} />
          <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
          <Route path="/admin/dashboard" element={<div>DASHBOARD-PAGE</div>} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  )
}

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const admin = { id: 1, username: 'a', role: 'admin', role_permissions: {} }

function summary(id: number, over: Partial<PerfRunSummary> = {}): PerfRunSummary {
  return {
    id, kind: 'server', state: 'succeeded', created_at: '2026-10-01T10:00:00Z', finished_at: '2026-10-01T10:05:00Z',
    actor: 'a', params: { slides: [] }, progress: null, cancel_requested: false, error: null, summary: {}, ...over,
  }
}
const detail = (id: number, over: Partial<PerfRunSummary> = {}): PerfRunDetail => ({ ...summary(id, over), result: null })

beforeEach(() => {
  // jsdom has no layout: opening a run scrolls its result into view.
  Element.prototype.scrollIntoView = () => {}
  api.adminMe.mockResolvedValue(admin)
  api.adminListSlides.mockResolvedValue([])
  api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe('AdminPerformancePage loading', () => {
  it('keeps a failed permission check on screen, with a retry, whatever else loads', async () => {
    api.adminMe.mockRejectedValueOnce(new RateLimitError()).mockResolvedValue(admin)
    mount()
    expect(await screen.findByText('Could not check your access to this page.')).toBeTruthy()
    // The history loading fine afterwards must not hide it.
    await waitFor(() => expect(api.adminPerfListRuns).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.getByText('Could not check your access to this page.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Run server test' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'Run server test' })).toBeTruthy()
    expect(screen.queryByText('Could not check your access to this page.')).toBeNull()
  })

  it('offers a retry when the slide list fails', async () => {
    api.adminListSlides.mockRejectedValueOnce(new Error('admin slides failed')).mockResolvedValue([])
    mount()
    expect(await screen.findByText('Failed to load the slide list.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText('Failed to load the slide list.')).toBeNull())
    expect(api.adminListSlides).toHaveBeenCalledTimes(2)
  })

  it('drops a history answer overtaken by a newer one', async () => {
    // The first load hangs; meanwhile a server run is started, finishes and
    // the history is reloaded. The first answer, landing last, is stale.
    const first = deferred<{ runs: PerfRunSummary[]; running_id: number | null }>()
    api.adminPerfListRuns
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce({ runs: [summary(9, { state: 'running' })], running_id: 9 })
      .mockResolvedValue({ runs: [summary(9)], running_id: null })
    api.adminPerfStartRun.mockResolvedValue({ id: 9 })
    api.adminPerfGetRun.mockResolvedValue(detail(9))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Run server test' }))
    expect(await screen.findByText('Server test #9')).toBeTruthy()
    await waitFor(() => expect(api.adminPerfListRuns).toHaveBeenCalledTimes(3))
    await act(async () => { first.resolve({ runs: [summary(5)], running_id: 5 }) })
    expect(screen.getByLabelText('Select run #9')).toBeTruthy()
    expect(screen.queryByLabelText('Select run #5')).toBeNull()
    expect(api.adminPerfGetRun.mock.calls.map((c) => c[0])).not.toContain(5)
  })
})

describe('AdminPerformancePage run detail', () => {
  it('keeps a failed run detail on screen past a history refresh, and retries that run', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3)], running_id: null })
    api.adminPerfGetRun.mockRejectedValueOnce(new Error('perf run failed')).mockResolvedValue(detail(3))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    expect(await screen.findByText('Failed to load measurement #3.')).toBeTruthy()
    // The history loaded fine and says nothing about the run's own failure.
    expect(screen.queryByText('Failed to load the measurement history.')).toBeNull()
    const lists = api.adminPerfListRuns.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Server test #3')).toBeTruthy()
    // Retry asked for the run again, not for the list.
    expect(api.adminPerfGetRun.mock.calls.map((c) => c[0])).toEqual([3, 3])
    expect(api.adminPerfListRuns.mock.calls.length).toBe(lists)
    expect(screen.queryByText('Failed to load measurement #3.')).toBeNull()
  })

  it('says a polled run vanished even after the history refresh that follows', async () => {
    const { ApiCodeError } = await import('../lib/api')
    api.adminPerfListRuns
      .mockResolvedValueOnce({ runs: [summary(9, { state: 'running' })], running_id: 9 })
      .mockResolvedValue({ runs: [], running_id: null })
    api.adminPerfGetRun.mockRejectedValue(new ApiCodeError('adminPerf.err.notFound'))
    mount()
    expect(await screen.findByText('This measurement no longer exists.')).toBeTruthy()
    await waitFor(() => expect(api.adminPerfListRuns).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(screen.getByText('This measurement no longer exists.')).toBeTruthy()
    // Retrying a deleted run cannot help; it can only be dismissed.
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('This measurement no longer exists.')).toBeNull()
  })
})

describe('AdminPerformancePage poll', () => {
  it('does not replace a run the admin opened while it was going', async () => {
    const poll = deferred<PerfRunDetail>()
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(9, { state: 'running' })], running_id: 9 })
    api.adminPerfGetRun.mockImplementation((id: number) => (id === 9 ? poll.promise : Promise.resolve(detail(3))))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    expect(await screen.findByText('Server test #3')).toBeTruthy()
    await act(async () => { poll.resolve(detail(9)) })
    expect(screen.getByText('Server test #3')).toBeTruthy()
    expect(screen.queryByText('Server test #9')).toBeNull()
    // Both requests can be cancelled.
    for (const call of api.adminPerfGetRun.mock.calls) expect(call[1]?.signal).toBeInstanceOf(AbortSignal)
  })

  it('backs off while the server keeps failing, says so, and recovers', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: 9 })
    api.adminPerfGetRun.mockRejectedValue(new Error('perf run failed'))
    mount()
    const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
    await flush()
    expect(api.adminPerfGetRun).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/Cannot reach the server to check on the running test/)).toBeTruthy()
    // 3 s, 6 s, 12 s, 24 s, then capped at 30 s.
    const waits = [3000, 6000, 12000, 24000, 30000, 30000]
    for (const [i, ms] of waits.entries()) {
      await flush(ms - 1)
      expect(api.adminPerfGetRun).toHaveBeenCalledTimes(i + 1)
      await flush(1)
      expect(api.adminPerfGetRun).toHaveBeenCalledTimes(i + 2)
    }
    api.adminPerfGetRun.mockResolvedValue(detail(9, { state: 'running', progress: { phase: 'tiles', percent: 50 } }))
    await flush(30000)
    expect(screen.queryByText(/Cannot reach the server/)).toBeNull()
    const n = api.adminPerfGetRun.mock.calls.length
    await flush(1500)
    expect(api.adminPerfGetRun).toHaveBeenCalledTimes(n + 1)
  })

  it('starts the backoff over after a poll gets through', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: 9 })
    const running = detail(9, { state: 'running', progress: { phase: 'tiles', percent: 50 } })
    api.adminPerfGetRun
      .mockRejectedValueOnce(new Error('perf run failed'))
      .mockRejectedValueOnce(new Error('perf run failed'))
      .mockResolvedValueOnce(running)
      .mockRejectedValue(new Error('perf run failed'))
    mount()
    const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
    await flush()
    // Failed at 0 and 3 s; through at 9 s; failed again at 10.5 s.
    await flush(3000 + 6000 + 1500)
    expect(api.adminPerfGetRun).toHaveBeenCalledTimes(4)
    // The first failure after a success waits 3 s again, not the 12 s the
    // count from before the success would give.
    await flush(2999)
    expect(api.adminPerfGetRun).toHaveBeenCalledTimes(4)
    await flush(1)
    expect(api.adminPerfGetRun).toHaveBeenCalledTimes(5)
  })

  it('keeps the poll-failure live region mounted, changing only its text', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const poll = deferred<PerfRunDetail>()
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: 9 })
    api.adminPerfGetRun.mockReturnValueOnce(poll.promise)
    mount()
    const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
    await flush()
    const before = screen.getAllByRole('status')
    expect(before.some((el) => /Cannot reach the server/.test(el.textContent || ''))).toBe(false)
    await act(async () => { poll.reject(new Error('perf run failed')) })
    await flush()
    const region = screen.getByText(/Cannot reach the server to check on the running test/)
    // Announced by a region that was already there, not one inserted with it.
    expect(before).toContain(region)
  })

  it('shows the finished result after the admin opened and closed another run meanwhile', async () => {
    const poll = deferred<PerfRunDetail>()
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(9, { state: 'running' })], running_id: 9 })
    api.adminPerfGetRun.mockImplementation((id: number) => (id === 9 ? poll.promise : Promise.resolve(detail(3))))
    mount()
    // The poll is under way before the admin does anything.
    await waitFor(() => expect(api.adminPerfGetRun).toHaveBeenCalledWith(9, expect.anything()))
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    expect(await screen.findByText('Server test #3')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByText('Server test #3')).toBeNull()
    await act(async () => { poll.resolve(detail(9)) })
    expect(screen.getByText('Server test #9')).toBeTruthy()
  })

  it('shows the finished result after a run the admin tried to open failed to load', async () => {
    const poll = deferred<PerfRunDetail>()
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(9, { state: 'running' })], running_id: 9 })
    api.adminPerfGetRun.mockImplementation((id: number) => (id === 9 ? poll.promise : Promise.reject(new Error('perf run failed'))))
    mount()
    await waitFor(() => expect(api.adminPerfGetRun).toHaveBeenCalledWith(9, expect.anything()))
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    expect(await screen.findByText('Failed to load measurement #3.')).toBeTruthy()
    await act(async () => { poll.resolve(detail(9)) })
    expect(screen.getByText('Server test #9')).toBeTruthy()
  })

  it('does not replace a run the admin is still opening when the poll finishes', async () => {
    const poll = deferred<PerfRunDetail>()
    const opening = deferred<PerfRunDetail>()
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(9, { state: 'running' })], running_id: 9 })
    api.adminPerfGetRun.mockImplementation((id: number) => (id === 9 ? poll.promise : opening.promise))
    mount()
    await waitFor(() => expect(api.adminPerfGetRun).toHaveBeenCalledWith(9, expect.anything()))
    // Nothing is on screen yet, but run #3 is on its way: the finished run
    // must not take its place (nor cancel its request).
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    await act(async () => { poll.resolve(detail(9)) })
    expect(screen.queryByText('Server test #9')).toBeNull()
    await act(async () => { opening.resolve(detail(3)) })
    expect(screen.getByText('Server test #3')).toBeTruthy()
    expect(screen.queryByText('Server test #9')).toBeNull()
  })

  it('returns to the login screen when the session is lost mid-run', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: 9 })
    api.adminPerfGetRun.mockRejectedValue(new UnauthorizedError(401))
    mount()
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(screen.queryByText(/Failed to load measurement/)).toBeNull()
  })

  it('goes to the dashboard, not the login, when the role loses the page mid-run', async () => {
    // A 403 comes with a token that still works: the login screen would only
    // let the admin back in to be refused again.
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: 9 })
    api.adminPerfGetRun.mockRejectedValue(new UnauthorizedError(403))
    mount()
    expect(await screen.findByText('DASHBOARD-PAGE')).toBeTruthy()
    expect(screen.queryByText('LOGIN-PAGE')).toBeNull()
    expect(screen.queryByText(/Failed to load measurement/)).toBeNull()
  })
})

describe('AdminPerformancePage export', () => {
  it('exports the open run with progress and a cancel that leaves no error', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3)], running_id: null })
    api.adminPerfGetRun.mockResolvedValue(detail(3))
    let signal: AbortSignal | undefined
    api.adminPerfExport.mockImplementation((_f: string, _ids: number[], opts: { signal: AbortSignal; onProgress: (n: number) => void }) => {
      signal = opts.signal
      opts.onProgress(5000)
      return new Promise((_res, rej) => opts.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))))
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Export (JSON)' }))
    expect(api.adminPerfExport).toHaveBeenCalledWith('json', [3], expect.anything())
    expect(await screen.findByText(/^Exporting… \d/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the export of run #3' }))
    expect(signal!.aborted).toBe(true)
    expect(await screen.findByRole('button', { name: 'Export (JSON)' })).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers the export Cancel only on the run being exported', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(4)], running_id: null })
    api.adminPerfGetRun.mockImplementation((id: number) => Promise.resolve(detail(id)))
    let signal: AbortSignal | undefined
    api.adminPerfExport.mockImplementation((_f: string, _ids: number[], opts: { signal: AbortSignal }) => {
      signal = opts.signal
      return new Promise(() => {})
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Export (JSON)' }))
    expect(await screen.findByRole('button', { name: 'Cancel the export of run #3' })).toBeTruthy()
    // Run #4 opened while #3 still downloads: its view has no Cancel that
    // would stop #3's export.
    fireEvent.click(screen.getByRole('button', { name: 'View run #4' }))
    expect(await screen.findByText('Server test #4')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel the export of run #4' })).toBeNull()
    expect(signal!.aborted).toBe(false)
  })

  it('names the export of a run not on screen, explains the disabled Export, and can cancel it', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [summary(3), summary(4)], running_id: null })
    api.adminPerfGetRun.mockImplementation((id: number) => Promise.resolve(detail(id)))
    let signal: AbortSignal | undefined
    api.adminPerfExport.mockImplementation((_f: string, _ids: number[], opts: { signal: AbortSignal }) => {
      signal = opts.signal
      return new Promise((_res, rej) => opts.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))))
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'View run #3' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Export (JSON)' }))
    await screen.findByRole('button', { name: 'Cancel the export of run #3' })
    // On its own run's panel the export needs no second line.
    expect(screen.queryByText('Exporting run #3…')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'View run #4' }))
    expect(await screen.findByText('Server test #4')).toBeTruthy()
    const line = screen.getByText('Exporting run #3…')
    // Outside run #4's result panel.
    expect(line.closest('section')).toBeNull()
    const exportBtn = screen.getByRole('button', { name: 'Export (JSON)' }) as HTMLButtonElement
    expect(exportBtn.disabled).toBe(true)
    expect(exportBtn.title).toMatch(/Run #3 is still being exported/)
    const describedBy = exportBtn.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)?.textContent).toContain('Exporting run #3…')

    // Closing the result keeps the line (and its Cancel) on screen.
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the export of run #3' }))
    expect(signal!.aborted).toBe(true)
    await waitFor(() => expect(screen.queryByText('Exporting run #3…')).toBeNull())
  })
})
