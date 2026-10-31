// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { STORAGE_KEYS } from '../storageKeys'

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.restoreAllMocks()
  vi.resetModules()
})

describe('getAuthState', () => {
  it('sends the staff token this tab holds', async () => {
    const { getAuthState } = await import('./auth')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'staff.jwt.token')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJson({ mode: 'ldap', authenticated: true }))
    await expect(getAuthState()).resolves.toMatchObject({ authenticated: true })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.headers).toEqual({ Authorization: 'Bearer staff.jwt.token' })
    expect(init.credentials).toBe('same-origin')
  })

  it('sends no Authorization header without a token', async () => {
    const { getAuthState } = await import('./auth')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJson({ mode: 'ldap', authenticated: false }))
    await getAuthState()
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).has('Authorization')).toBe(false)
  })
})

describe('appLogout', () => {
  it.each([200, 204])('resolves when the server ended the session (%s)', async (status) => {
    const { appLogout } = await import('./auth')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      status === 204 ? new Response(null, { status }) : okJson({ ok: true }),
    )
    await expect(appLogout()).resolves.toBeUndefined()
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/logout')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
  })

  it('rejects a 401: only the app\'s 2xx answer clears the cookie', async () => {
    // The app answers logout with 200 whatever the session's state, so a 401
    // can only come from something in front of it — which left the cookie,
    // and with it the session, in place.
    const { appLogout } = await import('./auth')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"detail":"Not authenticated"}', {
      status: 401, headers: { 'content-type': 'application/json' },
    }))
    await expect(appLogout()).rejects.toThrow()
  })

  it('rejects when the session may still be there (rate limit, app or gateway down)', async () => {
    const { appLogout } = await import('./auth')
    const { isRateLimitError } = await import('./shared')
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('<html>429</html>', { status: 429 }))
    expect(isRateLimitError(await appLogout().catch((e) => e))).toBe(true)
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('<html>502</html>', { status: 502 }))
    await expect(appLogout()).rejects.toThrow()
  })
})

describe('admin token from the opening tab', () => {
  function openedFrom(opener: unknown, search = '?from=admin') {
    window.history.replaceState(null, '', `/slides/abc${search}`)
    Object.defineProperty(window, 'opener', { value: opener, writable: true, configurable: true })
  }

  function dashboardTab(token: string | null) {
    return { sessionStorage: { getItem: (k: string) => (k === STORAGE_KEYS.adminToken ? token : null) } }
  }

  afterEach(() => {
    openedFrom(null, '')
  })

  it("copies the dashboard tab's token and lets go of the opener", async () => {
    openedFrom(dashboardTab('staff.jwt'))
    await import('./auth')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBe('staff.jwt')
    expect(window.opener).toBeNull()
  })

  it('adopts nothing outside an admin preview', async () => {
    const opener = dashboardTab('staff.jwt')
    openedFrom(opener, '?from=elsewhere')
    await import('./auth')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    expect(window.opener).toBe(opener)
  })

  it('keeps a token the tab already holds', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'own.jwt')
    openedFrom(dashboardTab('staff.jwt'))
    await import('./auth')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBe('own.jwt')
  })

  it('adopts nothing from an opener on another origin', async () => {
    openedFrom({ get sessionStorage(): Storage { throw new DOMException('cross-origin', 'SecurityError') } })
    await import('./auth')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    expect(window.opener).toBeNull()
  })

  it('never reads a token out of localStorage, and removes any left there', async () => {
    localStorage.setItem(STORAGE_KEYS.adminToken, JSON.stringify({ t: 'fresh', exp: Date.now() + 5000 }))
    await import('./auth')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
  })
})

