// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminListMetadataOptions: vi.fn(),
  adminUpdateTour: vi.fn(),
  tourConflictOf: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import type { TourSummary } from '../../lib/api'
import TourMetadataModal from './TourMetadataModal'

const TOUR: TourSummary = {
  id: 5, public_id: 't5', slide_public_id: 'p7', slide_id: 7, title: 'Lobule', description: '',
  is_published: false, unlisted: false, revision: 3, attributes: { level: ['basic'] },
}

function mount(tour: TourSummary = TOUR) {
  const props = { onSaved: vi.fn(), onConflict: vi.fn(), onClose: vi.fn() }
  render(
    <I18nProvider>
      <TourMetadataModal tour={tour} slideAttributes={{ stain: ['HE'], organ: ['Liver'] }} {...props} />
    </I18nProvider>,
  )
  return props
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const checkbox = (value: string) => screen.getByLabelText(value, { exact: false }) as HTMLInputElement

describe('TourMetadataModal', () => {
  it("lists the slide's metadata as inherited and the tour's own as picked", async () => {
    api.adminListMetadataOptions.mockResolvedValue({ options: { stain: ['HE', 'PAS'], level: ['basic', 'advanced'] } })
    mount()
    const inherited = screen.getByRole('region', { name: 'From the slide' }).textContent
    expect(inherited).toContain('HE')
    expect(inherited).toContain('Liver')
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    expect(checkbox('basic').checked).toBe(true)
    expect(checkbox('advanced').checked).toBe(false)
    // Nothing changed yet, so nothing to save.
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves the whole own-attribute map against the tour revision', async () => {
    api.adminListMetadataOptions.mockResolvedValue({ options: { level: ['basic', 'advanced'] } })
    const saved = { ...TOUR, revision: 4, attributes: { level: ['advanced', 'basic'] } }
    api.adminUpdateTour.mockResolvedValue({ ok: true, revision: 4, tour: saved })
    const props = mount()
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    fireEvent.click(checkbox('advanced'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledWith(saved))
    expect(api.adminUpdateTour).toHaveBeenCalledWith(5, { attributes: { level: ['basic', 'advanced'] } }, 3)
    expect(props.onClose).toHaveBeenCalled()
  })

  it('keeps the picks on a conflict and hands the server copy to the editor', async () => {
    api.adminListMetadataOptions.mockResolvedValue({ options: { level: ['basic', 'advanced'] } })
    const current = { tour: { ...TOUR, revision: 9, attributes: { level: ['advanced'] } }, steps: [] }
    api.adminUpdateTour.mockRejectedValue(new Error('409'))
    api.tourConflictOf.mockReturnValue(current)
    const props = mount()
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    fireEvent.click(checkbox('basic'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('alert')
    expect(props.onConflict).toHaveBeenCalledWith(current)
    expect(props.onClose).not.toHaveBeenCalled()
    expect(checkbox('basic').checked).toBe(false)
    // "Reload" adopts the server's metadata.
    fireEvent.click(screen.getByRole('button', { name: 'Reload (discard my changes)' }))
    expect(checkbox('advanced').checked).toBe(true)
    expect(checkbox('basic').checked).toBe(false)
  })

  it('asks before closing over unsaved picks', async () => {
    api.adminListMetadataOptions.mockResolvedValue({ options: { level: ['basic', 'advanced'] } })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const props = mount()
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    fireEvent.click(checkbox('advanced'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(confirmSpy).toHaveBeenCalled()
    expect(props.onClose).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it("can't be closed while a save is in flight", async () => {
    api.adminListMetadataOptions.mockResolvedValue({ options: { level: ['basic', 'advanced'] } })
    api.adminUpdateTour.mockReturnValue(new Promise(() => {}))
    // Would confirm a discard, so only the in-flight guard can keep it open.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const props = mount()
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    fireEvent.click(checkbox('advanced'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true))
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByRole('dialog'))
    expect(props.onClose).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('locks the picks while a save is in flight', async () => {
    // A successful save closes the dialog, so a pick made after Save (and not
    // in the request) would be lost. The pickers are disabled until it lands.
    api.adminListMetadataOptions.mockResolvedValue({ options: { level: ['basic', 'advanced'] } })
    let resolveSave: (v: unknown) => void = () => {}
    api.adminUpdateTour.mockReturnValue(new Promise((r) => { resolveSave = r }))
    const props = mount()
    await waitFor(() => expect(checkbox('advanced')).toBeTruthy())
    expect(checkbox('basic').closest('fieldset')!.disabled).toBe(false)
    fireEvent.click(checkbox('advanced'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(checkbox('basic').closest('fieldset')!.disabled).toBe(true))
    const saved = { ...TOUR, revision: 4, attributes: { level: ['basic', 'advanced'] } }
    resolveSave({ ok: true, revision: 4, tour: saved })
    await waitFor(() => expect(props.onClose).toHaveBeenCalled())
    expect(props.onSaved).toHaveBeenCalledWith(saved)
  })
})
