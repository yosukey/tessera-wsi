import { describe, expect, it } from 'vitest'
import type { ImportPlan, ImportPlanSlide } from '../api'
import {
  importResolutionsToSend,
  importRowChoosable,
  initialImportResolutions,
  keptStageActions,
  retryFailedResolutions,
} from './importRetry'

function slide(public_id: string, over: Partial<ImportPlanSlide> = {}): ImportPlanSlide {
  return {
    public_id, catalog_id: '', title: public_id, tour_count: 0,
    wsi_original_basename: `${public_id}.svs`, wsi_present_in_bundle: true,
    filename_conflict: false, public_id_conflict: false, catalog_id_conflict: false,
    content_errors: [], content_error_count: 0, assets_over_limit: 0, credit_skipped: [],
    ...over,
  }
}

function plan(slides: ImportPlanSlide[], applied = false): ImportPlan {
  return {
    stage_id: 'import-000000000000', bundle_app_version: '1',
    blocking_code: null, blocking_reason: null, slide_count: slides.length, tour_count: 0,
    slides, unknown_fields: [], data_space: null, applied,
  }
}

describe('initialImportResolutions', () => {
  it('defaults conflicts to copy on a fresh bundle', () => {
    const p = plan([slide('a', { filename_conflict: true }), slide('b')])
    expect(initialImportResolutions(p)).toEqual({ a: 'copy' })
  })

  it('keeps an earlier skip and leaves imported slides out', () => {
    const p = plan([
      // Imported last time: its own copy now holds the filename, but it is
      // not re-importable, so it gets no choice at all.
      slide('imp', { filename_conflict: true, apply_status: 'imported' }),
      slide('skp', { filename_conflict: true, apply_status: 'skipped' }),
      slide('fld', { filename_conflict: true, apply_status: 'failed' }),
    ], true)
    expect(initialImportResolutions(p)).toEqual({ skp: 'skip', fld: 'copy' })
  })
})

describe('a slide this system already has (public_id_conflict)', () => {
  it('defaults to skip, so re-uploading an applied bundle does not duplicate it', () => {
    const p = plan([
      slide('dup', { filename_conflict: true, public_id_conflict: true }),
      slide('ren', { public_id_conflict: true }), // its file renamed since
      slide('name', { filename_conflict: true }),
      slide('new'),
    ])
    expect(initialImportResolutions(p)).toEqual({ dup: 'skip', ren: 'skip', name: 'copy' })
    // Sent explicitly: the backend's default for an unnamed slide is copy.
    expect(importResolutionsToSend(p, initialImportResolutions(p)))
      .toEqual({ dup: 'skip', ren: 'skip', name: 'copy' })
  })

  it('offers the choice for it, but not for a slide imported from this stage', () => {
    expect(importRowChoosable(slide('a', { public_id_conflict: true }))).toBe(true)
    expect(importRowChoosable(slide('b', { filename_conflict: true }))).toBe(true)
    expect(importRowChoosable(slide('c', { apply_status: 'skipped' }))).toBe(true)
    expect(importRowChoosable(slide('d'))).toBe(false)
    expect(importRowChoosable(slide('e', { public_id_conflict: true, apply_status: 'imported' }))).toBe(false)
  })
})

describe('importResolutionsToSend', () => {
  it('passes a fresh plan through unchanged', () => {
    const p = plan([slide('a', { filename_conflict: true })])
    expect(importResolutionsToSend(p, { a: 'skip' })).toEqual({ a: 'skip' })
  })

  it('omits an unchanged earlier skip so the backend leaves the slide alone', () => {
    // Naming it with "skip" would re-attempt it — and import it if its
    // conflict has gone since.
    const p = plan([
      slide('skp', { apply_status: 'skipped' }),
      slide('chg', { apply_status: 'skipped', filename_conflict: true }),
      slide('imp', { apply_status: 'imported' }),
      slide('fld', { apply_status: 'failed', filename_conflict: true }),
    ], true)
    expect(importResolutionsToSend(p, { skp: 'skip', chg: 'copy', imp: 'copy', fld: 'skip' }))
      .toEqual({ chg: 'copy', fld: 'skip' })
  })
})

describe('retryFailedResolutions', () => {
  it("repeats only the failed slides' choices", () => {
    expect(retryFailedResolutions(['b', 'c'], { a: 'skip', b: 'copy' })).toEqual({ b: 'copy' })
  })
})

describe('keptStageActions', () => {
  it('is null unless the stage was kept', () => {
    expect(keptStageActions({ stage_kept: false, retry: { failed_slides: 1, skipped_assets: 0 } })).toBeNull()
  })

  it('reports what can be retried', () => {
    expect(keptStageActions({ stage_kept: true, retry: { failed_slides: 2, skipped_assets: 3 } }))
      .toEqual({ failed_slides: 2, skipped_assets: 3 })
  })
})
