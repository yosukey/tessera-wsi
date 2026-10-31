// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminGetSlide: vi.fn(),
  adminPerfListRuns: vi.fn(),
  adminPerfSaveClientRun: vi.fn(),
  adminPerfCancelRun: vi.fn(),
  adminPerfStartRun: vi.fn(),
  adminPerfDeleteRun: vi.fn(),
  adminPerfExport: vi.fn(),
}))
vi.mock('../../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../../lib/api')>('../../../lib/api')
  return { ...actual, ...api }
})
const perf = vi.hoisted(() => ({ runClientTest: vi.fn() }))
vi.mock('../../../lib/admin/perfClientTest', async () => {
  const actual = await vi.importActual<typeof import('../../../lib/admin/perfClientTest')>('../../../lib/admin/perfClientTest')
  return { ...actual, ...perf }
})

import { I18nProvider } from '../../../lib/i18n'
import {
  ApiCodeError, throwApi,
  type PerfClientResult, type PerfRunDetail, type PerfRunSummary, type PerfServerResult, type Slide,
} from '../../../lib/api'
import { apiErrorMessage } from '../../../lib/admin/apiError'
import en from '../../../lib/i18n/en'
import ja from '../../../lib/i18n/ja'
import PerfClientSection from './PerfClientSection'
import PerfServerSection from './PerfServerSection'
import PerfHistorySection from './PerfHistorySection'
import { ClientResultView, CompareView, ServerResultView, bandErrorCount, runCaveats } from './PerfResults'

const wrap = (ui: React.ReactElement) => <I18nProvider>{ui}</I18nProvider>

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const slide = {
  id: 7, public_id: 'abc123', title: 'Liver', catalog_id: '', wsi_filename: 'liver.svs',
  tile_version: 3, is_published: true,
} as unknown as Slide

function run(id: number, over: Partial<PerfRunSummary> = {}): PerfRunSummary {
  return {
    id, kind: 'client', state: 'succeeded', created_at: '2026-10-01T10:00:00Z', finished_at: '2026-10-01T10:01:00Z',
    actor: 'admin', params: { slide_id: 7, mode: 'app', tiles: 60, concurrency: 6 }, progress: null,
    cancel_requested: false, error: null, summary: { rtt_ms: 5, mbps: 100, tiles_per_s: 20 }, ...over,
  }
}

