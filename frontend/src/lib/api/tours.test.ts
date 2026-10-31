import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeFetch } from '../../test/fakeFetch'
import { applyVisibility } from '../visibility'
import { ApiCodeError } from './shared'
import {
  STEP_CONFLICT_CODE,
  TOUR_CONFLICT_CODE,
  adminAddStep,
  adminAppendStepPin,
  adminAppendStepShape,
  adminDeleteStep,
  adminPublishTour,
  adminReorderSteps,
  adminSetTourUnlisted,
  adminUpdateStep,
  adminUpdateTour,
  stepConflictOf,
  tourConflictOf,
  tourVisibilityApi,
  type TourStep,
} from './tours'

afterEach(() => {
  vi.unstubAllGlobals()
})

const serverStep: TourStep = {
  id: 7, order: 1, label: 'theirs', center_x: 0, center_y: 0, fov_um_w: 1, fov_um_h: 1,
  rotation_deg: 0, pins: [], shapes_l0: [], description_md: '', revision: 4,
}

describe('expected revisions on the wire', () => {
  it('step PATCH carries the step revision in its JSON body', async () => {
    const { requests } = fakeFetch([{ body: { ok: true, revision: 4, step: serverStep } }])
    const res = await adminUpdateStep(3, 7, { label: 'mine' }, 3)
    expect(requests[0]).toMatchObject({ url: '/api/admin/tours/3/steps/7', method: 'PATCH' })
    expect(requests[0].json).toEqual({ label: 'mine', expected_revision: 3 })
    expect(res.revision).toBe(4)
  })

  it('a revision of 0 is sent (not mistaken for "none")', async () => {
    const { requests } = fakeFetch([{ body: { ok: true, revision: 1, tour: {} } }])
    await adminUpdateTour(3, { title: 'T' }, 0)
    expect(requests[0].json).toEqual({ title: 'T', expected_revision: 0 })
  })

  it('omits expected_revision when the caller has none (unconditional write)', async () => {
    const { requests } = fakeFetch([
      { body: { ok: true, revision: 1, step: serverStep } },
      { body: { ok: true, revision: 1 } },
      { body: { ok: true, revision: 1 } },
    ])
    await adminUpdateStep(3, 7, { label: 'x' }, null)
    await adminDeleteStep(3, 7)
    await adminPublishTour(3, true, undefined)
    expect(requests[0].json).toEqual({ label: 'x' })
    expect(requests[1].url).toBe('/api/admin/tours/3/steps/7')
    expect(requests[2].form).toEqual({ publish: '1' })
  })

  it('tour PATCH, reorder, add, delete, publish and unlisted carry the tour revision', async () => {
    const { requests } = fakeFetch([
      { body: { ok: true, revision: 6, tour: {} } },
      { body: { ok: true, count: 2, revision: 7 } },
      { body: { id: 11, revision: 8 } },
      { body: { ok: true, revision: 9 } },
      { body: { ok: true, is_published: true, revision: 10 } },
      { body: { ok: true, unlisted: true, revision: 11 } },
    ])
    await adminUpdateTour(3, { title: 'T' }, 5)
    await adminReorderSteps(3, [2, 1], 6)
    const added = await adminAddStep(3, { order: 3, center_x: 1 }, 7)
    await adminDeleteStep(3, 2, 8)
    await adminPublishTour(3, true, 9)
    await adminSetTourUnlisted(3, true, 10)

    expect(requests[0].json).toEqual({ title: 'T', expected_revision: 5 })
    expect(requests[1].json).toEqual({ step_ids: [2, 1], expected_revision: 6 })
    expect(requests[2].form).toEqual({ order: '3', center_x: '1', expected_revision: '7' })
    expect(added).toEqual({ id: 11, revision: 8 })
    // DELETE has no body: the revision travels in the query string.
    expect(requests[3]).toMatchObject({ method: 'DELETE', url: '/api/admin/tours/3/steps/2?expected_revision=8' })
    expect(requests[4].form).toEqual({ publish: '1', expected_revision: '9' })
    expect(requests[5].form).toEqual({ unlisted: '1', expected_revision: '10' })
  })

  it('appends POST one item and never an expected revision (appends commute)', async () => {
    const { requests } = fakeFetch([
      { body: { ok: true, id: 'pin-1', revision: 5, step: serverStep } },
      { body: { ok: true, id: 'shape-1', revision: 6, step: serverStep } },
    ])
    const pin = { id: '', x: 1, y: 2, shape: 'pin' as const, color: 'red', rotation_deg: 0, label: '', z: 3 }
    const res = await adminAppendStepPin(3, 7, pin)
    await adminAppendStepShape(3, 7, {
      id: '', kind: 'rect', x: 0, y: 0, w: 1, h: 1, stroke_color: 'red', fill_enabled: false,
      fill_opacity: 0.5, points: [], smoothing: true, label: '', label_position: 'above', z: 0,
    })
    expect(requests[0]).toMatchObject({ method: 'POST', url: '/api/admin/tours/3/steps/7/pins', json: pin })
    expect(requests[1]).toMatchObject({ method: 'POST', url: '/api/admin/tours/3/steps/7/shapes' })
    expect('expected_revision' in requests[0].json).toBe(false)
    expect('expected_revision' in requests[1].json).toBe(false)
    expect(res.id).toBe('pin-1')
  })
})

