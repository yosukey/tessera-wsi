// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getViewerSettings: vi.fn() }))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../lib/i18n'
import { STORAGE_KEYS } from '../lib/storageKeys'
import AnalyticsNoticeToast from './AnalyticsNoticeToast'

beforeEach(() => {
  localStorage.clear()
  api.getViewerSettings.mockResolvedValue({ analytics_mode: 'hashed', analytics_notice_toast_enabled: true })
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.resetAllMocks()
})

async function mount() {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/']}><AnalyticsNoticeToast /></MemoryRouter>
    </I18nProvider>,
  )
  // The live region is there before the settings fetch settles, empty.
  const region = screen.getByRole('status')
  expect(region.textContent).toBe('')
  await act(async () => {})
  return region
}

const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

describe('AnalyticsNoticeToast', () => {
  it('inserts the notice into an already-mounted live region', async () => {
    const region = await mount()
    expect(screen.getByRole('status')).toBe(region)
    expect(region.textContent).toContain('About activity recording')
  })

  it('holds the auto-dismiss while hovered, then restarts it on leave', async () => {
    const region = await mount()
    const box = screen.getByText('About activity recording').parentElement!.parentElement!
    fireEvent.pointerEnter(box, { pointerType: 'mouse' })
    advance(20_000)
    expect(region.textContent).toContain('About activity recording')
    expect(localStorage.getItem(STORAGE_KEYS.analyticsNoticeDismissed)).toBeNull()
    fireEvent.pointerLeave(box, { pointerType: 'mouse' })
    advance(7_999)
    expect(region.textContent).toContain('About activity recording')
    advance(1)
    expect(region.textContent).toBe('')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.analyticsNoticeDismissed)!).mode).toBe('hashed')
  })

  it('does not hold the auto-dismiss for a tap, whose emulated hover never leaves', async () => {
    const region = await mount()
    const box = screen.getByText('About activity recording').parentElement!.parentElement!
    // What a touch browser sends for a tap on the text: a touch pointer, then
    // the compatibility mouse events with no mouseleave after them.
    fireEvent.pointerEnter(box, { pointerType: 'touch' })
    fireEvent.mouseEnter(box)
    advance(8_000)
    expect(region.textContent).toBe('')
  })

  it('holds the auto-dismiss while focus is inside', async () => {
    const region = await mount()
    const close = screen.getByRole('button', { name: 'Dismiss' })
    act(() => close.focus())
    advance(20_000)
    expect(region.textContent).toContain('About activity recording')
    act(() => close.blur())
    advance(8_000)
    expect(region.textContent).toBe('')
  })
})