function clientResult(over: Partial<PerfClientResult> = {}): PerfClientResult {
  return {
    schema: 1,
    params: { slide_id: 7, slide_title: 'Liver', mode: 'app', tiles: 60, concurrency: 6 },
    network: { rtt_ms: { count: 1, p50: 5 }, downloads: [{ bytes: 1e6, seconds: 0.1, mbps: 80 }], mbps: 80 },
    tiles: {
      requested: 60, ok: 58, errors: 2, wall_s: 3, tiles_per_s: 19, mb_per_s: 1,
      ttfb_ms: { count: 58, p50: 10, p95: 20 }, total_ms: { count: 58, p50: 12, p95: 25 },
      by_band: [], cache: {}, ttfb_by_cache: {},
      error_ms: { count: 2, p50: 3, max: 4 },
    },
    client: { user_agent: 'UA/1' },
    summary: { rtt_ms: 5, mbps: 80, tiles_per_s: 19, tile_errors: 2 },
    ...over,
  }
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('PerfServerSection cancel', () => {
  const running = { ...run(9, { kind: 'server', state: 'running' }), progress: { phase: 'open', percent: 10 }, result: null } as PerfRunDetail
  const mount = (r: PerfRunDetail | null) => wrap(
    <PerfServerSection isAdmin slides={[slide]} running={r} pending={false} disabled={false} onStarted={() => {}} onConflict={() => {}} />,
  )

  it('turns the button off while the cancel is on its way, so a second click sends nothing', async () => {
    const d = deferred<void>()
    api.adminPerfCancelRun.mockReturnValue(d.promise)
    render(mount(running))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    const btn = await screen.findByRole('button', { name: 'Cancelling…' })
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(btn)
    expect(api.adminPerfCancelRun).toHaveBeenCalledTimes(1)
    await act(async () => { d.resolve() })
  })

  it('says nothing when the run had already stopped', async () => {
    api.adminPerfCancelRun.mockRejectedValue(new ApiCodeError('adminPerf.err.notRunning'))
    render(mount(running))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.adminPerfCancelRun).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByRole('alert')).toBeNull()
    // Usable again in case the run is in fact still going.
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('clears a failed cancel once the run is over', async () => {
    api.adminPerfCancelRun.mockRejectedValue(new Error('perf cancel failed'))
    const { rerender } = render(mount(running))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Failed to cancel the measurement.')
    rerender(mount(null))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps its live region mounted and only changes the text', () => {
    const { container, rerender } = render(mount(null))
    const region = container.querySelector('[aria-live="polite"]')!
    expect(region.textContent).toBe('')
    rerender(mount(running))
    expect(container.querySelector('[aria-live="polite"]')).toBe(region)
    expect(region.textContent).toBe('Opening the slide')
  })
})

describe('PerfClientSection', () => {
  const props = {
    isAdmin: true, slides: [slide], disabled: false,
    onRunningChange: () => {}, onFinished: vi.fn(), onServerRunFound: vi.fn(),
  }

  it('measures the slide as it is now, not as the page listed it', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
    api.adminGetSlide.mockResolvedValue({ ...slide, tile_version: 4 })
    perf.runClientTest.mockResolvedValue(clientResult())
    api.adminPerfSaveClientRun.mockResolvedValue({ id: 11 })
    render(wrap(<PerfClientSection {...props} />))
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    await waitFor(() => expect(props.onFinished).toHaveBeenCalledWith(11))
    expect(api.adminGetSlide).toHaveBeenCalledWith(7, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(perf.runClientTest.mock.calls[0][0].slide.tile_version).toBe(4)
  })

  it('refuses the cache path for a slide unpublished since the list loaded', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
    api.adminGetSlide.mockResolvedValue({ ...slide, is_published: false })
    render(wrap(<PerfClientSection {...props} />))
    fireEvent.click(screen.getByLabelText(/Normal path through the nginx cache/))
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    expect((await screen.findByRole('alert')).textContent).toContain('only serves published slides')
    expect(perf.runClientTest).not.toHaveBeenCalled()
  })

  it('flags a result when a server benchmark started during the test', async () => {
    api.adminPerfListRuns
      .mockResolvedValueOnce({ runs: [], running_id: null })
      .mockResolvedValueOnce({ runs: [], running_id: 5 })
    api.adminGetSlide.mockResolvedValue(slide)
    perf.runClientTest.mockResolvedValue(clientResult())
    api.adminPerfSaveClientRun.mockResolvedValue({ id: 12 })
    render(wrap(<PerfClientSection {...props} />))
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    await waitFor(() => expect(api.adminPerfSaveClientRun).toHaveBeenCalled())
    expect(api.adminPerfSaveClientRun.mock.calls[0][0].summary.server_overlap).toBe(true)
    expect(props.onServerRunFound).toHaveBeenCalledWith(5)
  })

  it('shows its own message, not the browser\'s English, for a transport failure', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
    api.adminGetSlide.mockResolvedValue(slide)
    perf.runClientTest.mockRejectedValue(new TypeError('Failed to fetch'))
    render(wrap(<PerfClientSection {...props} />))
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    expect((await screen.findByRole('alert')).textContent).toBe('The browser test failed.')
  })

  it('translates a malformed descriptor', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
    api.adminGetSlide.mockResolvedValue(slide)
    perf.runClientTest.mockRejectedValue(new ApiCodeError('adminPerf.client.err.badDescriptor'))
    render(wrap(<PerfClientSection {...props} />))
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    expect((await screen.findByRole('alert')).textContent).toBe('The slide\'s Deep Zoom descriptor is not in the expected format.')
  })

  it('keeps its live region mounted from the start', async () => {
    api.adminPerfListRuns.mockResolvedValue({ runs: [], running_id: null })
    api.adminGetSlide.mockReturnValue(new Promise(() => {}))
    const { container } = render(wrap(<PerfClientSection {...props} />))
    const region = container.querySelector('[aria-live="polite"]')!
    expect(region.textContent).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Run browser test' }))
    await waitFor(() => expect(region.textContent).toBe('Measuring round-trip time'))
    expect(container.querySelector('[aria-live="polite"]')).toBe(region)
  })
})

