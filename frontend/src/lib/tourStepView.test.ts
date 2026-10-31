// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { FALLBACK_MPP, fovFromPhysical, fovToPhysical, stepMpp } from './tourStepView'

describe('stored FOV units', () => {
  it('uses one reference factor: mpp_x, then mpp_y, then the fallback', () => {
    expect(stepMpp({ mpp_x: 0.25, mpp_y: 0.5 })).toBe(0.25)
    expect(stepMpp({ mpp_x: 0, mpp_y: 0.5 })).toBe(0.5)
    expect(stepMpp({})).toBe(FALLBACK_MPP)
  })

  it('is the identity for square or uncalibrated pixels', () => {
    for (const slide of [{ mpp_x: 0.25, mpp_y: 0.25 }, { mpp_x: 0.3 }, {}]) {
      for (const rot of [0, 37, 90]) {
        expect(fovToPhysical(slide, rot, { w: 600, h: 450 })).toEqual({ w: 600, h: 450 })
      }
    }
  })

  it('converts per screen axis on anisotropic slides, rotation-aware', () => {
    const slide = { mpp_x: 0.25, mpp_y: 0.5 }
    // 1000 × 800 level-0 px at 0°: stored as 250 × 200 (reference factor
    // 0.25), physically 250 × 400 µm.
    expect(fovToPhysical(slide, 0, { w: 250, h: 200 })).toEqual({ w: 250, h: 400 })
    // At 90° the screen width runs along image Y.
    const p90 = fovToPhysical(slide, 90, { w: 250, h: 200 })
    expect(p90.w).toBeCloseTo(500, 9)
    expect(p90.h).toBeCloseTo(200, 9)
  })

  it('round-trips through the editor exactly', () => {
    const slide = { mpp_x: 0.25, mpp_y: 0.5 }
    for (const rot of [0, 12.5, 45, 90, 181]) {
      const stored = { w: 321.5, h: 123.25 }
      const back = fovFromPhysical(slide, rot, fovToPhysical(slide, rot, stored))
      expect(back.w).toBeCloseTo(stored.w, 9)
      expect(back.h).toBeCloseTo(stored.h, 9)
    }
  })
})
