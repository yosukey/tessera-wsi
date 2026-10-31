// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminPerfExport, isApiCodeError, type PerfStats, type Slide } from '../api'
import { perfStats, round2, runClientTest, sampleTiles, type Descriptor } from './perfClientTest'
import statsFixture from './perfStats.fixture.json'

const DESCRIPTOR = '<?xml version="1.0" encoding="UTF-8"?>'
  + '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpeg" Overlap="1" TileSize="256">'
  + '<Size Width="4000" Height="3000"/></Image>'

const slide = {
  id: 7, public_id: 'abc123', title: 'Liver', catalog_id: '', wsi_filename: 'liver.svs',
  tile_version: 3, is_published: true,
} as unknown as Slide

// jsdom has no Resource Timing: a stand-in observer that receives one entry
// per tile response, named by its absolute URL as a browser names them. Its
// entries arrive only through takeRecords(), as when the browser queues them
// after the response body has resolved.
type FakeEntry = {
  name: string; nextHopProtocol: string
  startTime?: number; requestStart?: number; responseStart?: number; responseEnd?: number
}
class FakePerformanceObserver {
  static active: FakePerformanceObserver[] = []
  pending: FakeEntry[] = []
  constructor(_callback: unknown) {}
  observe() { FakePerformanceObserver.active.push(this) }
  takeRecords() { const p = this.pending; this.pending = []; return p }
  disconnect() { FakePerformanceObserver.active = FakePerformanceObserver.active.filter((o) => o !== this) }
}

// How the stub answers the n-th tile request (0-based, in issue order).
type TileAnswer = {
  status?: number
  // X-Cache-Status; null sends no header.
  cache?: string | null
  // Throws as fetch() does when the request never got an answer.
  networkError?: boolean
  // Extra Resource Timing fields for its entry (ms after the fetch started).
  // startTime / requestStart override the entry's own (an entry from before
  // the tile phase; a cross-origin one whose detail the browser zeroes).
  timing?: { queued: number; ttfb: number; body: number; startTime?: number; requestStart?: number }
  // The browser reports two entries for the URL (the same tile twice).
  duplicate?: boolean
}

// Answers the browser test's requests the way the server does: the
// descriptor, the ping / payload probes, and tiles (a nginx HIT each unless
// `answer` says otherwise). `reportProtocol` decides which tile responses
// get a Resource Timing entry. A request whose signal was aborted fails as
// fetch() does.
function stubServer(
  onTile?: (n: number) => void,
  reportProtocol: (n: number) => boolean = () => true,
  answer: (n: number) => TileAnswer = () => ({}),
  descriptor = DESCRIPTOR,
) {
  let tileCount = 0
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError')
    if (url.startsWith('/dzi/abc123.dzi')) return new Response(descriptor, { status: 200 })
    if (url.startsWith('/api/admin/perf/ping')) return new Response('{"t":1}', { status: 200 })
    if (url.startsWith('/api/admin/perf/payload')) {
      const size = Number(new URL(url, 'http://x').searchParams.get('size'))
      return new Response(new Uint8Array(size), { status: 200 })
    }
    if (url.startsWith('/dzi/abc123_files/')) {
      const n = tileCount++
      onTile?.(n)
      const a = answer(n)
      if (a.networkError) throw new TypeError('Failed to fetch')
      if (reportProtocol(n)) {
        const name = new URL(url, window.location.href).href
        const entry: FakeEntry = { name, nextHopProtocol: 'h2' }
        if (a.timing) {
          const t0 = performance.now()
          Object.assign(entry, {
            startTime: t0,
            requestStart: t0 + a.timing.queued,
            responseStart: t0 + a.timing.queued + a.timing.ttfb,
            responseEnd: t0 + a.timing.queued + a.timing.ttfb + a.timing.body,
          })
          if (a.timing.startTime !== undefined) entry.startTime = a.timing.startTime
          if (a.timing.requestStart !== undefined) entry.requestStart = a.timing.requestStart
        }
        for (const o of FakePerformanceObserver.active) {
          o.pending.push(entry)
          if (a.duplicate) o.pending.push({ ...entry })
        }
      }
      const cache = a.cache === undefined ? 'HIT' : a.cache
      return new Response(new Uint8Array(1000), {
        status: a.status ?? 200,
        headers: cache === null ? {} : { 'X-Cache-Status': cache },
      })
    }
    throw new Error(`unexpected fetch ${url}`)
  })
  vi.stubGlobal('fetch', fn)
  vi.stubGlobal('PerformanceObserver', FakePerformanceObserver)
  return fn
}

