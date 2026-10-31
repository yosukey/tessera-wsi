// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminStartExport: vi.fn(),
  adminDownloadExport: vi.fn(),
  pollTransferJob: vi.fn(),
}))
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, ...api }
})

import { useSlideExport } from './useSlideExport'

afterEach(() => {
  vi.resetAllMocks()
})

const done = {
  id: 'j', kind: 'slide_export', state: 'succeeded', created_at: '', progress: {}, error: null,
  result: { export_id: 'export-aaaaaaaaaaaa', archive_format: 'tar.zst' },
}

describe('useSlideExport', () => {
  it('reports the running job\'s progress, then downloads the bundle', async () => {
    let report: ((job: unknown) => void) | undefined
    let finish: ((job: unknown) => void) | undefined
    api.adminStartExport.mockResolvedValue({ job_id: 'j' })
    api.pollTransferJob.mockImplementation((_id: string, onProgress: (job: unknown) => void) => {
      report = onProgress
      return new Promise((resolve) => { finish = resolve })
    })
    const { result } = renderHook(() => useSlideExport())
    let run: Promise<void> | undefined
    act(() => { run = result.current.exportSlides([1, 2]) })
    await waitFor(() => expect(report).toBeDefined())
    // No time limit is passed: the poll lasts as long as the job runs.
    expect(api.pollTransferJob.mock.calls[0][2]).toBeUndefined()
    act(() => report!({ state: 'running', progress: { phase: 'writing_wsi', bytes_written: 42 } }))
    expect(result.current.progress).toEqual({ phase: 'writing_wsi', bytes_written: 42 })
    expect(result.current.exporting).toBe(true)
    await act(async () => { finish!(done); await run })
    expect(api.adminDownloadExport).toHaveBeenCalledWith('export-aaaaaaaaaaaa', 'tar.zst')
    expect(result.current.exporting).toBe(false)
    expect(result.current.progress).toBeNull()
  })

  it('stops polling on unmount and never starts the download', async () => {
    let signal: AbortSignal | undefined
    api.adminStartExport.mockResolvedValue({ job_id: 'j' })
    api.pollTransferJob.mockImplementation(
      (_id: string, _p: unknown, _t: unknown, _i: unknown, opts: { signal?: AbortSignal }) => {
        signal = opts?.signal
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        })
      },
    )
    const { result, unmount } = renderHook(() => useSlideExport())
    let run: Promise<void> | undefined
    act(() => { run = result.current.exportSlides([1]) })
    await waitFor(() => expect(signal).toBeDefined())
    unmount()
    expect(signal!.aborted).toBe(true)
    // Resolves quietly: nothing for the gone caller to report.
    await expect(run).resolves.toBeUndefined()
    expect(api.adminDownloadExport).not.toHaveBeenCalled()
  })

  it('still rethrows a failed export', async () => {
    api.adminStartExport.mockResolvedValue({ job_id: 'j' })
    api.pollTransferJob.mockResolvedValue({
      ...done, state: 'failed', error: 'x',
      result: { code: 'adminDash.apiErr.exportWsiMissing', params: {} },
    })
    const { result } = renderHook(() => useSlideExport())
    await act(async () => {
      await expect(result.current.exportSlides([1])).rejects.toMatchObject({
        code: 'adminDash.apiErr.exportWsiMissing',
      })
    })
    expect(api.adminDownloadExport).not.toHaveBeenCalled()
  })
})
