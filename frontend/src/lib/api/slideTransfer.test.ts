// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { pollTransferJob, transferJobError } from './slideTransfer'
import { ApiCodeError } from './shared'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function jobResponse(state: string, progress: Record<string, unknown> = {}) {
  return new Response(JSON.stringify({
    id: 'j', kind: 'slide_import', state, created_at: '', progress, error: null, result: {},
  }), { status: 200, headers: { 'content-type': 'application/json' } })
}

describe('pollTransferJob', () => {
  it('keeps polling a running job for hours, with no fixed cut-off', async () => {
    vi.useFakeTimers()
    const hours = 3
    const polls = hours * 3600 + 2
    let n = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      n += 1
      return n < polls ? jobResponse('running', { phase: 'importing', done: n, total: polls }) : jobResponse('succeeded')
    }))
    const seen: number[] = []
    const result = pollTransferJob('j', (job) => { seen.push(job.progress.done ?? 0) }, undefined, 1000)
    let settled = false
    result.finally(() => { settled = true })
    for (let i = 0; i < polls && !settled; i++) {
      await vi.advanceTimersByTimeAsync(1000)
    }
    const job = await result
    expect(job.state).toBe('succeeded')
    expect(n).toBe(polls)
    // Every running poll's progress reached the caller.
    expect(seen.length).toBe(polls)
  })

  it('still gives up after repeated poll failures', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"gone"}', { status: 500 })))
    const result = pollTransferJob('j', undefined, undefined, 1000)
    const caught = result.catch((e) => e)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(await caught).toBeInstanceOf(Error)
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(5)
  })

  it('ends on the failed record the server makes of a job whose worker exited', async () => {
    // GET /api/admin/slides/transfer/jobs/{id} reaps a dead worker's job
    // into state failed with result.code jobWorkerExited (JobStore
    // .get_reaping_orphan); the unlimited poll must stop there, with a
    // translatable reason.
    vi.useFakeTimers()
    let n = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      n += 1
      if (n < 3) return jobResponse('running', { phase: 'exporting' })
      return new Response(JSON.stringify({
        id: 'j', kind: 'slide_export', state: 'failed', created_at: '', progress: {},
        error: 'the server worker running this job exited',
        result: { code: 'adminDash.apiErr.jobWorkerExited' },
      }), { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    const result = pollTransferJob('j', undefined, undefined, 1000)
    await vi.advanceTimersByTimeAsync(5000)
    const job = await result
    expect(job.state).toBe('failed')
    expect(n).toBe(3)
    const err = transferJobError(job, 'export failed')
    expect(err).toBeInstanceOf(ApiCodeError)
    expect((err as ApiCodeError).code).toBe('adminDash.apiErr.jobWorkerExited')
    // No further polls once terminal.
    await vi.advanceTimersByTimeAsync(5000)
    expect(n).toBe(3)
  })
})