describe('PerfHistorySection', () => {
  const props = { isAdmin: true, selectedId: null, onSelect: () => {}, onDeleted: () => {} }

  it('names each row\'s buttons after their run', () => {
    render(wrap(<PerfHistorySection {...props} runs={[run(5), run(6)]} />))
    expect(screen.getByRole('button', { name: 'View run #5' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Delete run #6' })).toBeTruthy()
  })

  it('says in the row what makes a browser run questionable, and nothing for runs without these fields', () => {
    render(wrap(<PerfHistorySection {...props} runs={[
      run(5, { summary: { rtt_ms: 5, tile_errors: 2, hidden_during_run: true, server_overlap: true, job_overlap: true } }),
      run(6),
    ]} />))
    expect(screen.getByText(/2 tile\(s\) failed/)).toBeTruthy()
    expect(screen.getByText(/tab was in the background/)).toBeTruthy()
    expect(screen.getByText(/overlapped a server test/)).toBeTruthy()
    expect(screen.getByText(/overlapped a backup/)).toBeTruthy()
    expect(screen.getAllByText(/⚠/)).toHaveLength(4)
  })

  it('exports with progress, and a cancel that leaves no error behind', async () => {
    let signal: AbortSignal | undefined
    api.adminPerfExport.mockImplementation((_f: string, _ids: unknown, opts: { signal: AbortSignal; onProgress: (n: number) => void }) => {
      signal = opts.signal
      opts.onProgress(2048)
      return new Promise((_res, rej) => opts.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))))
    })
    render(wrap(<PerfHistorySection {...props} runs={[run(5)]} />))
    fireEvent.click(screen.getByRole('button', { name: 'Export the whole history as CSV' }))
    expect(await screen.findByText(/^Exporting… 2/)).toBeTruthy()
    expect(api.adminPerfExport).toHaveBeenCalledWith('csv', undefined, expect.anything())
    // Named apart from the open run's export Cancel and the server test's.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the history export' }))
    expect(signal!.aborted).toBe(true)
    await waitFor(() => expect(screen.queryByText(/^Exporting…/)).toBeNull())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('CompareView', () => {
  it('warns when a browser run lost tiles, ran hidden or shared the machine', () => {
    const { rerender } = render(wrap(<CompareView a={run(1)} b={run(2)} />))
    expect(screen.queryByText(/not measured under normal conditions/)).toBeNull()
    for (const flag of [{ tile_errors: 1 }, { hidden_during_run: true }, { server_overlap: true }, { job_overlap: true }]) {
      rerender(wrap(<CompareView a={run(1)} b={run(2, { summary: { rtt_ms: 4, ...flag } })} />))
      expect(screen.getByText(/not measured under normal conditions/)).toBeTruthy()
    }
  })

  it('treats runs without these fields as unknown, not flagged', () => {
    expect(runCaveats(run(1, { summary: { rtt_ms: 5 } }))).toEqual([])
    expect(runCaveats(run(1, { summary: null }))).toEqual([])
    expect(runCaveats(run(1, { kind: 'server', summary: { peak_tiles_per_s: 3 } }))).toEqual([])
  })

  it('says when two browser runs were timed by different methods, still showing the figures', () => {
    const old = run(1, { summary: { rtt_ms: 5, mbps: 100, tile_ttfb_p95_ms: 30 } })
    const timed = (id: number) => run(id, { summary: { rtt_ms: 5, mbps: 300, tile_ttfb_p95_ms: 10, timing: 2 } })
    const { rerender } = render(wrap(<CompareView a={old} b={timed(2)} />))
    expect(screen.getByText(/measured with different methods/)).toBeTruthy()
    // Still a comparison, figures and all — just not like for like.
    expect(screen.getByText('300 Mbps')).toBeTruthy()
    rerender(wrap(<CompareView a={timed(1)} b={timed(2)} />))
    expect(screen.queryByText(/measured with different methods/)).toBeNull()
    rerender(wrap(<CompareView a={old} b={run(2)} />))
    expect(screen.queryByText(/measured with different methods/)).toBeNull()
    // A server run has no browser timing method.
    rerender(wrap(<CompareView a={run(1, { kind: 'server', summary: {} })} b={run(2, { kind: 'server', summary: { timing: 2 } })} />))
    expect(screen.queryByText(/measured with different methods/)).toBeNull()
  })

  it('flags any timing difference, not only an unmarked run against a marked one', () => {
    // 1: a marked run whose tiles (some or all) kept the fetch() timing. Its
    // download was still timed from the body alone, so against an unmarked run
    // it differs too, although both took the tile times around fetch().
    const marked = (id: number, timing: number) => run(id, { summary: { rtt_ms: 5, mbps: 300, tile_ttfb_p95_ms: 10, timing } })
    const old = run(1, { summary: { rtt_ms: 5, mbps: 100, tile_ttfb_p95_ms: 30 } })
    const { rerender } = render(wrap(<CompareView a={marked(1, 1)} b={marked(2, 2)} />))
    expect(screen.getByText(/measured with different methods/)).toBeTruthy()
    rerender(wrap(<CompareView a={old} b={marked(2, 1)} />))
    expect(screen.getByText(/measured with different methods/)).toBeTruthy()
    rerender(wrap(<CompareView a={marked(1, 1)} b={marked(2, 1)} />))
    expect(screen.queryByText(/measured with different methods/)).toBeNull()
  })
})

describe('ClientResultView', () => {
  it('reports the failed tiles\' timing, the overlap, and survives a result without client data', () => {
    const result = clientResult({ client: undefined as unknown as PerfClientResult['client'], summary: { server_overlap: true, job_overlap: true } })
    render(wrap(<ClientResultView run={run(3)} result={result} />))
    expect(screen.getByText(/Time to the error response: median 3 ms, max 4 ms/)).toBeTruthy()
    expect(screen.getByText(/A server test was running during this run/)).toBeTruthy()
    expect(screen.getByText(/A backup, restore or slide transfer was running/)).toBeTruthy()
    expect(screen.getByText(/Run #3 by admin · —/)).toBeTruthy()
  })

  it('labels the server memory in MiB, the unit the server measures it in', () => {
    const result = clientResult({
      tiles: { ...clientResult().tiles, timing: { resource: 50, fetch: 8 } },
      server: { recorded_at: '2026-10-01T10:00:00Z', environment: { mem_total_mb: 16000, cgroup: { memory_max_mb: 4096, cpu_limit: 2 } } },
    })
    render(wrap(<ClientResultView run={run(3)} result={result} />))
    fireEvent.click(screen.getByRole('button', { name: /Conditions/ }))
    expect(screen.getByText('16,000 MiB')).toBeTruthy()
    expect(screen.getByText(/memory 4,096 MiB/)).toBeTruthy()
    expect(screen.getByText(/Resource Timing, from when the request was sent: 50/)).toBeTruthy()
  })
})

describe('ServerResultView', () => {
  const stats = { count: 10, p50: 20, p95: 40, max: 50 }
  const band = (b: 'high' | 'mid' | 'low', over = {}) => ({
    band: b, level: 10, generate_ms: stats, encode_ms: stats, total_ms: stats, avg_bytes: 20000, ...over,
  })
  function serverResult(over: Partial<PerfServerResult> = {}): PerfServerResult {
    return {
      schema: 1,
      scope: 'below_http',
      environment: { cpu_model: 'Xeon', cpu_physical: 32, cpu_logical: 64, cpu_effective: 2 },
      params: { cold: false, tiles_per_band: 30, max_concurrency: 4, step_seconds: 3 },
      slides: [
        {
          id: 1, title: 'Kidney', format: 'wsi', vendor: 'aperio', width: 90000, height: 60000, file_bytes: 2e9,
          warm_bytes: 5e8, warm_fraction: 0.25,
          open: { first_ms: 30, repeat_ms: 1 },
          bands: [band('high', { errors: 2, skipped: 1 }), band('mid', { errors: 0, skipped: 0 }), band('low')],
        },
        {
          id: 2, title: 'Lung', format: 'wsi', vendor: 'aperio', width: 90000, height: 60000, file_bytes: 2e9,
          error: 'measurement failed: bad block', reader_latched: true, warm_fraction: 1,
          bands: [band('high')],
        },
      ],
      scaling: [
        { concurrency: 1, tiles: 40, errors: 0, skipped: 0, seconds: 3, tiles_per_s: 13, latency_ms: stats, cpu_busy_pct: 40, iowait_pct: 1, fresh_tile_pct: 100 },
        { concurrency: 2, tiles: 70, errors: 0, skipped: 3, seconds: 3, tiles_per_s: 23, latency_ms: stats, cpu_busy_pct: 80, iowait_pct: 1, fresh_tile_pct: 12.5 },
      ],
      system: {
        cpu_busy_pct: { avg: 50, max: 80 }, iowait_pct: { avg: 1, max: 2 }, mem_available_mb_min: 1000,
        load_avg_end: null, samples: 10, cpu_basis: 'cgroup', cpu_effective: 2,
      },
      // A stored "no bottleneck" the server did not rule out on failed band
      // renders.
      summary: { peak_tiles_per_s: 23, job_overlap: true },
      advice: [{ code: 'noBottleneck', params: {} }],
      truncated: false,
      duration_s: 60,
      ...over,
    }
  }
  const serverRun = run(4, { kind: 'server' })

  it('shows failed and skipped renders, the new-tile share, and the slide notes', () => {
    render(wrap(<ServerResultView run={serverRun} result={serverResult()} />))
    // Band table: its own column, only where a band failed or skipped.
    expect(screen.getByText('2 ✕ · 1 skipped')).toBeTruthy()
    expect(screen.getByText(/Skipped: renders refused only because/)).toBeTruthy()
    expect(screen.getByText(/2 single-tile render\(s\) failed/)).toBeTruthy()
    // Scaling table.
    expect(screen.getByText('12.5%')).toBeTruthy()
    expect(screen.getByText(/\(3 skipped\)/)).toBeTruthy()
    expect(screen.getAllByText(/A low share means the step mostly re-rendered/).length).toBeGreaterThan(0)
    // A latched reader keeps what it measured.
    expect(screen.getByText(/bad block/)).toBeTruthy()
    expect(screen.getByText(/failed for good part-way through/)).toBeTruthy()
    expect(screen.getAllByRole('table').length).toBe(3)
    // Only the partly warmed slide gets the note.
    expect(screen.getAllByText(/had been read before the tiles/)).toHaveLength(1)
    expect(screen.getByText(/\(25% of the file\)/)).toBeTruthy()
    // Whole run.
    expect(screen.getByText(/measured below the app's HTTP layer/)).toBeTruthy()
    expect(screen.getByText(/A backup, restore or slide transfer was running/)).toBeTruthy()
    expect(screen.queryByText('No bottleneck stood out in this run.')).toBeNull()
    expect(bandErrorCount(serverResult())).toBe(2)
  })

  it('says how CPU busy was measured and how many CPUs the app may use', () => {
    render(wrap(<ServerResultView run={serverRun} result={serverResult()} />))
    fireEvent.click(screen.getByRole('button', { name: /Environment/ }))
    expect(screen.getByText(/Xeon \(32 \/ 64\) · 2 usable by the app/)).toBeTruthy()
    expect(screen.getByText(/the app's cgroup \(container \/ service\), against the 2 CPUs/)).toBeTruthy()
  })

  it('reads a result without any of these fields', () => {
    const old = serverResult({ scope: undefined, summary: { peak_tiles_per_s: 23 } })
    old.slides = [{ ...old.slides[0], warm_bytes: undefined, warm_fraction: undefined, bands: [band('high')] }]
    old.scaling = old.scaling.map(({ skipped, fresh_tile_pct, ...rest }) => rest)
    delete old.system.cpu_basis
    old.environment = { cpu_model: 'Xeon', cpu_physical: 32, cpu_logical: 64 }
    render(wrap(<ServerResultView run={serverRun} result={old} />))
    expect(screen.getByText('No bottleneck stood out in this run.')).toBeTruthy()
    expect(screen.queryByText(/New tiles/)).toBeNull()
    expect(screen.queryByText(/Failed \/ skipped/)).toBeNull()
    expect(screen.queryByText(/HTTP layer/)).toBeNull()
    expect(screen.queryByText(/⚠/)).toBeNull()
  })

  it('flags a server run a background job overlapped in the history and the comparison', () => {
    const raced = run(5, { kind: 'server', summary: { peak_tiles_per_s: 20, job_overlap: true } })
    expect(runCaveats(raced)).toEqual(['jobOverlap'])
    render(wrap(<CompareView a={run(4, { kind: 'server', summary: { peak_tiles_per_s: 22 } })} b={raced} />))
    expect(screen.getByText(/these server runs ended early, had failed tile renders .* or overlapped a backup/)).toBeTruthy()
  })

  it('flags a server run cut short or with failed renders in the history and the comparison', () => {
    const clean = run(4, { kind: 'server', summary: { peak_tiles_per_s: 22, truncated: false, band_errors: 0, tile_errors: 0 } })
    const cut = run(5, { kind: 'server', summary: { peak_tiles_per_s: 20, truncated: true } })
    const lossy = run(6, { kind: 'server', summary: { peak_tiles_per_s: 20, band_errors: 2, tile_errors: 3 } })
    expect(runCaveats(clean)).toEqual([])
    expect(runCaveats(cut)).toEqual(['truncated'])
    expect(runCaveats(lossy)).toEqual(['renderErrors'])
    render(wrap(<PerfHistorySection isAdmin selectedId={null} onSelect={() => {}} onDeleted={() => {}} runs={[clean, cut, lossy]} />))
    expect(screen.getByText(/ended early/)).toBeTruthy()
    expect(screen.getByText(/5 tile render\(s\) failed/)).toBeTruthy()
    expect(screen.getAllByText(/⚠/)).toHaveLength(2)
    cleanup()
    for (const b of [cut, lossy]) {
      const { unmount } = render(wrap(<CompareView a={clean} b={b} />))
      expect(screen.getByText(/not measured under normal conditions/)).toBeTruthy()
      unmount()
    }
    render(wrap(<CompareView a={clean} b={run(7, { kind: 'server', summary: { peak_tiles_per_s: 21 } })} />))
    expect(screen.queryByText(/not measured under normal conditions/)).toBeNull()
  })
})

describe('job refused during a benchmark', () => {
  it('reads as the translated message wherever a job start renders coded errors', async () => {
    const r = new Response(JSON.stringify({ detail: { code: 'adminPerf.err.perfRunActive', params: {} } }), {
      status: 409, headers: { 'content-type': 'application/json' },
    })
    const err = await throwApi(r, 'backup start failed').catch((e) => e)
    const t = (k: string) => { const m = (en as Record<string, unknown>)[k]; return typeof m === 'string' ? m : k }
    expect(apiErrorMessage(err, t)).toBe(en['adminPerf.err.perfRunActive'])
  })
})

describe('Japanese wording', () => {
  const msg = (k: string, p: Record<string, string | number> = {}) => {
    const m = (ja as Record<string, unknown>)[k]
    return typeof m === 'function' ? (m as (q: typeof p) => string)(p) : String(m)
  }

  it('words the CPU lines as complete phrases', () => {
    expect(msg('adminPerf.env.cpuEffective', { n: 2 })).toBe('アプリが使用できるのは2個')
    for (const basis of ['host', 'cgroup', 'process']) {
      // A value that stops on "…割合で" leaves its verb unsaid.
      expect(msg(`adminPerf.env.cpuBasis.${basis}`, { n: 2 })).not.toMatch(/で(（.*）)?$/)
      // One register for all three: 使用できる (not 使えます), and the count
      // written 2個, unspaced.
      expect(msg(`adminPerf.env.cpuBasis.${basis}`, { n: 2 })).toContain('使用できる2個')
      expect(msg(`adminPerf.env.cpuBasis.${basis}`, { n: 2 })).not.toMatch(/使えます| 2 個/)
    }
    // The process basis also covers a CPU affinity mask (not only an
    // unreadable cgroup), in both languages.
    expect(msg('adminPerf.env.cpuBasis.process', { n: 2 })).toContain('アフィニティ')
    expect((en['adminPerf.env.cpuBasis.process'] as (p: { n: number }) => string)({ n: 2 })).toContain('affinity')
  })

  it('calls stopping a measurement or an export 中止, as the run button does', () => {
    expect(msg('adminPerf.server.cancel')).toBe('中止')
    expect(msg('adminPerf.export.cancel')).toBe('中止')
    for (const k of ['adminPerf.err.perfRunActive', 'adminPerf.export.cancelAll', 'adminPerf.export.cancelRun']) {
      expect(msg(k, { id: 3 })).toContain('中止')
      expect(msg(k, { id: 3 })).not.toContain('キャンセル')
    }
  })
})
