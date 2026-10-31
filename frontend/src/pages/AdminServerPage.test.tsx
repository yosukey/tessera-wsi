// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ adminMe: vi.fn(), clearAdminSession: vi.fn() }))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})
// Stand-ins for the sections: rendering one at all is what the page must not
// do before the permission check (the live-session list carries force-end
// buttons).
vi.mock('../components/admin/AdminServerSection', () => ({ default: () => <div>SERVER-SECTION</div> }))
vi.mock('../components/admin/AdminSyncSessionsSection', () => ({ default: () => <div>SYNC-SECTION</div> }))

import { I18nProvider } from '../lib/i18n'
import { RateLimitError } from '../lib/api'
import AdminServerPage from './AdminServerPage'

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/server']}>
      <I18nProvider>
        <Routes>
          <Route path="/admin/server" element={<AdminServerPage />} />
          <Route path="/admin/dashboard" element={<div>DASHBOARD</div>} />
          <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  )
}

const me = (role: string, perms: Record<string, boolean> = {}) =>
  ({ id: 1, username: 'u', role, role_permissions: perms })

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

describe('AdminServerPage permission check', () => {
  it('renders nothing of the sections while /me is in flight', async () => {
    api.adminMe.mockReturnValue(new Promise(() => {}))
    mount()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(screen.queryByText('SERVER-SECTION')).toBeNull()
    expect(screen.queryByText('SYNC-SECTION')).toBeNull()
  })

  it('renders the sections once the role may see the page', async () => {
    api.adminMe.mockResolvedValue(me('manager', { manager_can_view_server: true }))
    mount()
    expect(await screen.findByText('SERVER-SECTION')).toBeTruthy()
    expect(screen.getByText('SYNC-SECTION')).toBeTruthy()
  })

  it('sends a role without the server flag back without ever rendering them', async () => {
    api.adminMe.mockResolvedValue(me('instructor', { instructor_can_view_server: false }))
    mount()
    expect(await screen.findByText('DASHBOARD')).toBeTruthy()
    expect(screen.queryByText('SYNC-SECTION')).toBeNull()
  })

  it('offers a retry when the check itself fails', async () => {
    api.adminMe.mockRejectedValueOnce(new RateLimitError()).mockResolvedValue(me('admin'))
    mount()
    expect(await screen.findByText('Could not check your access to this page.')).toBeTruthy()
    expect(screen.queryByText('SERVER-SECTION')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('SERVER-SECTION')).toBeTruthy())
  })
})

describe('AdminServerPage Logout', () => {
  it('asks first, and keeps the session when declined', async () => {
    const confirmSpy = vi.fn((_msg?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminMe.mockResolvedValue(me('admin'))
    mount()
    await screen.findByText('SERVER-SECTION')
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(confirmSpy.mock.calls).toEqual([['Log out?']])
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(screen.getByText('SERVER-SECTION')).toBeTruthy()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
  })
})
