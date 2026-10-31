// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminListUsers: vi.fn(),
  adminGetRolePermissions: vi.fn(),
  adminCreateUser: vi.fn(),
  adminUpdateRolePermissions: vi.fn(),
  adminResetUserMfa: vi.fn(),
  adminDeleteUser: vi.fn(),
}))
vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  ...api,
}))

import type { AdminMe } from '../../lib/api'
import { I18nProvider } from '../../lib/i18n'
import AdminUsersSection from './AdminUsersSection'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const me = (enabled: boolean): AdminMe => ({
  id: 1, username: 'admin', role: 'admin', role_permissions: {} as AdminMe['role_permissions'],
  mfa: { enabled, enabled_at: null, recovery_codes_remaining: 10, available: true },
})

describe('AdminUsersSection', () => {
  it("updates the admin's own 2FA badge in place when it changes above, without refetching", async () => {
    api.adminListUsers.mockResolvedValue([
      { id: 1, username: 'admin', role: 'admin', is_active: true, mfa_enabled: false, created_at: '2026-10-26T00:00:00+00:00' },
    ])
    api.adminGetRolePermissions.mockResolvedValue({
      manager_can_view_analytics: false, manager_can_view_server: false,
      instructor_can_view_analytics: false, instructor_can_view_server: false,
    })
    const { rerender } = render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    await screen.findByText('admin')
    expect(screen.queryByText('2FA')).toBeNull()
    rerender(<I18nProvider><AdminUsersSection me={me(true)} /></I18nProvider>)
    expect(await screen.findByText('2FA')).toBeTruthy()
    expect(api.adminListUsers).toHaveBeenCalledTimes(1)
  })
})

describe('AdminUsersSection create', () => {
  it('locks the new-user fields until the create lands', async () => {
    // The success clears them, which would wipe what was typed meanwhile.
    api.adminListUsers.mockResolvedValue([])
    api.adminGetRolePermissions.mockResolvedValue({
      manager_can_view_analytics: false, manager_can_view_server: false,
      instructor_can_view_analytics: false, instructor_can_view_server: false,
    })
    let finish: (v: unknown) => void = () => {}
    api.adminCreateUser.mockReturnValue(new Promise((r) => { finish = r }))
    render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    const submit = await screen.findByRole('button', { name: 'Create user' })
    const form = submit.closest('form')!
    const [username, password] = [...form.querySelectorAll('input')] as HTMLInputElement[]
    fireEvent.change(username, { target: { value: 'bob' } })
    fireEvent.change(password, { target: { value: 'secret-pass' } })
    fireEvent.submit(form)
    await waitFor(() => expect(username.closest('fieldset')!.disabled).toBe(true))
    expect(username.closest('fieldset')!.contains(form.querySelector('select'))).toBe(true)
    finish({ id: 2, username: 'bob', role: 'instructor', is_active: true, mfa_enabled: false, created_at: '2026-10-31T00:00:00+00:00' })
    await waitFor(() => expect(username.closest('fieldset')!.disabled).toBe(false))
    expect(username.value).toBe('')
  })
})

describe('AdminUsersSection role permissions', () => {
  it('locks the table while a change is out', async () => {
    // One change at a time, so answers can't land out of order.
    api.adminListUsers.mockResolvedValue([])
    api.adminGetRolePermissions.mockResolvedValue({
      manager_can_view_analytics: false, manager_can_view_server: false,
      instructor_can_view_analytics: false, instructor_can_view_server: false,
    })
    api.adminUpdateRolePermissions.mockReturnValue(new Promise(() => {}))
    render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    await waitFor(() => expect(document.querySelectorAll('table input[type=checkbox]')).toHaveLength(4))
    const boxes = [...document.querySelectorAll('table input[type=checkbox]')] as HTMLInputElement[]
    expect(boxes[0].closest('fieldset')!.disabled).toBe(false)
    fireEvent.click(boxes[0])
    await waitFor(() => expect(boxes[0].closest('fieldset')!.disabled).toBe(true))
    expect(boxes.every((b) => b.closest('fieldset') === boxes[0].closest('fieldset'))).toBe(true)
    expect(api.adminUpdateRolePermissions).toHaveBeenCalledWith({ manager_can_view_analytics: true })
  })
})

describe('AdminUsersSection row actions', () => {
  const PERMS = {
    manager_can_view_analytics: false, manager_can_view_server: false,
    instructor_can_view_analytics: false, instructor_can_view_server: false,
  }
  const row = (id: number, username: string, mfa = false) => ({
    id, username, role: 'instructor', is_active: true, mfa_enabled: mfa, created_at: '2026-10-26T00:00:00+00:00',
  })

  it('locks the two-factor reset form while the reset is out', async () => {
    // Its success closes the form and clears the password.
    api.adminListUsers.mockResolvedValue([row(1, 'admin'), row(2, 'bob', true)])
    api.adminGetRolePermissions.mockResolvedValue(PERMS)
    api.adminResetUserMfa.mockReturnValue(new Promise(() => {}))
    render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    const resets = await screen.findAllByRole('button', { name: 'Reset 2FA' })
    fireEvent.click(resets[resets.length - 1])
    const pw = screen.getByLabelText('Your password (to confirm)') as HTMLInputElement
    fireEvent.change(pw, { target: { value: 'pw' } })
    fireEvent.submit(pw.closest('form')!)
    await waitFor(() => expect(pw.closest('fieldset')!.disabled).toBe(true))
    expect(pw.closest('fieldset')!.contains(screen.getByRole('button', { name: 'Cancel' }))).toBe(true)
  })

  it('takes one row action at a time across the rows', async () => {
    // A busy id per row would let another row's finish re-enable a delete
    // still out; every row's buttons wait.
    api.adminListUsers.mockResolvedValue([row(1, 'admin'), row(2, 'bob'), row(3, 'carol')])
    api.adminGetRolePermissions.mockResolvedValue(PERMS)
    let finish: () => void = () => {}
    api.adminDeleteUser.mockReturnValue(new Promise<void>((r) => { finish = r }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    await screen.findByText('carol')
    const deletes = () => screen.getAllByRole('button', { name: /^(Delete|…)$/ }) as HTMLButtonElement[]
    fireEvent.click(deletes()[1])
    await waitFor(() => expect(deletes().every((b) => b.disabled)).toBe(true))
    expect((screen.getAllByRole('button', { name: 'Disable' }) as HTMLButtonElement[]).every((b) => b.disabled)).toBe(true)
    expect(api.adminDeleteUser).toHaveBeenCalledTimes(1)
    await act(async () => finish())
    expect(screen.queryByText('bob')).toBeNull()
    confirmSpy.mockRestore()
  })
})

describe('AdminUsersSection role permissions load', () => {
  it('says the permissions could not be loaded, rather than "Loading…" for ever', async () => {
    api.adminListUsers.mockResolvedValue([])
    api.adminGetRolePermissions.mockRejectedValue(new Error('down'))
    render(<I18nProvider><AdminUsersSection me={me(false)} /></I18nProvider>)
    const alert = await screen.findByText(/Failed to load the role permissions/)
    expect(alert.closest('[role="alert"]')!.textContent).toContain('Reload the page to try again.')
    expect(screen.queryByText('Loading…')).toBeNull()
    // A raw (English) server text is not appended to the localized headline.
    expect(alert.textContent).toBe('Failed to load the role permissions')
  })
})
