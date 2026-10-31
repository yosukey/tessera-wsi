import { describe, expect, it } from 'vitest'
import type { TourStep } from './api'
import { applyStepOrder, mergeStep, resolveStepSave, type StepSaveResult } from './tourEditHelpers'

function step(id: number, revision: number | undefined, label = `s${id}`): TourStep {
  return {
    id, order: id, label, center_x: 0, center_y: 0, fov_um_w: 1, fov_um_h: 1,
    rotation_deg: 0, pins: [], shapes_l0: [], description_md: '', revision,
  }
}

describe('resolveStepSave: what a finished save does to the step editor', () => {
  const sent = JSON.stringify({ label: 'sent' })
  const typedLater = JSON.stringify({ label: 'sent + typed while saving' })
  const saved = (revision: number, reloadError: string | null = null): StepSaveResult =>
    ({ kind: 'saved', revision, reloadError })

  it('closes the editor when it still shows exactly what was saved', () => {
    const r = resolveStepSave(7, sent, { stepId: 7, state: sent }, saved(3))
    expect(r).toEqual({
      close: true, baseline: null, revision: null, conflict: null,
      message: { key: 'adminTour.stepUpdated' },
    })
  })

  it('keeps edits typed during the save open, re-baselined at the new revision', () => {
    const r = resolveStepSave(7, sent, { stepId: 7, state: typedLater }, saved(3))
    expect(r.close).toBe(false)
    // Only what came after Save is unsaved now...
    expect(r.baseline).toBe(sent)
    // ...and the next save must expect the revision this one produced, or
    // the editor would conflict with its own save.
    expect(r.revision).toBe(3)
    expect(r.conflict).toBeNull()
    expect(r.message.key).toBe('adminTour.stepUpdatedEditsKept')
  })

  it('keeps the editor open, saved, when the reload after the save failed', () => {
    const r = resolveStepSave(7, sent, { stepId: 7, state: sent }, saved(3, 'offline'))
    expect(r).toMatchObject({ close: false, baseline: sent, revision: 3, conflict: null })
    expect(r.message).toEqual({ key: 'adminTour.reloadAfterSaveFailed', params: { error: 'offline' } })
  })

  it('touches no editor state once the editor was cancelled mid-save', () => {
    const r = resolveStepSave(7, sent, { stepId: null, state: null }, saved(3))
    expect(r).toEqual({
      close: false, baseline: null, revision: null, conflict: null,
      message: { key: 'adminTour.stepUpdated' },
    })
  })

  it('on a conflict keeps every edit and the old baseline/revision, and offers the server copy', () => {
    const server = step(7, 5, 'theirs')
    for (const live of [sent, typedLater]) {
      const r = resolveStepSave(7, sent, { stepId: 7, state: live }, { kind: 'conflict', server })
      // Nothing was saved: no close, no re-baseline (Cancel still asks), no
      // revision change (a plain Save conflicts again; only Overwrite sends 5).
      expect(r).toEqual({
        close: false, baseline: null, revision: null, conflict: server,
        message: { key: 'adminTour.stepConflict' },
      })
    }
  })

  it('reports a conflict on a cancelled editor without offering to resolve it', () => {
    const r = resolveStepSave(7, sent, { stepId: null, state: null }, { kind: 'conflict', server: step(7, 5) })
    expect(r.conflict).toBeNull()
    expect(r.message.key).toBe('adminTour.stepConflictClosed')
  })

  it('clears a pending conflict once a save (e.g. "Overwrite with mine") lands', () => {
    const r = resolveStepSave(7, sent, { stepId: 7, state: typedLater }, saved(6))
    expect(r.conflict).toBeNull()
    expect(r.revision).toBe(6)
  })
})

describe('mergeStep: folding a server copy of one step into the list', () => {
  it('replaces the step when the incoming copy is as new or newer', () => {
    const list = [step(1, 0), step(2, 4)]
    expect(mergeStep(list, step(2, 5, 'new'))[1].label).toBe('new')
    expect(mergeStep(list, step(2, 4, 'same rev'))[1].label).toBe('same rev')
  })

  it('keeps a newer local copy, so out-of-order responses cannot drop an append', () => {
    // Two "copy to next step" appends: the second response (revision 2)
    // arrived first; the first's (revision 1) must not undo it.
    const afterSecond = mergeStep([step(2, 0)], { ...step(2, 2), pins: [
      { id: 'a', x: 0, y: 0, shape: 'pin', color: 'red', rotation_deg: 0, label: '', z: 0 },
      { id: 'b', x: 0, y: 0, shape: 'pin', color: 'red', rotation_deg: 0, label: '', z: 0 },
    ] })
    const afterFirst = mergeStep(afterSecond, step(2, 1))
    expect(afterFirst).toBe(afterSecond)
    expect(afterFirst[0].pins.map((p) => p.id)).toEqual(['a', 'b'])
  })

  it('ignores steps the list does not hold and returns the same array', () => {
    const list = [step(1, 0)]
    expect(mergeStep(list, step(9, 3))).toBe(list)
  })

  it('treats a copy without a revision as the oldest', () => {
    expect(mergeStep([step(1, undefined, 'old')], step(1, 0, 'new'))[0].label).toBe('new')
    expect(mergeStep([step(1, 0, 'kept')], step(1, undefined, 'unknown'))[0].label).toBe('kept')
  })
})

describe('applyStepOrder: the step list after a reorder or delete, without a reload', () => {
  it('renumbers a reorder 1..n in the requested sequence', () => {
    const out = applyStepOrder([step(1, 0), step(2, 0), step(3, 0)], [3, 1, 2])
    expect(out.map((s) => [s.id, s.order])).toEqual([[3, 1], [1, 2], [2, 3]])
  })

  it('closes the gap a delete leaves, the way the server does', () => {
    const steps = [step(1, 0), step(2, 0), step(3, 0)]
    const out = applyStepOrder(steps, [1, 3])
    expect(out.map((s) => [s.id, s.order])).toEqual([[1, 1], [3, 2]])
    // Unchanged steps keep their identity.
    expect(out[0]).toBe(steps[0])
  })
})
