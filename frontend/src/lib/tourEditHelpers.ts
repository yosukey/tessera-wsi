import type { PinItem, Shape, TourStep, TransitionMode } from './api'
import { makeUid } from './uid'

export type Step = TourStep

export type EditState = {
  label: string
  description_md: string
  rotation_deg: string
  // Physical µm as shown in the editor; converted to/from the stored units
  // with fovToPhysical / fovFromPhysical (lib/tourStepView).
  fov_um_w: string
  fov_um_h: string
  center_x: string
  center_y: string
  pins: PinItem[]
  shapes_l0: Shape[]
  transition_mode: TransitionMode
}

export function stepPins(step: Step | null | undefined): PinItem[] {
  if (!step) return []
  return (step.pins ?? []).map((p) => ({
    ...p,
    label: p.label || '',
  }))
}

export function makePinId(): string {
  return makeUid('pin')
}

export function makeShapeId(): string {
  return makeUid('shape')
}

// The step list as the server leaves it after a reorder or a delete: the
// steps named in `ids`, numbered 1..n in that sequence; any other step is
// gone. A reorder passes the requested sequence, a delete the remaining steps
// in their current order (the server closes the gap the same way). Exact when
// the write expected the revision this list was loaded at, so the editor can
// apply it without reloading the tour.
export function applyStepOrder(steps: Step[], ids: number[]): Step[] {
  const byId = new Map(steps.map((s) => [s.id, s]))
  const out: Step[] = []
  ids.forEach((id, i) => {
    const s = byId.get(id)
    if (s) out.push(s.order === i + 1 ? s : { ...s, order: i + 1 })
  })
  return out
}

// Fold a server copy of one step into the list, unless the list already holds
// a newer revision of it. Responses to overlapping requests (several "copy to
// next step" appends to one target, a save racing a refresh) can arrive out
// of order, and an older copy must not undo a newer one. A step the list
// doesn't hold is ignored: adding / removing steps is a structure change the
// caller reloads the tour for. Returns `steps` itself when nothing changed.
export function mergeStep(steps: Step[], incoming: Step): Step[] {
  let changed = false
  const next = steps.map((s) => {
    if (s.id !== incoming.id) return s
    // A copy without a revision (a server that sends no revision) counts as oldest.
    if ((s.revision ?? -1) > (incoming.revision ?? -1)) return s
    changed = true
    return incoming
  })
  return changed ? next : steps
}

// What a step save came back with (AdminTourEditor.saveEdit).
export type StepSaveResult =
  // Persisted; `revision` is the step revision this save produced.
  // `reloadError` is set when the follow-up tour reload failed — the save
  // itself still landed.
  | { kind: 'saved'; revision: number; reloadError: string | null }
  // Refused with 409: the step changed elsewhere (another window, another
  // admin) after the revision the editor sent. `server` is its current copy,
  // straight from the 409 payload.
  | { kind: 'conflict'; server: Step }

export type StepSaveResolution = {
  // Close the editor: the save landed and the editor still shows exactly what
  // it sent.
  close: boolean
  // For an editor that stays open on the saved step: the new baseline (JSON of
  // the editor state the server now holds) and the revision its next save must
  // expect. null leaves the current ones in place.
  baseline: string | null
  revision: number | null
  // The server copy to offer "Reload (discard my changes)" / "Overwrite with
  // mine" against; null clears any earlier conflict.
  conflict: Step | null
  message: { key: string; params?: Record<string, string | number> }
}

// Decide what a finished step save does to the editor. `submitted` is the
// JSON of the editor state the request carried; `live` is the editor as it is
// now — the fields stay editable while a save is in flight, so it may hold
// newer edits, or have been cancelled (stepId null).
export function resolveStepSave(
  stepId: number,
  submitted: string,
  live: { stepId: number | null; state: string | null },
  result: StepSaveResult,
): StepSaveResolution {
  const sameEditor = live.stepId === stepId && live.state !== null
  if (result.kind === 'conflict') {
    // Nothing was saved. The editor keeps every edit — including any typed
    // while the request was out — and its baseline and revision, so Cancel
    // still asks before discarding and a plain Save conflicts again; only the
    // explicit "Overwrite with mine" sends the server's revision.
    return {
      close: false,
      baseline: null,
      revision: null,
      conflict: sameEditor ? result.server : null,
      message: { key: sameEditor ? 'adminTour.stepConflict' : 'adminTour.stepConflictClosed' },
    }
  }
  if (sameEditor && live.state === submitted && !result.reloadError) {
    return { close: true, baseline: null, revision: null, conflict: null, message: { key: 'adminTour.stepUpdated' } }
  }
  // Keep the editor open: either it has edits newer than the request, or the
  // reload failed and the step card behind it would show stale values. The
  // submitted state is now what's saved, at the revision this save produced,
  // so only changes made after Save count as unsaved (Cancel won't prompt for
  // already-saved ones) and the next save doesn't conflict with this one.
  return {
    close: false,
    baseline: sameEditor ? submitted : null,
    revision: sameEditor ? result.revision : null,
    conflict: null,
    message: result.reloadError
      ? { key: 'adminTour.reloadAfterSaveFailed', params: { error: result.reloadError } }
      : { key: sameEditor ? 'adminTour.stepUpdatedEditsKept' : 'adminTour.stepUpdated' },
  }
}
