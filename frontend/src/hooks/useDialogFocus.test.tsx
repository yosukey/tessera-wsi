// @vitest-environment jsdom
import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import AboutDialog from '../components/AboutDialog'
import SafeMarkdown from '../components/SafeMarkdown'
import FollowerJoinDialog from '../components/FollowerJoinDialog'
import BulkCategoryModal from '../components/admin/BulkCategoryModal'
import AdminAnalyticsClearModal from '../components/admin/AdminAnalyticsClearModal'
import { LockFieldset } from '../components/ui'
import { useDialogFocus } from './useDialogFocus'

afterEach(() => {
  cleanup()
  document.body.style.overflow = ''
})

function AboutHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open about</button>
      {open && <AboutDialog onClose={() => setOpen(false)} />}
    </>
  )
}

const tab = (shift = false) => fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Tab', shiftKey: shift })

describe('AboutDialog focus', () => {
  it('moves focus in, keeps Tab inside, and returns focus to the trigger on close', () => {
    render(<AboutHarness />)
    const trigger = screen.getByRole('button', { name: 'open about' })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'About Tessera' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    const first = document.activeElement
    // Shift+Tab from the first control wraps to the last one, Tab back to the first.
    tab(true)
    const last = document.activeElement
    expect(last).not.toBe(first)
    expect(dialog.contains(last)).toBe(true)
    tab()
    expect(document.activeElement).toBe(first)

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps Shift+Tab inside when the dialog container itself has focus', () => {
    render(<AboutHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'open about' }))
    const dialog = screen.getByRole('dialog', { name: 'About Tessera' })
    const first = document.activeElement
    // A click on the panel's background focuses its tabIndex={-1} container.
    const container = dialog.firstElementChild as HTMLElement
    container.focus()
    expect(document.activeElement).toBe(container)
    tab(true)
    const last = document.activeElement
    expect(last).not.toBe(container)
    expect(last).not.toBe(first)
    expect(dialog.contains(last)).toBe(true)
    // ...and it is the last control: Tab wraps from it to the first.
    tab()
    expect(document.activeElement).toBe(first)
  })
})

describe('SafeMarkdown image lightbox focus', () => {
  it('focuses the lightbox, traps Tab and returns focus to the image', () => {
    render(<I18nProvider><SafeMarkdown markdown="![Glomerulus](https://example.org/g.png)" /></I18nProvider>)
    const image = screen.getByRole('button', { name: 'Glomerulus' })
    image.focus()
    fireEvent.keyDown(image, { key: 'Enter' })

    const dialog = screen.getByRole('dialog', { name: 'Glomerulus' })
    const close = screen.getByRole('button', { name: 'Close' })
    expect(document.activeElement).toBe(close)
    tab()
    expect(document.activeElement).toBe(close)
    expect(dialog.contains(document.activeElement)).toBe(true)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(image)
  })

  it('moves Shift+Tab from the focused backdrop to the close button instead of the page', () => {
    render(<I18nProvider><SafeMarkdown markdown="![Glomerulus](https://example.org/g.png)" /></I18nProvider>)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Glomerulus' }), { key: 'Enter' })
    const dialog = screen.getByRole('dialog', { name: 'Glomerulus' })
    dialog.focus()
    // fireEvent returns false when the handler called preventDefault, i.e.
    // the browser's own focus move (out of the dialog) was suppressed.
    expect(fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })).toBe(false)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }))
  })
})

// Opens `dialog` from a trigger button; the dialog's close callback closes it.
function Harness({ dialog }: { dialog: (close: () => void) => React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <I18nProvider>
      <button type="button" onClick={() => setOpen(true)}>open dialog</button>
      {open && dialog(() => setOpen(false))}
    </I18nProvider>
  )
}

// Opens the harness's dialog and checks the trap: focus is inside, Shift+Tab
// from the first control wraps to the last and Tab from the last to the first.
// Returns the trigger and the first control focused.
function openAndCheckTrap() {
  const trigger = screen.getByRole('button', { name: 'open dialog' })
  trigger.focus()
  fireEvent.click(trigger)
  const dialog = screen.getByRole('dialog')
  const first = document.activeElement as HTMLElement
  expect(dialog.contains(first)).toBe(true)
  tab(true)
  const last = document.activeElement
  expect(last).not.toBe(first)
  expect(dialog.contains(last)).toBe(true)
  tab()
  expect(document.activeElement).toBe(first)
  return { trigger, first }
}

describe('modal dialogs keep focus inside', () => {
  it('FollowerJoinDialog: starts in the name field; Escape cancels from any control', () => {
    const onCancel = vi.fn()
    render(<Harness dialog={(close) => (
      <FollowerJoinDialog slideRef={null} onJoin={close} onCancel={() => { onCancel(); close() }} />
    )} />)
    const { trigger, first } = openAndCheckTrap()
    expect(first).toBe(screen.getByRole('textbox'))
    screen.getByRole('button', { name: 'Join' }).focus()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('BulkCategoryModal', () => {
    render(<Harness dialog={(close) => (
      <BulkCategoryModal target="slides" items={[]} metaOptions={{}} customMetaKeys={[]} onApply={() => {}} onClose={close} />
    )} />)
    const { trigger } = openAndCheckTrap()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('AdminAnalyticsClearModal', () => {
    render(<Harness dialog={(close) => (
      <AdminAnalyticsClearModal onCancel={close} onSubmit={() => {}} />
    )} />)
    const { trigger } = openAndCheckTrap()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})

describe('useDialogFocus and a LockFieldset', () => {
  it('skips the controls a locked fieldset disables', () => {
    function Dialog() {
      const ref = React.useRef<HTMLDivElement | null>(null)
      useDialogFocus(ref)
      return (
        <div ref={ref} tabIndex={-1}>
          <LockFieldset locked><input aria-label="locked" /></LockFieldset>
          <button type="button">close</button>
        </div>
      )
    }
    render(<Dialog />)
    // Not the locked input, which can't take focus.
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'close' }))
  })
})
