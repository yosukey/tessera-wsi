// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter } from 'react-router'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API modules are the section's only I/O; the queue, i18n and ui are real.
const api = vi.hoisted(() => ({
  adminUploadSlide: vi.fn(),
  adminGetStorage: vi.fn(),
  adminGetMyIp: vi.fn(),
}))
vi.mock('../../lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api/admin')>('../../lib/api/admin')
  return { ...actual, ...api }
})
vi.mock('../../lib/api/auth', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api/auth')>('../../lib/api/auth')
  return {
    ...actual,
    isStaffFromToken: () => true,
    isManagerOrAboveFromToken: () => true,
    isAdminFromToken: () => false,
  }
})

import { I18nProvider } from '../../lib/i18n'
import { NetworkError } from '../../lib/api/shared'
import AdminUploadSection from './AdminUploadSection'

beforeEach(() => {
  localStorage.clear()
  api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0 })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('AdminUploadSection unmount', () => {
  it('aborts the in-flight upload and clears the running flag when unmounted mid-run', async () => {
    let signal: AbortSignal | undefined
    // Hangs until aborted, like a multi-GB transfer in progress.
    api.adminUploadSlide.mockImplementation((_f: File, opts: { signal: AbortSignal }) => {
      signal = opts.signal
      return new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')))
      })
    })
    const onRunningChange = vi.fn()
    const refreshSlides = vi.fn().mockResolvedValue(undefined)
    const { container, unmount } = render(
      <MemoryRouter>
        <I18nProvider>
          <AdminUploadSection refreshSlides={refreshSlides} onRunningChange={onRunningChange} />
        </I18nProvider>
      </MemoryRouter>,
    )
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.svs')] } })
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    await waitFor(() => expect(signal).toBeDefined())
    await waitFor(() => expect(onRunningChange).toHaveBeenLastCalledWith(true))

    unmount()

    expect(signal!.aborted).toBe(true)
    expect(onRunningChange).toHaveBeenLastCalledWith(false)
  })

  async function startHangingUpload() {
    let signal: AbortSignal | undefined
    api.adminUploadSlide.mockImplementation((_f: File, opts: { signal: AbortSignal }) => {
      signal = opts.signal
      return new Promise((_res, rej) => {
        opts.signal.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')))
      })
    })
    const rendered = render(
      <MemoryRouter>
        <I18nProvider>
          <AdminUploadSection refreshSlides={vi.fn().mockResolvedValue(undefined)} />
        </I18nProvider>
      </MemoryRouter>,
    )
    const input = rendered.container.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.svs')] } })
    const before = window.history.length
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    await waitFor(() => expect(signal).toBeDefined())
    // The guard pushes exactly one sentinel entry once the run starts.
    await waitFor(() => expect(window.history.length).toBe(before + 1))
    expect(window.history.state?.wsiNavGuard).toBeTruthy()
    return { ...rendered, before, getSignal: () => signal! }
  }

  it('asks before browser Back abandons a running upload, and keeps it on cancel', async () => {
    const confirmSpy = vi.fn().mockReturnValue(false)
    vi.stubGlobal('confirm', confirmSpy)
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    try {
      const { before, getSignal } = await startHangingUpload()

      window.dispatchEvent(new PopStateEvent('popstate'))

      expect(confirmSpy).toHaveBeenCalledTimes(1)
      expect(confirmSpy).toHaveBeenCalledWith('Uploads are still running. Leaving this page cancels them. Leave anyway?')
      expect(getSignal().aborted).toBe(false) // declined: the upload carries on
      expect(back).not.toHaveBeenCalled() // and the page stays put
      expect(window.history.length).toBe(before + 2) // sentinel re-pushed
    } finally {
      back.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('lets Back leave and aborts the upload once the operator accepts', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    try {
      const { unmount, getSignal } = await startHangingUpload()

      window.dispatchEvent(new PopStateEvent('popstate'))

      expect(back).toHaveBeenCalledTimes(1) // the second Back that really leaves
      // The page going away unmounts the section, which aborts the transfer.
      unmount()
      expect(getSignal().aborted).toBe(true)
    } finally {
      back.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('does not touch an idle queue on unmount', async () => {
    const { container, unmount } = render(
      <MemoryRouter>
        <I18nProvider>
          <AdminUploadSection refreshSlides={vi.fn()} />
        </I18nProvider>
      </MemoryRouter>,
    )
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.svs')] } })
    await screen.findByRole('button', { name: 'Start upload' })
    unmount()
    expect(api.adminUploadSlide).not.toHaveBeenCalled()
  })
})

describe('AdminUploadSection free-space preflight', () => {
  const GiB = 1024 ** 3
  // What GET /api/admin/storage reports for one shared volume.
  const storage = (free: number) => ({
    total_bytes: 100 * GiB,
    free_bytes: free,
    used_bytes: 0,
    upload_space: {
      target_free_bytes: free,
      spool_free_bytes: free,
      same_volume: true,
      copies: 2,
      reserve_fraction: 0.1,
      reserve_min_bytes: 256 * 1024 * 1024,
    },
  })
  // A File reporting a multi-GB size without allocating it.
  const sized = (name: string, size: number) => {
    const f = new File(['x'], name)
    Object.defineProperty(f, 'size', { value: size })
    return f
  }
  const renderSection = () => render(
    <MemoryRouter>
      <I18nProvider>
        <AdminUploadSection refreshSlides={vi.fn().mockResolvedValue(undefined)} />
      </I18nProvider>
    </MemoryRouter>,
  )
  const select = (container: HTMLElement, files: File[]) => {
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(input, { target: { files } })
  }

  it('names the first file the server would refuse and blocks the start', async () => {
    // 10 GiB free: a 3 GiB file fits (2x + reserve ≈ 6.6 GiB), but after it
    // lands only 7 GiB are left for the second, which needs ≈ 6.6 GiB, and the
    // third finds 4 GiB. A plain "total 9 GiB + 1 GiB margin" rule would pass it.
    api.adminGetStorage.mockResolvedValue(storage(10 * GiB))
    const { container } = renderSection()
    select(container, [sized('a.svs', 3 * GiB), sized('b.svs', 3 * GiB), sized('c.svs', 3 * GiB)])
    // A polite status, not an alert: an alert would re-announce on every file
    // added or removed while the batch stays short.
    const warning = await screen.findByRole('status')
    await waitFor(() => expect(warning.textContent).toContain('"c.svs"'))
    expect(warning.textContent).not.toContain('"a.svs"')
    expect(screen.queryByRole('alert')).toBeNull()
    expect((screen.getByRole('button', { name: 'Start upload' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('lets a batch the server would take start, though it is within 1 GiB of the free space', async () => {
    // 2 x 300 MiB + the 256 MiB reserve ≈ 856 MiB of 1 GiB free: a plain margin
    // rule (300 MiB + 1 GiB > 1 GiB) would block it, the server takes it.
    api.adminGetStorage.mockResolvedValue(storage(GiB))
    const { container } = renderSection()
    select(container, [sized('a.svs', 300 * 1024 * 1024)])
    const start = await screen.findByRole('button', { name: 'Start upload' })
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('status')).toBeNull()
    expect((start as HTMLButtonElement).disabled).toBe(false)
  })

  it('does not block when the server cannot measure the volumes', async () => {
    api.adminGetStorage.mockResolvedValue({
      ...storage(0),
      upload_space: { ...storage(0).upload_space, target_free_bytes: null, spool_free_bytes: null },
    })
    const { container } = renderSection()
    select(container, [sized('a.svs', 7 * GiB)])
    const start = await screen.findByRole('button', { name: 'Start upload' })
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('status')).toBeNull()
    expect((start as HTMLButtonElement).disabled).toBe(false)
  })

  it('gates "Retry failed" on the same simulation, with the reason shown', async () => {
    let free = 100 * GiB
    api.adminGetStorage.mockImplementation(async () => storage(free))
    api.adminUploadSlide.mockRejectedValue(new Error('unreadable'))
    const { container } = renderSection()
    select(container, [sized('a.svs', 3 * GiB)])
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(2))
    free = 2 * GiB // the space went elsewhere meanwhile; reloaded after the run
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    const retry = await screen.findByRole('button', { name: 'Retry failed' })
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(true))
    expect((await screen.findByRole('status')).textContent).toContain('"a.svs"')
  })

  it('leaves files whose bytes are probably on the server out of the retry simulation', async () => {
    // b.svs was fully sent and only the answer was lost ('network-verify'):
    // the server most likely has it, inside the reported free space already.
    // Counting it again would block a retry of a.svs that fits.
    let free = 100 * GiB
    api.adminGetStorage.mockImplementation(async () => storage(free))
    api.adminUploadSlide.mockImplementation(async (file: File, opts: { onSent?: () => void }) => {
      if (file.name === 'b.svs') {
        opts.onSent?.()
        throw new NetworkError()
      }
      throw new Error('unreadable')
    })
    const { container } = renderSection()
    select(container, [sized('a.svs', 3 * GiB), sized('b.svs', 3 * GiB)])
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(2))
    // a.svs alone needs ≈ 6.6 GiB; with b.svs after it, b.svs would need
    // another ≈ 6.6 GiB of the 4 GiB a.svs leaves.
    free = 7 * GiB
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    const retry = await screen.findByRole('button', { name: 'Retry failed' })
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(3))
    await screen.findByText(/Connection lost after the file finished uploading/)
    expect((retry as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('words the warning for Retry when only the retry is short', async () => {
    // The queued c.svs fits on its own; adding the failed a.svs back in front
    // of it does not. Start stays enabled, Retry is disabled, and the warning
    // says it is about the retry.
    let free = 100 * GiB
    api.adminGetStorage.mockImplementation(async () => storage(free))
    api.adminUploadSlide.mockRejectedValue(new Error('unreadable'))
    const { container } = renderSection()
    select(container, [sized('a.svs', 3 * GiB)])
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(2))
    free = 7 * GiB
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    await screen.findByRole('button', { name: 'Retry failed' })
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(3))
    select(container, [sized('c.svs', 3 * GiB)])
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalledTimes(4))
    expect((screen.getByRole('button', { name: 'Start upload' }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: 'Retry failed' }) as HTMLButtonElement).disabled).toBe(true)
    const warning = await screen.findByRole('status')
    await waitFor(() => expect(warning.textContent).toMatch(/to retry the failed files/))
    expect(warning.textContent).toContain('"c.svs"')
  })
})

describe('AdminUploadSection size cap', () => {
  const GiB = 1024 ** 3
  const sized = (name: string, size: number) => {
    const f = new File(['x'], name)
    Object.defineProperty(f, 'size', { value: size })
    return f
  }
  const renderSection = () => render(
    <MemoryRouter>
      <I18nProvider>
        <AdminUploadSection refreshSlides={vi.fn().mockResolvedValue(undefined)} />
      </I18nProvider>
    </MemoryRouter>,
  )

  it('names and enforces the cap the server reports', async () => {
    api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0, max_upload_bytes: 16 * GiB })
    api.adminUploadSlide.mockResolvedValue({ ok: true, slide_id: 1 })
    const { container } = renderSection()
    expect(await screen.findByText(/Max 16 GB/)).toBeTruthy()
    const input = container.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [sized('big.svs', 9 * GiB), sized('huge.svs', 17 * GiB)] } })
    fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
    expect(await screen.findByText('File size exceeds the limit (16 GB)')).toBeTruthy()
    // The 9 GiB file (over the 8 GiB default) went out; the 17 GiB one did not.
    expect(api.adminUploadSlide).toHaveBeenCalledTimes(1)
    expect((api.adminUploadSlide.mock.calls[0][0] as File).name).toBe('big.svs')
  })

  it('names no limit while the server has not reported one', async () => {
    renderSection()
    await waitFor(() => expect(api.adminGetStorage).toHaveBeenCalled())
    expect(screen.queryByText(/Max \d/)).toBeNull()
    expect(screen.getByText(/^Allowed: \.svs/)).toBeTruthy()
  })
})
