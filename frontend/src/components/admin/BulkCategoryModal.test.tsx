// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../lib/i18n'
import BulkCategoryModal from './BulkCategoryModal'

afterEach(cleanup)

describe('BulkCategoryModal a11y', () => {
  it('labels the dialog with its title', () => {
    render(
      <I18nProvider>
        <BulkCategoryModal
          target="slides"
          items={[{ attributes: {} }]}
          metaOptions={{}} customMetaKeys={[]} onApply={vi.fn()} onClose={vi.fn()}
        />
      </I18nProvider>,
    )
    const dialog = screen.getByRole('dialog')
    const titleId = dialog.getAttribute('aria-labelledby')
    expect(titleId).toBeTruthy()
    expect(document.getElementById(titleId!)?.textContent).toMatch(/1/)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })
})

describe('BulkCategoryModal for tours', () => {
  it('titles the dialog for tours and says it edits their own metadata', () => {
    render(
      <I18nProvider>
        <BulkCategoryModal
          target="tours"
          items={[{ attributes: { stain: ['H&E'] } }, { attributes: null }]}
          metaOptions={{ stain: ['H&E', 'PAS'] }} customMetaKeys={[]} onApply={vi.fn()} onClose={vi.fn()}
        />
      </I18nProvider>,
    )
    expect(screen.getByRole('dialog').textContent).toContain('Bulk tour metadata (2 tours)')
    expect(screen.getByText(/inherit from their slide/)).toBeTruthy()
  })
})

describe('BulkCategoryModal while applying', () => {
  function mount(busy: boolean) {
    const onApply = vi.fn()
    render(
      <I18nProvider>
        <BulkCategoryModal
          target="tours"
          items={[{ attributes: { stain: ['H&E'] } }]}
          metaOptions={{ stain: ['H&E', 'PAS'] }} customMetaKeys={[]} onApply={onApply} onClose={vi.fn()}
          busy={busy}
        />
      </I18nProvider>,
    )
    return onApply
  }

  it('can apply once a value is picked, and not while the change is in flight', () => {
    mount(false)
    const apply = () => screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement
    expect(apply().disabled).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /PAS \(0\/1\)/ }))
    expect(apply().disabled).toBe(false)
    cleanup()
    mount(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /PAS \(0\/1\)/ }))
    expect(apply().disabled).toBe(true)
  })
  it('locks the key, mode and value picks while the change is in flight', () => {
    // A successful apply closes the dialog, so a pick made after Apply (not in
    // the request) would be dropped without a word.
    mount(false)
    const fieldset = () => screen.getByRole('checkbox', { name: /PAS \(0\/1\)/ }).closest('fieldset')!
    expect(fieldset().disabled).toBe(false)
    cleanup()
    mount(true)
    expect(fieldset().disabled).toBe(true)
    for (const select of screen.getAllByRole('combobox')) expect(fieldset().contains(select)).toBe(true)
  })
})
