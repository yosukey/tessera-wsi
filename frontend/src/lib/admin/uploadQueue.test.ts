import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The upload endpoint is the queue's only I/O. Each test scripts one
// adminUploadSlide call per attempt, as a function of its callbacks.
type UploadOpts = { onProgress?: (p: number) => void; onSent?: () => void; signal?: AbortSignal }
const api = vi.hoisted(() => ({ adminUploadSlide: vi.fn(), adminListSlides: vi.fn() }))
vi.mock('../api/admin', () => api)
vi.mock('../api/auth', () => ({ isStaffFromToken: () => true }))

import { GatewayError, NetworkError } from '../api/shared'
import { UploadQueue } from './uploadQueue'

const svs = () => new File(['x'], 'a.svs')

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe('UploadQueue: when a lost connection may have created the slide', () => {
  it('retries a drop before the whole body was sent, even at 99 %', async () => {
    api.adminUploadSlide
      .mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
        opts.onProgress?.(99)
        throw new NetworkError()
      })
      .mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
        opts.onProgress?.(99)
        opts.onSent?.()
        return { ok: true, slide_id: 5 }
      })
    const q = new UploadQueue([svs()])
    const run = q.start()
    await vi.runAllTimersAsync()
    await run
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(2)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 5, attempts: 2 })
  })

  it('asks for verification, not a retry, for a drop after the whole body was sent', async () => {
    const seen: string[] = []
    api.adminUploadSlide.mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
      opts.onProgress?.(99)
      opts.onSent?.()
      throw new NetworkError()
    })
    const q = new UploadQueue([svs()])
    q.subscribe(() => seen.push(q.items[0].status))
    const run = q.start()
    await vi.runAllTimersAsync()
    await run
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
    expect(q.items[0]).toMatchObject({ status: 'failed', failureKind: 'network-verify' })
    // The item showed "processing" from the moment the body was sent.
    expect(seen).toContain('processing')
  })

  it('reports a cancel after the whole body was sent for verification', async () => {
    api.adminUploadSlide.mockImplementation(async (_f: File, opts: UploadOpts) => {
      opts.onSent?.()
      return new Promise((_res, rej) => {
        opts.signal?.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')))
      })
    })
    const q = new UploadQueue([svs()])
    const run = q.start()
    await vi.waitFor(() => expect(q.items[0].status).toBe('processing'))
    q.cancelAll()
    await run
    expect(q.items[0]).toMatchObject({ status: 'failed', failureKind: 'canceled-after-upload' })
  })

  it('reports a cancel while the body is still being sent as canceled', async () => {
    api.adminUploadSlide.mockImplementation(async (_f: File, opts: UploadOpts) => {
      opts.onProgress?.(99)
      return new Promise((_res, rej) => {
        opts.signal?.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')))
      })
    })
    const q = new UploadQueue([svs()])
    const run = q.start()
    await vi.waitFor(() => expect(q.items[0].progress).toBe(99))
    q.cancelAll()
    await run
    expect(q.items[0].status).toBe('canceled')
  })
})

describe('UploadQueue: the per-file size cap comes from the server', () => {
  const sized = (bytes: number) => {
    const f = new File(['x'], 'a.svs')
    Object.defineProperty(f, 'size', { value: bytes })
    return f
  }

  it('refuses a file over the cap it was given without sending it', async () => {
    const q = new UploadQueue([sized(11)])
    q.maxUploadBytes = 10
    await q.start()
    expect(api.adminUploadSlide).not.toHaveBeenCalled()
    expect(q.items[0]).toMatchObject({ status: 'failed', failureKind: 'too-large' })
  })

  it('sends a file above the 8 GiB default once the server allows it', async () => {
    api.adminUploadSlide.mockResolvedValueOnce({ ok: true, slide_id: 7 })
    const q = new UploadQueue([sized(9 * 1024 ** 3)])
    q.maxUploadBytes = 16 * 1024 ** 3
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 7 })
  })

  it('leaves the decision to the server while the cap is unknown', async () => {
    api.adminUploadSlide.mockResolvedValueOnce({ ok: true, slide_id: 8 })
    const q = new UploadQueue([sized(100 * 1024 ** 3)])
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
  })
})

describe('UploadQueue: a proxy answering for the server', () => {
  it('retries a gateway error before the body was sent', async () => {
    api.adminUploadSlide
      .mockImplementationOnce(async () => { throw new GatewayError(502) })
      .mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
        opts.onSent?.()
        return { ok: true, slide_id: 9 }
      })
    const q = new UploadQueue([svs()])
    const run = q.start()
    await vi.runAllTimersAsync()
    await run
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(2)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 9 })
  })

  it('asks for verification for a gateway timeout after the body was sent', async () => {
    api.adminUploadSlide.mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
      opts.onSent?.()
      throw new GatewayError(504)
    })
    const q = new UploadQueue([svs()])
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
    expect(q.items[0]).toMatchObject({ status: 'failed', failureKind: 'network-verify' })
  })
})

