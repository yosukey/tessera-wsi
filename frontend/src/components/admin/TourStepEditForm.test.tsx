// @vitest-environment jsdom
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { I18nProvider } from '../../lib/i18n'
import type { EditState } from '../../lib/tourEditHelpers'
import TourStepEditForm from './TourStepEditForm'

afterEach(cleanup)

const editState: EditState = {
  label: 'my unsaved label', description_md: '', rotation_deg: '0', fov_um_w: '100', fov_um_h: '100',
  center_x: '0', center_y: '0', pins: [], shapes_l0: [], transition_mode: 'smooth',
}

function renderForm(props: Partial<React.ComponentProps<typeof TourStepEditForm>> = {}) {
  const handlers = {
    onSave: vi.fn(), onCancel: vi.fn(), onReloadConflict: vi.fn(), onOverwriteConflict: vi.fn(),
  }
  render(
    <I18nProvider>
      <TourStepEditForm
        editState={editState}
        setEditState={() => {}}
        hasNext
        addingPin={false}
        setAddingPin={() => {}}
        pendingPinShape={null}
        setPendingPinShape={() => {}}
        addingShape={null}
        setAddingShape={() => {}}
        cancelDraft={() => {}}
        onApplyCurrentView={() => {}}
        onCopyPinToNext={() => {}}
        onCopyShapeToNext={() => {}}
        saving={false}
        conflict={false}
        fovEstimated={false}
        {...handlers}
        {...props}
      />
    </I18nProvider>,
  )
  return handlers
}

describe('TourStepEditForm conflict banner', () => {
  it('is absent until a save conflicts', () => {
    renderForm()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps the edits on screen and offers reload or overwrite', () => {
    const h = renderForm({ conflict: true })
    expect(screen.getByRole('alert').textContent).toContain('changed in another window or by another user')
    // The user's unsaved edits are still in the fields.
    expect(screen.getByDisplayValue('my unsaved label')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Reload (discard my changes)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite with mine' }))
    expect(h.onReloadConflict).toHaveBeenCalledTimes(1)
    expect(h.onOverwriteConflict).toHaveBeenCalledTimes(1)
    expect(h.onSave).not.toHaveBeenCalled()
  })

  it('disables both ways out while a save is in flight', () => {
    renderForm({ conflict: true, saving: true })
    expect((screen.getByRole('button', { name: 'Reload (discard my changes)' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Overwrite with mine' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('TourStepEditForm label', () => {
  it('is marked required and not flagged while it holds text', () => {
    renderForm()
    const input = screen.getByRole('textbox', { name: 'Label' }) as HTMLInputElement
    expect(input.required).toBe(true)
    expect(input.getAttribute('aria-invalid')).toBe('false')
    expect(screen.queryByText('A label is required.')).toBeNull()
  })

  it('flags a blank label as soon as the editor shows it', () => {
    renderForm({ editState: { ...editState, label: '   ' } })
    const input = screen.getByRole('textbox', { name: 'Label' })
    expect(input.getAttribute('aria-invalid')).toBe('true')
    const hint = screen.getByText('A label is required.')
    expect(input.getAttribute('aria-describedby')).toBe(hint.id)
  })

  it('brings a blank label into view when Save or Overwrite is pressed', () => {
    const h = renderForm({ editState: { ...editState, label: '' }, conflict: true })
    const input = screen.getByRole('textbox', { name: 'Label' })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(document.activeElement).toBe(input)
    // The refusal itself (and its message) stays with the editor's save.
    expect(h.onSave).toHaveBeenCalledTimes(1)
    ;(document.activeElement as HTMLElement).blur()
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite with mine' }))
    expect(document.activeElement).toBe(input)
    expect(h.onOverwriteConflict).toHaveBeenCalledTimes(1)
  })

  it('leaves the focus alone when the label is filled in', () => {
    renderForm()
    const save = screen.getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    expect(document.activeElement).toBe(save)
  })
})
