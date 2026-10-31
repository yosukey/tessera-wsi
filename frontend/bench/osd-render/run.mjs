// Drives harness.html in headless Chromium across a settings matrix.
// usage: node run.mjs <out.json> <plan.json | inline JSON array>
// Each plan entry: { scenario: zoomin|deep|panback, vp: fhd|hidpi|ipad|phone,
//   latency: ms, ir: 0|1 (immediateRender), cache: maxImageCacheCount,
//   mtpf?: maxTilesPerFrame (1), sample?: 0 to skip per-frame screen sampling
//   (faster, timings only), drawer?: canvas|webgl (canvas), reps?: n (1),
//   cc?: the tiles' Cache-Control (default no-cache) }.
// Results are appended to <out.json>, one record per run with every step.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BENCH_BASE || 'http://127.0.0.1:8765'
const out = process.argv[2]
const planArg = process.argv[3]
const plan = JSON.parse(fs.existsSync(planArg) ? fs.readFileSync(planArg, 'utf8') : planArg)

const VIEWPORTS = {
  fhd: { viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 },
  hidpi: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 },
  ipad: { viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2, hasTouch: true },
  phone: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, hasTouch: true },
}

const SCENARIOS = {
  // Successive ×2 zooms at the centre, then home and one ×16 jump (a fast
  // multi-notch wheel / pinch).
  zoomin: [
    { op: 'zoomBy', f: 2 }, { op: 'zoomBy', f: 2 }, { op: 'zoomBy', f: 2 }, { op: 'zoomBy', f: 2 },
    { op: 'home' }, { op: 'zoomBy', f: 16 },
  ],
  // Zoom deep, pan 13 screens in a lawnmower path, then zoom back out home.
  deep: [
    { op: 'zoomBy', f: 32 },
    ...Array(6).fill({ op: 'panBy', dx: 1, dy: 0 }),
    { op: 'panBy', dx: 0, dy: 1 },
    ...Array(6).fill({ op: 'panBy', dx: -1, dy: 0 }),
    { op: 'home' },
  ],
  // Zoom deep, pan 6 screens right, then the same 6 back.
  panback: [
    { op: 'zoomBy', f: 32 },
    ...Array(6).fill({ op: 'panBy', dx: 1, dy: 0 }),
    ...Array(6).fill({ op: 'panBy', dx: -1, dy: 0 }),
  ],
}

const browser = await chromium.launch({ headless: true })
const results = fs.existsSync(out) ? JSON.parse(fs.readFileSync(out, 'utf8')) : []
for (const run of plan) {
  for (let rep = 0; rep < (run.reps || 1); rep++) {
    await fetch(`${BASE}/__config?latency=${run.latency}&cc=${encodeURIComponent(run.cc || 'no-cache')}`)
    const ctx = await browser.newContext(VIEWPORTS[run.vp])
    const page = await ctx.newPage()
    await page.goto(`${BASE}/harness.html?ir=${run.ir}&cache=${run.cache}&drawer=${run.drawer || 'canvas'}&sample=${run.sample === 0 ? 0 : 1}&mtpf=${run.mtpf || 1}`)
    await page.evaluate(() => window.H.ready)
    const info = await page.evaluate(() => window.H.info())
    const steps = []
    for (const cmd of SCENARIOS[run.scenario]) {
      steps.push(await page.evaluate((c) => window.H.step(c), cmd))
    }
    await ctx.close()
    const rec = { ...run, rep, info, steps }
    results.push(rec)
    fs.writeFileSync(out, JSON.stringify(results, null, 1))
    const last = steps[steps.length - 1]
    console.log(JSON.stringify({ ...run, rep, last }))
  }
}
await browser.close()
