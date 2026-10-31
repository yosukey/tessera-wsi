// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import {
  MAX_FREEHAND_POINTS,
  capFreehandPoints,
  findOverCapShape,
  freehandStroke,
  normalizeShape,
} from './tourShapes'

type P = { x: number; y: number }

// A long wiggly stroke sampled every ~1.2 image px.
function wiggle(n: number): P[] {
  const pts: P[] = []
  for (let i = 0; i < n; i++) pts.push({ x: i * 1.2, y: 40 * Math.sin(i / 25) })
  return pts
}

function distToSegment(p: P, a: P, b: P) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const L = dx * dx + dy * dy
  const t = L ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L)) : 0
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
}

describe('freehand point cap', () => {
  it('matches the server limit', () => {
    // services/app/wsi_app/schemas.py rejects shapes above this.
    expect(MAX_FREEHAND_POINTS).toBe(4096)
  })

  it('keeps a stroke under the cap exactly as sampled', () => {
    const raw = wiggle(500)
    const s = freehandStroke(raw[0], 1)
    raw.slice(1).forEach((p) => s.add(p))
    expect(s.points()).toEqual(raw)
  })

  it('never exceeds the cap and never cuts the tail', () => {
    for (const n of [4097, 10_000, 50_000]) {
      const raw = wiggle(n)
      const s = freehandStroke(raw[0], 1)
      for (const p of raw.slice(1)) {
        s.add(p)
      }
      const pts = s.points()
      expect(pts.length).toBeLessThanOrEqual(MAX_FREEHAND_POINTS)
      expect(pts[0]).toBe(raw[0])
      expect(pts[pts.length - 1]).toBe(raw[raw.length - 1])
      // Thinned, not truncated: far more than a token handful of vertices.
      expect(pts.length).toBeGreaterThan(MAX_FREEHAND_POINTS / 2)
    }
  })

  it('drops jitter below the minimum spacing', () => {
    const s = freehandStroke({ x: 0, y: 0 }, 1)
    expect(s.add({ x: 0.5, y: 0 })).toBe(false)
    expect(s.add({ x: 2, y: 0 })).toBe(true)
    expect(s.points()).toEqual([{ x: 0, y: 0 }, { x: 2, y: 0 }])
  })

  it('caps other strokes with bounded error, keeping both ends', () => {
    const raw = wiggle(20_000)
    const capped = capFreehandPoints(raw)
    expect(capped.length).toBeLessThanOrEqual(MAX_FREEHAND_POINTS)
    expect(capped[0]).toBe(raw[0])
    expect(capped[capped.length - 1]).toBe(raw[raw.length - 1])
    // Every raw sample lies close to the kept polyline.
    const idx = new Map(raw.map((p, i) => [p, i] as const))
    let worst = 0
    for (let j = 0; j + 1 < capped.length; j++) {
      const a = idx.get(capped[j])!
      const b = idx.get(capped[j + 1])!
      for (let i = a; i <= b; i++) worst = Math.max(worst, distToSegment(raw[i], capped[j], capped[j + 1]))
    }
    expect(worst).toBeLessThan(10)
    expect(capFreehandPoints(raw.slice(0, 10))).toEqual(raw.slice(0, 10))
  })

  it('normalizeShape caps freehand points; findOverCapShape names the culprit', () => {
    const big = wiggle(9000)
    const shape = normalizeShape({ id: 's1', kind: 'freehand', x: 0, y: 0, w: 1, h: 1, points: big })
    expect(shape.points.length).toBeLessThanOrEqual(MAX_FREEHAND_POINTS)
    const ok = normalizeShape({ id: 'a', kind: 'rect', x: 0, y: 0, w: 1, h: 1 })
    expect(findOverCapShape([ok, shape])).toBe(-1)
    expect(findOverCapShape([ok, { ...shape, points: big }])).toBe(1)
  })
})
