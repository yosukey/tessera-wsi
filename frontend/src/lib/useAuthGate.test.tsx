// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getAuthState: vi.fn() }))
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  ...api,
}))

import { STORAGE_KEYS } from './storageKeys'
import { useAuthGate } from './useAuthGate'

function Gated() {
  useAuthGate()
  return (
    <div data-testid="page">
      viewer
      <Link to="/viewer/next">next slide</Link>
    </div>
  )
}

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search}</div>
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/viewer/:id" element={<Gated />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

function staffToken(role: string): string {
  const b64url = (o: object) => btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const exp = Math.floor(Date.now() / 1000) + 3600
  return `${b64url({ alg: 'HS256', typ: 'JWT' })}.${b64url({ role, exp })}.sig`
}

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.clearAllMocks()
})

describe('useAuthGate', () => {
  it('sends an unauthenticated visitor to /login under a gated mode', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: false, app_session: false })
    renderAt('/viewer/abc')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toMatch(/^\/login\?next=/))
  })

  it('lets staff through when the server counts their token', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, staffToken('manager'))
    api.getAuthState.mockResolvedValue({ mode: 'google_oauth', authenticated: true, app_session: false })
    renderAt('/viewer/abc')
    await waitFor(() => expect(api.getAuthState).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByTestId('page')).toBeTruthy()
  })

  it('redirects a tab whose unexpired staff token the server rejected', async () => {
    // Revoked, suspended or password changed: the token still decodes and is
    // within its exp, but only the server knows it no longer counts.
    sessionStorage.setItem(STORAGE_KEYS.adminToken, staffToken('manager'))
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: false, app_session: false })
    renderAt('/viewer/abc')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toMatch(/^\/login\?next=/))
  })

  it('renders the page when the auth-state probe itself fails', async () => {
    api.getAuthState.mockRejectedValue(new TypeError('Failed to fetch'))
    renderAt('/viewer/abc')
    await waitFor(() => expect(api.getAuthState).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByTestId('page')).toBeTruthy()
  })

  it('probes once per page visit, not once per slide the mounted viewer moves to', async () => {
    // react-router hands out a new navigate function whenever the pathname
    // changes. The viewer stays mounted across slides (a live-sync presenter
    // moving on), so a probe keyed on it would run again for every follower on every
    // move, against a rate limit those followers often share.
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: true, app_session: true })
    renderAt('/viewer/abc')
    await waitFor(() => expect(api.getAuthState).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByText('next slide'))
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByTestId('page')).toBeTruthy()
    expect(api.getAuthState).toHaveBeenCalledTimes(1)
  })
})
