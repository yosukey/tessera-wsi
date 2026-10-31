// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Cancelling a running backup. The API module is the section's only I/O;
// i18n, ui and error formatting are real.
const api = vi.hoisted(() => ({
  adminListBackups: vi.fn(),
  adminListRollbacks: vi.fn(),
  adminGetBackupEstimate: vi.fn(),
  adminGetRestoreResult: vi.fn(),
  adminGetMyIp: vi.fn(),
  adminGetBackupJob: vi.fn(),
  adminCreateBackup: vi.fn(),
  adminCancelBackupJob: vi.fn(),
  adminGetActiveJob: vi.fn(),
  adminVerifyBackup: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import type { BackupJob } from '../../lib/api'
import AdminBackupSection from './AdminBackupSection'

const JOB_KEY = 'wsi.admin.backupJobId'
const POLL = { timeout: 4000 }

// What the next poll answers; tests move it along.
let current: BackupJob

function backupJob(over: Partial<BackupJob>): BackupJob {
  return {
    id: 'b-1', kind: 'backup', state: 'running', created_at: '',
    progress: { phase: 'writing_wsi', bytes_written: 1024 }, error: null, result: {}, ...over,
  }
}

beforeEach(() => {
  sessionStorage.clear()
  api.adminListBackups.mockResolvedValue({ items: [], total_bytes: 0 })
  api.adminListRollbacks.mockResolvedValue({ items: [], total_bytes: 0 })
  api.adminGetBackupEstimate.mockResolvedValue({})
  api.adminGetRestoreResult.mockResolvedValue({ result: null })
  api.adminGetMyIp.mockResolvedValue({ via_cloudflare_tunnel: false })
  api.adminCreateBackup.mockResolvedValue({ job_id: 'b-1' })
  api.adminCancelBackupJob.mockResolvedValue(undefined)
  api.adminGetActiveJob.mockResolvedValue(null)
  current = backupJob({})
  api.adminGetBackupJob.mockImplementation(async () => current)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.restoreAllMocks()
})

function mount() {
  return render(<I18nProvider><AdminBackupSection /></I18nProvider>)
}

async function startBackup() {
  fireEvent.click(await screen.findByRole('button', { name: 'Start backup' }))
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(Array.from(dialog.querySelectorAll('button')).find(b => b.textContent === 'Start backup')!)
}

describe('AdminBackupSection backup cancel', () => {
  it('cancels a running backup after confirming, shows cancelling, then a neutral notice', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    await startBackup()
    const cancel = await screen.findByRole('button', { name: 'Cancel backup' }, POLL)
    await screen.findByText(/Backup in progress/, undefined, POLL)

    fireEvent.click(cancel)
    expect(confirm).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(api.adminCancelBackupJob).toHaveBeenCalledWith('b-1'))
    // Shown at once, before the server reports the request.
    await screen.findByText('Cancelling the backup…')
    const busy = screen.getByRole('button', { name: 'Cancelling…' }) as HTMLButtonElement
    expect(busy.disabled).toBe(true)

    // The server sees the request, then the job stops.
    current = backupJob({ cancel_requested: true })
    await new Promise((r) => setTimeout(r, 1200))
    screen.getByText('Cancelling the backup…')
    current = backupJob({ state: 'cancelled', result: { cancelled_by: 'admin' } })

    await screen.findByText('The backup was cancelled. No archive was saved.', undefined, POLL)
    expect(screen.queryByRole('button', { name: /Cancel backup|Cancelling/ })).toBeNull()
    expect((screen.getByRole('button', { name: 'Start backup' }) as HTMLButtonElement).disabled).toBe(false)
    expect(sessionStorage.getItem(JOB_KEY)).toBeNull()
    // Neutral: no error styling / alert for an operator's own cancel.
    expect(screen.queryByRole('alert')).toBeNull()
    // Polling stopped at the terminal state.
    const polls = api.adminGetBackupJob.mock.calls.length
    await new Promise((r) => setTimeout(r, 1300))
    expect(api.adminGetBackupJob.mock.calls.length).toBe(polls)
  })

  it('does nothing when the confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    mount()
    await startBackup()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel backup' }, POLL))
    expect(api.adminCancelBackupJob).not.toHaveBeenCalled()
    screen.getByRole('button', { name: 'Cancel backup' })
  })

  it('shows cancelling for a job resumed after a remount whose cancel is pending', async () => {
    sessionStorage.setItem(JOB_KEY, 'b-1')
    current = backupJob({ cancel_requested: true })
    mount()
    await screen.findByText('Cancelling the backup…', undefined, POLL)
    expect((screen.getByRole('button', { name: 'Cancelling…' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('explains a backup that finished before the cancel could stop it', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    await startBackup()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel backup' }, POLL))
    await waitFor(() => expect(api.adminCancelBackupJob).toHaveBeenCalled())
    current = backupJob({
      state: 'succeeded', progress: { phase: 'done', bytes_written: 10 },
      result: { backup_id: 'x', size_bytes: 10, warnings: [], cancel_too_late: true },
    })
    await screen.findByText(/finished before it could be cancelled/, undefined, POLL)
  })

  it('reports a failed cancel request and offers the button again', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.adminCancelBackupJob.mockRejectedValue(new Error('the cancel request could not be recorded'))
    mount()
    await startBackup()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel backup' }, POLL))
    await screen.findByText('the cancel request could not be recorded')
    const again = screen.getByRole('button', { name: 'Cancel backup' }) as HTMLButtonElement
    expect(again.disabled).toBe(false)
  })
})

describe('AdminBackupSection job edge cases', () => {
  it('translates the coded 409 of a cancel that came after the job ended', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { ApiCodeError } = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
    api.adminCancelBackupJob.mockRejectedValue(
      new ApiCodeError('adminDash.apiErr.backupCancelFinished', { state: 'succeeded' }),
    )
    mount()
    await startBackup()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel backup' }, POLL))
    await screen.findByText('The backup had already finished, so there was nothing to cancel.')
  })

  it('stops polling and says why when the server fails a dead worker\'s job', async () => {
    mount()
    await startBackup()
    await screen.findByText(/Backup in progress/, undefined, POLL)
    current = backupJob({
      state: 'failed', error: 'the server worker running this job exited',
      result: { code: 'adminDash.apiErr.jobWorkerExited' },
    })
    await screen.findByText(/The server process running this job exited/, undefined, POLL)
    expect(sessionStorage.getItem(JOB_KEY)).toBeNull()
    const polls = api.adminGetBackupJob.mock.calls.length
    await new Promise((r) => setTimeout(r, 1300))
    expect(api.adminGetBackupJob.mock.calls.length).toBe(polls)
  })

  it('reports, once, a backup cancelled while the section was unmounted', async () => {
    sessionStorage.setItem(JOB_KEY, 'b-1')
    current = backupJob({ state: 'cancelled', result: { cancelled_by: 'admin' } })
    mount()
    await screen.findByText('The backup was cancelled. No archive was saved.', undefined, POLL)
    expect(sessionStorage.getItem(JOB_KEY)).toBeNull()
    expect(screen.queryByRole('button', { name: /Cancel backup|Cancelling/ })).toBeNull()
    // The next remount has no pointer and says nothing.
    cleanup()
    mount()
    await screen.findByRole('button', { name: 'Start backup' })
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('The backup was cancelled. No archive was saved.')).toBeNull()
  })

  it('reports a too-late cancel that ended while the section was unmounted', async () => {
    sessionStorage.setItem(JOB_KEY, 'b-1')
    current = backupJob({
      state: 'succeeded', progress: { phase: 'done', bytes_written: 10 },
      result: { backup_id: 'x', size_bytes: 10, warnings: [], cancel_too_late: true },
    })
    mount()
    await screen.findByText(/finished before it could be cancelled/, undefined, POLL)
    expect(sessionStorage.getItem(JOB_KEY)).toBeNull()
  })

  it('reports a failure that happened while the section was unmounted', async () => {
    sessionStorage.setItem(JOB_KEY, 'b-1')
    current = backupJob({
      state: 'failed', error: 'x', result: { code: 'adminDash.apiErr.jobWorkerExited' },
    })
    mount()
    await screen.findByText(/The server process running this job exited/, undefined, POLL)
  })

  it('renders coded warnings translated, falling back to the sentence for an unknown code', async () => {
    mount()
    await startBackup()
    await screen.findByText(/Backup in progress/, undefined, POLL)
    current = backupJob({
      state: 'succeeded', progress: { phase: 'done', bytes_written: 10 },
      result: {
        backup_id: 'x', size_bytes: 10,
        warnings: ['thumbnail 7.jpg is missing on disk; not archived', 'something new from a newer server'],
        warning_items: [
          { code: 'thumbnailMissing', params: { name: '7.jpg', slide: 7 } },
          { code: 'notKnownYet', params: {} },
        ],
      },
    })
    await screen.findByText(
      'Thumbnail 7.jpg is missing on disk; not archived (rebuild thumbnails after a restore).',
      undefined, POLL,
    )
    screen.getByText('something new from a newer server')
    expect(screen.queryByText('thumbnail 7.jpg is missing on disk; not archived')).toBeNull()
  })
})

describe('AdminBackupSection: a backup this tab did not start', () => {
  it('follows a backup started elsewhere, with its Cancel', async () => {
    api.adminGetActiveJob.mockResolvedValue(backupJob({ id: 'b-9' }))
    current = backupJob({ id: 'b-9' })
    mount()
    await screen.findByRole('button', { name: 'Cancel backup' }, POLL)
    expect(sessionStorage.getItem(JOB_KEY)).toBe('b-9')
    expect((screen.getByRole('button', { name: 'Starting…' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('follows the running backup when Start answers busy', async () => {
    mount()
    await screen.findByRole('button', { name: 'Start backup' })
    api.adminCreateBackup.mockRejectedValueOnce(new Error('another backup/restore job is in progress'))
    api.adminGetActiveJob.mockResolvedValue(backupJob({ id: 'b-7' }))
    current = backupJob({ id: 'b-7' })
    await startBackup()
    await screen.findByRole('button', { name: 'Cancel backup' }, POLL)
    expect(screen.queryByText(/another backup\/restore job/)).toBeNull()
  })

  it('names another kind of job that holds the slot', async () => {
    api.adminGetActiveJob.mockResolvedValue(backupJob({ id: 'x-1', kind: 'slide_import' }))
    mount()
    await screen.findByText('Another job is running: a slide import. A backup can start once it has finished.')
  })
})

describe('AdminBackupSection: verifying a saved backup', () => {
  const item = {
    id: '20261028T000000Z-abcd1234', path: '', size_bytes: 1024, created_at: '2026-10-28T00:00:00Z',
    scope: 'metadata', app_version: '1.0.0', sha256: 'a'.repeat(64), note: '',
  }

  it('reports an intact backup', async () => {
    api.adminListBackups.mockResolvedValue({ items: [item], total_bytes: 1024 })
    api.adminVerifyBackup.mockResolvedValue({ job_id: 'v-1' })
    api.adminGetBackupJob.mockImplementation(async () => ({
      id: 'v-1', kind: 'backup_verify', state: 'succeeded', created_at: '', progress: {}, error: null,
      result: { backup_id: item.id, sha256_checked: true, members: 3, bytes: 10 },
    }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    await screen.findByText(new RegExp(`Backup ${item.id} is intact`), undefined, POLL)
    expect(api.adminVerifyBackup).toHaveBeenCalledWith(item.id)
  })

  it('reports a damaged backup as an alert', async () => {
    api.adminListBackups.mockResolvedValue({ items: [item], total_bytes: 1024 })
    api.adminVerifyBackup.mockResolvedValue({ job_id: 'v-2' })
    api.adminGetBackupJob.mockImplementation(async () => ({
      id: 'v-2', kind: 'backup_verify', state: 'failed', created_at: '', progress: {},
      error: 'the archive is damaged or truncated (bad crc)', result: { damaged: true },
    }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    const alert = await screen.findByRole('alert', undefined, POLL)
    expect(alert.textContent).toMatch(/is damaged and should not be relied on: the archive is damaged/)
  })
})

describe('AdminBackupSection: a check that could not run, or was stopped', () => {
  const item = {
    id: '20261028T000000Z-abcd5678', path: '', size_bytes: 1024, created_at: '2026-10-28T00:00:00Z',
    scope: 'metadata', app_version: '1.0.0', sha256: 'a'.repeat(64), note: '',
  }

  it('does not call the backup damaged when the check itself failed', async () => {
    api.adminListBackups.mockResolvedValue({ items: [item], total_bytes: 1024 })
    api.adminVerifyBackup.mockResolvedValue({ job_id: 'v-3' })
    api.adminGetBackupJob.mockImplementation(async () => ({
      id: 'v-3', kind: 'backup_verify', state: 'failed', created_at: '', progress: {},
      error: 'the archive could not be read (Permission denied)', result: {},
    }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    const alert = await screen.findByRole('alert', undefined, POLL)
    expect(alert.textContent).toMatch(/could not be checked/)
    expect(alert.textContent).not.toMatch(/is damaged/)
  })

  it('offers Stop check while the check runs, and follows one started elsewhere', async () => {
    api.adminListBackups.mockResolvedValue({ items: [item], total_bytes: 1024 })
    api.adminGetActiveJob.mockResolvedValue({
      id: 'v-4', kind: 'backup_verify', state: 'running', created_at: '',
      progress: { backup_id: item.id }, error: null, result: {},
    })
    current = backupJob({ id: 'v-4', kind: 'backup_verify', progress: { backup_id: item.id } })
    mount()
    const stop = await screen.findByRole('button', { name: 'Stop check' }, POLL)
    expect((screen.getByRole('button', { name: 'Start backup' }) as HTMLButtonElement).disabled).toBe(true)
    let accept: () => void = () => {}
    api.adminCancelBackupJob.mockReturnValueOnce(new Promise<void>((r) => { accept = r }))
    fireEvent.click(stop)
    await waitFor(() => expect(api.adminCancelBackupJob).toHaveBeenCalledWith('v-4'))
    // One stop per check: disabled while it is out, and still once accepted
    // while the check winds down (a second would report it as finished).
    await waitFor(() => expect((stop as HTMLButtonElement).disabled).toBe(true))
    await act(async () => accept())
    expect((stop as HTMLButtonElement).disabled).toBe(true)
    current = backupJob({ id: 'v-4', kind: 'backup_verify', state: 'cancelled' })
    await screen.findByText(new RegExp(`The check of backup ${item.id} was stopped`), undefined, POLL)
  })
})

