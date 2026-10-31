// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminUploadTourAsset: vi.fn(),
  adminDeleteTourAsset: vi.fn(),
  adminListTourAssets: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import TourAssetsPanel from './TourAssetsPanel'

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function panel(tourId: number, setAssets: (a: { name: string; url: string }[]) => void, maxBytes: number | null = 2621440) {
  return (
    <I18nProvider>
      <TourAssetsPanel
        tourId={tourId}
        tourPublicId={`pub${tourId}`}
        assets={[]}
        setAssets={setAssets}
        maxBytes={maxBytes}
        onInsertMarkdown={() => false}
        editorOpen={false}
      />
    </I18nProvider>
  )
}

describe('TourAssetsPanel', () => {
  it('drops an upload that finishes after the admin moved to another tour', async () => {
    let finishUpload!: (v: { name: string }) => void
    api.adminUploadTourAsset.mockReturnValue(new Promise((r) => { finishUpload = r }))
    api.adminListTourAssets.mockResolvedValue({ items: [{ name: 'a.png', url: '/assets/tours/pub1/a.png' }] })
    const setAssets = vi.fn()
    const { container, rerender } = render(panel(1, setAssets))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })
    expect(api.adminUploadTourAsset).toHaveBeenCalledWith(1, expect.any(File), 'rename', 2621440)

    rerender(panel(2, setAssets))
    await act(async () => { finishUpload({ name: 'a.png' }) })

    expect(setAssets).not.toHaveBeenCalled()
    expect(screen.queryByText('Uploaded: a.png')).toBeNull()
  })

  it('applies an upload that finishes on the same tour', async () => {
    api.adminUploadTourAsset.mockResolvedValue({ name: 'a.png' })
    const items = [{ name: 'a.png', url: '/assets/tours/pub1/a.png' }]
    api.adminListTourAssets.mockResolvedValue({ items })
    const setAssets = vi.fn()
    const { container } = render(panel(1, setAssets))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })

    expect(await screen.findByText('Uploaded: a.png')).toBeTruthy()
    expect(setAssets).toHaveBeenCalledWith(items)
  })

  it('names the server cap it was given when a file is too large', async () => {
    const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
    api.adminUploadTourAsset.mockRejectedValue(new actual.FileTooLargeError())
    const { container } = render(panel(1, vi.fn(), 10 * 1024 * 1024))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })

    expect(await screen.findByText('File is too large. Maximum allowed size is 10 MB.')).toBeTruthy()
  })

  it('falls back to the server wording while the cap is unknown', async () => {
    const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
    api.adminUploadTourAsset.mockRejectedValue(new actual.FileTooLargeError())
    const { container } = render(panel(1, vi.fn(), null))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })

    expect(await screen.findByText('File is larger than the server accepts.')).toBeTruthy()
    expect(api.adminUploadTourAsset).toHaveBeenCalledWith(1, expect.any(File), 'rename', null)
  })

  it('takes one upload at a time, its controls locked until it lands', async () => {
    // Each answer replaces the whole list with its own fetch: two out at
    // once could land out of order and drop the later asset.
    let finishUpload!: (v: { name: string }) => void
    api.adminUploadTourAsset.mockReturnValue(new Promise((r) => { finishUpload = r }))
    api.adminListTourAssets.mockResolvedValue({ items: [] })
    const { container } = render(panel(1, vi.fn()))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })
    await act(async () => {})
    expect(input.disabled).toBe(true)
    expect((container.querySelector('select') as HTMLSelectElement).disabled).toBe(true)
    // A second pick meanwhile (were it to get through) sends nothing.
    fireEvent.change(input, { target: { files: [new File(['y'], 'b.png', { type: 'image/png' })] } })
    expect(api.adminUploadTourAsset).toHaveBeenCalledTimes(1)
    await act(async () => { finishUpload({ name: 'a.png' }) })
    expect(input.disabled).toBe(false)
  })
})