afterEach(() => {
  vi.unstubAllGlobals()
  // A test that froze performance.now() must not leave it frozen.
  vi.restoreAllMocks()
  FakePerformanceObserver.active = []
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
})

describe('runClientTest', () => {
  it('records the conditions it measured under alongside every tile request', async () => {
    stubServer()
    const r = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 3 }, () => {})

    expect(r.slide).toMatchObject({
      id: 7, public_id: 'abc123', width: 4000, height: 3000, tile_size: 256, overlap: 1,
      tile_format: 'jpeg', max_level: 12, tile_version: 3, published: true,
    })
    const c = r.client
    expect(c.user_agent).toBe(navigator.userAgent)
    expect(c.device_pixel_ratio).toBe(window.devicePixelRatio || 1)
    expect(c.viewport).toEqual({ width: window.innerWidth, height: window.innerHeight })
    expect(c.page_origin).toBe(window.location.origin)
    expect(r.tiles.ok).toBeGreaterThan(0)
    expect(c.http_protocols).toEqual({ h2: r.tiles.ok })
    expect(c.hidden_during_run).toBe(false)
    expect(Date.parse(c.finished_at!)).toBeGreaterThanOrEqual(Date.parse(c.started_at!))
    // The observer is released once the tile phase is over.
    expect(FakePerformanceObserver.active).toHaveLength(0)

    const samples = r.tiles.samples!
    expect(samples.columns).toEqual(
      ['start_ms', 'band', 'level', 'col', 'row', 'ttfb_ms', 'total_ms', 'bytes', 'cache', 'ok', 'protocol'],
    )
    expect(samples.rows).toHaveLength(r.tiles.requested)
    const starts = samples.rows.map((row) => row[0] as number)
    expect(starts).toEqual([...starts].sort((a, b) => a - b))
    expect(samples.rows[0].slice(7)).toEqual([1000, 'HIT', true, 'h2'])
    // The result must survive the JSON round trip to the server unchanged.
    expect(JSON.parse(JSON.stringify(r))).toEqual(r)
  })

  it('counts tiles the browser reported no protocol for as unknown', async () => {
    stubServer(undefined, (n) => n >= 2)
    const r = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 1 }, () => {})
    expect(r.client.http_protocols).toEqual({ h2: r.tiles.ok - 2, unknown: 2 })
    expect(r.tiles.samples!.rows.slice(0, 2).map((row) => row[10])).toEqual(['', ''])
  })

  it('flags a run during which the tab went to the background', async () => {
    let hidden = false
    stubServer(() => {
      if (hidden) return
      hidden = true
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    const r = await runClientTest({ slide, mode: 'cache', tiles: 10, concurrency: 2 }, () => {})
    expect(r.client.hidden_during_run).toBe(true)
  })

  it('records tile times from Resource Timing, keeping fetch() timing where it has none', async () => {
    // Every tile spent 30 ms queued in the browser before its request went
    // out; the server answered in 10 ms and sent the body in 5. Tile 0 gets
    // no timeline (as when the browser hides it).
    stubServer(undefined, () => true, (n) => (n === 0 ? {} : { timing: { queued: 30, ttfb: 10, body: 5 } }))
    const r = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {})
    expect(r.tiles.timing).toEqual({ resource: r.tiles.requested - 1, fetch: 1 })
    const rows = r.tiles.samples!.rows
    for (const row of rows.slice(1)) expect([row[5], row[6]]).toEqual([10, 15])
    // Tile 0's own fetch() timing is real time and varies; the median is
    // the Resource Timing figure.
    expect(r.tiles.ttfb_ms.p50).toBe(10)
    expect(r.tiles.total_ms.p50).toBe(15)
  })

  it('leaves failed tiles out of the figures, counting them and timing the HTTP errors apart', async () => {
    // Tile 1 never gets an answer; every fourth one is a 500.
    stubServer(undefined, () => true, (n) => (
      n === 1 ? { networkError: true } : n % 4 === 0 ? { status: 500, timing: { queued: 0, ttfb: 3, body: 1 } } : {}
    ))
    const r = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 1 }, () => {})
    // The 4000×3000 slide has a single tile at 1/16: 4 + 4 + 1 requests.
    expect(r.tiles.requested).toBe(9)
    const http500 = [0, 4, 8].length
    expect(r.tiles.errors).toBe(http500 + 1)
    expect(r.tiles.ok).toBe(9 - http500 - 1)
    expect(r.summary.tile_errors).toBe(r.tiles.errors)
    expect(r.summary.hidden_during_run).toBe(false)
    // Only responses that got an HTTP status have a time to report.
    expect(r.tiles.error_ms).toMatchObject({ count: http500, p50: 4, max: 4 })
    expect(r.tiles.ttfb_ms.count).toBe(r.tiles.ok)
    expect(r.tiles.by_band.reduce((n, b) => n + b.count, 0)).toBe(r.tiles.ok)
    expect(r.tiles.cache).toEqual({ HIT: r.tiles.ok })
    expect(r.client.http_protocols).toEqual({ h2: r.tiles.ok })
    const failed = r.tiles.samples!.rows.filter((row) => row[9] === false)
    expect(failed).toHaveLength(r.tiles.errors)
    expect(failed.every((row) => row[10] === '')).toBe(true)
  })

  it('reports the nginx hit rate only in cache mode and only from tiles that carried the header', async () => {
    // HIT, MISS, no header, HIT, MISS, no header, ...
    const cycle = (n: number): TileAnswer => ({ cache: ['HIT', 'MISS', null][n % 3] })
    stubServer(undefined, () => true, cycle)
    const r = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 1 }, () => {})
    expect(r.tiles.cache).toEqual({ HIT: 3, MISS: 3, NONE: 3 })
    expect(r.summary.cache_hit_pct).toBe(50)

    stubServer(undefined, () => true, () => ({ cache: null }))
    const bare = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 1 }, () => {})
    expect(bare.summary.cache_hit_pct).toBeNull()

    stubServer(undefined, () => true, cycle)
    const app = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {})
    expect(app.summary.cache_hit_pct).toBeNull()
  })

  it('stops at once when cancelled mid-run, releasing the observer', async () => {
    const ctrl = new AbortController()
    const fetchFn = stubServer((n) => { if (n === 2) ctrl.abort() })
    const removed = vi.spyOn(document, 'removeEventListener')
    await expect(runClientTest({ slide, mode: 'cache', tiles: 30, concurrency: 1 }, () => {}, ctrl.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    const tileCalls = fetchFn.mock.calls.filter(([u]) => String(u).startsWith('/dzi/abc123_files/'))
    // The tile in flight when Cancel was pressed, and the one that found the
    // signal aborted: nothing after.
    expect(tileCalls).toHaveLength(4)
    expect(FakePerformanceObserver.active).toHaveLength(0)
    expect(removed).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
  })

  it('reports a malformed Deep Zoom descriptor as a coded error', async () => {
    stubServer(undefined, undefined, undefined, '<?xml version="1.0"?><Nothing/>')
    const err = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {}).catch((e) => e)
    expect(isApiCodeError(err) && err.code).toBe('adminPerf.client.err.badDescriptor')
  })

  it('sizes the download from the body transfer alone, not the round trip before it', async () => {
    // A link moving 10 MB/s whose server takes 50 ms to start answering.
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const RATE = 1e7
    const sizes: number[] = []
    stubServer()
    const tiles = vi.mocked(fetch).getMockImplementation()!
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (!url.startsWith('/api/admin/perf/payload')) return tiles(input, init)
      const size = Number(new URL(url, 'http://x').searchParams.get('size'))
      sizes.push(size)
      clock += 50
      const r = new Response(null, { status: 200 })
      Object.defineProperty(r, 'arrayBuffer', {
        value: async () => { clock += (size / RATE) * 1000; return new ArrayBuffer(size) },
      })
      return r
    }))
    const r = await runClientTest({ slide, mode: 'app', tiles: 10, concurrency: 1 }, () => {})
    // 1 MiB probe at 10 MB/s → 1.5 s worth is 15 MB. Counting the 50 ms
    // first byte as transfer time would have read 6.8 MB/s and asked for
    // about 10 MB.
    expect(sizes).toEqual([1024 * 1024, 15_000_000, 15_000_000])
    expect(r.network.downloads.map((d) => d.mbps)).toEqual([80, 80, 80])
    expect(r.network.mbps).toBe(80)
  })

  it('keeps the fetch() timing of a download whose Resource Timing body took no time', async () => {
    // Same link as above, but the browser stamps responseStart and
    // responseEnd at the same instant. Believed, the 1 MiB probe would read 0 s
    // (0 Mbps), its rate would come out as 1 GB/s and the downloads would be
    // sized to the 32 MiB cap.
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const perf = performance as unknown as { getEntriesByName?: unknown }
    const hadEntries = 'getEntriesByName' in performance
    const original = perf.getEntriesByName
    perf.getEntriesByName = (name: string) => [{ name, startTime: clock, responseStart: clock, responseEnd: clock }]
    const RATE = 1e7
    const sizes: number[] = []
    stubServer()
    const tiles = vi.mocked(fetch).getMockImplementation()!
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (!url.startsWith('/api/admin/perf/payload')) return tiles(input, init)
      const size = Number(new URL(url, 'http://x').searchParams.get('size'))
      sizes.push(size)
      clock += 50
      const r = new Response(null, { status: 200 })
      Object.defineProperty(r, 'arrayBuffer', {
        value: async () => { clock += (size / RATE) * 1000; return new ArrayBuffer(size) },
      })
      return r
    }))
    try {
      const r = await runClientTest({ slide, mode: 'app', tiles: 10, concurrency: 1 }, () => {})
      expect(sizes).toEqual([1024 * 1024, 15_000_000, 15_000_000])
      expect(r.network.downloads.map((d) => d.mbps)).toEqual([80, 80, 80])
    } finally {
      if (hadEntries) perf.getEntriesByName = original
      else delete perf.getEntriesByName
    }
  })

  // Where a case below falls back to fetch() timing, it does so for the
  // affected tiles only; the rest keep their Resource Timing figure (ttfb 10,
  // total 15).
  const timed = { queued: 30, ttfb: 10, body: 5 }
  const viaResource = (r: Awaited<ReturnType<typeof runClientTest>>) => (
    r.tiles.samples!.rows.filter((row) => row[5] === 10 && row[6] === 15).length
  )

  it('takes no Mbps from a download the browser had buffered, and re-probes with a larger body', async () => {
    // A 10 MB/s link, but the 1 MiB probe's body is already buffered when
    // fetch() resolves (no Resource Timing either): its fetch() fallback reads
    // 0 s. Believed, that would be an absurd Mbps figure and a probe rate that
    // would size the measurement to the 32 MiB cap.
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const RATE = 1e7
    const sizes: number[] = []
    stubServer()
    const tiles = vi.mocked(fetch).getMockImplementation()!
    const buffered = new Set([1024 * 1024])
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (!url.startsWith('/api/admin/perf/payload')) return tiles(input, init)
      const size = Number(new URL(url, 'http://x').searchParams.get('size'))
      sizes.push(size)
      clock += 50
      // A buffered body has already arrived by the time fetch() resolves, so
      // reading it takes no time; any other streams at the link's rate.
      const transfer = (size / RATE) * 1000
      if (buffered.has(size)) clock += transfer
      const r = new Response(null, { status: 200 })
      Object.defineProperty(r, 'arrayBuffer', {
        value: async () => { if (!buffered.has(size)) clock += transfer; return new ArrayBuffer(size) },
      })
      return r
    }))
    const r = await runClientTest({ slide, mode: 'app', tiles: 10, concurrency: 1 }, () => {})
    // The 8 MiB re-probe times at 10 MB/s and sizes the measurement (15 MB).
    expect(sizes).toEqual([1024 * 1024, 8 * 1024 * 1024, 15_000_000, 15_000_000])
    expect(r.network.downloads.map((d) => d.mbps)).toEqual([null, 80, 80, 80])
    expect(r.network.mbps).toBe(80)

    // Nothing could be timed: no figure, and the largest body for the
    // measurement rather than one sized from a 0 s reading.
    sizes.length = 0
    buffered.add(8 * 1024 * 1024).add(32 * 1024 * 1024)
    const none = await runClientTest({ slide, mode: 'app', tiles: 10, concurrency: 1 }, () => {})
    expect(sizes).toEqual([1024 * 1024, 8 * 1024 * 1024, 32 * 1024 * 1024, 32 * 1024 * 1024])
    expect(none.network.downloads.map((d) => d.mbps)).toEqual([null, null, null, null])
    expect(none.network.mbps).toBeNull()
  })

  it('claims the Resource Timing method only when it timed every tile that succeeded', async () => {
    // Every tile has its own timeline; a failed one (no entry) is in no figure.
    stubServer(undefined, (n) => n !== 0, (n) => (n === 0 ? { status: 500 } : { timing: timed }))
    const all = await runClientTest({ slide, mode: 'cache', tiles: 6, concurrency: 2 }, () => {})
    expect(all.tiles.timing!.fetch).toBe(1)
    expect(all.summary.timing).toBe(2)
  })

  it('marks a run whose tiles fell back to fetch() timing as such', async () => {
    // Entries without a timeline (as the default stub sends), one zeroed
    // cross-origin entry, and no observer at all: each leaves some OK tiles
    // timed around fetch(), so the run must not claim the newer method.
    stubServer()
    const none = await runClientTest({ slide, mode: 'cache', tiles: 6, concurrency: 2 }, () => {})
    expect(none.tiles.timing).toEqual({ resource: 0, fetch: none.tiles.requested })
    expect(none.summary.timing).toBe(1)

    stubServer(undefined, () => true, (n) => ({ timing: n === 2 ? { ...timed, requestStart: 0 } : timed }))
    const one = await runClientTest({ slide, mode: 'cache', tiles: 6, concurrency: 2 }, () => {})
    expect(one.tiles.timing!.fetch).toBe(1)
    expect(one.summary.timing).toBe(1)

    stubServer(undefined, () => true, () => ({ timing: timed }))
    vi.stubGlobal('PerformanceObserver', undefined)
    const noObserver = await runClientTest({ slide, mode: 'cache', tiles: 6, concurrency: 2 }, () => {})
    expect(noObserver.summary.timing).toBe(1)
  })

  it('ignores a Resource Timing entry that started before the tile phase', async () => {
    // A straggler from an earlier run of the same URLs ('cache' mode reuses
    // its sample) must not be matched to this run's request.
    stubServer(undefined, () => true, (n) => ({ timing: n < 3 ? { ...timed, startTime: -1 } : timed }))
    const r = await runClientTest({ slide, mode: 'cache', tiles: 12, concurrency: 1 }, () => {})
    expect(r.tiles.timing).toEqual({ resource: r.tiles.requested - 3, fetch: 3 })
    expect(viaResource(r)).toBe(r.tiles.requested - 3)
  })

  it('falls back when the same URL has two entries', async () => {
    stubServer(undefined, () => true, (n) => ({ timing: timed, duplicate: n === 0 }))
    const r = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {})
    expect(r.tiles.timing).toEqual({ resource: r.tiles.requested - 1, fetch: 1 })
  })

  it('falls back on a cross-origin entry whose timeline the browser zeroed', async () => {
    // Without Timing-Allow-Origin a cross-origin entry has requestStart 0.
    stubServer(undefined, () => true, (n) => ({ timing: n % 2 ? { ...timed, requestStart: 0 } : timed }))
    const r = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {})
    const zeroed = Math.floor(r.tiles.requested / 2)
    expect(r.tiles.timing).toEqual({ resource: r.tiles.requested - zeroed, fetch: zeroed })
    expect(viaResource(r)).toBe(r.tiles.requested - zeroed)
  })

  it('times every tile with fetch() in a browser without PerformanceObserver', async () => {
    stubServer(undefined, () => true, () => ({ timing: timed }))
    vi.stubGlobal('PerformanceObserver', undefined)
    const r = await runClientTest({ slide, mode: 'app', tiles: 12, concurrency: 1 }, () => {})
    expect(r.tiles.ok).toBe(r.tiles.requested)
    expect(r.tiles.timing).toEqual({ resource: 0, fetch: r.tiles.requested })
    expect(r.client.http_protocols).toEqual({ unknown: r.tiles.ok })
  })
})

