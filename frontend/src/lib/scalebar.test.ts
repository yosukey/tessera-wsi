// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import type OpenSeadragon from 'openseadragon'
import { attachScaleBar, formatLengthUm, niceLength, slidePixelSize, type PixelSize } from './scalebar'
import { fakeViewer } from '../test/fakeViewer'

function render(rotDeg: number, mpp: number | PixelSize, imgPxPerScreenPx = 1) {
  const container = document.createElement('div')
  const viewer = fakeViewer(rotDeg, imgPxPerScreenPx)
  const detach = attachScaleBar({ viewer: viewer as unknown as OpenSeadragon.Viewer, mpp, container })
  const [label, barWrap] = Array.from(container.children) as HTMLElement[]
  const out = { text: label.textContent ?? '', px: parseFloat(barWrap.style.width), viewer }
  detach()
  return out
}

function parseUm(text: string): number {
  const m = /^([\d.]+) (nm|µm|mm)$/.exec(text)
  if (!m) return NaN
  const v = parseFloat(m[1])
  return m[2] === 'nm' ? v / 1000 : m[2] === 'mm' ? v * 1000 : v
}

// Physical length of a screen-horizontal bar of `px` screen px.
function physical(px: number, rotDeg: number, mx: number, my: number) {
  const t = (rotDeg * Math.PI) / 180
  return px * Math.hypot(mx * Math.cos(t), my * Math.sin(t))
}

describe('slidePixelSize', () => {
  it('keeps both axes and falls back to square pixels on one axis', () => {
    expect(slidePixelSize({ mpp_x: 0.25, mpp_y: 0.5 })).toEqual({ x: 0.25, y: 0.5 })
    expect(slidePixelSize({ mpp_x: 0.3, mpp_y: 0 })).toEqual({ x: 0.3, y: 0.3 })
    expect(slidePixelSize({ mpp_x: 0, mpp_y: 0.4 })).toEqual({ x: 0.4, y: 0.4 })
    expect(slidePixelSize({ mpp_x: 0, mpp_y: 0 })).toBeNull()
    expect(slidePixelSize({ mpp_x: Number.NaN, mpp_y: Number.POSITIVE_INFINITY })).toBeNull()
  })
})

describe('attachScaleBar on anisotropic pixels', () => {
  const aniso = { x: 0.25, y: 0.5 }

  it.each([0, 30, 45, 90, 135, 180, 270, -60])('labels the bar with its physical length at %i°', (rot) => {
    const r = render(rot, aniso)
    expect(parseUm(r.text)).toBeCloseTo(physical(r.px, rot, aniso.x, aniso.y), 9)
  })

  it('measures along image Y after a 90° rotation', () => {
    // A single-factor bar would read 20 µm here: the screen-horizontal
    // bar spans Y pixels at 0.5 µm/px once rotated.
    const r = render(90, aniso)
    expect(r.text).toBe(`${Math.round(physical(r.px, 90, 0.25, 0.5))} µm`)
    expect(physical(r.px, 90, 0.25, 0.5)).toBeCloseTo(r.px * 0.5, 9)
  })

  it('removes every handler it registered', () => {
    const container = document.createElement('div')
    const viewer = fakeViewer(0)
    const detach = attachScaleBar({ viewer: viewer as unknown as OpenSeadragon.Viewer, mpp: aniso, container })
    expect(viewer.handlerCount()).toBeGreaterThan(0)
    detach()
    expect(viewer.handlerCount()).toBe(0)
  })
})

describe('scale bar labels', () => {
  it('never rounds a sub-micrometre bar to "0 µm"', () => {
    expect(render(0, 0.002).text).toBe('200 nm')
  })

  it('keeps label == drawn length and the bar within [56, 140] px over 15 decades', () => {
    for (let e = -12; e <= 3; e += 0.137) {
      const umPerPx = 10 ** e
      const r = render(0, umPerPx)
      const lab = parseUm(r.text)
      expect(lab).toBeGreaterThan(0)
      expect(Math.abs(lab - r.px * umPerPx) / lab).toBeLessThan(1e-6)
      expect(r.px).toBeGreaterThanOrEqual(140 / 2.5 - 1e-9)
      expect(r.px).toBeLessThanOrEqual(140 + 1e-9)
    }
  })

  it('formats by significant digits with nm / µm / mm', () => {
    const cases: [number, string][] = [
      [0.0005, '0.5 nm'], [0.2, '200 nm'], [1, '1 µm'], [500, '500 µm'],
      [1000, '1 mm'], [2000, '2 mm'], [10000, '10 mm'], [2e-4, '0.2 nm'],
    ]
    for (const [um, text] of cases) expect(formatLengthUm(um)).toBe(text)
  })

  it('picks the largest 1-2-5 length that fits', () => {
    expect([niceLength(1, 140), niceLength(0.002, 140), niceLength(3.3, 140)]).toEqual([100, 0.2, 200])
  })
})
