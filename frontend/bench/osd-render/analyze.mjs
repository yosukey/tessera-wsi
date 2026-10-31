// Summarise bench results into Markdown tables.
// usage: node analyze.mjs res1.json [res2.json ...]
import fs from 'node:fs'

const runs = process.argv.slice(2).flatMap((f) => JSON.parse(fs.readFileSync(f, 'utf8')))
const median = (xs) => {
  const a = xs.filter((x) => x != null).sort((p, q) => p - q)
  if (!a.length) return null
  const m = Math.floor(a.length / 2)
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2
}
// Every setting a run can vary; runs differing in any of them are never merged.
const key = (r) => [r.scenario, r.vp, r.latency, r.ir, r.cache, r.sample === 0 ? 0 : 1, r.mtpf || 1,
  r.cc || 'no-cache', r.drawer || 'canvas'].join('|')
const groups = new Map()
for (const r of runs) {
  const k = key(r)
  if (!groups.has(k)) groups.set(k, [])
  groups.get(k).push(r)
}

// Aggregate the steps of one scenario that matter for it:
//   zoomin: the 4 successive x2 steps (sum), the zoom-out home, the x16 jump
//   deep:   the 13 pans (sum) and the final zoom-out home
//   panback: the 6 outbound pans (sum) and the 6 return pans (sum)
function parts(r) {
  const s = r.steps
  const sum = (xs) => ({
    settle_ms: xs.reduce((a, q) => a + q.settle_ms, 0),
    req200: xs.reduce((a, q) => a + q.req200, 0),
    req304: xs.reduce((a, q) => a + q.req304, 0),
    blank_area_ms: xs.reduce((a, q) => a + q.blank_area_ms, 0),
    low_area_ms: xs.reduce((a, q) => a + q.low_area_ms, 0),
    max_blank: Math.max(...xs.map((q) => q.max_blank)),
    // Tiles OSD loaded, whether or not the request reached the server
    // (Chromium reuses an image already loaded in the page without asking).
    tile_loaded: xs.reduce((a, q) => a + q.tile_loaded, 0),
    frames: xs.reduce((a, q) => a + q.frames, 0),
    cache_records: xs[xs.length - 1].cache_records,
  })
  if (r.scenario === 'zoomin') return { 'zoom x2 (x4)': sum(s.slice(0, 4)), 'home': sum([s[4]]), 'jump x16': sum([s[5]]) }
  if (r.scenario === 'deep') return { 'zoom x32': sum([s[0]]), 'pan x13': sum(s.slice(1, 14)), 'home': sum([s[14]]) }
  if (r.scenario === 'panback') return { 'pan out x6': sum(s.slice(1, 7)), 'pan back x6': sum(s.slice(7, 13)) }
  return {}
}

const rows = []
for (const [k, rs] of groups) {
  const [scenario, vp, latency, ir, cache, sample, mtpf, cc, drawer] = k.split('|')
  const ps = rs.map(parts)
  for (const part of Object.keys(ps[0])) {
    const m = (f) => median(ps.map((p) => p[part][f]))
    rows.push({ scenario, vp, latency, ir, cache, sample, mtpf, cc, drawer, part, n: rs.length, loads: m('tile_loaded'),
      settle: m('settle_ms'), r200: m('req200'), r304: m('req304'), blank: m('blank_area_ms'),
      low: m('low_area_ms'), maxBlank: m('max_blank'), frames: m('frames'), records: m('cache_records') })
  }
}
const order = (r) => [r.scenario, r.vp, r.part, r.latency.padStart(4), r.cc, r.drawer, r.sample, r.mtpf, r.ir, r.cache.padStart(5)].join('|')
rows.sort((a, b) => order(a).localeCompare(order(b)))
console.log('| scenario | viewport | part | lat ms | tile Cache-Control | drawer | sampled | tiles/frame | immediate | cache | n | settle ms | tiles loaded | 200 | 304 | blank ms·area | low-res ms·area | max blank | fps | records |')
console.log('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')
for (const r of rows) {
  const fps = r.frames && r.settle ? Math.round((r.frames / r.settle) * 1000) : ''
  console.log(`| ${r.scenario} | ${r.vp} | ${r.part} | ${r.latency} | ${r.cc} | ${r.drawer} | ${r.sample === '1' ? 'y' : 'n'} | ${r.mtpf} | ${r.ir === '1' ? 'on' : 'off'} | ${r.cache} | ${r.n} | ${r.settle} | ${r.loads} | ${r.r200} | ${r.r304} | ${r.sample === '1' ? r.blank : '—'} | ${r.sample === '1' ? r.low : '—'} | ${r.sample === '1' ? Math.round(r.maxBlank * 100) + '%' : '—'} | ${fps} | ${r.records} |`)
}