describe('sampleTiles', () => {
  // Large enough that every band has more tiles than the sample asks for.
  const d: Descriptor = { width: 100_000, height: 80_000, tileSize: 256, overlap: 1, format: 'jpeg' }

  it('splits the sample over the three bands, the remainder going to full resolution', () => {
    const jobs = sampleTiles('abc123', d, 10, 42, 3)
    const by = (band: string) => jobs.filter((j) => j.band === band)
    expect([by('high').length, by('mid').length, by('low').length]).toEqual([4, 3, 3])
    expect([by('high')[0].level, by('mid')[0].level, by('low')[0].level]).toEqual([17, 15, 13])
    for (const j of jobs) {
      const scale = 2 ** (17 - j.level)
      expect(j.col).toBeLessThan(Math.ceil(Math.ceil(100_000 / scale) / 256))
      expect(j.row).toBeLessThan(Math.ceil(Math.ceil(80_000 / scale) / 256))
      expect(j.url).toBe(`/dzi/abc123_files/${j.level}/${j.col}_${j.row}.jpeg?v=3`)
    }
    // No tile twice within a band.
    for (const band of ['high', 'mid', 'low']) {
      const keys = by(band).map((j) => `${j.col}_${j.row}`)
      expect(new Set(keys).size).toBe(keys.length)
    }
  })

  it('is fixed by its seed', () => {
    expect(sampleTiles('abc123', d, 30, 7, 3)).toEqual(sampleTiles('abc123', d, 30, 7, 3))
    expect(sampleTiles('abc123', d, 30, 7, 3)).not.toEqual(sampleTiles('abc123', d, 30, 8, 3))
  })

  it('takes a whole level when it has fewer tiles than asked', () => {
    // 1000 px square: 4×4 tiles at full resolution, one tile at 1/4 and 1/16.
    const jobs = sampleTiles('abc123', { ...d, width: 1000, height: 1000 }, 60, 1, null)
    expect(jobs.filter((j) => j.band === 'high')).toHaveLength(16)
    expect(jobs.filter((j) => j.band === 'mid')).toHaveLength(1)
    expect(jobs.filter((j) => j.band === 'low')).toHaveLength(1)
    expect(jobs[0].url).not.toContain('?v=')
  })

  it('clamps the lower bands to level 0 on a tiny slide', () => {
    const jobs = sampleTiles('abc123', { ...d, width: 2, height: 2 }, 3, 1, 3)
    expect(jobs.map((j) => [j.band, j.level, j.col, j.row])).toEqual([
      ['high', 1, 0, 0], ['mid', 0, 0, 0], ['low', 0, 0, 0],
    ])
  })
})