describe('UploadQueue: retrying an item that may already be on the server', () => {
  const sentThenDropped = async (_f: File, opts: UploadOpts) => {
    opts.onSent?.()
    throw new NetworkError()
  }

  it('marks it done when its slide is in the list, without re-sending', async () => {
    api.adminUploadSlide.mockImplementationOnce(sentThenDropped)
    vi.setSystemTime(new Date('2026-10-28T12:00:00Z'))
    api.adminListSlides.mockResolvedValueOnce([
      { id: 4, wsi_filename: '4_A.svs', file_size_bytes: 1, created_at: '2026-10-28T12:00:05+00:00' },
    ])
    const q = new UploadQueue([new File(['x'], 'a.svs')])
    await q.start()
    expect(q.requeueFailed()).toBe(true)
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 4, foundOnServer: true })
  })

  it('sends it again when the slide is not there', async () => {
    api.adminUploadSlide
      .mockImplementationOnce(sentThenDropped)
      .mockResolvedValueOnce({ ok: true, slide_id: 6 })
    api.adminListSlides.mockResolvedValueOnce([{ id: 3, wsi_filename: '3_other.svs' }])
    const q = new UploadQueue([new File(['x'], 'a.svs')])
    await q.start()
    q.requeueFailed()
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(2)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 6 })
    expect(q.items[0].foundOnServer).toBeUndefined()
  })
})

describe('UploadQueue: the accepted extensions come from the server', () => {
  it('refuses an extension the server does not list, without sending it', async () => {
    const q = new UploadQueue([new File(['x'], 'a.ndpi')])
    q.allowedExtensions = ['.svs']
    await q.start()
    expect(api.adminUploadSlide).not.toHaveBeenCalled()
    expect(q.items[0]).toMatchObject({ status: 'failed', failureKind: 'ext' })
  })

  it('sends an extension only the server lists', async () => {
    api.adminUploadSlide.mockResolvedValueOnce({ ok: true, slide_id: 2 })
    const q = new UploadQueue([new File(['x'], 'a.qptiff')])
    q.allowedExtensions = ['.svs', '.qptiff']
    await q.start()
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 2 })
  })
})

describe('UploadQueue: the slide a retry finds must be new', () => {
  it('does not take a same-named slide created before the item was sent', async () => {
    vi.setSystemTime(new Date('2026-10-28T12:00:00Z'))
    api.adminUploadSlide
      .mockImplementationOnce(async (_f: File, opts: UploadOpts) => {
        opts.onSent?.()
        throw new NetworkError()
      })
      .mockResolvedValueOnce({ ok: true, slide_id: 11 })
    api.adminListSlides.mockResolvedValueOnce([
      { id: 4, wsi_filename: '4_a.svs', created_at: '2026-10-27T09:00:00+00:00' },
    ])
    const q = new UploadQueue([new File(['x'], 'a.svs')])
    await q.start()
    q.requeueFailed()
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(2)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 11 })
  })
})

describe('UploadQueue: the slide a retry finds must be this file', () => {
  const sentThenDropped = async (_f: File, opts: UploadOpts) => {
    opts.onSent?.()
    throw new NetworkError()
  }

  it.each([
    ['of another size', { file_size_bytes: 2, created_at: '2026-10-28T12:00:05+00:00' }],
    ['without a created_at', { file_size_bytes: 1, created_at: '' }],
    ['with an unreadable created_at', { file_size_bytes: 1, created_at: 'not a date' }],
    ['without a recorded size', { created_at: '2026-10-28T12:00:05+00:00' }],
  ])('does not take a same-named slide %s, and sends the file again', async (_name, fields) => {
    vi.setSystemTime(new Date('2026-10-28T12:00:00Z'))
    api.adminUploadSlide
      .mockImplementationOnce(sentThenDropped)
      .mockResolvedValueOnce({ ok: true, slide_id: 11 })
    api.adminListSlides.mockResolvedValueOnce([{ id: 4, wsi_filename: '4_a.svs', ...fields }])
    const q = new UploadQueue([new File(['x'], 'a.svs')])
    await q.start()
    q.requeueFailed()
    await q.start()
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(2)
    expect(q.items[0]).toMatchObject({ status: 'success', slideId: 11 })
    expect(q.items[0].foundOnServer).toBeUndefined()
  })
})