describe('409 conflicts carry the server copy', () => {
  it('a step conflict throws an ApiCodeError whose current step stepConflictOf returns', async () => {
    fakeFetch([{
      status: 409,
      body: { detail: { code: STEP_CONFLICT_CODE, params: { revision: 4 }, current: serverStep } },
    }])
    const err = await adminUpdateStep(3, 7, { label: 'mine' }, 3).catch((e) => e)
    expect(err).toBeInstanceOf(ApiCodeError)
    expect(err.code).toBe(STEP_CONFLICT_CODE)
    expect(err.params).toEqual({ revision: 4 })
    expect(stepConflictOf(err)).toEqual(serverStep)
    expect(tourConflictOf(err)).toBeNull()
  })

  it('a tour conflict exposes the { tour, steps } envelope via tourConflictOf', async () => {
    const current = { tour: { id: 3, title: 'Theirs', revision: 9 }, steps: [serverStep] }
    fakeFetch([{ status: 409, body: { detail: { code: TOUR_CONFLICT_CODE, params: { revision: 9 }, current } } }])
    const err = await adminReorderSteps(3, [7], 8).catch((e) => e)
    expect(tourConflictOf(err)).toEqual(current)
    expect(stepConflictOf(err)).toBeNull()
  })

  it('other errors, including other 409s, are not conflicts', async () => {
    fakeFetch([
      { status: 409, body: { detail: 'order conflicts with an existing step; refresh and retry' } },
      { status: 409, body: { detail: { code: 'adminTour.apiErr.annotationLimit', params: { max: 2 } } } },
    ])
    const plain = await adminAddStep(3, { order: 1 }, 1).catch((e) => e)
    const capped = await adminAppendStepPin(3, 7, {
      id: '', x: 0, y: 0, shape: 'pin', color: 'red', rotation_deg: 0, label: '', z: 0,
    }).catch((e) => e)
    for (const err of [plain, capped, new Error('boom'), null]) {
      expect(stepConflictOf(err)).toBeNull()
      expect(tourConflictOf(err)).toBeNull()
    }
    // A conflict code without its payload can't be resolved from: not one.
    expect(stepConflictOf(new ApiCodeError(STEP_CONFLICT_CODE))).toBeNull()
    expect(tourConflictOf(new ApiCodeError(TOUR_CONFLICT_CODE, undefined, { tour: {} }))).toBeNull()
  })
})

describe('tourVisibilityApi', () => {
  it("threads the revision from one call into the next, so this tab's own first call is no conflict", async () => {
    const { requests } = fakeFetch([
      { body: { ok: true, unlisted: true, revision: 4 } },
      { body: { ok: true, is_published: true, revision: 5 } },
    ])
    const vis = tourVisibilityApi(3, 3)
    // draft -> link-only: unlisted first, then publish (lib/visibility).
    const r = await applyVisibility({ is_published: false, unlisted: false }, 'unlisted', vis.api)
    expect(r).toMatchObject({ is_published: true, unlisted: true })
    expect(requests.map((q) => [q.url, q.form?.expected_revision])).toEqual([
      ['/api/admin/tours/3/unlisted', '3'],
      ['/api/admin/tours/3/publish', '4'],
    ])
    expect(vis.revision()).toBe(5)
  })

  it('keeps the last confirmed revision when a later call fails', async () => {
    fakeFetch([
      { body: { ok: true, unlisted: true, revision: 4 } },
      { status: 409, body: { detail: 'Cannot publish a tour while its parent slide is a draft' } },
    ])
    const vis = tourVisibilityApi(3, 3)
    await expect(applyVisibility({ is_published: false, unlisted: false }, 'unlisted', vis.api)).rejects.toThrow()
    expect(vis.revision()).toBe(4)
  })
})