describe('perfStats', () => {
  // The fixture is perf_bench.stats_ms's own output (see its _comment).
  it.each(statsFixture.cases.map((c) => [c.values.length, c] as const))(
    'matches the server stats_ms on %i values',
    (_n, c) => {
      expect(perfStats(c.values)).toEqual(c.expected as PerfStats)
    },
  )

  it('rounds like Python round(x, 2)', () => {
    expect([0.125, 0.375, 0.625, 0.875, 1.005, 2.675, 1.115, 2.5].map(round2))
      .toEqual([0.12, 0.38, 0.62, 0.88, 1, 2.67, 1.11, 2.5])
  })
})

describe('adminPerfExport', () => {
  it('downloads the requested runs, named by the server or else by the fallback', async () => {
    const urls: string[] = []
    let disposition: string | null = 'attachment; filename="from-server.json"'
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input))
      return new Response('{}', { status: 200, headers: disposition ? { 'content-disposition': disposition } : {} })
    }))
    const created = vi.fn(() => 'blob:x')
    const urlStatics = URL as unknown as { createObjectURL?: unknown; revokeObjectURL?: unknown }
    urlStatics.createObjectURL = created
    urlStatics.revokeObjectURL = () => {}
    const names: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download)
    })
    try {
      await adminPerfExport('json', [5])
      disposition = null
      await adminPerfExport('json', [5])
      await adminPerfExport('csv')
    } finally {
      delete urlStatics.createObjectURL
      delete urlStatics.revokeObjectURL
    }
    expect(urls).toEqual([
      '/api/admin/perf/export.json?ids=5', '/api/admin/perf/export.json?ids=5', '/api/admin/perf/export.csv',
    ])
    expect(names).toEqual(['from-server.json', 'tessera-perf-run-5.json', 'tessera-perf.csv'])
    expect(created).toHaveBeenCalledTimes(3)
  })
})
