// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API module is the section's only I/O; i18n, ui and error formatting are real.
const api = vi.hoisted(() => ({
  adminListBackups: vi.fn(),
  adminListRollbacks: vi.fn(),
  adminGetBackupEstimate: vi.fn(),
  adminGetRestoreResult: vi.fn(),
  adminGetMyIp: vi.fn(),
  adminRestoreFromUpload: vi.fn(),
  adminGetBackupJob: vi.fn(),
  adminGetRestorePreflight: vi.fn(),
  adminApplyRestore: vi.fn(),
  adminCancelRestoreStage: vi.fn(),
  adminCreateBackup: vi.fn(),
  adminGetResetPreflight: vi.fn(),
  adminApplyReset: vi.fn(),
  adminDeleteBackup: vi.fn(),
  waitForJob: vi.fn(),
  waitForRestoreApplied: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import {
  ApiCodeError, JobNotFoundError, NetworkError, RateLimitError, RestoreRefusedError, UnauthorizedError,
  clearAdminSession,
} from '../../lib/api'
import { STORAGE_KEYS } from '../../lib/storageKeys'
import AdminBackupSection from './AdminBackupSection'

// Locked by its own `disabled` or by a disabled <fieldset> around it (which a
// browser honours, though the control's own `disabled` stays false).
const isLocked = (el: Element) => (el as HTMLInputElement).disabled || el.closest('fieldset[disabled]') !== null

const STAGE = 'stage-aaaaaaaaaaaa'
const JOB = 'job-1'
const MANIFEST = {
  scope: 'full', app_version: '1.0.0', created_at: '', slide_count: 1, tour_count: 0,
  note: '', archive_format: 'tar.zst',
}
const STAGED = {
  stage_id: STAGE, manifest: MANIFEST, current_app_version: '1.0.0',
  plan: { backup_app_version: '1.0.0', current_app_version: '1.0.0', blocking_reason: null, blocking_code: null },
}
const LIVE = {
  slide_count: 1, tour_count: 0, wsi_file_count: 0, wsi_files_referenced_by_db: 0,
  wsi_total_bytes: 0, assets_total_bytes: 0,
}
const RESTORE_JOB_KEY = 'wsi.admin.restoreStageJobId'
// Polls run a second apart.
const POLL = { timeout: 4000 }

function job(over: Record<string, unknown>) {
  return {
    id: JOB, kind: 'restore_stage', state: 'running', created_at: '',
    progress: {}, error: null, result: {}, ...over,
  }
}

function preflight(integrity: Record<string, unknown> = {}) {
  return {
    stage_id: STAGE, scope: 'full', manifest: MANIFEST,
    integrity: { ok: true, errors: [], warnings: [], missing_wsi_files: [], unexpected_wsi_files: [], ...integrity },
    live_state: LIVE, archive_state: { ...LIVE, scope: 'full', wsi_uploads_pending: 0 },
  }
}

beforeEach(() => {
  sessionStorage.clear()
  api.adminListBackups.mockResolvedValue({ items: [], total_bytes: 0 })
  api.adminListRollbacks.mockResolvedValue({ items: [], total_bytes: 0 })
  api.adminGetBackupEstimate.mockResolvedValue({})
  api.adminGetRestoreResult.mockResolvedValue({ result: null })
  api.adminGetMyIp.mockResolvedValue({ via_cloudflare_tunnel: false })
  api.adminRestoreFromUpload.mockResolvedValue({ job_id: JOB })
  api.adminGetBackupJob.mockResolvedValue(job({ state: 'succeeded', result: STAGED }))
  api.adminGetRestorePreflight.mockResolvedValue(preflight())
  api.adminCancelRestoreStage.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function upload(container: HTMLElement) {
  const input = container.querySelector('input[type=file]') as HTMLInputElement
  fireEvent.change(input, { target: { files: [new File(['x'], 'b.tar.zst')] } })
}

async function applyButtonReady() {
  const open = await screen.findByRole('button', { name: 'Restart service & apply' }, POLL)
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false))
  return open
}

// Stage an uploaded archive, open the confirm dialog and submit a password.
async function stageAndApply(container: HTMLElement) {
  upload(container)
  fireEvent.click(await applyButtonReady())
  fireEvent.change(await screen.findByPlaceholderText(/password/i), { target: { value: 'pw' } })
  const dialog = screen.getByRole('dialog')
  fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Restart service & apply')!)
}

function mount() {
  return render(<I18nProvider><AdminBackupSection /></I18nProvider>)
}

describe('AdminBackupSection staged-archive release', () => {
  it('keeps a staged archive across a remount instead of orphaning or releasing it', async () => {
    const first = mount()
    upload(first.container)
    await applyButtonReady()
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBe(JOB)
    first.unmount()
    expect(api.adminCancelRestoreStage).not.toHaveBeenCalled()

    // A sidebar tab switch (or a reload) back: the staged preview is offered again.
    mount()
    await applyButtonReady()
    expect(api.adminGetBackupJob).toHaveBeenCalledWith(JOB, expect.anything())
    expect(api.adminCancelRestoreStage).not.toHaveBeenCalled()
  })

  it('keeps the staged archive when a new upload is refused, and releases it once one is accepted', async () => {
    const { container } = mount()
    upload(container)
    await applyButtonReady()

    // A second upload the server refuses (busy): the first stage stays.
    api.adminRestoreFromUpload.mockRejectedValueOnce(new Error('another backup/restore job is in progress'))
    upload(container)
    await screen.findByText(/another backup\/restore job is in progress/)
    await applyButtonReady()
    expect(api.adminCancelRestoreStage).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBe(JOB)

    // One the server accepts: it is told which stage it replaces, and removes
    // that one itself before measuring free space (not this page, after).
    api.adminRestoreFromUpload.mockResolvedValueOnce({ job_id: 'job-2' })
    upload(container)
    await waitFor(() => expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBe('job-2'))
    const calls = api.adminRestoreFromUpload.mock.calls
    const lastCall = calls[calls.length - 1]
    expect(lastCall[2]).toMatchObject({ replaces: STAGE })
    expect(api.adminCancelRestoreStage).not.toHaveBeenCalled()
  })

  it('does not offer the old stage again when an accepted upload\'s answer was lost', async () => {
    const { container } = mount()
    upload(container)
    await applyButtonReady()
    api.adminRestoreFromUpload.mockImplementationOnce(async (_f: File, _p: unknown, opts: { onSent?: () => void }) => {
      opts.onSent?.()
      throw new NetworkError()
    })
    upload(container)
    await waitFor(() => expect(api.adminRestoreFromUpload).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Restart service & apply' })).toBeNull())
  })

  it('releases the stage, and forgets it, when the preview is cancelled', async () => {
    const { container } = mount()
    upload(container)
    await applyButtonReady()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.adminCancelRestoreStage).toHaveBeenCalledWith(STAGE))
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
  })

  it('locks the password while the restore request is out', async () => {
    // Its answer clears it.
    api.adminApplyRestore.mockReturnValue(new Promise(() => {}))
    const { container } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(api.adminApplyRestore).toHaveBeenCalled())
    expect(isLocked(screen.getByPlaceholderText(/password/i))).toBe(true)
  })

  it('keeps the stage on unmount when the apply result is ambiguous (network error)', async () => {
    api.adminApplyRestore.mockRejectedValue(new NetworkError())
    api.waitForRestoreApplied.mockRejectedValue(new Error('timed out'))
    const { container, unmount } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(api.adminApplyRestore).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByRole('dialog').textContent).not.toContain('Applying'))
    unmount()
    expect(api.adminCancelRestoreStage).not.toHaveBeenCalled()
    // The server may be applying it: a remount must not offer it again.
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
  })

  it('waits for the result of an ambiguous apply instead of telling the operator to retry', async () => {
    api.adminApplyRestore.mockRejectedValue(new NetworkError())
    api.waitForRestoreApplied.mockRejectedValue(new Error('timed out'))
    const { container } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(api.waitForRestoreApplied).toHaveBeenCalledWith(STAGE, 90_000))
    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain('Could not confirm whether the restore was applied'))
    // The generic "could not reach the server, try again" text would invite a
    // second apply while the first may be running.
    expect(screen.getByRole('dialog').textContent).not.toContain('Check your connection and try again')
  })

  it('reloads onto the outcome banner when the ambiguous apply turns out to have been applied', async () => {
    api.adminApplyRestore.mockRejectedValue(new NetworkError())
    const done = { ok: true, stage_id: STAGE, scope: 'full', applied_at: 'x', app_version: '1.0.0' }
    api.waitForRestoreApplied.mockResolvedValue(done)
    const reload = vi.fn()
    const realLocation = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, reload } })
    try {
      const { container } = mount()
      await stageAndApply(container)
      await waitFor(() => expect(reload).toHaveBeenCalled())
      expect(JSON.parse(sessionStorage.getItem('wsi.admin.postRestore') ?? 'null')).toMatchObject({ stage_id: STAGE, ok: true })
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
    }
  })

  it('does not poll after a definite rejection, and keeps the stage', async () => {
    api.adminApplyRestore.mockRejectedValue(new ApiCodeError('adminBackup.error.badPassword'))
    const { container } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(screen.getByRole('dialog').textContent).not.toContain('Applying'))
    expect(api.waitForRestoreApplied).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBe(JOB)
  })
})

