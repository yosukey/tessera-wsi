// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminListSeasonalBackgrounds: vi.fn(),
  adminUpdateSeasonalBackground: vi.fn(),
  adminResetSeasonalBackground: vi.fn(),
  adminUploadSeasonalBackgroundImage: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import type { SeasonalBackground } from '../../lib/api'
import AdminSeasonalBackgroundsSection from './AdminSeasonalBackgroundsSection'

const SLOT1: SeasonalBackground = {
  slot: 1, enabled: true, label: 'Spring', start_month: 3, start_day: 1, end_month: 5, end_day: 31,
  image_url: '', has_image: false, title_color: '',
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('AdminSeasonalBackgroundsSection', () => {
  it('locks a row while its save is in flight', async () => {
    // The response replaces the row's fields with the ones sent, so an edit
    // made after Save would be overwritten and marked as saved. The row's
    // controls are disabled until the save lands.
    api.adminListSeasonalBackgrounds.mockResolvedValue([SLOT1])
    let resolveSave: (v: SeasonalBackground) => void = () => {}
    api.adminUpdateSeasonalBackground.mockReturnValue(new Promise((r) => { resolveSave = r }))
    render(<I18nProvider><AdminSeasonalBackgroundsSection /></I18nProvider>)
    const label = await screen.findByDisplayValue('Spring') as HTMLInputElement
    const row = label.closest('fieldset')!
    expect(row.disabled).toBe(false)

    fireEvent.change(label, { target: { value: 'submitted' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(row.disabled).toBe(true))
    expect(api.adminUpdateSeasonalBackground).toHaveBeenCalledWith(1, expect.objectContaining({ label: 'submitted' }))

    resolveSave({ ...SLOT1, label: 'submitted' })
    await waitFor(() => expect(row.disabled).toBe(false))
    expect(label.value).toBe('submitted')
  })

  it("locks a row while its reset is out, and holds the other rows' requests", async () => {
    // The reset's answer replaces the whole row and marks it saved.
    api.adminListSeasonalBackgrounds.mockResolvedValue([SLOT1])
    let finish: (v: SeasonalBackground) => void = () => {}
    api.adminResetSeasonalBackground.mockReturnValue(new Promise((r) => { finish = r }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<I18nProvider><AdminSeasonalBackgroundsSection /></I18nProvider>)
    const label = await screen.findByDisplayValue('Spring') as HTMLInputElement
    const row = label.closest('fieldset')!
    const resets = screen.getAllByRole('button', { name: 'Reset slot' }) as HTMLButtonElement[]
    fireEvent.click(resets[0])
    await waitFor(() => expect(row.disabled).toBe(true))
    // Row 2 can still be edited, but its requests wait.
    expect(resets[1].closest('fieldset')!.disabled).toBe(false)
    expect(resets[1].disabled).toBe(true)
    finish({ ...SLOT1, label: '', enabled: false })
    await waitFor(() => expect(row.disabled).toBe(false))
    expect(label.value).toBe('')
    expect(resets[1].disabled).toBe(false)
    confirmSpy.mockRestore()
  })

  it('locks a row while its image upload is out', async () => {
    // Two uploads to one slot could land out of order.
    api.adminListSeasonalBackgrounds.mockResolvedValue([SLOT1])
    api.adminUploadSeasonalBackgroundImage.mockReturnValue(new Promise(() => {}))
    render(<I18nProvider><AdminSeasonalBackgroundsSection /></I18nProvider>)
    const label = await screen.findByDisplayValue('Spring') as HTMLInputElement
    const file = label.closest('fieldset')!.querySelector('input[type=file]') as HTMLInputElement
    fireEvent.change(file, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })
    await waitFor(() => expect(label.closest('fieldset')!.disabled).toBe(true))
    // Only that row's Save reads "Saving…" during a save, not during this.
    expect(screen.queryByRole('button', { name: 'Saving…' })).toBeNull()
  })

  it('locks the rows until the slots have loaded', async () => {
    // The load replaces every row, so an edit made before it would be lost.
    api.adminListSeasonalBackgrounds.mockReturnValue(new Promise(() => {}))
    render(<I18nProvider><AdminSeasonalBackgroundsSection /></I18nProvider>)
    const labels = screen.getAllByPlaceholderText('Label (e.g. New Year)') as HTMLInputElement[]
    expect(labels.every((l) => l.closest('fieldset')!.disabled)).toBe(true)
  })

  it('offers the load again when it failed, rather than leaving the rows locked', async () => {
    api.adminListSeasonalBackgrounds
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValueOnce([SLOT1])
    render(<I18nProvider><AdminSeasonalBackgroundsSection /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    const label = await screen.findByDisplayValue('Spring') as HTMLInputElement
    expect(label.closest('fieldset')!.disabled).toBe(false)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})
