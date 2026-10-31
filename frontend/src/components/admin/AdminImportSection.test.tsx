// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API module is the section's only I/O; everything else (i18n, Button,
// error formatting) is real.
const api = vi.hoisted(() => ({
  adminStageSlideImport: vi.fn(),
  adminApplySlideImport: vi.fn(),
  adminRetrySlideImportAssets: vi.fn(),
  adminGetSlideImportStage: vi.fn(),
  adminCancelSlideImport: vi.fn(),
  pollTransferJob: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import { ApiCodeError, clearAdminSession } from '../../lib/api'
import type { ImportPlanSlide, ImportResult } from '../../lib/api'
import { STORAGE_KEYS } from '../../lib/storageKeys'
import AdminImportSection from './AdminImportSection'

const STAGE = 'import-aaaaaaaaaaaa'
const OLDER = 'import-older000000'
// The job the upload starts, which stages the bundle on the server.
const STAGE_JOB = 'stage-job'

function planSlide(public_id: string, over: Partial<ImportPlanSlide> = {}): ImportPlanSlide {
  return {
    public_id, catalog_id: '', title: `Slide ${public_id}`, tour_count: 0,
    wsi_original_basename: `${public_id}.svs`, wsi_present_in_bundle: true,
    filename_conflict: false, public_id_conflict: false, catalog_id_conflict: false,
    content_errors: [], content_error_count: 0, assets_over_limit: 0, credit_skipped: [],
    ...over,
  }
}

function stageResponse(slides: ImportPlanSlide[], extra: Record<string, unknown> = {}) {
  return {
    stage_id: STAGE,
    plan: {
      stage_id: STAGE, bundle_app_version: '1.0.0',
      blocking_code: null, blocking_reason: null, slide_count: slides.length, tour_count: 0,
      slides, unknown_fields: [], data_space: null, ...extra,
    },
  }
}

function applyResult(over: Partial<ImportResult>): ImportResult {
  return {
    imported: [], skipped: [], failed: [], imported_count: 0, skipped_count: 0,
    failed_count: 0, warnings: [], warning_count: 0, stage_id: STAGE,
    already_imported: [], already_imported_count: 0, stage_kept: false,
    retry: { failed_slides: 0, skipped_assets: 0 },
    ...over,
  }
}

function succeeded(result: unknown) {
  return { id: 'j', kind: 'slide_import', state: 'succeeded', created_at: '', progress: {}, error: null, result }
}

// A poll that only ends when the section's signal aborts it (the admin left).
function untilAborted(opts: { signal?: AbortSignal }) {
  return new Promise((_resolve, reject) => {
    opts?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
  })
}

type PollStep = unknown | Error | ((opts: { signal?: AbortSignal }) => Promise<unknown>)

// What the staging job's poll answers, and the answers to the polls of the
// jobs that follow (apply / attachment retry), in order.
let stagePoll: PollStep
let jobPolls: PollStep[]

function answer(step: PollStep, opts: { signal?: AbortSignal }) {
  if (typeof step === 'function') return (step as (o: { signal?: AbortSignal }) => Promise<unknown>)(opts)
  if (step instanceof Error) return Promise.reject(step)
  return Promise.resolve(step)
}

function mount(onImported = vi.fn()) {
  const view = render(
    <I18nProvider>
      <AdminImportSection onImported={onImported} />
    </I18nProvider>,
  )
  return { ...view, onImported }
}

function pickFile(container: HTMLElement) {
  const input = container.querySelector('input[type=file]') as HTMLInputElement
  fireEvent.change(input, { target: { files: [new File(['x'], 'bundle.tar.zst')] } })
}

async function uploadAndApply(container: HTMLElement) {
  pickFile(container)
  fireEvent.click(await screen.findByRole('button', { name: 'Import' }))
}

beforeEach(() => {
  sessionStorage.clear()
  api.adminStageSlideImport.mockResolvedValue({ job_id: STAGE_JOB })
  api.adminApplySlideImport.mockResolvedValue({ job_id: 'j' })
  api.adminRetrySlideImportAssets.mockResolvedValue({ job_id: 'j' })
  api.adminCancelSlideImport.mockResolvedValue(undefined)
  // A failed slide that conflicts (default "copy") and one that imports.
  stagePoll = succeeded(stageResponse([
    planSlide('A', { filename_conflict: true }),
    planSlide('B', { filename_conflict: true }),
  ]))
  jobPolls = []
  api.pollTransferJob.mockImplementation(
    (id: string, _a: unknown, _b: unknown, _c: unknown, opts: { signal?: AbortSignal }) =>
      answer(id === STAGE_JOB ? stagePoll : jobPolls.shift(), opts ?? {}),
  )
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const partial = applyResult({
  imported_count: 1, failed_count: 1,
  failed: [{ public_id: 'B', error: 'boom', code: 'importStepInvalid', params: { tour: 'T', step: 2, detail: 'bad' } }],
  stage_kept: true, retry: { failed_slides: 1, skipped_assets: 2 },
})

describe('AdminImportSection retry actions', () => {
  it('offers both retries for a kept stage and retries only the failed slide', async () => {
    jobPolls.push(succeeded(partial), succeeded(applyResult({
      imported_count: 1, already_imported_count: 1,
    })))
    const { container, onImported } = mount()
    await uploadAndApply(container)

    const retrySlides = await screen.findByRole('button', { name: 'Retry failed slides (1)' })
    expect(screen.getByRole('button', { name: 'Retry skipped attachments (2)' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Discard bundle' })).toBeTruthy()
    expect(screen.getByText(/24 hours have passed since the last attempt it is deleted automatically/)).toBeTruthy()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
    expect(onImported).toHaveBeenCalledTimes(1)

    fireEvent.click(retrySlides)
    await screen.findByText(/had already been imported by an earlier attempt/)
    // Same stage, and only the failed slide's choice — A was imported.
    expect(api.adminApplySlideImport).toHaveBeenLastCalledWith(STAGE, { B: 'copy' })
    // Nothing left: the server removed the stage, so the actions go away.
    expect(screen.queryByRole('button', { name: /Retry failed slides/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Discard bundle' })).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBeNull()
    expect(onImported).toHaveBeenCalledTimes(2)
  })

  it('runs the attachment-only retry against the kept stage', async () => {
    jobPolls.push(succeeded(partial), succeeded({
      stage_id: STAGE, copied: [], copied_count: 2, still_skipped_count: 0,
      warnings: [], warning_count: 0, stage_kept: true,
      retry: { failed_slides: 1, skipped_assets: 0 },
    }))
    const { container, onImported } = mount()
    await uploadAndApply(container)

    fireEvent.click(await screen.findByRole('button', { name: 'Retry skipped attachments (2)' }))
    await screen.findByText('Imported 2 attachment(s); 0 still could not be imported.')
    expect(api.adminRetrySlideImportAssets).toHaveBeenCalledWith(STAGE)
    // The failed slide still keeps the stage; only that action remains.
    expect(screen.getByRole('button', { name: 'Retry failed slides (1)' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Retry skipped attachments/ })).toBeNull()
    expect(onImported).toHaveBeenCalledTimes(2)
  })

  it('discards the kept stage', async () => {
    jobPolls.push(succeeded(partial))
    const { container } = mount()
    await uploadAndApply(container)

    fireEvent.click(await screen.findByRole('button', { name: 'Discard bundle' }))
    await waitFor(() => expect(api.adminCancelSlideImport).toHaveBeenCalledWith(STAGE))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull())
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBeNull()
  })

  it('shows no retry actions when nothing was kept', async () => {
    jobPolls.push(succeeded(applyResult({ imported_count: 2 })))
    const { container } = mount()
    await uploadAndApply(container)

    await screen.findByText('Imported 2, skipped 0, failed 0.')
    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Discard bundle' })).toBeNull()
  })

  it('returns to a kept stage remembered from earlier in the session', async () => {
    sessionStorage.setItem(STORAGE_KEYS.importKeptStageId, STAGE)
    api.adminGetSlideImportStage.mockResolvedValue(stageResponse([
      planSlide('A', { filename_conflict: true, apply_status: 'imported' }),
      planSlide('B', {
        apply_status: 'failed',
        failure: { code: 'importSlideFailed', error: 'boom', params: {} },
      }),
    ], { applied: true, already_imported_count: 1, retry: { failed_slides: 1, skipped_assets: 0 } }))
    jobPolls.push(succeeded(applyResult({
      imported_count: 1, already_imported_count: 1,
    })))
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'Review bundle' }))
    expect(await screen.findByText(/Already imported/)).toBeTruthy()
    // The catch-all failure code carries the server's reason along.
    expect(screen.getByText('Failed last time: A slide failed to import: boom')).toBeTruthy()
    expect(api.adminGetSlideImportStage).toHaveBeenCalledWith(STAGE)

    fireEvent.click(screen.getByRole('button', { name: 'Retry import' }))
    await screen.findByText(/had already been imported by an earlier attempt/)
    // Nothing named for the already-imported slide.
    expect(api.adminApplySlideImport).toHaveBeenCalledWith(STAGE, {})
  })

  it('forgets a remembered stage that is gone from the server', async () => {
    sessionStorage.setItem(STORAGE_KEYS.importKeptStageId, STAGE)
    api.adminGetSlideImportStage.mockRejectedValue(new ApiCodeError('adminDash.apiErr.importStageMissing'))
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'Review bundle' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Review bundle' })).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBeNull()
  })
})

describe('a bundle being staged on the server', () => {
  it('is remembered as soon as the server has staged it', async () => {
    const { container, unmount } = mount()
    pickFile(container)
    await screen.findByRole('button', { name: 'Import' })
    // Not applied yet, but not stranded by a tab switch either.
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
    unmount()
    mount()
    expect(await screen.findByRole('button', { name: 'Review bundle' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Discard bundle' })).toBeTruthy()
  })

  it('is picked up again after a tab switch while the server unpacks it', async () => {
    stagePoll = untilAborted
    const { container, unmount } = mount()
    pickFile(container)
    expect(await screen.findByText('Unpacking and checking the bundle on the server…')).toBeTruthy()
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBe(STAGE_JOB)
    unmount()

    stagePoll = succeeded(stageResponse([planSlide('A')]))
    mount()
    expect(await screen.findByRole('button', { name: 'Import' })).toBeTruthy()
    expect(api.pollTransferJob).toHaveBeenLastCalledWith(STAGE_JOB, expect.any(Function), undefined, undefined, expect.anything())
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
  })

  it('cancels the upload itself when the section unmounts mid-upload', async () => {
    let signal: AbortSignal | undefined
    api.adminStageSlideImport.mockImplementation(
      (_f: File, _p: unknown, opts: { signal?: AbortSignal }) => {
        signal = opts.signal
        return untilAborted(opts)
      },
    )
    const { container, unmount } = mount()
    pickFile(container)
    await waitFor(() => expect(signal).toBeDefined())
    unmount()
    expect(signal?.aborted).toBe(true)
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
  })

  it('is followed by a section mounted while its upload still awaits the server', async () => {
    // Its body was sent, so the unmount does not cancel it (keepAfterSent),
    // and its job id comes only with the answer.
    let answer: ((v: { job_id: string }) => void) | undefined
    let progress: ((pct: number) => void) | undefined
    let sent: (() => void) | undefined
    api.adminStageSlideImport.mockImplementation(
      (_f: File, onProgress: (pct: number) => void, opts: { onSent?: () => void }) => {
        progress = onProgress
        sent = opts.onSent
        return new Promise((resolve) => { answer = resolve })
      },
    )
    const first = mount()
    pickFile(first.container)
    await waitFor(() => expect(answer).toBeDefined())
    progress!(100)
    sent!()
    first.unmount()

    // Not a fresh upload view: a second upload would displace this one's job
    // id and strand the bundle it stages.
    mount()
    expect(await screen.findByText('Uploading… 100%')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Uploading…' }) as HTMLButtonElement).disabled).toBe(true)
    answer!({ job_id: STAGE_JOB })
    expect(await screen.findByRole('button', { name: 'Import' })).toBeTruthy()
    expect(api.pollTransferJob).toHaveBeenCalledWith(STAGE_JOB, expect.any(Function), undefined, undefined, expect.anything())
    expect(api.adminStageSlideImport).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
  })

  it('reports its body as being sent until it is, and asks before a reload until the server answers', async () => {
    let opts: { onSent?: () => void } = {}
    let answer: ((v: { job_id: string }) => void) | undefined
    api.adminStageSlideImport.mockImplementation((_f: File, _p: unknown, o: { onSent?: () => void }) => {
      opts = o
      return new Promise((resolve) => { answer = resolve })
    })
    const onSendingChange = vi.fn()
    const view = render(
      <I18nProvider>
        <AdminImportSection onSendingChange={onSendingChange} />
      </I18nProvider>,
    )
    const reloadAsks = () => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))
    expect(reloadAsks()).toBe(false)
    pickFile(view.container)
    await waitFor(() => expect(onSendingChange).toHaveBeenLastCalledWith(true))
    // Leaving now would cancel it: the guard has its history entry in place.
    expect(window.history.state?.wsiNavGuard).toBeTruthy()
    expect(reloadAsks()).toBe(true)
    act(() => opts.onSent!())
    await waitFor(() => expect(onSendingChange).toHaveBeenLastCalledWith(false))
    expect(reloadAsks()).toBe(true)
    act(() => answer!({ job_id: STAGE_JOB }))
    expect(await screen.findByRole('button', { name: 'Import' })).toBeTruthy()
    expect(reloadAsks()).toBe(false)
  })

  it("shows the error of an upload it attached to", async () => {
    let fail: ((err: unknown) => void) | undefined
    api.adminStageSlideImport.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject }))
    const first = mount()
    pickFile(first.container)
    await waitFor(() => expect(fail).toBeDefined())
    first.unmount()
    mount()
    await screen.findByRole('button', { name: 'Uploading…' })
    fail!(new ApiCodeError('adminDash.apiErr.importUploadGateway', { status: 524 }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/HTTP 524/)
    expect((screen.getByRole('button', { name: /Choose/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('reports a bundle the staging job refused, in the admin language', async () => {
    stagePoll = {
      id: STAGE_JOB, kind: 'slide_import_stage', state: 'failed', created_at: '', progress: {},
      error: 'no manifest', result: { code: 'adminDash.apiErr.importNotABundle', params: {} },
    }
    const { container } = mount()
    pickFile(container)
    expect((await screen.findByRole('alert')).textContent).toMatch(/not a Tessera slide-export bundle/)
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBeNull()
  })

  it('names the file limit of a bundle refused as too large', async () => {
    stagePoll = {
      id: STAGE_JOB, kind: 'slide_import_stage', state: 'failed', created_at: '', progress: {},
      error: 'too many members',
      result: { code: 'adminDash.apiErr.importTooManyFiles', params: { limit: 101505 } },
    }
    const { container } = mount()
    pickFile(container)
    const alert = (await screen.findByRole('alert')).textContent
    expect(alert).toMatch(/more than 101505 files and folders/)
    expect(alert).not.toMatch(/not a Tessera slide-export bundle/)
  })

  it('translates the proxy answering the upload instead of the app', async () => {
    api.adminStageSlideImport.mockRejectedValue(
      new ApiCodeError('adminDash.apiErr.importUploadGateway', { status: 504 }),
    )
    const { container } = mount()
    pickFile(container)
    expect((await screen.findByRole('alert')).textContent).toMatch(/HTTP 504/)
  })

  it('Cancel drops the staged bundle and offers the one it displaced again', async () => {
    sessionStorage.setItem(STORAGE_KEYS.importKeptStageId, OLDER)
    const { container } = mount()
    pickFile(container)
    await screen.findByRole('button', { name: 'Import' })
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.adminCancelSlideImport).toHaveBeenCalledWith(STAGE))
    await waitFor(() => expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(OLDER))
    expect(await screen.findByRole('button', { name: 'Review bundle' })).toBeTruthy()
  })

  it('locks Import and the choices while the Cancel is out', async () => {
    // An Import sent meanwhile would race the DELETE, and the cancel then
    // clears a stage that is being applied.
    const { container } = mount()
    pickFile(container)
    const importBtn = await screen.findByRole('button', { name: 'Import' }) as HTMLButtonElement
    let finish: () => void = () => {}
    api.adminCancelSlideImport.mockReturnValue(new Promise<void>((r) => { finish = r }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(importBtn.disabled).toBe(true))
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
    // Still labelled as it was: a cancel is not an import.
    expect(importBtn.textContent).toBe('Import')
    fireEvent.click(importBtn)
    expect(api.adminApplySlideImport).not.toHaveBeenCalled()
    await act(async () => finish())
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Import' })).toBeNull())
  })

  it('shows what the bundle cannot carry over before and after the import', async () => {
    stagePoll = succeeded(stageResponse([
      planSlide('A', { credit_skipped: ['license_id'] }),
    ], { unknown_fields: ['credit_holder_v2', 'tour.extra'] }))
    jobPolls.push(succeeded(applyResult({
      imported_count: 1,
      warnings: [
        { public_id: '', code: 'importUnknownField', params: { field: 'credit_holder_v2' } },
        { public_id: 'A', code: 'importCreditSkipped', params: { field: 'license_id' } },
      ],
      warning_count: 2,
    })))
    const { container } = mount()
    pickFile(container)
    expect(await screen.findByText(/does not recognise.*credit_holder_v2, tour\.extra/)).toBeTruthy()
    expect(screen.getByText(/imported empty: License/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    expect(await screen.findByText('Bundle field "credit_holder_v2" is not recognised by this Tessera release and was not imported.')).toBeTruthy()
    expect(screen.getByText('A: Image credit "License" was not imported (value not accepted by this system).')).toBeTruthy()
  })
})

describe('a stage whose job outlives the section', () => {
  it('is remembered when the section unmounts while the job runs', async () => {
    jobPolls.push(untilAborted)
    const { container, unmount } = mount()
    await uploadAndApply(container)
    await waitFor(() => expect(api.pollTransferJob).toHaveBeenCalledTimes(2))
    // The admin switches sidebar tabs while a long apply is still running.
    unmount()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
    mount()
    expect(await screen.findByRole('button', { name: /Review bundle/ })).toBeTruthy()
  })

  it('does not replace an older kept stage when the new job removes its own', async () => {
    sessionStorage.setItem(STORAGE_KEYS.importKeptStageId, OLDER)
    jobPolls.push(succeeded(applyResult({ imported_count: 2 })))
    const { container } = mount()
    await uploadAndApply(container)
    await waitFor(() => expect(api.pollTransferJob).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(OLDER),
    )
  })
})

describe('an older kept stage displaced by a job', () => {
  it('is offered again when the job removed its own stage while the section was away', async () => {
    sessionStorage.setItem(STORAGE_KEYS.importKeptStageId, OLDER)
    jobPolls.push(untilAborted)
    const { container, unmount } = mount()
    await uploadAndApply(container)
    await waitFor(() => expect(api.pollTransferJob).toHaveBeenCalledTimes(2))
    unmount()
    // The job then imports everything, so the server removes STAGE.
    api.adminGetSlideImportStage.mockImplementation(async (id: string) => {
      if (id === STAGE) throw new ApiCodeError('adminDash.apiErr.importStageMissing')
      return stageResponse([planSlide('A')])
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /Review bundle/ }))
    await waitFor(() => expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(OLDER))
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: /Review bundle/ }))
    await waitFor(() => expect(api.adminGetSlideImportStage).toHaveBeenLastCalledWith(OLDER))
  })

  it('keeps the new stage on offer when the poll gives up on a job that may still run', async () => {
    jobPolls.push(new Error('network'))
    const { container } = mount()
    await uploadAndApply(container)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBe(STAGE)
  })
})

describe('AdminImportSection job progress and results', () => {
  // What the live regions currently say (the progress line's off-screen one
  // among them).
  const announced = () => screen.queryAllByRole('status').map((el) => el.textContent ?? '')

  it('shows what the server reports of a running staging job and apply', async () => {
    let stageProgress: ((job: unknown) => void) | undefined
    let releaseStage: (() => void) | undefined
    let applyProgress: ((job: unknown) => void) | undefined
    api.pollTransferJob.mockImplementation(
      (id: string, onProgress: (job: unknown) => void, _a: unknown, _b: unknown, opts: { signal?: AbortSignal }) => {
        if (id === STAGE_JOB) {
          stageProgress = onProgress
          return new Promise((resolve) => {
            releaseStage = () => resolve(succeeded(stageResponse([planSlide('A')])))
          })
        }
        applyProgress = onProgress
        return untilAborted(opts ?? {})
      },
    )
    const { container } = mount()
    pickFile(container)
    await waitFor(() => expect(stageProgress).toBeDefined())
    act(() => stageProgress!({
      state: 'running', progress: { phase: 'copying', bytes_copied: 512 * 1024 * 1024, total_bytes: 1024 * 1024 * 1024 },
    }))
    // Shown, and — as the phase is new — announced (the live region holds it too).
    expect(await screen.findAllByText(/Receiving the bundle on the server… .*\(50%\)/)).toHaveLength(2)
    expect(announced()).toContainEqual(expect.stringMatching(/\(50%\)/))
    // A same-phase update changes the visible line only: a screen reader is
    // not read every poll's new figure.
    act(() => stageProgress!({
      state: 'running', progress: { phase: 'copying', bytes_copied: 768 * 1024 * 1024, total_bytes: 1024 * 1024 * 1024 },
    }))
    expect(await screen.findByText(/Receiving the bundle on the server… .*\(75%\)/)).toBeTruthy()
    expect(announced().some((x) => /\(75%\)/.test(x))).toBe(false)
    act(() => stageProgress!({ state: 'running', progress: { phase: 'extracting' } }))
    expect(await screen.findAllByText('Unpacking the bundle…')).toHaveLength(2)
    expect(announced()).toContain('Unpacking the bundle…')
    act(() => releaseStage!())

    fireEvent.click(await screen.findByRole('button', { name: 'Import' }))
    await waitFor(() => expect(applyProgress).toBeDefined())
    act(() => applyProgress!({ state: 'running', progress: { phase: 'importing', done: 3, total: 10 } }))
    expect(await screen.findAllByText('Importing… 3 of 10 slide(s) done')).toHaveLength(2)
    act(() => applyProgress!({ state: 'running', progress: { phase: 'importing', done: 4, total: 10 } }))
    expect(await screen.findByText('Importing… 4 of 10 slide(s) done')).toBeTruthy()
    expect(announced()).toContain('Importing… 3 of 10 slide(s) done')
    expect(announced()).not.toContain('Importing… 4 of 10 slide(s) done')
  })

  it('flags a slide this system already has and defaults it to skip', async () => {
    stagePoll = succeeded(stageResponse([
      planSlide('A', { filename_conflict: true, public_id_conflict: true }),
      planSlide('B', { filename_conflict: true }),
      // Renamed since: only its public_id is taken.
      planSlide('C', { public_id_conflict: true }),
    ]))
    jobPolls.push(succeeded(applyResult({ imported_count: 1, skipped_count: 2 })))
    const { container } = mount()
    pickFile(container)
    await screen.findByRole('button', { name: 'Import' })
    expect(screen.getAllByText(/already on this system/)).toHaveLength(2)
    const selects = container.querySelectorAll('select')
    expect(Array.from(selects).map((el) => (el as HTMLSelectElement).value)).toEqual(['skip', 'copy', 'skip'])
    // The operator can still choose a copy.
    fireEvent.change(selects[2], { target: { value: 'copy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))
    await screen.findByText('Imported 1, skipped 2, failed 0.')
    expect(api.adminApplySlideImport).toHaveBeenCalledWith(STAGE, { A: 'skip', B: 'copy', C: 'copy' })
  })

  it('colours the result by what happened', async () => {
    const tone = (text: string) => (screen.getByText(text) as HTMLElement).style.background
    jobPolls.push(succeeded(applyResult({
      failed_count: 2,
      failed: [{ public_id: 'A', error: 'x' }, { public_id: 'B', error: 'y' }],
    })))
    const first = mount()
    await uploadAndApply(first.container)
    await screen.findByText('Imported 0, skipped 0, failed 2.')
    expect(tone('Imported 0, skipped 0, failed 2.')).toContain('--color-danger-bg')
    first.unmount()
    cleanup()

    jobPolls.push(succeeded(applyResult({
      imported_count: 1, failed_count: 1, failed: [{ public_id: 'B', error: 'y' }],
    })))
    const second = mount()
    await uploadAndApply(second.container)
    await screen.findByText('Imported 1, skipped 0, failed 1.')
    expect(tone('Imported 1, skipped 0, failed 1.')).toContain('--color-warning-bg')
    second.unmount()
    cleanup()

    jobPolls.push(succeeded(applyResult({ imported_count: 2 })))
    const third = mount()
    await uploadAndApply(third.container)
    await screen.findByText('Imported 2, skipped 0, failed 0.')
    expect(tone('Imported 2, skipped 0, failed 0.')).toContain('--color-success-bg')
  })

  it('explains a damaged bundle without pointing at Restore', async () => {
    stagePoll = {
      id: STAGE_JOB, kind: 'slide_import_stage', state: 'failed', created_at: '', progress: {},
      error: 'corrupt', result: { code: 'adminDash.apiErr.importCorrupt', params: {} },
    }
    const { container } = mount()
    pickFile(container)
    const alert = (await screen.findByRole('alert')).textContent ?? ''
    expect(alert).toMatch(/truncated or damaged/)
    expect(alert).not.toMatch(/Restore/)
  })
})

describe('sign-out while a sent bundle waits for the server', () => {
  // Once its body is sent the upload is not cancelled by the section going
  // away (the server is staging it); its job id must not be written to
  // sessionStorage when the answer comes after Logout has removed the pointers,
  // or the next account to sign in on the tab would be attached to the
  // signed-out admin's staged bundle.
  it('does not leave the job pointer for the next account', async () => {
    let answerUpload: ((v: { job_id: string }) => void) | undefined
    let sent: (() => void) | undefined
    api.adminStageSlideImport.mockImplementation(
      (_f: File, _onProgress: (pct: number) => void, opts: { onSent?: () => void }) => {
        sent = opts.onSent
        return new Promise((resolve) => { answerUpload = resolve })
      },
    )
    const first = mount()
    pickFile(first.container)
    await waitFor(() => expect(answerUpload).toBeDefined())
    sent!()
    first.unmount()          // Logout: the dashboard goes away ...
    clearAdminSession()      // ... and the session's pointers are cleared
    answerUpload!({ job_id: STAGE_JOB })   // the server answers afterwards
    await new Promise((r) => setTimeout(r, 0))

    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBeNull()
    // The next account opens Import: nothing to attach to, so it offers the picker.
    mount()
    expect(api.pollTransferJob).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(STORAGE_KEYS.importKeptStageId)).toBeNull()
  })

  it('still remembers the job when the admin did not sign out', async () => {
    let answerUpload: ((v: { job_id: string }) => void) | undefined
    api.adminStageSlideImport.mockImplementation(
      () => new Promise((resolve) => { answerUpload = resolve }),
    )
    const first = mount()
    pickFile(first.container)
    await waitFor(() => expect(answerUpload).toBeDefined())
    first.unmount()          // a tab switch, not a sign-out
    answerUpload!({ job_id: STAGE_JOB })
    await new Promise((r) => setTimeout(r, 0))
    expect(sessionStorage.getItem(STORAGE_KEYS.importStageJobId)).toBe(STAGE_JOB)
  })
})