describe('AdminBackupSection restore staging job', () => {
  it('shows extraction progress, then the preview, for a job resumed after a reload', async () => {
    sessionStorage.setItem(RESTORE_JOB_KEY, JOB)
    api.adminGetBackupJob
      .mockResolvedValueOnce(job({ progress: { phase: 'extracting', snapshot_id: null, bytes_extracted: 3 * 1024 ** 2 } }))
      .mockResolvedValue(job({ state: 'succeeded', result: STAGED }))
    mount()
    await screen.findByText('Extracting on server… 3.0 MB')
    await applyButtonReady()
  })

  it('reports a failed staging job and forgets it', async () => {
    api.adminGetBackupJob.mockResolvedValue(job({ state: 'failed', error: 'archive is corrupt or not a supported format' }))
    const { container } = mount()
    upload(container)
    await screen.findByText('archive is corrupt or not a supported format', undefined, POLL)
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
    expect((screen.getByRole('button', { name: 'Upload archive' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('shows the copy of the upload into staging, the staging job\'s first step', async () => {
    api.adminGetBackupJob
      .mockResolvedValueOnce(job({ progress: {
        phase: 'copying', snapshot_id: null, bytes_copied: 1024 ** 2, total_bytes: 3 * 1024 ** 2,
      } }))
      .mockResolvedValue(job({ state: 'succeeded', result: STAGED }))
    const { container } = mount()
    upload(container)
    await screen.findByText('Copying the upload on server… 1.0 MB / 3.0 MB', undefined, POLL)
    await applyButtonReady()
  })

  it('translates a staging volume that filled up while the upload was copied', async () => {
    api.adminGetBackupJob.mockResolvedValue(job({
      state: 'failed', error: 'not enough free space under /srv/backups to stage the upload',
      result: { code: 'adminDash.apiErr.uploadNoSpace' },
    }))
    const { container } = mount()
    upload(container)
    await screen.findByText('Not enough free disk space on the server to accept this upload.', undefined, POLL)
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
  })

  it("says the outcome is unknown when a proxy answered the upload instead of the app", async () => {
    api.adminRestoreFromUpload.mockRejectedValue(
      new ApiCodeError('adminDash.apiErr.restoreUploadGateway', { status: 524 }),
    )
    const { container } = mount()
    upload(container)
    await screen.findByText(/HTTP 524.*not known whether it received the archive/)
  })

  it('offers Cancel until the whole archive is sent, whatever the progress figure', async () => {
    const call = {} as { progress: (p: number) => void; sent?: () => void }
    api.adminRestoreFromUpload.mockImplementation(
      (_f: File, onProgress: (p: number) => void, opts: { onSent?: () => void } = {}) => {
        call.progress = onProgress
        call.sent = opts.onSent
        return new Promise(() => {})
      },
    )
    const { container } = mount()
    upload(container)
    await waitFor(() => expect(api.adminRestoreFromUpload).toHaveBeenCalled())
    act(() => call.progress(99))
    await screen.findByRole('button', { name: 'Uploading… 99%' })
    screen.getByRole('button', { name: 'Cancel' })
    act(() => call.sent?.())
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull())
    expect(screen.queryByRole('button', { name: 'Uploading… 99%' })).toBeNull()
  })

  it('does not leave the staging job pointer for the next account when signed out meanwhile', async () => {
    const call = {} as { sent?: () => void; answer?: (v: { job_id: string }) => void }
    api.adminRestoreFromUpload.mockImplementation(
      (_f: File, _p: unknown, opts: { onSent?: () => void } = {}) => {
        call.sent = opts.onSent
        return new Promise((resolve) => { call.answer = resolve as typeof call.answer })
      },
    )
    const first = mount()
    upload(first.container)
    await waitFor(() => expect(call.answer).toBeDefined())
    act(() => call.sent?.())
    first.unmount()           // Logout ...
    clearAdminSession()       // ... clears the pointers
    call.answer!({ job_id: JOB })   // the server answers afterwards
    await new Promise((r) => setTimeout(r, 0))
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
  })

  it('stops waiting when the staging job record is gone (404)', async () => {
    api.adminGetBackupJob.mockRejectedValue(new JobNotFoundError())
    const { container } = mount()
    upload(container)
    await screen.findByText(/no longer has a record of this job/, undefined, POLL)
    expect(sessionStorage.getItem(RESTORE_JOB_KEY)).toBeNull()
    expect((screen.getByRole('button', { name: 'Upload archive' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('AdminBackupSection backup job', () => {
  it('locks the note and components while the start request is in flight', async () => {
    // The backup carries what was set at the click; a note typed meanwhile
    // would show beside a backup that doesn't have it.
    api.adminCreateBackup.mockReturnValue(new Promise(() => {}))
    mount()
    const note = await screen.findByPlaceholderText('Optional description for this backup') as HTMLTextAreaElement
    fireEvent.change(note, { target: { value: 'before upgrade' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start backup' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!)
    await waitFor(() => expect(api.adminCreateBackup).toHaveBeenCalled())
    expect(api.adminCreateBackup.mock.calls[0][1]).toBe('before upgrade')
    expect(note.disabled).toBe(true)
    const toggles = [...document.querySelectorAll('input[type=checkbox]')] as HTMLInputElement[]
    expect(toggles).toHaveLength(3)
    expect(toggles.every((c) => c.disabled)).toBe(true)
  })

  it('treats a job record that disappeared (404) as finished instead of polling forever', async () => {
    api.adminCreateBackup.mockResolvedValue({ job_id: 'b-1' })
    api.adminGetBackupJob.mockRejectedValue(new JobNotFoundError())
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Start backup' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!)
    await screen.findByText(/no longer has a record of this job/, undefined, POLL)
    expect(sessionStorage.getItem('wsi.admin.backupJobId')).toBeNull()
    expect((screen.getByRole('button', { name: 'Start backup' }) as HTMLButtonElement).disabled).toBe(false)
    const polls = api.adminGetBackupJob.mock.calls.length
    await new Promise((r) => setTimeout(r, 1500))
    expect(api.adminGetBackupJob.mock.calls.length).toBe(polls)
  })

  it('lists what a successful backup left out', async () => {
    api.adminCreateBackup.mockResolvedValue({ job_id: 'b-2' })
    api.adminGetBackupJob.mockResolvedValue({
      id: 'b-2', kind: 'backup', state: 'succeeded', created_at: '', error: null,
      progress: { phase: 'done', bytes_written: 10 },
      result: { backup_id: 'x', size_bytes: 10, warnings: ['thumbnail 7.jpg is missing on disk; not archived'] },
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Start backup' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!)
    await screen.findByText('thumbnail 7.jpg is missing on disk; not archived', undefined, POLL)
    screen.getByText(/some files were left out/)
  })

  it('shows no warning block for a clean backup or a server that sends none', async () => {
    api.adminCreateBackup.mockResolvedValue({ job_id: 'b-3' })
    api.adminGetBackupJob.mockResolvedValue({
      id: 'b-3', kind: 'backup', state: 'succeeded', created_at: '', error: null,
      progress: { phase: 'done', bytes_written: 10 }, result: { backup_id: 'y', size_bytes: 10 },
    })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Start backup' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!)
    await waitFor(() => expect(api.adminGetBackupJob).toHaveBeenCalled(), POLL)
    await waitFor(() => expect(api.adminListBackups.mock.calls.length).toBeGreaterThan(1), POLL)
    expect(screen.queryByText(/some files were left out/)).toBeNull()
  })
})

describe('AdminBackupSection restore refusals and warnings', () => {
  it('shows a coded refusal (ARCHIVE_INCONSISTENT) as localized text, not the bare code', async () => {
    api.adminApplyRestore.mockRejectedValue(
      new RestoreRefusedError('ARCHIVE_INCONSISTENT', 'full-scope archive is missing 1 wsi file(s)'),
    )
    const { container } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain('internally inconsistent'))
    expect(screen.getByRole('dialog').textContent).not.toContain('ARCHIVE_INCONSISTENT')
  })

  it('falls back to the server message for a refusal code it has no text for', async () => {
    api.adminApplyRestore.mockRejectedValue(new RestoreRefusedError('SOMETHING_NEW', 'server says no'))
    const { container } = mount()
    await stageAndApply(container)
    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain('server says no'))
  })

  it('lists integrity warnings without blocking the apply', async () => {
    api.adminGetRestorePreflight.mockResolvedValue(preflight({ warnings: ['tour row with an invalid public_id'] }))
    const { container } = mount()
    upload(container)
    await applyButtonReady()
    expect(screen.getByText('tour row with an invalid public_id')).toBeTruthy()
    expect(screen.getByText(/Warnings \(the restore can still proceed\)/)).toBeTruthy()
  })
})

describe('AdminBackupSection reset', () => {
  async function submitReset() {
    api.adminGetResetPreflight.mockResolvedValue({ live_state: LIVE, preserved: { tables: [] } })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Reset content data…' }))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Reset content data…' }).length).toBe(1))
    fireEvent.click(await screen.findByRole('button', { name: 'Reset content data…' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(screen.getByPlaceholderText('RESET'), { target: { value: 'RESET' } })
    fireEvent.change(screen.getByPlaceholderText(/password/i), { target: { value: 'pw' } })
    fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Restart service & reset')!)
    return dialog
  }

  it('locks the confirm text and password while the reset request is out', async () => {
    // Its answer clears both.
    api.adminApplyReset.mockReturnValue(new Promise(() => {}))
    await submitReset()
    await waitFor(() => expect(isLocked(screen.getByPlaceholderText('RESET'))).toBe(true))
    expect(isLocked(screen.getByPlaceholderText(/password/i))).toBe(true)
  })

  it('says the outcome is unknown when the reset request got no answer', async () => {
    api.adminApplyReset.mockRejectedValue(new NetworkError())
    const dialog = await submitReset()
    await waitFor(() => expect(dialog.textContent).toContain('Could not confirm whether the reset was started'))
    expect(api.waitForRestoreApplied).not.toHaveBeenCalled()
  })

  it('shows a failed reset job without waiting for a restart', async () => {
    api.adminApplyReset.mockResolvedValue({ job_id: 'r-1', stage_id: STAGE, scope: 'reset' })
    api.waitForJob.mockResolvedValue(job({ id: 'r-1', kind: 'restore_apply', state: 'failed', error: 'reset staging failed: disk full' }))
    const dialog = await submitReset()
    await waitFor(() => expect(dialog.textContent).toContain('reset staging failed: disk full'))
    expect(api.waitForRestoreApplied).not.toHaveBeenCalled()
  })

  describe('when applying it signs this tab out', () => {
    let assign: ReturnType<typeof vi.fn>
    let alert: ReturnType<typeof vi.spyOn>
    const realLocation = window.location

    beforeEach(() => {
      assign = vi.fn()
      alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
      Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, assign } })
      sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
      api.adminApplyReset.mockResolvedValue({ job_id: 'r-1', stage_id: STAGE, scope: 'reset' })
    })

    afterEach(() => {
      Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
      alert.mockRestore()
    })

    // The dev in-process apply signs everyone out before its job reads as
    // succeeded: every later poll is a 401.
    function pollsThenSignedOut(states: string[]) {
      api.waitForJob.mockImplementation(async (_id: string, opts: { onPoll?: (j: unknown) => void }) => {
        for (const state of states) opts.onPoll?.(job({ id: 'r-1', kind: 'restore_apply', state }))
        throw new UnauthorizedError(401)
      })
    }

    it('sends the operator to sign in, carrying the stage id, once the job was seen at work', async () => {
      pollsThenSignedOut(['running'])
      await submitReset()
      await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin'))
      // Polled without the staff-token redirect, which would have left with nothing to show.
      expect(api.waitForJob).toHaveBeenCalledWith('r-1', expect.objectContaining({ keepLogin: true }))
      expect(alert).toHaveBeenCalledWith(expect.stringContaining('The reset was applied'))
      expect(JSON.parse(sessionStorage.getItem(STORAGE_KEYS.adminPostRestore)!)).toEqual({ awaitingStageId: STAGE })
      expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
      expect(api.waitForRestoreApplied).not.toHaveBeenCalled()
    })

    it('does not claim it was applied when the very first poll was refused', async () => {
      pollsThenSignedOut([])
      await submitReset()
      await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin'))
      expect(alert).toHaveBeenCalledWith(expect.stringContaining('Could not confirm'))
      expect(JSON.parse(sessionStorage.getItem(STORAGE_KEYS.adminPostRestore)!)).toEqual({ awaitingStageId: STAGE })
    })
  })

  it('waits for the restore result of its stage once the job has handed over to the restart', async () => {
    api.adminApplyReset.mockResolvedValue({ job_id: 'r-1', stage_id: STAGE, scope: 'reset' })
    // The restart took the job's worker down before its last poll.
    api.waitForJob.mockRejectedValue(new NetworkError())
    api.waitForRestoreApplied.mockRejectedValue(new Error('timed out'))
    const dialog = await submitReset()
    await waitFor(() => expect(api.waitForRestoreApplied).toHaveBeenCalledWith(STAGE, 90_000))
    await waitFor(() => expect(dialog.textContent).toContain('Service did not come back up'))
  })
})

describe('AdminBackupSection last restore result', () => {
  it("shows the awaited apply's result after signing in again, with a failed sign-out", async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminPostRestore, JSON.stringify({ awaitingStageId: STAGE }))
    api.adminGetRestoreResult.mockResolvedValue({ result: {
      ok: true, stage_id: STAGE, scope: 'reset', applied_at: 'x',
      session_revocation_error: 'database is locked',
    } })
    mount()
    await screen.findByText('Reset complete. The dashboard is now empty.')
    expect(screen.getByRole('alert').textContent).toMatch(/not every login from before it could be signed out \(database is locked\)/)
    // It is this apply's result: no "not recorded yet" warning.
    expect(screen.queryByText(/No result has been recorded yet/)).toBeNull()
  })

  it('shows a post-apply failure instead of a clean success', async () => {
    // Dev in-process apply: the swap committed (ok stays true), then bringing
    // the app up to it failed; the apply answered 500 with the same message.
    api.adminGetRestoreResult.mockResolvedValue({ result: {
      ok: true, stage_id: STAGE, scope: 'full', applied_at: 'x',
      post_apply_error: 'the data was swapped in, but its schema could not be brought up to date (boom); restart the app',
      post_apply_stage: 'schema',
    } })
    mount()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/could not be brought up to it afterwards: the data was swapped in, but its schema could not be brought up to date \(boom\)/)
    expect(screen.getByText('Restart needed')).toBeTruthy()
    expect(screen.queryByText('Restore complete.')).toBeNull()
    expect(screen.queryByText('OK')).toBeNull()
  })

  it('shows no sign-out warning when every session was signed out', async () => {
    api.adminGetRestoreResult.mockResolvedValue({ result: { ok: true, stage_id: STAGE, scope: 'full' } })
    mount()
    await screen.findByText('Restore complete.')
    expect(screen.queryByText(/could be signed out/)).toBeNull()
  })
})

describe('AdminBackupSection error text', () => {
  it('shows localized text, not the "rate-limited" sentinel, when the list load is throttled', async () => {
    api.adminListBackups.mockRejectedValue(new RateLimitError())
    mount()
    await screen.findByText(/Too many requests/)
    expect(screen.queryByText('rate-limited')).toBeNull()
  })
})

describe('AdminBackupSection a11y', () => {
  it('labels the restore confirmation dialog with its title', async () => {
    const { container } = mount()
    upload(container)
    fireEvent.click(await applyButtonReady())
    const dialog = await screen.findByRole('dialog')
    const titleId = dialog.getAttribute('aria-labelledby')
    expect(titleId).toBeTruthy()
    expect(document.getElementById(titleId!)?.textContent).toBeTruthy()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })

  const tab = (shift = false) => fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Tab', shiftKey: shift })

  it('keeps focus inside the backup confirmation and returns it to the trigger', async () => {
    mount()
    const trigger = await screen.findByRole('button', { name: 'Start backup' })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = await screen.findByRole('dialog')
    // Its primary action has focus; Tab from it (the last control) wraps.
    const start = Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!
    expect(document.activeElement).toBe(start)
    tab()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }))
    tab(true)
    expect(document.activeElement).toBe(start)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps focus inside the restore confirmation, starting in the password field', async () => {
    const { container } = mount()
    upload(container)
    const open = await applyButtonReady()
    open.focus()
    fireEvent.click(open)
    const dialog = await screen.findByRole('dialog')
    const password = screen.getByPlaceholderText(/password/i)
    expect(document.activeElement).toBe(password)
    tab(true)
    expect(document.activeElement).not.toBe(password)
    expect(dialog.contains(document.activeElement)).toBe(true)
    tab()
    expect(document.activeElement).toBe(password)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(open)
  })
})

describe('AdminBackupSection credentials note', () => {
  it('says archives carry credentials', async () => {
    mount()
    await screen.findByText(/LDAP bind password and the Google client secret/)
  })
})

describe('AdminBackupSection saved backups', () => {
  const item = (id: string) => ({
    id, path: '', size_bytes: 1024, created_at: '2026-10-28T00:00:00Z', scope: 'metadata',
    app_version: '1.0.0', sha256: 'a'.repeat(64), note: '', archive_format: 'tar.zst',
    includes_analytics: false, includes_history: false,
  })

  it('takes one delete at a time across the rows', async () => {
    // A second click would send a second DELETE for the same archive.
    api.adminListBackups.mockResolvedValue({ items: [item('b-1'), item('b-2')], total_bytes: 2048 })
    api.adminDeleteBackup.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2))
    const deletes = screen.getAllByRole('button', { name: 'Delete' }) as HTMLButtonElement[]
    fireEvent.click(deletes[0])
    await waitFor(() => expect(deletes.every((b) => b.disabled)).toBe(true))
    expect(api.adminDeleteBackup).toHaveBeenCalledTimes(1)
    vi.mocked(window.confirm).mockRestore()
  })
})
