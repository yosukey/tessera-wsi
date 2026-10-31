// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminLogin: vi.fn(),
  adminLoginMfa: vi.fn(),
  getAuthState: vi.fn(),
  isThirdPartyAnalyticsLoaded: vi.fn(),
}))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  ...api,
}))

import { ApiCodeError } from '../lib/api'
import { I18nProvider } from '../lib/i18n'
import { STORAGE_KEYS } from '../lib/storageKeys'
import AdminLogin from './AdminLogin'

beforeEach(() => {
  api.getAuthState.mockResolvedValue({ mode: 'none', authenticated: true, app_session: false })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  sessionStorage.clear()
})

describe('AdminLogin', () => {
  it("starts the dashboard from its default list, not the last session's filters", async () => {
    // An expired session reaches the login page without a logout, so the
    // previous account's saved search / filters are still in the tab.
    sessionStorage.setItem(STORAGE_KEYS.adminSlideListState, '{"searchQ":"liver"}')
    api.adminLogin.mockResolvedValue({ access_token: 'tok' })
    api.isThirdPartyAnalyticsLoaded.mockReturnValue(false)
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/admin']}>
          <Routes>
            <Route path="/admin" element={<AdminLogin />} />
            <Route path="/admin/dashboard" element={<div>DASHBOARD</div>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'u' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    await screen.findByText('DASHBOARD')
    await waitFor(() => expect(sessionStorage.getItem(STORAGE_KEYS.adminSlideListState)).toBeNull())
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBe('tok')
  })

  function renderLogin() {
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/admin']}>
          <Routes>
            <Route path="/admin" element={<AdminLogin />} />
            <Route path="/admin/dashboard" element={<div>DASHBOARD</div>} />
            <Route path="/login" element={<div>APP LOGIN</div>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'u' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
  }

  it('sends a visitor without an app session to the app sign-in first (LDAP / Google)', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'google_oauth', authenticated: false, app_session: false })
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/admin']}>
          <Routes>
            <Route path="/admin" element={<AdminLogin />} />
            <Route path="/login" element={<div>APP LOGIN</div>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
    await screen.findByText('APP LOGIN')
  })

  it('goes to the app sign-in when the login API says it comes first', async () => {
    api.getAuthState
      .mockResolvedValueOnce({ mode: 'ldap', authenticated: true, app_session: true })
      .mockResolvedValue({ mode: 'ldap', authenticated: false, app_session: false })
    api.adminLogin.mockRejectedValue(new ApiCodeError('admin.err.appAuthRequired'))
    renderLogin()
    await screen.findByText('APP LOGIN')
  })

  it('under Basic, explains that the site sign-in comes first', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'basic', authenticated: false, app_session: false })
    api.adminLogin.mockRejectedValue(new ApiCodeError('admin.err.appAuthRequired'))
    renderLogin()
    await screen.findByText('Sign in to this site first, then sign in to the dashboard.')
  })

  it('asks for the authenticator code when the account has two-factor on', async () => {
    api.adminLogin.mockResolvedValue({ mfa_required: true, mfa_token: 'pending' })
    api.adminLoginMfa.mockResolvedValue({ access_token: 'tok2' })
    api.isThirdPartyAnalyticsLoaded.mockReturnValue(false)
    renderLogin()
    const input = await screen.findByLabelText('Authentication code')
    // No token yet: the password alone signs nobody in.
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    fireEvent.change(input, { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await screen.findByText('DASHBOARD')
    expect(api.adminLoginMfa).toHaveBeenCalledWith('pending', '123456')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBe('tok2')
  })

  it('locks the code step while the code is checked', async () => {
    // A failure clears the code, and "Back to sign-in" meanwhile would not
    // stop a late success from signing in.
    api.adminLogin.mockResolvedValue({ mfa_required: true, mfa_token: 'pending' })
    api.adminLoginMfa.mockReturnValue(new Promise(() => {}))
    renderLogin()
    const input = await screen.findByLabelText('Authentication code') as HTMLInputElement
    fireEvent.change(input, { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await waitFor(() => expect(input.closest('fieldset')!.disabled).toBe(true))
    expect(input.closest('fieldset')!.contains(screen.getByRole('button', { name: 'Back to sign-in' }))).toBe(true)
  })

  it('stays locked while it asks the auth mode after "site sign-in first"', async () => {
    // That answer leaves the page or resets the form.
    api.adminLogin.mockRejectedValue(new ApiCodeError('admin.err.appAuthRequired'))
    api.getAuthState.mockReturnValue(new Promise(() => {}))
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/admin']}>
          <Routes><Route path="/admin" element={<AdminLogin />} /></Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
    const user = screen.getByLabelText('Username') as HTMLInputElement
    fireEvent.change(user, { target: { value: 'u' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    await waitFor(() => expect(api.getAuthState).toHaveBeenCalled())
    expect(user.closest('fieldset')!.disabled).toBe(true)
  })

  it('locks the password step while the sign-in is out', async () => {
    // Its answer clears the password.
    api.adminLogin.mockReturnValue(new Promise(() => {}))
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/admin']}>
          <Routes><Route path="/admin" element={<AdminLogin />} /></Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
    const user = screen.getByLabelText('Username') as HTMLInputElement
    fireEvent.change(user, { target: { value: 'u' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    await waitFor(() => expect(user.closest('fieldset')!.disabled).toBe(true))
  })

  it('shows a wrong code and lets the user switch to a recovery code', async () => {
    api.adminLogin.mockResolvedValue({ mfa_required: true, mfa_token: 'pending' })
    api.adminLoginMfa.mockRejectedValue(new ApiCodeError('adminMfa.err.invalidCode'))
    renderLogin()
    fireEvent.change(await screen.findByLabelText('Authentication code'), { target: { value: '000000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await screen.findByText('The code is incorrect, or it has already been used.')
    fireEvent.click(screen.getByRole('button', { name: 'Use a recovery code instead' }))
    expect(screen.getByLabelText('Recovery code')).toBeTruthy()
  })

  it('returns to the password step when the pending sign-in expired', async () => {
    api.adminLogin.mockResolvedValue({ mfa_required: true, mfa_token: 'pending' })
    api.adminLoginMfa.mockRejectedValue(new ApiCodeError('adminMfa.err.sessionExpired'))
    renderLogin()
    fireEvent.change(await screen.findByLabelText('Authentication code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await screen.findByText('This sign-in has expired. Enter your username and password again.')
    expect(screen.getByLabelText('Password')).toBeTruthy()
  })
})
