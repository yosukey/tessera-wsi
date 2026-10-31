// Pure helpers behind the slide-import retry actions (AdminImportSection).
// The backend keeps an import's stage — the uploaded bundle, extracted — while
// a retry could still do something, and re-running apply on it only attempts
// the slides an earlier run did not import (slide_transfer.apply_import).
import type { ImportPlan, ImportPlanSlide, ImportResolution, ImportRetryInfo } from '../api'

// Initial per-slide choices for a preflight table. A slide whose public_id a
// slide here already carries — this bundle, most likely, imported before —
// defaults to "skip": "copy" would add a second, published duplicate of it
// under a new id, for every slide of a re-uploaded, already-applied bundle.
// The operator can still choose "copy". A slide that only
// clashes by its file name defaults to "copy" (imported under a renamed
// file). One an earlier apply of this same stage chose to skip keeps "skip",
// so re-applying doesn't quietly turn it into a copy. Slides that apply
// already imported get no entry: they are not re-importable.
//
// Every conflicting slide gets an explicit entry (sent with the apply): the
// backend's own default for an unnamed slide stays "copy".
export function initialImportResolutions(plan: ImportPlan): Record<string, ImportResolution> {
  const init: Record<string, ImportResolution> = {}
  for (const s of plan.slides) {
    if (s.apply_status === 'imported') continue
    if (s.apply_status === 'skipped' || s.public_id_conflict) init[s.public_id] = 'skip'
    else if (s.filename_conflict) init[s.public_id] = 'copy'
  }
  return init
}

// Whether a preflight row offers the copy / skip choice: it collides with
// this library (by file name or public_id), or an earlier apply skipped it.
export function importRowChoosable(s: ImportPlanSlide): boolean {
  if (s.apply_status === 'imported') return false
  return s.filename_conflict || s.public_id_conflict || s.apply_status === 'skipped'
}

// The resolutions to send for an apply. On a re-apply the backend leaves a
// previously skipped slide alone unless the request names it — and naming it
// with "skip" would re-attempt it, importing it after all if its filename
// conflict has gone since. So an unchanged "skip" on such a row is left out,
// as is anything for an already-imported row.
export function importResolutionsToSend(
  plan: ImportPlan,
  resolutions: Record<string, ImportResolution>,
): Record<string, ImportResolution> {
  const status = new Map(plan.slides.map((s) => [s.public_id, s.apply_status ?? null]))
  const out: Record<string, ImportResolution> = {}
  for (const [pid, value] of Object.entries(resolutions)) {
    const st = status.get(pid)
    if (st === 'imported') continue
    if (st === 'skipped' && value === 'skip') continue
    out[pid] = value
  }
  return out
}

// "Retry failed slides" re-applies with the choices the operator made for
// those slides — and only those, so a slide they skipped stays skipped.
export function retryFailedResolutions(
  failedIds: string[],
  last: Record<string, ImportResolution>,
): Record<string, ImportResolution> {
  const out: Record<string, ImportResolution> = {}
  for (const pid of failedIds) {
    if (last[pid]) out[pid] = last[pid]
  }
  return out
}

// Which retry actions a finished apply / attachment retry offers: null when
// the server did not keep the stage (nothing left to retry).
export function keptStageActions(
  data: { stage_kept: boolean; retry: ImportRetryInfo },
): ImportRetryInfo | null {
  if (!data.stage_kept) return null
  return { failed_slides: data.retry.failed_slides, skipped_assets: data.retry.skipped_assets }
}
