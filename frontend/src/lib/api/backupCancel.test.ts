// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminCancelBackupJob, isTerminalJobState, waitForJob } from './backup'

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('backup job cancel API', () => {
  it('POSTs to the job cancel endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(202, { job_id: 'abc', cancel_requested: true }))
    vi.stubGlobal('fetch', fetchMock)
    await adminCancelBackupJob('abc')
    expect(fetchMock).toHaveBeenCalledWith('/api/admin/backup/jobs/abc/cancel', expect.objectContaining({ method: 'POST' }))
  })

  it('throws the server detail when the job already finished (409)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(409, { detail: 'the job has already finished (succeeded)' })))
    await expect(adminCancelBackupJob('abc')).rejects.toThrow('the job has already finished (succeeded)')
  })

  it('treats cancelled as terminal', async () => {
    expect(isTerminalJobState('cancelled')).toBe(true)
    expect(isTerminalJobState('running')).toBe(false)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(200, {
      id: 'abc', kind: 'backup', state: 'cancelled', created_at: '', progress: {}, error: null, result: {},
    })))
    const j = await waitForJob('abc', { intervalMs: 1 })
    expect(j.state).toBe('cancelled')
  })
})