describe('adminPreviewLinkProps', () => {
  function clickEvent(init: { button: number; ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean }) {
    const a = document.createElement('a')
    a.href = '/slides/abc?from=admin'
    const preventDefault = vi.fn()
    return { event: { ...init, ctrlKey: !!init.ctrlKey, metaKey: !!init.metaKey, shiftKey: !!init.shiftKey, currentTarget: a, preventDefault }, preventDefault }
  }

  it('keeps the opener on the link and the Referer off', async () => {
    const { adminPreviewLinkProps } = await import('./auth')
    expect(adminPreviewLinkProps).toMatchObject({ target: '_blank', rel: 'opener', referrerPolicy: 'no-referrer' })
  })

  it('leaves a plain left click to the link', async () => {
    const { adminPreviewLinkProps } = await import('./auth')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { event, preventDefault } = clickEvent({ button: 0 })
    adminPreviewLinkProps.onClick(event as never)
    expect(open).not.toHaveBeenCalled()
    expect(preventDefault).not.toHaveBeenCalled()
  })

  it.each([
    ['Ctrl-click', { button: 0, ctrlKey: true }],
    ['Cmd-click', { button: 0, metaKey: true }],
    ['Shift-click', { button: 0, shiftKey: true }],
    ['middle-click', { button: 1 }],
  ])('opens a %s through window.open, which keeps the opener', async (_name, init) => {
    const { adminPreviewLinkProps } = await import('./auth')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { event, preventDefault } = clickEvent(init)
    adminPreviewLinkProps.onAuxClick(event as never)
    expect(preventDefault).toHaveBeenCalled()
    expect(open).toHaveBeenCalledWith(`${window.location.origin}/slides/abc?from=admin`, '_blank')
  })

  it('ignores a right click', async () => {
    const { adminPreviewLinkProps } = await import('./auth')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { event, preventDefault } = clickEvent({ button: 2 })
    adminPreviewLinkProps.onAuxClick(event as never)
    expect(open).not.toHaveBeenCalled()
    expect(preventDefault).not.toHaveBeenCalled()
  })
})

describe('clearToken', () => {
  it("drops the dashboard's saved search and filters with the token", async () => {
    const { clearToken } = await import('./auth')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 't')
    sessionStorage.setItem(STORAGE_KEYS.adminSlideListState, '{"searchQ":"liver"}')
    clearToken()
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.adminSlideListState)).toBeNull()
  })
})

describe('adminUpdateAuthSettings', () => {
  it('sends the allowed email addresses, and an explicit clear for an emptied list', async () => {
    const { adminUpdateAuthSettings } = await import('./auth')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => okJson({}))
    await adminUpdateAuthSettings({ google_allowed_emails: 'friend@gmail.com' })
    await adminUpdateAuthSettings({ google_allowed_emails: '' })
    const sent = (i: number) => (fetchMock.mock.calls[i][1] as RequestInit).body as FormData
    expect(sent(0).get('google_allowed_emails')).toBe('friend@gmail.com')
    expect(sent(0).has('clear_google_allowed_emails')).toBe(false)
    expect(sent(1).has('google_allowed_emails')).toBe(false)
    expect(sent(1).get('clear_google_allowed_emails')).toBe('1')
  })
})

describe('adminSignOutEverywhere', () => {
  it('POSTs with the staff token so the server can end every session of the account', async () => {
    const { adminSignOutEverywhere } = await import('./auth')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'staff.jwt.token')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJson({ ok: true }))
    await expect(adminSignOutEverywhere()).resolves.toBeUndefined()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/me/sign-out-everywhere')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({ Authorization: 'Bearer staff.jwt.token' })
  })

  it('rejects when the server refuses, so the page does not pretend it signed out', async () => {
    const { adminSignOutEverywhere } = await import('./auth')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"detail":"boom"}', {
      status: 500, headers: { 'content-type': 'application/json' },
    }))
    await expect(adminSignOutEverywhere()).rejects.toThrow()
  })
})

describe('clearAdminSession', () => {
  it('forgets the token and the pointers to the signed-out admin\'s server-side work', async () => {
    const { clearAdminSession } = await import('./auth')
    const keys = [
      STORAGE_KEYS.adminToken,
      STORAGE_KEYS.backupJobId,
      STORAGE_KEYS.restoreStageJobId,
      STORAGE_KEYS.importKeptStageId,
      STORAGE_KEYS.importKeptStageFallbackId,
      STORAGE_KEYS.importStageJobId,
      STORAGE_KEYS.adminSlideListState,
    ]
    for (const k of keys) sessionStorage.setItem(k, 'x')
    sessionStorage.setItem(STORAGE_KEYS.adminPostRestore, '{"awaitingStageId":"s"}')
    clearAdminSession()
    for (const k of keys) expect(sessionStorage.getItem(k)).toBeNull()
    // The post-restore marker belongs to a different flow (the session ended
    // mid-restore) and is not part of an explicit sign-out.
    expect(sessionStorage.getItem(STORAGE_KEYS.adminPostRestore)).not.toBeNull()
  })

  it('clearToken alone leaves those pointers: the mid-restore re-login still needs them', async () => {
    const { clearToken } = await import('./auth')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 't')
    sessionStorage.setItem(STORAGE_KEYS.restoreStageJobId, 'job')
    clearToken()
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.restoreStageJobId)).toBe('job')
  })
})
