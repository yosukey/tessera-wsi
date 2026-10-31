// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminListExports: vi.fn(),
  adminDeleteExport: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import AdminExportHistorySection from './AdminExportHistorySection'

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function section(reloadSignal: number) {
  return (
    <I18nProvider>
      <AdminExportHistorySection reloadSignal={reloadSignal} />
    </I18nProvider>
  )
}

describe('AdminExportHistorySection', () => {
  it('ignores a superseded load that settles after the newer one', async () => {
    let failFirst!: (e: Error) => void
    api.adminListExports
      .mockReturnValueOnce(new Promise((_, reject) => { failFirst = reject }))
      .mockResolvedValueOnce([])
    const { rerender } = render(section(0))
    rerender(section(1))
    expect(await screen.findByText('No export bundles are currently available.')).toBeTruthy()

    await act(async () => { failFirst(new Error('stale load failed')) })

    expect(screen.getByText('No export bundles are currently available.')).toBeTruthy()
    expect(screen.queryByText(/stale load failed/)).toBeNull()
  })

  it('takes one row action at a time across the rows', async () => {
    // A busy id per row would let another row's finish re-enable a delete
    // still out; every row's buttons wait.
    const bundle = (id: string) => ({
      export_id: id, size_bytes: 1, sha256: 'a'.repeat(64), created_at: '2026-10-30T00:00:00Z',
      archive_format: 'tar.zst', slide_count: 1, tour_count: 0, slides: [],
    })
    api.adminListExports.mockResolvedValue([bundle('e-1'), bundle('e-2')])
    api.adminDeleteExport.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(section(0))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2))
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    await waitFor(() => expect(
      [...screen.getAllByRole('button', { name: /^(Delete|Download)$/ })].every((b) => (b as HTMLButtonElement).disabled),
    ).toBe(true))
    expect(api.adminDeleteExport).toHaveBeenCalledTimes(1)
    vi.mocked(window.confirm).mockRestore()
  })
})
