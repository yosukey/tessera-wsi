// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getAuthState: vi.fn(), appLogout: vi.fn() }))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  ...api,
}))

import { RateLimitError } from '../lib/api'
import { I18nProvider } from '../lib/i18n'
import SignOutButton from './SignOutButton'

const realLocation = window.location
const assign = vi.fn()
const confirmSpy = vi.fn((_msg?: string) => true)
const alertSpy = vi.fn((_msg?: string) => {})

beforeEach(() => {
  localStorage.setItem('wsi-locale', 'en')
  api.appLogout.mockResolvedValue(undefined)
  confirmSpy.mockReturnValue(true)
  vi.stubGlobal('confirm', confirmSpy)
  vi.stubGlobal('alert', alertSpy)
  Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, assign } })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
})

function mount() {
  return render(<I18nProvider><SignOutButton /></I18nProvider>)
}

describe('SignOutButton', () => {
  it.each(['ldap', 'google_oauth'])('signs a %s session out and goes to the login page', async (mode) => {
    api.getAuthState.mockResolvedValue({ mode, authenticated: true, app_session: true })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(confirmSpy).toHaveBeenCalledWith('Sign out?')
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'))
    expect(api.appLogout).toHaveBeenCalledTimes(1)
  })

  it('stays signed in when the confirmation is declined', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: true, app_session: true })
    confirmSpy.mockReturnValue(false)
    mount()
    const button = await screen.findByRole('button', { name: 'Sign out' }) as HTMLButtonElement
    fireEvent.click(button)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(api.appLogout).not.toHaveBeenCalled()
    expect(assign).not.toHaveBeenCalled()
    expect(button.disabled).toBe(false)
  })

  it.each([
    ['none', true, false],
    ['basic', true, true],
    ['ldap', false, false],
    // Staff let in by their dashboard token, or a Cloudflare Access identity:
    // signing out ends neither, so /login would only send them back here.
    ['ldap', true, false],
    ['google_oauth', true, false],
  ])('is not offered in mode %s (authenticated: %s, app session: %s)', async (mode, authenticated, appSession) => {
    api.getAuthState.mockResolvedValue({ mode, authenticated, app_session: appSession })
    const { container } = mount()
    await waitFor(() => expect(api.getAuthState).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(container.innerHTML).toBe('')
  })

  it.each([
    ['cannot be reached', new TypeError('Failed to fetch')],
    // appLogout rejects for an answer that did not end the session.
    ['answers without ending the session', new RateLimitError()],
  ])('stays on the page, and says so, when the server %s', async (_case, err) => {
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: true, app_session: true })
    api.appLogout.mockRejectedValue(err)
    mount()
    const button = await screen.findByRole('button', { name: 'Sign out' }) as HTMLButtonElement
    fireEvent.click(button)
    await waitFor(() => expect(button.disabled).toBe(false))
    expect(alertSpy).toHaveBeenCalledWith('Could not sign out: you are still signed in. Please try again.')
    expect(assign).not.toHaveBeenCalled()
  })
})
