// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminMe: vi.fn(),
  adminUpdateMe: vi.fn(),
  adminSignOutEverywhere: vi.fn(),
  clearAdminSession: vi.fn(),
}))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})
// The MFA section loads its own data; it is not what these tests are about.
vi.mock('../components/admin/AccountMfaSection', () => ({ default: () => null }))

import { I18nProvider } from '../lib/i18n'
import { ApiCodeError, RateLimitError, UnauthorizedError } from '../lib/api'
import AdminAccount from './AdminAccount'

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/account']}>
      <I18nProvider>
        <Routes>
          <Route path="/admin/account" element={<AdminAccount />} />
          <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.adminMe.mockResolvedValue({ id: 1, username: 'alice', role: 'admin' })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('AdminAccount sign-out', () => {
  it('Logout clears the whole session, pointers to server-side work included, and goes to the login screen', async () => {
    mount()
    await screen.findByDisplayValue('alice')
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
  })

  it('Logout does nothing when the confirmation is declined', async () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    mount()
    await screen.findByDisplayValue('alice')
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(screen.queryByText('LOGIN-PAGE')).toBeNull()
  })

  it('Sign out everywhere asks the server, then clears the session and leaves', async () => {
    api.adminSignOutEverywhere.mockResolvedValue(undefined)
    mount()
    await screen.findByDisplayValue('alice')
    fireEvent.click(screen.getByRole('button', { name: 'Sign out everywhere' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(api.adminSignOutEverywhere).toHaveBeenCalledTimes(1)
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
  })

  it('a 401 means the account is already signed out everywhere: the session is cleared and the page leaves', async () => {
    api.adminSignOutEverywhere.mockRejectedValue(new UnauthorizedError(401))
    mount()
    await screen.findByDisplayValue('alice')
    fireEvent.click(screen.getByRole('button', { name: 'Sign out everywhere' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
  })

  it('any other failure keeps the session and the page, and says it failed', async () => {
    api.adminSignOutEverywhere.mockRejectedValue(new RateLimitError())
    mount()
    await screen.findByDisplayValue('alice')
    fireEvent.click(screen.getByRole('button', { name: 'Sign out everywhere' }))
    await waitFor(() => expect(api.adminSignOutEverywhere).toHaveBeenCalled())
    await screen.findByText(/Too many requests|Could not sign out everywhere/i)
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(screen.queryByText('LOGIN-PAGE')).toBeNull()
  })

  it('a rejected token when the page opens returns to the login screen without clearing pointers', async () => {
    api.adminMe.mockRejectedValue(new UnauthorizedError(401))
    mount()
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    // The session ended on its own (expiry): the admin signs straight back in
    // to whatever job they had under way, so its pointers stay.
    expect(api.clearAdminSession).not.toHaveBeenCalled()
  })

  it('a transient failure when the page opens does not evict the admin', async () => {
    api.adminMe.mockRejectedValue(new ApiCodeError('adminDash.apiErr.generic'))
    mount()
    await waitFor(() => expect(api.adminMe).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByText('LOGIN-PAGE')).toBeNull()
    expect(api.clearAdminSession).not.toHaveBeenCalled()
  })
})

describe('AdminAccount password change in flight', () => {
  it('locks the form until the change lands, and sends it once', async () => {
    // The success clears the fields and says the password changed: a new
    // password typed meanwhile would be wiped and never set.
    let finish: (v: unknown) => void = () => {}
    api.adminUpdateMe.mockReturnValue(new Promise((r) => { finish = r }))
    const { container } = mount()
    await screen.findByDisplayValue('alice')
    const field = (name: string) => container.querySelector(`input[name="${name}"]`) as HTMLInputElement
    const form = field('new-password').closest('form')!
    fireEvent.change(form.querySelector('input[name="current-password"]')!, { target: { value: 'old-pass' } })
    fireEvent.change(field('new-password'), { target: { value: 'new-pass-1' } })
    fireEvent.change(field('confirm-new-password'), { target: { value: 'new-pass-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Update password' }))
    await waitFor(() => expect(field('new-password').closest('fieldset')!.disabled).toBe(true))
    fireEvent.submit(form)
    expect(api.adminUpdateMe).toHaveBeenCalledTimes(1)
    expect(api.adminUpdateMe).toHaveBeenCalledWith({ current_password: 'old-pass', new_password: 'new-pass-1' })
    finish({})
    await screen.findByText('Password updated.')
    expect(field('new-password').closest('fieldset')!.disabled).toBe(false)
  })
})

describe('AdminAccount sign out everywhere', () => {
  it('sends it once, the button waiting for the answer', async () => {
    api.adminSignOutEverywhere.mockReturnValue(new Promise(() => {}))
    mount()
    await screen.findByDisplayValue('alice')
    const btn = screen.getByRole('button', { name: 'Sign out everywhere' }) as HTMLButtonElement
    fireEvent.click(btn)
    await waitFor(() => expect(btn.disabled).toBe(true))
    expect(api.adminSignOutEverywhere).toHaveBeenCalledTimes(1)
  })
})

describe('AdminAccount when the account details fail to load', () => {
  it('says why two-factor and user management are missing, and how to recover', async () => {
    api.adminMe.mockRejectedValue(new RateLimitError())
    mount()
    const alert = await screen.findByText(/Could not load your account details/)
    const box = alert.closest('[role="alert"]')!
    expect(box.textContent).toMatch(/Too many requests/)
    expect(box.textContent).toContain('Reload the page to try again.')
    // The forms that don't need those details stay usable.
    expect((screen.getByRole('button', { name: 'Update password' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
