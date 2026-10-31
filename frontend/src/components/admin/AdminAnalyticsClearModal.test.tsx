// @vitest-environment jsdom
import React, { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../lib/i18n'
import AdminAnalyticsClearModal from './AdminAnalyticsClearModal'

afterEach(() => {
  cleanup()
  document.body.style.overflow = ''
})

function mount(onSubmit = vi.fn(), { strict = false, onCancel = () => {} } = {}) {
  const tree = (
    <I18nProvider>
      <AdminAnalyticsClearModal onCancel={onCancel} onSubmit={onSubmit} tzLabel="Asia/Tokyo" />
    </I18nProvider>
  )
  render(strict ? <StrictMode>{tree}</StrictMode> : tree)
  return onSubmit
}

const dateInputs = () => document.querySelectorAll<HTMLInputElement>('input[type="date"]')

describe('AdminAnalyticsClearModal', () => {
  it('names the analytics timezone next to the dates', () => {
    mount()
    expect(screen.getByText('Analytics timezone: Asia/Tokyo')).toBeTruthy()
  })

  it('announces the warning and keeps focus in the dialog on a ranged clear', () => {
    const onSubmit = mount()
    fireEvent.change(dateInputs()[0], { target: { value: '2026-01-01' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const warning = screen.getByRole('alert')
    expect(warning.textContent).toMatch(/on or after 2026-01-01.*cannot be undone/)
    expect(screen.getByRole('dialog').getAttribute('aria-describedby')).toBe(warning.id)
    const back = screen.getByRole('button', { name: 'Back' })
    expect(document.activeElement).toBe(back)
    // No typed word for a ranged clear.
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    expect(onSubmit).toHaveBeenCalledWith({ fromDate: '2026-01-01', toDate: undefined })
  })

  it('requires the typed word for a wipe-everything and submits an explicit all scope', () => {
    const onSubmit = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const input = screen.getByRole('textbox')
    expect(document.activeElement).toBe(input)
    const yes = screen.getByRole('button', { name: 'Yes, delete' }) as HTMLButtonElement
    expect(yes.disabled).toBe(true)
    fireEvent.change(input, { target: { value: 'delete' } })
    expect(yes.disabled).toBe(true)
    fireEvent.change(input, { target: { value: 'DELETE' } })
    expect(yes.disabled).toBe(false)
    fireEvent.click(yes)
    expect(onSubmit).toHaveBeenCalledWith({ all: true })
  })

  it('returns focus to the Delete button when going back, and forgets the typed word', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'DELETE' } })
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('')
  })

  it('does not submit a wipe on the Enter that commits an IME composition', () => {
    const onSubmit = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const input = screen.getByRole('textbox')
    // A Japanese IME has the word in the field before the composition ends.
    fireEvent.change(input, { target: { value: 'DELETE' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    expect(onSubmit).not.toHaveBeenCalled()
    // The plain Enter after it does submit.
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 })
    expect(onSubmit).toHaveBeenCalledWith({ all: true })
  })

  it('accepts the word typed in full-width by a Japanese IME', () => {
    const onSubmit = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'ＤＥＬＥＴＥ' } })
    const yes = screen.getByRole('button', { name: 'Yes, delete' }) as HTMLButtonElement
    expect(yes.disabled).toBe(false)
    fireEvent.click(yes)
    expect(onSubmit).toHaveBeenCalledWith({ all: true })
  })

  it('does not close on the Escape that cancels an IME conversion', () => {
    const onCancel = vi.fn()
    mount(vi.fn(), { onCancel })
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const input = screen.getByRole('textbox')
    fireEvent.keyDown(input, { key: 'Escape', isComposing: true })
    fireEvent.keyDown(input, { key: 'Escape', keyCode: 229 })
    expect(onCancel).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Escape', keyCode: 27 })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('leaves the initial focus where the dialog put it under StrictMode', () => {
    mount(vi.fn(), { strict: true })
    // useDialogFocus focuses the first control, the From date; StrictMode's
    // second effect run must not count as a phase change onto Delete.
    expect(document.activeElement).toBe(dateInputs()[0])
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(document.activeElement).toBe(screen.getByRole('textbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' }))
  })
})
