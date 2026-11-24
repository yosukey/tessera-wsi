// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getAuthState: vi.fn(), appLogin: vi.fn() }))
vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  ...api,
}))

import { I18nProvider } from '../lib/i18n'
import Login from './Login'

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search}</div>
}

function renderAt(url: string) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

const ldap = { mode: 'ldap', authenticated: false }

async function submit() {
  fireEvent.change(await screen.findByLabelText('Username'), { target: { value: 'u' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'p' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Login return path', () => {
  it('returns to the ?next= target after signing in', async () => {
    api.getAuthState.mockResolvedValue(ldap)
    api.appLogin.mockResolvedValue(undefined)
    renderAt('/login?next=' + encodeURIComponent('/slides/abc?sync=tok'))
    await submit()
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/slides/abc?sync=tok'))
  })

  it('ignores an off-site or malformed next and goes home', async () => {
    for (const bad of ['//evil.com', '/\\evil.com', 'https://evil.com/x']) {
      api.getAuthState.mockResolvedValue(ldap)
      api.appLogin.mockResolvedValue(undefined)
      renderAt('/login?next=' + encodeURIComponent(bad))
      await submit()
      await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/'))
      cleanup()
    }
  })

  it('sends an already-signed-in visitor straight to next', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'ldap', authenticated: true })
    renderAt('/login?next=' + encodeURIComponent('/tours/t1'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/tours/t1'))
  })

  it('hands the Google flow the validated next', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'google_oauth', authenticated: false })
    renderAt('/login?next=' + encodeURIComponent('/slides/abc?sync=tok'))
    const a = (await screen.findByRole('link')) as HTMLAnchorElement
    expect(a.getAttribute('href')).toBe('/api/auth/oauth/start?next=' + encodeURIComponent('/slides/abc?sync=tok'))
  })
})

describe('Login when /api/auth/state fails', () => {
  it('shows an error with a retry, never the auth-is-off hint', async () => {
    api.getAuthState.mockRejectedValueOnce(new Error('boom'))
    renderAt('/login')
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText(/Authentication is not enabled/)).toBeNull()

    api.getAuthState.mockResolvedValue(ldap)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByLabelText('Username')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(api.getAuthState).toHaveBeenCalledTimes(2)
  })
})

describe('Login stale probes and markup', () => {
  // Renders Login plus a control that changes ?next= in place, which re-runs
  // the auth-state effect without remounting the page.
  function renderWithNextSwitch(url: string) {
    function Switch() {
      const navigate = useNavigate()
      return <button type="button" onClick={() => navigate('/login?next=%2Fb')}>switch</button>
    }
    return render(
      <I18nProvider>
        <MemoryRouter initialEntries={[url]}>
          <Switch />
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    )
  }

  it('hides the previously fetched form when a re-probe fails', async () => {
    api.getAuthState.mockResolvedValueOnce(ldap)
    renderWithNextSwitch('/login?next=%2Fa')
    expect(await screen.findByLabelText('Username')).toBeTruthy()
    api.getAuthState.mockRejectedValueOnce(new Error('boom'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByLabelText('Username')).toBeNull()
  })

  it('ignores a superseded probe that settles late', async () => {
    let resolveFirst: (v: unknown) => void = () => {}
    api.getAuthState.mockReturnValueOnce(new Promise((r) => { resolveFirst = r }))
    api.getAuthState.mockResolvedValueOnce(ldap)
    renderWithNextSwitch('/login?next=%2Fa')
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))
    expect(await screen.findByLabelText('Username')).toBeTruthy()
    // The first probe now claims the visitor is signed in; acting on it would
    // navigate to the stale ?next=/a target.
    await act(async () => { resolveFirst({ mode: 'ldap', authenticated: true }) })
    expect(screen.queryByTestId('where')).toBeNull()
    expect(screen.getByLabelText('Username')).toBeTruthy()
  })

  it('renders the Google sign-in as one link with no nested button', async () => {
    api.getAuthState.mockResolvedValue({ mode: 'google_oauth', authenticated: false })
    renderAt('/login')
    const a = (await screen.findByRole('link', { name: 'Sign in with Google' })) as HTMLAnchorElement
    expect(a.getAttribute('href')).toBe('/api/auth/oauth/start')
    expect(a.querySelector('button')).toBeNull()
  })
})
